"""qgis_puppeteer.hub_spawn のユニットテスト。

ADR-0001 §4 "Hub の起動競合回避" を Qt 非依存で検証する。
TCP probe、非ブロッキング排他ロック、spawn ロジックの各層を単体テスト。

実ネットワーク接続を伴うが、ローカル listen ソケットを作る程度なので
`@pytest.mark.integration` マークは付けない（ユニット扱い）。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from qgis_puppeteer.hub_spawn import (
    EnsureOutcome,
    HubStartupError,
    SpawnOutcome,
    _exclusive_nonblocking_lock,
    default_hub_command,
    ensure_hub_reachable,
    tcp_probe,
    try_spawn_hub,
)

# ============================================================
# フィクスチャ
# ============================================================


# 子プロセスから qgis_puppeteer を import するためのパッケージ親パス。
# Windows パスのバックスラッシュは unicode_escape でエスケープする。
_pkg_parent = str(Path(__file__).parent.parent).encode("unicode_escape").decode()


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def listen_socket() -> Any:
    """127.0.0.1 で listen する TCP ソケットを作成し、テスト後に閉じる。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    yield s
    s.close()


# ============================================================
# tcp_probe
# ============================================================


class TestTcpProbe:
    def test_returns_true_when_port_open(self, listen_socket: socket.socket) -> None:
        port = listen_socket.getsockname()[1]
        assert tcp_probe("127.0.0.1", port, timeout=1.0) is True

    def test_returns_false_when_port_closed(self) -> None:
        port = _get_free_port()  # 取得直後に閉じる → 誰も listen していない
        assert tcp_probe("127.0.0.1", port, timeout=0.2) is False

    def test_returns_false_on_unreachable_host(self) -> None:
        # 192.0.2.0/24 は RFC 5737 でドキュメンテーション用、到達不能想定
        assert tcp_probe("192.0.2.1", 9999, timeout=0.3) is False


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

    def test_concurrent_second_thread_fails_to_acquire(self, tmp_path: Path) -> None:
        """同一プロセス内のロック挙動は OS 依存のため別プロセスで検証する。"""
        # Windows msvcrt.locking は fd ごとの排他、POSIX fcntl.flock は
        # 同一プロセスでは通ることがあるため、ここでは skip。
        # 別プロセス間排他は test_concurrent_second_process で確認。
        pytest.skip("same-process semantics vary by platform")

    def test_concurrent_second_process_fails_to_acquire(self, tmp_path: Path) -> None:
        """別プロセスがロック中なら False を返すこと。"""
        lock_path = tmp_path / "cross.lock"
        # 子プロセスに 2 秒間ロックを保持させる
        holder_script = f"""
import sys, time
sys.path.insert(0, {_pkg_parent!r})
from pathlib import Path
from qgis_puppeteer.hub_spawn import _exclusive_nonblocking_lock

with _exclusive_nonblocking_lock(Path({str(lock_path)!r})) as locked:
    assert locked is True
    print("LOCKED", flush=True)
    time.sleep(2.0)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 子プロセスが "LOCKED" を出力するまで待つ
            assert proc.stdout is not None
            line = proc.stdout.readline().strip()
            assert line == "LOCKED", f"unexpected: {line!r}"

            # この時点で子がロックを保持中 → 親は取れない
            with _exclusive_nonblocking_lock(lock_path) as locked:
                assert locked is False
        finally:
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)

        # 子が終了したので親は再取得できる
        with _exclusive_nonblocking_lock(lock_path) as locked:
            assert locked is True


# ============================================================
# default_hub_command
# ============================================================


class TestDefaultHubCommand:
    def test_contains_python_module_port(self) -> None:
        cmd = default_hub_command(port=9999)
        assert cmd[0] == sys.executable
        assert cmd[1] == "-m"
        assert cmd[2] == "qgis_puppeteer.hub"
        assert "--port" in cmd
        assert "9999" in cmd

    def test_custom_python(self) -> None:
        cmd = default_hub_command(port=1, python="C:/custom/python.exe")
        assert cmd[0] == "C:/custom/python.exe"


# ============================================================
# try_spawn_hub — 動作検証（偽の Popen と probe を注入）
# ============================================================


class _FakePopen:
    """subprocess.Popen の互換モック（poll/kill/returncode のみ）。"""

    def __init__(
        self,
        *,
        exit_immediately: bool = False,
        exit_code: int = 1,
    ) -> None:
        self._exit_immediately = exit_immediately
        self.returncode: int | None = exit_code if exit_immediately else None
        self.kill_called = False

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9


class TestTrySpawnHub:
    def test_returns_another_when_lock_held(self, tmp_path: Path) -> None:
        """他プロセスがロックを保持している間は spawn せず戻る。"""
        lock_path = tmp_path / "busy.lock"

        # 別スレッド経由で別プロセスにロックを握ってもらう
        holder_script = f"""
