"""``pytest_qgis_puppeteer.spawn`` の単体テスト。

実 QGIS subprocess を起動するロジックは integration test に任せ、ここでは:

- 引数の排他チェック（``ValueError``）
- env 合成（``QPUPPETEER_HUB_PORT`` を helper が常に勝つ等）
- pid フィルタによる instance 解決
- dev mode skip 動作
- 排他 / 自動採番 / labels 等の単体ロジック

を `subprocess.Popen` を monkeypatch して検証する。
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest
from pytest_qgis_puppeteer import spawn as spawn_module
from pytest_qgis_puppeteer.spawn import (
    SpawnedWorker,
    WorkerRegisterRejected,
    WorkerRegisterTimeout,
    _build_command,
    _build_env,
    _wait_for_worker_by_token,
    spawn_qgis,
    stop_registered_worker,
)
from qgis_puppeteer.client import RegistrationRejectedError
from qgis_puppeteer.protocol import RejectedRegistration

# ============================================================
# fakes
# ============================================================


@dataclass
class _FakeInstance:
    """``InstanceInfo`` の最小スタブ。spawn.py は ``pid`` / ``instance_id`` /
    ``label`` しか触らないので、これで足りる。

    ``state`` / ``project`` は `wait_for_ready` の選別条件（active であること、
    必要なら project が入っていること）を再現するために持つ。
    """

    instance_id: str
    pid: int
    label: str = ""
    launch_token: str | None = None
    state: str = "active"
    project: str | None = None


class _FakeAutomationClient:
    """``E2EAutomationClient`` の単体テスト用スタブ。

    ``list_instances()`` の戻り値をシナリオごとに切り替えるため、`feed()` で
    時系列を仕込めるようにしている。``execute_python`` は呼ばれた事実だけ記録。

    ``wait_for_ready`` は本物（`E2EAutomationClient`）の契約のうち
    「``state == "active"`` で selector に一致するものを返す」だけを真似る。
    往復 probe までは再現しない（そちらは test_automation_client 側で本物を
    テストしている）。
    """

    def __init__(self) -> None:
        self._scripted: list[list[_FakeInstance]] = []
        self.execute_python_calls: list[dict[str, Any]] = []
        self._popens: list[Any] | None = None

    def feed(self, *snapshots: list[_FakeInstance]) -> None:
        self._scripted.extend(snapshots)

    def wait_for_ready(
        self,
        selector: str | None = None,
        *,
        timeout_s: float = 60.0,
        poll_interval_s: float = 0.25,
        **_kwargs: Any,
    ) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            for info in self.list_instances():
                if getattr(info, "state", "active") != "active":
                    continue
                if selector is None or getattr(info, "launch_token", None) == selector:
                    return info.instance_id
            if time.monotonic() >= deadline:
                raise TimeoutError(f"fake: nothing became ready within {timeout_s}s")
            time.sleep(poll_interval_s)

    def bind_popens(self, popens: list[Any]) -> None:
        """ADR-0005 D6: spawn_qgis が注入した launch_token を fed instance に
        反映できるよう、生成された _FakePopen list を結びつける。

        feed() の scripted instance は token を事前に知り得ない（spawn 内部で
        ランダム発番されるため）。list_instances() 時に最新 Popen の env から
        QPUPPETEER_LAUNCH_TOKEN を読み、launch_token 未設定の instance に刻む。
        """
        self._popens = popens

    def _current_token(self) -> str | None:
        if not self._popens:
            return None
        return self._popens[-1].env.get("QPUPPETEER_LAUNCH_TOKEN")

    def list_instances(self) -> list[_FakeInstance]:
        if not self._scripted:
            return []
        snapshot = self._scripted[0] if len(self._scripted) == 1 else self._scripted.pop(0)
        token = self._current_token()
        if token is not None:
            for inst in snapshot:
                if inst.launch_token is None:
                    inst.launch_token = token
        return snapshot

    # QGIS 側で起きることの代役（閉じる、応答しない等）。None なら何もしない
    on_execute_python: Any = None

    def execute_python(self, code: str, *, instance: str | None = None) -> Any:
        self.execute_python_calls.append({"code": code, "instance": instance})
        if self.on_execute_python is not None:
            self.on_execute_python(code, instance)
        return None


class _FakeProcesses:
    """``spawn._pid_alive`` / ``spawn._force_kill_pid`` の代役。

    ``alive`` に入っている pid だけが生きている。強制終了された pid は
    ``force_killed`` に積まれ、``alive`` から消える。
    """

    def __init__(self) -> None:
        self.alive: set[int] = set()
        self.force_killed: list[int] = []

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive

    def force_kill(self, pid: int) -> None:
        self.force_killed.append(pid)
        self.alive.discard(pid)


@pytest.fixture
def processes(monkeypatch: pytest.MonkeyPatch) -> _FakeProcesses:
    fake = _FakeProcesses()
    monkeypatch.setattr(spawn_module, "_pid_alive", fake.pid_alive)
    monkeypatch.setattr(spawn_module, "_force_kill_pid", fake.force_kill)
    return fake


class _FakePopen:
    """``subprocess.Popen`` の最小スタブ。

    drain thread を開始すべき stdout/stderr は ``BytesIO`` 風に作る（EOF で抜ける
    だけのシンプルな iterator）。``poll()`` は ``returncode`` を返す既存契約に
    合わせる。
    """

    def __init__(self, pid: int = 12345, env: dict[str, str] | None = None) -> None:
        self.pid = pid
        self.env = env or {}
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.stdout = _ClosablePipe(b"")
        self.stderr = _ClosablePipe(b"")
        self._wait_event = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._wait_event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._wait_event.set()

    def wait(self, timeout: float | None = None) -> int:
        self._wait_event.wait(timeout=timeout)
        if self.returncode is None:
            import subprocess as _sp

            raise _sp.TimeoutExpired(cmd="fake", timeout=timeout or 0.0)
        return self.returncode


class _ClosablePipe:
    """``readline()`` で空行を返す BytesIO 風 pipe。drain thread が抜けられればよい。"""

    def __init__(self, data: bytes) -> None:
        self._lines = [data] if data else []
        self.closed = False

    def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


# ============================================================
# _build_command
# ============================================================


class TestBuildCommand:
    """引数排他チェックとコマンド組み立て。"""

    def test_qgis_bin_only(self) -> None:
        assert _build_command(qgis_bin="qgis-bin.exe", args=None, command=None) == ["qgis-bin.exe"]

    def test_qgis_bin_with_args(self) -> None:
        assert _build_command(
            qgis_bin="qgis-bin.exe",
            args=["--profile=test", "--clean-canvas"],
            command=None,
        ) == ["qgis-bin.exe", "--profile=test", "--clean-canvas"]

    def test_command_only(self) -> None:
        assert _build_command(
            qgis_bin=None,
            args=None,
            command=["launcher.bat", "--config", "e2e"],
        ) == ["launcher.bat", "--config", "e2e"]

    def test_command_and_qgis_bin_is_error(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            _build_command(qgis_bin="qgis.exe", args=None, command=["launcher.bat"])

    def test_command_and_args_is_error(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            _build_command(qgis_bin=None, args=["--x"], command=["launcher.bat"])

    def test_neither_specified_is_error(self) -> None:
        with pytest.raises(ValueError, match="must be specified"):
            _build_command(qgis_bin=None, args=None, command=None)

    def test_empty_command_is_error(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _build_command(qgis_bin=None, args=None, command=[])


# ============================================================
# _build_env
# ============================================================


class TestBuildEnv:
    """env 合成: helper の自動注入とユーザー指定の merge ルール。"""

    def test_minimal_injects_required_vars(self) -> None:
        env = _build_env(base={}, hub_port=9876, user_env=None)
        assert env["QPUPPETEER_HUB_PORT"] == "9876"
        assert env["QPUPPETEER_HUB_HOST"] == "127.0.0.1"
        assert env["QPUPPETEER_TRUSTED_MODE"] == "1"
        assert env["QPUPPETEER_ALLOW_TEST_HANDLERS"] == "1"

    def test_inherits_base_env(self) -> None:
        env = _build_env(
            base={"PATH": "/usr/bin", "LANG": "ja_JP.UTF-8"},
            hub_port=9876,
            user_env=None,
        )
        assert env["PATH"] == "/usr/bin"
        assert env["LANG"] == "ja_JP.UTF-8"

    def test_user_env_added_on_top(self) -> None:
        env = _build_env(
            base={},
            hub_port=9876,
            user_env={"MYAPP_FEATURE": "auth"},
        )
        assert env["MYAPP_FEATURE"] == "auth"

    def test_helper_wins_for_hub_port(self) -> None:
        """ユーザーが ``QPUPPETEER_HUB_PORT`` を指定しても helper が常に勝つ。"""
        env = _build_env(
            base={},
            hub_port=9876,
            user_env={"QPUPPETEER_HUB_PORT": "1234"},
        )
        assert env["QPUPPETEER_HUB_PORT"] == "9876"

    def test_user_env_can_override_trusted_mode(self) -> None:
        """``TRUSTED_MODE`` は setdefault なのでユーザーが先に指定すれば勝てる。"""
        env = _build_env(
            base={},
            hub_port=9876,
            user_env={"QPUPPETEER_TRUSTED_MODE": "0"},
        )
        assert env["QPUPPETEER_TRUSTED_MODE"] == "0"


# ============================================================
# _wait_for_worker_by_token（ADR-0005 D6）
# ============================================================


class TestWaitForWorkerByToken:
    """launch_token による決定的 instance_id 解決（pid diff heuristic を置換）。

    実体は `E2EAutomationClient.wait_for_ready` に委譲しているので、ここで見るのは
    「token を selector として渡すこと」と「TimeoutError を
    `WorkerRegisterTimeout` に変換すること」。選別と往復 probe の中身は
    test_automation_client の TestWaitForReady 側で本物をテストしている。
    """

    def test_skips_unresponsive_instances(self) -> None:
        """register 直後に GUI が塞がっている個体は「使える」ではない。

        `list_instances` が unresponsive も返すようになったので、state を見ずに
        token 一致だけで拾うと、何も処理できない個体を掴んでしまう。
        """
        client = _FakeAutomationClient()
        client.feed(
            [_FakeInstance(instance_id="mine", pid=1, launch_token="lt-me", state="unresponsive")],
            [_FakeInstance(instance_id="mine", pid=1, launch_token="lt-me", state="active")],
        )
        result = _wait_for_worker_by_token(
            client,  # type: ignore[arg-type]
            launch_token="lt-me",
            timeout_s=1.0,
            poll_interval_s=0.01,
        )
        assert result == "mine"

    def test_returns_instance_id_when_token_matches(self) -> None:
        client = _FakeAutomationClient()
        client.feed([_FakeInstance(instance_id="inst-1", pid=12345, launch_token="lt-me")])
        result = _wait_for_worker_by_token(
            client,  # type: ignore[arg-type]
            launch_token="lt-me",
            timeout_s=1.0,
            poll_interval_s=0.01,
        )
        assert result == "inst-1"

    def test_ignores_other_tokens_even_same_pid(self) -> None:
        """pid が再利用されても token が違えば自分の Worker ではない。"""
        client = _FakeAutomationClient()
        client.feed(
            [
                _FakeInstance(instance_id="other", pid=12345, launch_token="lt-old"),
                _FakeInstance(instance_id="mine", pid=12345, launch_token="lt-new"),
            ]
        )
        result = _wait_for_worker_by_token(
            client,  # type: ignore[arg-type]
            launch_token="lt-new",
            timeout_s=1.0,
            poll_interval_s=0.01,
        )
        assert result == "mine"

    def test_timeout_raises_worker_register_timeout(self) -> None:
        client = _FakeAutomationClient()
        client.feed([])  # 何も register されない
        with pytest.raises(WorkerRegisterTimeout, match="did not register"):
            _wait_for_worker_by_token(
                client,  # type: ignore[arg-type]
                launch_token="lt-missing",
                timeout_s=0.2,
                poll_interval_s=0.05,
            )

    def test_skips_tokenless_instances(self) -> None:
        """launch_token を持たない（helper 非経由）instance は対象外。"""
        client = _FakeAutomationClient()
        client.feed([_FakeInstance(instance_id="other", pid=99999, launch_token=None)])
        with pytest.raises(WorkerRegisterTimeout):
            _wait_for_worker_by_token(
                client,  # type: ignore[arg-type]
                launch_token="lt-x",
                timeout_s=0.2,
                poll_interval_s=0.05,
            )


# ============================================================
# SpawnedWorker
# ============================================================


class TestSpawnedWorker:
    """戻り値 dataclass の基本契約。"""

    def test_minimum_construction(self) -> None:
        worker = SpawnedWorker(instance_id="inst-1", pid=12345, label="worker-12345")
        assert worker.instance_id == "inst-1"
        assert worker.pid == 12345
        assert worker.label == "worker-12345"

    def test_captured_buffers_default_empty(self) -> None:
        worker = SpawnedWorker(instance_id="i", pid=1, label="l")
        assert worker._captured_stdout == []
        assert worker._captured_stderr == []

    def test_captured_buffers_are_per_instance(self) -> None:
        """frozen dataclass + default_factory で list が共有されないこと。"""
        a = SpawnedWorker(instance_id="a", pid=1, label="a")
        b = SpawnedWorker(instance_id="b", pid=2, label="b")
        a._captured_stdout.append("from a")
        assert b._captured_stdout == []

    def test_captured_stdout_returns_snapshot(self) -> None:
        """ADR-0004 P2: ``captured_stdout()`` は list の snapshot を返す
        （drain thread が並行 append しても 安全に呼べる）。"""
        worker = SpawnedWorker(instance_id="i", pid=1, label="l")
        worker._captured_stdout.extend(["line 1", "line 2"])
        snap = worker.captured_stdout()
        assert snap == ["line 1", "line 2"]
        # 戻り値は copy なので mutate しても元には影響しない
        snap.append("mutated")
        assert worker._captured_stdout == ["line 1", "line 2"]

    def test_captured_stderr_returns_snapshot(self) -> None:
        worker = SpawnedWorker(instance_id="i", pid=1, label="l")
        worker._captured_stderr.extend(["err 1", "err 2"])
        assert worker.captured_stderr() == ["err 1", "err 2"]

    def test_captured_methods_empty_initially(self) -> None:
        worker = SpawnedWorker(instance_id="i", pid=1, label="l")
        assert worker.captured_stdout() == []
        assert worker.captured_stderr() == []


# ============================================================
# spawn_qgis（context manager レベル）
# ============================================================


class TestSpawnQgisContextManager:
    """``spawn_qgis()`` 全体の挙動。Popen と sleep をモックして高速化。"""

    @pytest.fixture
    def mock_popen(self, monkeypatch: pytest.MonkeyPatch) -> list[_FakePopen]:
        """``subprocess.Popen`` を ``_FakePopen`` 返却に差し替える。

        生成された Popen インスタンスを list に積んで返すので、テストで
        ``mock_popen[0].terminated`` 等を assert できる。

        OS 固有の kill 経路も mock して `terminated` / `killed` フラグが立つ
        ようにする:

        - Windows: ``subprocess.run`` の taskkill コマンドを no-op にしつつ
          対応する ``_FakePopen`` の terminate/kill を呼ぶ。
        - Linux/Mac: ``os.kill`` を mock し、SIGTERM/SIGKILL を見て対応する
          ``_FakePopen`` の terminate/kill を呼ぶ。
        """
        created: list[_FakePopen] = []

        def _fake_popen(*args: Any, **kwargs: Any) -> _FakePopen:
            proc = _FakePopen(env=kwargs.get("env"))
            created.append(proc)
            return proc

        def _fake_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            # Windows: taskkill /PID <pid> ... → 対応する _FakePopen を terminate/kill
            if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "taskkill":
                try:
                    pid = int(cmd[2])
                except (ValueError, IndexError):
                    pid = None
                force = "/F" in cmd
                for proc in created:
                    if pid is None or proc.pid == pid:
                        if force:
                            proc.kill()
                        else:
                            proc.terminate()

            class _Result:
                returncode = 0
                stdout = b""
                stderr = b""

            return _Result()

        def _fake_os_kill(pid: int, sig: int) -> None:
            # Linux/Mac: SIGTERM/SIGKILL → 対応する _FakePopen を terminate/kill
            import signal as _signal

            for proc in created:
                if proc.pid != pid:
                    continue
                if sig == _signal.SIGTERM:
                    proc.terminate()
                elif sig == _signal.SIGKILL:
                    proc.kill()

        monkeypatch.setattr("pytest_qgis_puppeteer.spawn.subprocess.Popen", _fake_popen)
        monkeypatch.setattr("pytest_qgis_puppeteer.spawn.subprocess.run", _fake_run)
        monkeypatch.setattr("pytest_qgis_puppeteer.spawn.os.kill", _fake_os_kill)
        return created

    def test_dev_mode_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", "1")
        client = _FakeAutomationClient()
        with (
            pytest.raises(pytest.skip.Exception),
            spawn_qgis(
                hub_port=9876,
                automation_client=client,  # type: ignore[arg-type]
                qgis_bin="qgis-bin.exe",
            ),
        ):
            pass  # ここは到達しない

    def test_arg_validation_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """dev mode 判定の前に ``ValueError`` が来ないが、引数排他は
        ``__enter__`` の中で評価される。"""
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        with (
            pytest.raises(ValueError, match="mutually exclusive"),
            spawn_qgis(
                hub_port=9876,
                automation_client=client,  # type: ignore[arg-type]
                qgis_bin="qgis-bin.exe",
                command=["launcher.bat"],
            ),
        ):
            pass

    def test_happy_path_yields_worker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
    ) -> None:
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        # 1 回目: register 後（launch_token が刻まれる）, 2 回目: teardown dangling
        client.feed(
            [_FakeInstance(instance_id="inst-1", pid=12345)],  # register 後
            [],  # teardown 時の dangling check
        )
        with spawn_qgis(
            hub_port=9876,
            automation_client=client,  # type: ignore[arg-type]
            qgis_bin="qgis-bin.exe",
            args=["--clean-canvas"],
            register_timeout_s=2.0,
            graceful_shutdown_timeout_s=0.1,
        ) as worker:
            assert worker.instance_id == "inst-1"
            assert worker.pid == 12345
            assert worker.label == "worker-12345"
        # teardown でプロセスが片付けられている
        assert mock_popen[0].terminated or mock_popen[0].killed

    def test_explicit_label_is_respected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
    ) -> None:
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        client.feed(
            [_FakeInstance(instance_id="inst-1", pid=12345)],
            [],
        )
        with spawn_qgis(
            hub_port=9876,
            automation_client=client,  # type: ignore[arg-type]
            qgis_bin="qgis-bin.exe",
            label="clean_canvas_test",
            register_timeout_s=2.0,
            graceful_shutdown_timeout_s=0.1,
        ) as worker:
            assert worker.label == "clean_canvas_test"

    def test_register_timeout_terminates_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
    ) -> None:
        """register が来なかったら subprocess を片付けて raise する。"""
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.feed([])  # spawn 前の pre_existing_pids 採取（空）。以降 register なし
        with (
            pytest.raises(WorkerRegisterTimeout),
            spawn_qgis(
                hub_port=9876,
                automation_client=client,  # type: ignore[arg-type]
                qgis_bin="qgis-bin.exe",
                register_timeout_s=0.2,
                graceful_shutdown_timeout_s=0.1,
            ),
        ):
            pass
        # subprocess は terminate された（dangling 防止）
        assert mock_popen[0].terminated or mock_popen[0].killed

    @pytest.mark.parametrize(
        ("label", "env"),
        [
            ("x" * 129, None),
            ("1234", None),
            (None, {"QPUPPETEER_WORKER_LABEL": "@reserved"}),
        ],
        ids=["too-long", "digits-only", "env-at-prefix"],
    )
    def test_label_the_hub_would_reject_fails_before_launch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        label: str | None,
        env: dict[str, str] | None,
    ) -> None:
        """#13: 起こしてから断られると、待ったあとに「登録されなかった」としか分からない。"""
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        with (
            pytest.raises(ValueError, match="would reject the worker label"),
            spawn_qgis(
                hub_port=9876,
                automation_client=client,  # type: ignore[arg-type]
                qgis_bin="qgis-bin.exe",
                label=label,
                env=env,
            ),
        ):
            pass
        assert mock_popen == []  # QGIS は起こしていない

    def test_rejected_registration_kills_the_qgis_and_fails_fast(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        processes: _FakeProcesses,
    ) -> None:
        """#13: Hub が断ったら待たずに失敗し、断られた QGIS（pid）を止める。"""
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        processes.alive.add(23456)

        def _rejected(*_args: Any, **_kwargs: Any) -> str:
            raise RegistrationRejectedError(
                RejectedRegistration(
                    launch_token="lt-x",
                    pid=23456,
                    label="fresh-x",
                    code="label_conflict",
                    message="Label 'fresh-x' is already in use by a live instance",
                )
            )

        client.wait_for_ready = _rejected  # type: ignore[method-assign]
        started = time.monotonic()
        with (
            pytest.raises(WorkerRegisterRejected, match="already in use") as excinfo,
            spawn_qgis(
                hub_port=9876,
                automation_client=client,  # type: ignore[arg-type]
                command=["launcher.bat"],
                register_timeout_s=30.0,
                graceful_shutdown_timeout_s=0.1,
            ),
        ):
            pass
        assert time.monotonic() - started < 5.0
        assert isinstance(excinfo.value, WorkerRegisterTimeout)  # 既存の except でも拾える
        assert excinfo.value.rejection.pid == 23456
        assert processes.force_killed == [23456]
        assert mock_popen[0].terminated or mock_popen[0].killed

    def _timeout_spawn(self, client: _FakeAutomationClient) -> None:
        """登録待ちが時間切れになる spawn（ランチャーはすぐ終わる）。"""

        def _timeout(*_args: Any, **_kwargs: Any) -> str:
            raise TimeoutError("not usable in time")

        client.wait_for_ready = _timeout  # type: ignore[method-assign]
        with (
            pytest.raises(WorkerRegisterTimeout),
            spawn_qgis(
                hub_port=9876,
                automation_client=client,  # type: ignore[arg-type]
                command=["launcher.bat"],
                register_timeout_s=1.0,
                graceful_shutdown_timeout_s=0.1,
            ),
        ):
            pass

    @pytest.mark.parametrize("state", ["active", "unresponsive"])
    def test_qgis_registered_too_late_is_killed_after_the_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        processes: _FakeProcesses,
        state: str,
    ) -> None:
        """#16: 待ちが切れても、Hub に見えている自分の QGIS は pid で分かるので止める。

        閉じるよう頼まない（GUI が塞がっていると、応答の上限まで待つことになる）。
        """
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        client.feed([_FakeInstance(instance_id="inst-late", pid=23456, state=state)])
        processes.alive.add(23456)
        self._timeout_spawn(client)
        assert client.execute_python_calls == []
        assert processes.force_killed == [23456]

    def test_the_hub_is_told_to_abandon_the_launch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        processes: _FakeProcesses,
    ) -> None:
        """#16 案 2: 遅れて登録してくる QGIS が自分で閉じるよう、Hub に知らせる。"""
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        abandoned: list[str] = []
        client.abandon_launch = abandoned.append  # type: ignore[attr-defined]
        self._timeout_spawn(client)
        assert abandoned == [mock_popen[0].env["QPUPPETEER_LAUNCH_TOKEN"]]

    def test_someone_elses_qgis_is_left_alone_after_the_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        processes: _FakeProcesses,
    ) -> None:
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        client.feed([_FakeInstance(instance_id="other", pid=999, launch_token="lt-other")])
        processes.alive.add(999)
        self._timeout_spawn(client)
        assert client.execute_python_calls == []
        assert processes.force_killed == []

    def test_rejected_registration_found_after_the_timeout_is_killed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        processes: _FakeProcesses,
    ) -> None:
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        processes.alive.add(23456)
        client.list_rejected = lambda: [  # type: ignore[attr-defined]
            RejectedRegistration(
                launch_token=client._current_token() or "",
                pid=23456,
                label="x",
                code="label_conflict",
                message="",
            )
        ]
        self._timeout_spawn(client)
        assert processes.force_killed == [23456]

    def test_descendants_are_searched_only_when_the_hub_saw_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        processes: _FakeProcesses,
    ) -> None:
        """#18: Hub に何も見えないときだけ、起動したプロセスの子孫をたどる。"""
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        searched: list[int] = []
        monkeypatch.setattr(
            spawn_module,
            "_kill_unregistered_qgis",
            lambda proc, *, launch_token, launched_at, gave_up_at: searched.append(proc.pid),
        )
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        self._timeout_spawn(client)  # Hub には何も見えない
        assert searched == [mock_popen[0].pid]

        searched.clear()
        client2 = _FakeAutomationClient()
        client2.bind_popens(mock_popen)
        client2.feed([_FakeInstance(instance_id="late", pid=23456)])  # Hub に見えている
        processes.alive.add(23456)
        self._timeout_spawn(client2)
        assert searched == []
        assert 23456 in processes.force_killed

    def test_launcher_exited_worker_is_asked_to_close(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        processes: _FakeProcesses,
    ) -> None:
        """#9: ランチャーが先に終わっていても、一覧の pid の QGIS を閉じる。

        以前はここで、動いている QGIS の中で ``exitQgis()`` を呼んで落としていた。
        """
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        client.feed(
            [_FakeInstance(instance_id="inst-1", pid=23456)],  # register 検出
            [_FakeInstance(instance_id="inst-1", pid=23456)],  # teardown 時もまだ居る
        )
        processes.alive.add(23456)
        client.on_execute_python = lambda code, instance: processes.alive.discard(23456)
        with spawn_qgis(
            hub_port=9876,
            automation_client=client,  # type: ignore[arg-type]
            command=["launcher.bat"],
            register_timeout_s=2.0,
            graceful_shutdown_timeout_s=0.5,
        ):
            mock_popen[0].returncode = 0  # ランチャーは QGIS を起こして終わった
        codes = [call["code"] for call in client.execute_python_calls]
        assert len(codes) == 1
        assert "close_qgis(iface)" in codes[0]  # モーダルを reject してから閉じる
        assert "iface.mainWindow().close" in codes[0]  # 古いプラグイン向けの閉じ方
        assert "setDirty(False)" in codes[0]
        assert client.execute_python_calls[0]["instance"] == "inst-1"
        assert not any("exitQgis" in code for code in codes)
        assert processes.force_killed == []
        assert not mock_popen[0].terminated

    def test_worker_that_does_not_close_is_force_killed_alone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        processes: _FakeProcesses,
    ) -> None:
        """閉じない QGIS は、その pid だけを強制終了する。"""
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        client.feed(
            [_FakeInstance(instance_id="inst-1", pid=23456)],
            [_FakeInstance(instance_id="inst-1", pid=23456)],
        )
        processes.alive.add(23456)
        with spawn_qgis(
            hub_port=9876,
            automation_client=client,  # type: ignore[arg-type]
            command=["launcher.bat"],
            register_timeout_s=2.0,
            graceful_shutdown_timeout_s=0.1,
        ):
            mock_popen[0].returncode = 0
        assert len(client.execute_python_calls) == 1
        assert processes.force_killed == [23456]

    def test_unlisted_worker_falls_back_to_the_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
        processes: _FakeProcesses,
    ) -> None:
        """Worker が一覧に居なければ、従来どおり起動したプロセスを止める。"""
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        client.feed(
            [_FakeInstance(instance_id="inst-1", pid=12345)],
            [_FakeInstance(instance_id="other", pid=999)],  # 自分はもう居ない
        )
        with spawn_qgis(
            hub_port=9876,
            automation_client=client,  # type: ignore[arg-type]
            qgis_bin="qgis-bin.exe",
            register_timeout_s=2.0,
            graceful_shutdown_timeout_s=0.1,
        ):
            pass
        assert client.execute_python_calls == []
        assert processes.force_killed == []
        assert mock_popen[0].terminated or mock_popen[0].killed


