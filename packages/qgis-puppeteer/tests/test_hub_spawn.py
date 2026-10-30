"""qgis_puppeteer.hub_spawn のユニットテスト。

ADR-0001 §4 "Hub の起動競合回避" を Qt 非依存で検証する。
非ブロッキング排他ロック、spawn ロジック（Popen と「生きた Hub がいるか」の
確かめを偽物に差し替える）の各層を単体テスト。ロックの取り合いは別プロセスで確かめる。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from qgis_puppeteer.hub_file import HubFileInfo
from qgis_puppeteer.hub_spawn import (
    EnsureOutcome,
    HubStartupError,
    SpawnOutcome,
    _exclusive_nonblocking_lock,
    default_hub_command,
    ensure_hub,
    hub_lock_path,
    hub_log_path,
    hub_spawn_log_path,
    try_spawn_hub,
)

# ============================================================
# フィクスチャ
# ============================================================


# 子プロセスから qgis_puppeteer を import するためのパッケージ親パス。
# Windows パスのバックスラッシュは unicode_escape でエスケープする。
_pkg_parent = str(Path(__file__).parent.parent / "src").encode("unicode_escape").decode()

_INFO = HubFileInfo(addresses=("127.0.0.1:50123",), token="t" * 43, pid=1234)


@contextmanager
def _lock_held_by_other_process(lock_path: Path) -> Iterator[None]:
    """with の間、別プロセスに ``lock_path`` のロックを握らせる。

    抜けるときは子の stdin を閉じ、子がロックを放して終わるのを待つ。kill は
    しない：Windows の venv の python.exe は本体を子として起こすので、kill して
    も本体がロックを持ったまま残ることがある。
    """
    holder_script = f"""
import sys
sys.path.insert(0, {_pkg_parent!r})
from pathlib import Path
from qgis_puppeteer.hub_spawn import _exclusive_nonblocking_lock

with _exclusive_nonblocking_lock(Path({str(lock_path)!r})) as locked:
    assert locked is True
    print("LOCKED", flush=True)
    sys.stdin.read()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line == "LOCKED", f"unexpected: {line!r}"
        yield
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


class _FakePopen:
    """subprocess.Popen の互換モック（poll/kill/returncode/pid のみ）。"""

    def __init__(self, *, exit_code: int | None = None) -> None:
        self.returncode: int | None = exit_code
        self.pid = 99999
        self.kill_called = False

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9


class _FakeFinder:
    """``find_live_hub`` の偽物。``ready`` になるまで None、なった後は ``_INFO``。

    ``ready_after`` 回呼ばれたら自分で ready になる（None なら ``ready`` を立てるまで）。
    """

    def __init__(self, *, ready: bool = False, ready_after: int | None = None) -> None:
        self.ready = ready
        self.ready_after = ready_after
        self.calls: list[Path] = []

    def __call__(self, path: Any, *, timeout: float) -> HubFileInfo | None:
        del timeout
        self.calls.append(Path(path))
        if self.ready_after is not None and len(self.calls) > self.ready_after:
            self.ready = True
        return _INFO if self.ready else None


def _no_popen(*_args: Any, **_kwargs: Any) -> _FakePopen:
    raise AssertionError("must not spawn")


# ============================================================
# Hub ファイルの隣のパス
# ============================================================


class TestNeighbourPaths:
    def test_lock_log_and_spawn_log_sit_next_to_the_hub_file(self, tmp_path: Path) -> None:
        hub_file = tmp_path / "hub.json"
        assert hub_lock_path(hub_file) == tmp_path / "hub.json.lock"
        assert hub_log_path(hub_file) == tmp_path / "hub.log"
        assert hub_spawn_log_path(hub_file) == tmp_path / "hub.spawn.log"


# ============================================================
# _exclusive_nonblocking_lock
# ============================================================