import sys, time
sys.path.insert(0, {_pkg_parent!r})
from pathlib import Path
from qgis_puppeteer.hub_spawn import _exclusive_nonblocking_lock

with _exclusive_nonblocking_lock(Path({str(lock_path)!r})) as locked:
    print("LOCKED", flush=True)
    time.sleep(3.0)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            line = proc.stdout.readline().strip()
            assert line == "LOCKED"

            popen_called: list[Any] = []

            def fake_popen(*args: Any, **kwargs: Any) -> _FakePopen:
                popen_called.append((args, kwargs))
                return _FakePopen()

            outcome = try_spawn_hub(
                lock_path=lock_path,
                host="127.0.0.1",
                port=_get_free_port(),
                popen=fake_popen,
                startup_deadline=0.5,
            )
            assert outcome == SpawnOutcome.ANOTHER_PROCESS_OWNS_LOCK
            assert popen_called == [], "must not spawn when lock held"
        finally:
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)

    def test_already_listening_after_acquiring_lock(
        self, tmp_path: Path, listen_socket: socket.socket
    ) -> None:
        """ロック獲得後の再 probe で既に listen 中ならスキップ。"""
        lock_path = tmp_path / "free.lock"
        port = listen_socket.getsockname()[1]

        popen_called: list[Any] = []

        def fake_popen(*args: Any, **kwargs: Any) -> _FakePopen:
            popen_called.append((args, kwargs))
            return _FakePopen()

        outcome = try_spawn_hub(
            lock_path=lock_path,
            host="127.0.0.1",
            port=port,
            popen=fake_popen,
            startup_deadline=0.5,
        )
        assert outcome == SpawnOutcome.ALREADY_LISTENING
        assert popen_called == [], "must not spawn when already listening"

    def test_spawn_success_when_child_starts_listening(self, tmp_path: Path) -> None:
        """spawn 後に子が listen を開始したら SPAWNED を返す。

        実際には Popen を差し替え、テスト用の listen socket を起動する
        スレッドを別途走らせる。
        """
        lock_path = tmp_path / "spawn.lock"
        port = _get_free_port()

        # spawn 呼び出しのタイミングで listen を開始するスレッド
        listen_started = threading.Event()

        def delayed_listen() -> None:
            time.sleep(0.3)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.listen(5)
            listen_started.set()
            # 2 秒待ってから閉じる
            time.sleep(2.0)
            s.close()

        t = threading.Thread(target=delayed_listen, daemon=True)

        def fake_popen(*_args: Any, **_kwargs: Any) -> _FakePopen:
            t.start()
            return _FakePopen()  # 生存中のプロセスを模擬

        try:
            outcome = try_spawn_hub(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                popen=fake_popen,
                startup_deadline=2.0,
                poll_interval=0.05,
            )
            assert outcome == SpawnOutcome.SPAWNED
            assert listen_started.is_set()
        finally:
            t.join(timeout=3.0)

    def test_raises_when_child_exits_immediately(self, tmp_path: Path) -> None:
        """spawn 直後に子が死んだら HubStartupError。"""
        lock_path = tmp_path / "dead.lock"
        port = _get_free_port()

        def fake_popen(*_args: Any, **_kwargs: Any) -> _FakePopen:
            return _FakePopen(exit_immediately=True, exit_code=2)

        with pytest.raises(HubStartupError, match="exited immediately"):
            try_spawn_hub(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                popen=fake_popen,
                startup_deadline=2.0,
            )

    def test_raises_on_startup_timeout(self, tmp_path: Path) -> None:
        """spawn しても listen しなければタイムアウトで HubStartupError + kill。"""
        lock_path = tmp_path / "slow.lock"
        port = _get_free_port()

        fake = _FakePopen()

        def fake_popen(*_args: Any, **_kwargs: Any) -> _FakePopen:
            return fake

        with pytest.raises(HubStartupError, match="did not start listening"):
            try_spawn_hub(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                popen=fake_popen,
                startup_deadline=0.3,
                poll_interval=0.05,
            )
        assert fake.kill_called, "must kill runaway child"

    def test_passes_env_to_popen(self, tmp_path: Path) -> None:
        """`env=` が subprocess.Popen の kwargs に正しく渡ること。

        プラグインから `PYTHONPATH` を注入するフローを保証するため。
        """
        lock_path = tmp_path / "envpass.lock"
        port = _get_free_port()
        captured: dict[str, Any] = {}

        def fake_popen(*_args: Any, **kwargs: Any) -> _FakePopen:
            captured.update(kwargs)
            return _FakePopen(exit_immediately=True, exit_code=0)

        custom_env = {"FOO": "bar", "PYTHONPATH": "/custom/path"}
        # startup_deadline 後にすぐ exit_immediately で HubStartupError になる
        # が、その前に Popen kwargs は記録されているのでそれを検証する
        with pytest.raises(HubStartupError):
            try_spawn_hub(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                popen=fake_popen,
                env=custom_env,
                startup_deadline=0.1,
            )
        assert captured.get("env") == custom_env

    def test_omits_env_when_none(self, tmp_path: Path) -> None:
        """`env=None` のとき Popen の kwargs に env キーが含まれない
        （親環境を継承するため）。"""
        lock_path = tmp_path / "envnone.lock"
        port = _get_free_port()
        captured: dict[str, Any] = {}

        def fake_popen(*_args: Any, **kwargs: Any) -> _FakePopen:
            captured.update(kwargs)
            return _FakePopen(exit_immediately=True, exit_code=0)

        with pytest.raises(HubStartupError):
            try_spawn_hub(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                popen=fake_popen,
                startup_deadline=0.1,
            )
        assert "env" not in captured

    def test_closes_log_file_on_parent_side(self, tmp_path: Path) -> None:
        """`log_file` を渡した場合、親側のハンドルは Popen 直後に閉じられる。

        子プロセスは OS レベルで fd を継承済みなので親は close して OK。
        close し忘れると GC で `ResourceWarning: unclosed file` が出る。
        """
        lock_path = tmp_path / "log.lock"
        log_path = tmp_path / "hub.out"
        port = _get_free_port()

        captured: dict[str, Any] = {}

        def fake_popen(*_args: Any, **kwargs: Any) -> _FakePopen:
            captured.update(kwargs)
            return _FakePopen(exit_immediately=True, exit_code=0)

        with pytest.raises(HubStartupError):
            try_spawn_hub(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                popen=fake_popen,
                log_file=log_path,
                startup_deadline=0.1,
            )
        stdout = captured.get("stdout")
        # stdout は open された file-like。親側で close 済みであること。
        assert hasattr(stdout, "closed")
        assert stdout.closed is True, "parent side must close inherited log handle"

    def test_retains_child_reference_on_success(self, tmp_path: Path) -> None:
        """spawn 成功後、Popen オブジェクトがモジュールレベルに保持される。

        ローカル変数のみで放置すると GC で `ResourceWarning: subprocess is
        still running` が出るため、モジュール寿命まで参照を残す必要がある。
        """
        from qgis_puppeteer.hub_spawn import _SPAWNED_CHILDREN

        lock_path = tmp_path / "retain.lock"
        port = _get_free_port()
        before = len(_SPAWNED_CHILDREN)

        # spawn 後すぐに listen 開始するスレッド
        def delayed_listen() -> None:
            time.sleep(0.1)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.listen(5)
            time.sleep(1.0)
            s.close()

        t = threading.Thread(target=delayed_listen, daemon=True)
        fake = _FakePopen()

        def fake_popen(*_args: Any, **_kwargs: Any) -> _FakePopen:
            t.start()
            return fake

        try:
            outcome = try_spawn_hub(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                popen=fake_popen,
                startup_deadline=2.0,
                poll_interval=0.05,
            )
            assert outcome == SpawnOutcome.SPAWNED
            assert len(_SPAWNED_CHILDREN) == before + 1
            assert _SPAWNED_CHILDREN[-1] is fake
        finally:
            t.join(timeout=3.0)