# ============================================================
# stop_registered_worker / _force_kill_pid（#9）
# ============================================================


class TestStopRegisteredWorker:
    def test_dead_pid_is_left_alone(self, processes: _FakeProcesses) -> None:
        client = _FakeAutomationClient()
        stop_registered_worker(
            client,  # type: ignore[arg-type]
            instance_id="inst-1",
            pid=23456,
            graceful_timeout_s=0.1,
        )
        assert client.execute_python_calls == []
        assert processes.force_killed == []

    def test_unreachable_worker_is_force_killed(self, processes: _FakeProcesses) -> None:
        """閉じるよう頼めない（応答しない）QGIS は待たずに強制終了する。"""
        client = _FakeAutomationClient()

        def _raise(code: str, instance: str | None) -> None:
            raise TimeoutError("worker did not answer")

        client.on_execute_python = _raise
        processes.alive.add(23456)
        started = time.monotonic()
        stop_registered_worker(
            client,  # type: ignore[arg-type]
            instance_id="inst-1",
            pid=23456,
            graceful_timeout_s=5.0,
        )
        assert processes.force_killed == [23456]
        assert time.monotonic() - started < 2.0  # graceful_timeout_s は待たない


class TestForceKillPid:
    def test_windows_kills_the_pid_without_its_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(spawn_module.sys, "platform", "win32")
        monkeypatch.setattr(spawn_module.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd))
        spawn_module._force_kill_pid(23456)
        assert calls == [["taskkill", "/PID", "23456", "/F"]]

    @pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is posix only")
    def test_posix_sends_sigkill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import signal

        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(spawn_module.os, "kill", lambda pid, sig: calls.append((pid, sig)))
        spawn_module._force_kill_pid(23456)
        assert calls == [(23456, signal.SIGKILL)]