class TestExclusiveLock:
    def test_single_acquire_succeeds(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        with _exclusive_nonblocking_lock(lock_path) as locked:
            assert locked is True
        # ロック解放後は再取得できる
        with _exclusive_nonblocking_lock(lock_path) as locked:
            assert locked is True

    def test_second_process_fails_to_acquire(self, tmp_path: Path) -> None:
        """別プロセスがロック中なら False を返し、離せば取れる。"""
        lock_path = tmp_path / "cross.lock"
        with (
            _lock_held_by_other_process(lock_path),
            _exclusive_nonblocking_lock(lock_path) as locked,
        ):
            assert locked is False
        with _exclusive_nonblocking_lock(lock_path) as locked:
            assert locked is True


# ============================================================
# default_hub_command
# ============================================================


class TestDefaultHubCommand:
    def test_runs_the_hub_module_with_the_hub_file(self, tmp_path: Path) -> None:
        hub_file = tmp_path / "hub.json"
        cmd = default_hub_command(hub_file=hub_file)
        assert cmd == [sys.executable, "-m", "qgis_puppeteer.hub", "--hub-file", str(hub_file)]
        # ポートは決めない（Hub が空いているポートを選んで Hub ファイルに書く）
        assert "--port" not in cmd

    def test_custom_python(self, tmp_path: Path) -> None:
        cmd = default_hub_command(hub_file=tmp_path / "hub.json", python="C:/custom/python.exe")
        assert cmd[0] == "C:/custom/python.exe"


# ============================================================
# try_spawn_hub
# ============================================================


class TestTrySpawnHub:
    def test_returns_another_when_lock_held(self, tmp_path: Path) -> None:
        """他プロセスがロックを保持している間は確かめも spawn もせず戻る。"""
        hub_file = tmp_path / "hub.json"
        finder = _FakeFinder()
        with _lock_held_by_other_process(hub_lock_path(hub_file)):
            outcome, info = try_spawn_hub(
                hub_file=hub_file, popen=_no_popen, find_hub=finder, startup_deadline=0.5
            )
        assert outcome == SpawnOutcome.ANOTHER_PROCESS_OWNS_LOCK
        assert info is None
        assert finder.calls == []

    def test_already_running_after_acquiring_lock(self, tmp_path: Path) -> None:
        """ロック獲得後の確かめで生きた Hub がいれば起こさない。"""
        hub_file = tmp_path / "hub.json"
        outcome, info = try_spawn_hub(
            hub_file=hub_file, popen=_no_popen, find_hub=_FakeFinder(ready=True)
        )
        assert outcome == SpawnOutcome.ALREADY_RUNNING
        assert info == _INFO

    def test_spawned_when_the_hub_becomes_live(self, tmp_path: Path) -> None:
        """起こした後、Hub ファイルの Hub が生きれば SPAWNED と、その中身を返す。"""
        hub_file = tmp_path / "hub.json"
        finder = _FakeFinder(ready_after=3)
        commands: list[list[str]] = []

        def fake_popen(cmd: list[str], **_kwargs: Any) -> _FakePopen:
            commands.append(cmd)
            return _FakePopen()

        outcome, info = try_spawn_hub(
            hub_file=hub_file,
            popen=fake_popen,
            find_hub=finder,
            startup_deadline=2.0,
            poll_interval=0.01,
        )
        assert outcome == SpawnOutcome.SPAWNED
        assert info == _INFO
        assert commands == [default_hub_command(hub_file=hub_file)]
        assert all(p == hub_file for p in finder.calls)

    def test_uses_the_given_command(self, tmp_path: Path) -> None:
        commands: list[list[str]] = []

        def fake_popen(cmd: list[str], **_kwargs: Any) -> _FakePopen:
            commands.append(cmd)
            return _FakePopen()

        try_spawn_hub(
            hub_file=tmp_path / "hub.json",
            command=["python-qgis-ltr.bat", "bootstrap.py", "--hub-file", "x"],
            popen=fake_popen,
            find_hub=_FakeFinder(ready_after=1),
            poll_interval=0.01,
        )
        assert commands == [["python-qgis-ltr.bat", "bootstrap.py", "--hub-file", "x"]]

    def test_raises_with_exit_code_when_child_exits(self, tmp_path: Path) -> None:
        """生きた Hub になる前に子が終われば、その終了コードを添えて HubStartupError。"""
        with pytest.raises(HubStartupError, match="exited with code 3"):
            try_spawn_hub(
                hub_file=tmp_path / "hub.json",
                popen=lambda *_a, **_k: _FakePopen(exit_code=3),
                find_hub=_FakeFinder(),
                startup_deadline=2.0,
            )

    def test_child_that_exits_after_writing_the_hub_file_counts(self, tmp_path: Path) -> None:
        """子が終わった直後の最後の確かめで Hub がいれば成功とする。"""
        # 1 回目（lock 後）・2 回目（ループ）は None、子の終了を見た後の 3 回目で生きる
        finder = _FakeFinder(ready_after=2)
        outcome, info = try_spawn_hub(
            hub_file=tmp_path / "hub.json",
            popen=lambda *_a, **_k: _FakePopen(exit_code=0),
            find_hub=finder,
        )
        assert outcome == SpawnOutcome.SPAWNED
        assert info == _INFO

    def test_raises_on_startup_timeout_and_kills_the_tree(self, tmp_path: Path) -> None:
        """期限までに生きた Hub にならなければ、子を木ごと止めて HubStartupError。"""
        fake = _FakePopen()
        killed: list[Any] = []
        with pytest.raises(HubStartupError, match="did not start within"):
            try_spawn_hub(
                hub_file=tmp_path / "hub.json",
                popen=lambda *_a, **_k: fake,
                find_hub=_FakeFinder(),
                startup_deadline=0.2,
                poll_interval=0.02,
                kill_tree=killed.append,
            )
        assert killed == [fake]

    def test_passes_env_to_popen(self, tmp_path: Path) -> None:
        """`env=` が subprocess.Popen の kwargs に正しく渡ること（プラグインの PYTHONPATH）。"""
        captured: dict[str, Any] = {}

        def fake_popen(*_args: Any, **kwargs: Any) -> _FakePopen:
            captured.update(kwargs)
            return _FakePopen(exit_code=0)

        custom_env = {"FOO": "bar", "PYTHONPATH": "/custom/path"}
        with pytest.raises(HubStartupError):
            try_spawn_hub(
                hub_file=tmp_path / "hub.json",
                popen=fake_popen,
                find_hub=_FakeFinder(),
                env=custom_env,
            )
        assert captured.get("env") == custom_env

    def test_omits_env_when_none(self, tmp_path: Path) -> None:
        """`env=None` のとき Popen の kwargs に env キーが含まれない（親環境を継承）。"""
        captured: dict[str, Any] = {}

        def fake_popen(*_args: Any, **kwargs: Any) -> _FakePopen:
            captured.update(kwargs)
            return _FakePopen(exit_code=0)

        with pytest.raises(HubStartupError):
            try_spawn_hub(hub_file=tmp_path / "hub.json", popen=fake_popen, find_hub=_FakeFinder())
        assert "env" not in captured

    def test_closes_log_file_on_parent_side(self, tmp_path: Path) -> None:
        """`log_file` を渡した場合、親側のハンドルは Popen 直後に閉じられる。"""
        captured: dict[str, Any] = {}

        def fake_popen(*_args: Any, **kwargs: Any) -> _FakePopen:
            captured.update(kwargs)
            return _FakePopen(exit_code=0)

        with pytest.raises(HubStartupError):
            try_spawn_hub(
                hub_file=tmp_path / "hub.json",
                popen=fake_popen,
                find_hub=_FakeFinder(),
                log_file=tmp_path / "hub.spawn.log",
            )
        stdout = captured.get("stdout")
        assert hasattr(stdout, "closed")
        assert stdout.closed is True, "parent side must close inherited log handle"

    def test_retains_child_reference_on_success(self, tmp_path: Path) -> None:
        """spawn 成功後、Popen オブジェクトをモジュールに保持する（ResourceWarning 回避）。"""
        from qgis_puppeteer.hub_spawn import _SPAWNED_CHILDREN

        before = len(_SPAWNED_CHILDREN)
        fake = _FakePopen()
        outcome, _ = try_spawn_hub(
            hub_file=tmp_path / "hub.json",
            popen=lambda *_a, **_k: fake,
            find_hub=_FakeFinder(ready_after=1),
            poll_interval=0.01,
        )
        assert outcome == SpawnOutcome.SPAWNED
        assert len(_SPAWNED_CHILDREN) == before + 1
        assert _SPAWNED_CHILDREN[-1] is fake


# ============================================================
# ensure_hub — 全体フロー
# ============================================================


class TestEnsureHub:
    def test_already_running_skips_spawn(self, tmp_path: Path) -> None:
        ready = ensure_hub(
            hub_file=tmp_path / "hub.json", popen=_no_popen, find_hub=_FakeFinder(ready=True)
        )
        assert ready.outcome == EnsureOutcome.ALREADY_RUNNING
        assert ready.info == _INFO

    def test_spawns_when_no_live_hub(self, tmp_path: Path) -> None:
        ready = ensure_hub(
            hub_file=tmp_path / "hub.json",
            popen=lambda *_a, **_k: _FakePopen(),
            find_hub=_FakeFinder(ready_after=3),
            poll_interval=0.01,
            sleep=lambda _s: None,
        )
        assert ready.outcome == EnsureOutcome.SPAWNED_BY_US
        assert ready.info == _INFO

    def test_waits_while_another_process_spawns(self, tmp_path: Path) -> None:
        """ロックを別プロセスが持っていれば起こさず待ち、Hub が現れたら SPAWNED_BY_OTHER。"""
        hub_file = tmp_path / "hub.json"
        sleeps: list[float] = []
        # 最初の確かめと 1 回目の待ちの後は None、2 回目の待ちの後で生きる
        finder = _FakeFinder(ready_after=2)
        with _lock_held_by_other_process(hub_lock_path(hub_file)):
            ready = ensure_hub(
                hub_file=hub_file,
                popen=_no_popen,
                find_hub=finder,
                connect_backoff=(0.1, 0.2, 0.3),
                sleep=sleeps.append,
            )
        assert ready.outcome == EnsureOutcome.SPAWNED_BY_OTHER
        assert sleeps == [0.1, 0.2]

    def test_raises_when_no_hub_appears(self, tmp_path: Path) -> None:
        """他プロセスがロックを握ったまま Hub が現れなければ、待ちきって HubStartupError。"""
        hub_file = tmp_path / "hub.json"
        with (
            _lock_held_by_other_process(hub_lock_path(hub_file)),
            pytest.raises(HubStartupError, match="No live Hub appeared"),
        ):
            ensure_hub(
                hub_file=hub_file,
                popen=_no_popen,
                find_hub=_FakeFinder(),
                connect_backoff=(0.01, 0.01, 0.01),
                sleep=lambda _s: None,
            )