# ============================================================
# ensure_hub_reachable — 全体フロー
# ============================================================


class TestEnsureHubReachable:
    def test_already_listening_skips_spawn(
        self, tmp_path: Path, listen_socket: socket.socket
    ) -> None:
        """初回 probe で繋がれば spawn を試みずに戻る。"""
        lock_path = tmp_path / "skip.lock"
        port = listen_socket.getsockname()[1]

        def fake_popen(*_args: Any, **_kwargs: Any) -> _FakePopen:
            raise AssertionError("must not spawn")

        outcome = ensure_hub_reachable(
            lock_path=lock_path,
            host="127.0.0.1",
            port=port,
            popen=fake_popen,
            connect_backoff=(0.05,),
        )
        assert outcome == EnsureOutcome.ALREADY_LISTENING

    def test_spawns_when_not_listening(self, tmp_path: Path) -> None:
        """probe 失敗 → ロック獲得 → spawn して listen を開始 → SPAWNED_BY_US。"""
        lock_path = tmp_path / "spawn-it.lock"
        port = _get_free_port()

        def delayed_listen() -> None:
            time.sleep(0.2)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.listen(5)
            time.sleep(2.0)
            s.close()

        t = threading.Thread(target=delayed_listen, daemon=True)

        def fake_popen(*_args: Any, **_kwargs: Any) -> _FakePopen:
            t.start()
            return _FakePopen()

        try:
            outcome = ensure_hub_reachable(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                popen=fake_popen,
                startup_deadline=2.0,
                poll_interval=0.05,
                connect_backoff=(0.05,),
            )
            assert outcome == EnsureOutcome.SPAWNED_BY_US
        finally:
            t.join(timeout=3.0)

    def test_waits_when_other_process_spawning(self, tmp_path: Path) -> None:
        """他プロセスが spawn 中（ロック保持）で、途中で listen が
        開始された場合は SPAWNED_BY_OTHER を返す。"""
        lock_path = tmp_path / "other-spawning.lock"
        port = _get_free_port()

        # 外部プロセスにロックを先に保持させ、少し遅らせて listen 開始させる。
        # 親側の初回 probe は失敗し、ロック取得にも失敗（= ANOTHER）。
        # バックオフ中に helper が listen を開始して SPAWNED_BY_OTHER となる。
        helper_script = f"""
import sys, time, socket
sys.path.insert(0, {_pkg_parent!r})
from pathlib import Path
from qgis_puppeteer.hub_spawn import _exclusive_nonblocking_lock

with _exclusive_nonblocking_lock(Path({str(lock_path)!r})) as locked:
    assert locked
    print("LOCKED", flush=True)
    time.sleep(0.5)  # 親が initial probe と lock 試行を済ませるまで待つ
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", {port}))
    s.listen(5)
    print("LISTENING", flush=True)
    time.sleep(1.5)
    s.close()
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", helper_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "LOCKED"
            # ここで ensure_hub_reachable を呼ぶと、この時点では listen していない

            def fake_popen(*_args: Any, **_kwargs: Any) -> _FakePopen:
                raise AssertionError("must not spawn (other already did)")

            outcome = ensure_hub_reachable(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                popen=fake_popen,
                connect_backoff=(0.3, 0.5, 0.5),
                probe_timeout=0.1,
            )
            assert outcome == EnsureOutcome.SPAWNED_BY_OTHER
        finally:
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)

    def test_raises_when_nothing_becomes_reachable(self, tmp_path: Path) -> None:
        """他プロセスがロックを握ったまま listen しないなら、バックオフ
        消化後に HubStartupError。"""
        lock_path = tmp_path / "nobody.lock"
        port = _get_free_port()

        # ロックを長く握る helper（listen はしない）
        helper_script = f"""
import sys, time
sys.path.insert(0, {_pkg_parent!r})
from pathlib import Path
from qgis_puppeteer.hub_spawn import _exclusive_nonblocking_lock

with _exclusive_nonblocking_lock(Path({str(lock_path)!r})) as locked:
    print("LOCKED", flush=True)
    time.sleep(3.0)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", helper_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "LOCKED"

            def fake_popen(*_args: Any, **_kwargs: Any) -> _FakePopen:
                raise AssertionError("must not spawn (lock held)")

            with pytest.raises(HubStartupError, match="did not become reachable"):
                ensure_hub_reachable(
                    lock_path=lock_path,
                    host="127.0.0.1",
                    port=port,
                    popen=fake_popen,
                    connect_backoff=(0.05, 0.05, 0.05),
                    probe_timeout=0.05,
                )
        finally:
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