class TestShutdownRegisteredWorkers:
    def test_every_listed_worker_is_stopped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """session の後片付けも、pid に taskkill /T ではなく同じ手順で閉じる。"""
        from pytest_qgis_puppeteer.plugin import _shutdown_registered_workers

        stopped: list[tuple[str, int]] = []
        monkeypatch.setattr(
            spawn_module,
            "stop_registered_worker",
            lambda client, *, instance_id, pid, graceful_timeout_s: stopped.append(
                (instance_id, pid)
            ),
        )
        client = _FakeAutomationClient()
        client.feed([_FakeInstance("a", 111), _FakeInstance("b", 222)])
        _shutdown_registered_workers(client)  # type: ignore[arg-type]
        assert stopped == [("a", 111), ("b", 222)]


# ============================================================
# spawn_qgis_fixture（ADR-0004 Phase 3 fixture factory）
# ============================================================


class TestSpawnQgisFixture:
    """``spawn_qgis_fixture`` factory が pytest fixture を正しく生成するか。

    実 spawn_qgis() の subprocess 起動は重いので、ここでは factory 自体の
    return が pytest.fixture でラップされていること、scope パラメータが
    伝わることを確認するに留める。実 spawn 経路は既存の
    ``TestSpawnQgisContextManager`` でカバー済み。

    pytest 9 系では fixture は ``_pytest.fixtures.FixtureFunctionDefinition`` で
    包まれ、scope は ``_fixture_function_marker.scope`` 経由で取れる。
    pytest 内部 API なので version 上げ時に追従が要る。
    """

    def test_returns_fixture_object(self) -> None:
        from pytest_qgis_puppeteer.spawn import spawn_qgis_fixture

        f = spawn_qgis_fixture(scope="module", qgis_bin="x")
        # pytest.fixture でラップ済み（FixtureFunctionDefinition 等）
        assert f is not None
        # fixture marker（pytest 内部）が attach されている
        assert hasattr(f, "_fixture_function_marker")

    def test_default_scope_is_module(self) -> None:
        from pytest_qgis_puppeteer.spawn import spawn_qgis_fixture

        f = spawn_qgis_fixture(qgis_bin="x")
        assert f._fixture_function_marker.scope == "module"

    @pytest.mark.parametrize("scope", ["function", "class", "module", "session"])
    def test_scope_propagates(self, scope: str) -> None:
        from pytest_qgis_puppeteer.spawn import spawn_qgis_fixture

        f = spawn_qgis_fixture(scope=scope, qgis_bin="x")
        assert f._fixture_function_marker.scope == scope

    def test_name_propagates(self) -> None:
        from pytest_qgis_puppeteer.spawn import spawn_qgis_fixture

        f = spawn_qgis_fixture(name="my_qgis_fixture", qgis_bin="x")
        # fixture name は FixtureFunctionDefinition.name 経由
        assert f.name == "my_qgis_fixture"

    def test_exported_from_package_root(self) -> None:
        """``from pytest_qgis_puppeteer import spawn_qgis_fixture`` が動く。"""
        import pytest_qgis_puppeteer

        assert hasattr(pytest_qgis_puppeteer, "spawn_qgis_fixture")
        assert "spawn_qgis_fixture" in pytest_qgis_puppeteer.__all__

    def test_inner_fixture_signature(self) -> None:
        """生成 fixture の inner function は ``hub_port`` / ``automation_client`` を要求する。

        pytest はこれを fixture name として解釈し、自動的に既存 plugin の同名
        fixture から injection する。
        """
        import inspect

        from pytest_qgis_puppeteer.spawn import spawn_qgis_fixture

        f = spawn_qgis_fixture(qgis_bin="x")
        inner = f._get_wrapped_function()
        params = inspect.signature(inner).parameters
        assert set(params.keys()) == {"hub_port", "automation_client"}


# ============================================================
# 登録の要求を一度も送ってこない QGIS（#18）
# ============================================================


class _FakeWindowsProcess:
    """``spawn._WindowsProcess`` の代役。開いた 1 つのハンドルの役をする。"""

    def __init__(self, pid: int, created: float | None, environ: dict[str, str] | None) -> None:
        self.pid = pid
        self._created = created
        self._environ = environ
        self.terminated = False
        self.closed = False

    def created_at(self) -> float | None:
        return self._created

    def environ(self) -> dict[str, str] | None:
        return self._environ

    def terminate(self) -> bool:
        self.terminated = True
        return True

    def close(self) -> None:
        self.closed = True


class TestKillQgisWithToken:
    """環境変数の launch_token で、自分の起こした QGIS だけを止める（#18）。

    ランチャーの途中（cmd、Python、start）が終わっていると親子関係はたどれないが、
    token は環境変数で QGIS まで届いている。調べるのも止めるのも同じハンドルで行う。
    """

    def _processes(self, monkeypatch: pytest.MonkeyPatch) -> dict[int, _FakeWindowsProcess]:
        # (pid, 名前, 作られた時刻, 環境変数)。開けないものは None
        rows: list[tuple[int, str, float | None, dict[str, str] | None]] = [
            (300, "qgis-ltr-bin.exe", 1001.0, {"QPUPPETEER_LAUNCH_TOKEN": "lt-mine"}),  # 止める
            (310, "QGIS-BIN.EXE", 1002.0, {"qpuppeteer_launch_token": "lt-mine"}),  # 大文字小文字
            (320, "qgis-ltr-bin.exe", 1003.0, {}),  # 人が起こした QGIS（token 無し）
            (330, "qgis-ltr-bin.exe", 1004.0, {"QPUPPETEER_LAUNCH_TOKEN": "lt-other"}),
            (340, "qgis-ltr-bin.exe", 900.0, {"QPUPPETEER_LAUNCH_TOKEN": "lt-mine"}),  # 範囲外
            (350, "qgis-ltr-bin.exe", 1005.0, None),  # 環境変数を読めない
            (360, "python.exe", 1006.0, {"QPUPPETEER_LAUNCH_TOKEN": "lt-mine"}),  # QGIS でない
        ]
        opened = {pid: _FakeWindowsProcess(pid, t, e) for pid, _, t, e in rows}
        opened[370] = None  # type: ignore[assignment]  # 開けない（管理者で動いている等）
        monkeypatch.setattr(
            spawn_module,
            "_list_processes",
            lambda: [(p, 1, n) for p, n, _, _ in rows] + [(370, 1, "qgis-bin.exe")],
        )
        monkeypatch.setattr(spawn_module, "_open_process", opened.get)
        return opened

    def test_only_qgis_carrying_our_token_is_killed_through_its_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened = self._processes(monkeypatch)
        killed, unreadable = spawn_module._kill_qgis_with_token(
            "lt-mine", since=1000.0, until=1500.0
        )
        assert sorted(killed) == [300, 310]
        assert [p for p, h in opened.items() if h and h.terminated] == [300, 310]
        assert unreadable == 2  # 350（読めない）と 370（開けない）
        assert all(h.closed for h in opened.values() if h is not None and h.pid != 360)

    def test_unreadable_ones_are_reported(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        self._processes(monkeypatch)
        monkeypatch.setattr(spawn_module.sys, "platform", "win32")
        with caplog.at_level(logging.WARNING, logger="pytest_qgis_puppeteer.spawn"):
            spawn_module._kill_unregistered_qgis(
                _FakePopen(pid=100),  # type: ignore[arg-type]
                launch_token="lt-mine",
                launched_at=1000.0,
                gave_up_at=1500.0,
            )
        assert "Could not inspect 2 QGIS process(es)" in caplog.text
        assert "pid=300" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_windows_process_list_and_creation_time_are_real() -> None:
    import os

    me = os.getpid()
    rows = {pid: (ppid, name) for pid, ppid, name in spawn_module._list_processes()}
    assert rows[me][0] == os.getppid()
    assert rows[me][1].lower().startswith("python")
    process = spawn_module._open_process(me)
    assert process is not None
    try:
        created = process.created_at()
    finally:
        process.close()
    assert created is not None
    assert 0 <= time.time() - created < 3600


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_windows_process_handle_reads_the_token_and_kills() -> None:
    """#18: token は環境変数で子プロセスに届き、外から読める。同じハンドルで止められる。"""
    import os
    import subprocess

    env = dict(os.environ, QPUPPETEER_LAUNCH_TOKEN="lt-probe")
    child = subprocess.Popen(["ping", "-n", "30", "127.0.0.1"], env=env, stdout=subprocess.DEVNULL)
    try:
        time.sleep(0.3)
        process = spawn_module._open_process(child.pid)
        assert process is not None
        try:
            assert process.environ().get("QPUPPETEER_LAUNCH_TOKEN") == "lt-probe"
            created = process.created_at()
            assert created is not None and 0 <= time.time() - created < 60
            assert process.terminate() is True
        finally:
            process.close()
        assert child.wait(timeout=5) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
