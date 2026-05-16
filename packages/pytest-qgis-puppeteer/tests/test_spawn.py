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

import threading
from dataclasses import dataclass
from typing import Any

import pytest
from pytest_qgis_puppeteer.spawn import (
    SpawnedWorker,
    WorkerRegisterTimeout,
    _build_command,
    _build_env,
    _wait_for_worker_by_token,
    spawn_qgis,
)

# ============================================================
# fakes
# ============================================================


@dataclass
class _FakeInstance:
    """``InstanceInfo`` の最小スタブ。spawn.py は ``pid`` / ``instance_id`` /
    ``label`` しか触らないので、これで足りる。
    """

    instance_id: str
    pid: int
    label: str = ""
    launch_token: str | None = None


class _FakeAutomationClient:
    """``E2EAutomationClient`` の単体テスト用スタブ。

    ``list_instances()`` の戻り値をシナリオごとに切り替えるため、`feed()` で
    時系列を仕込めるようにしている。``execute_python`` は呼ばれた事実だけ記録。
    """

    def __init__(self) -> None:
        self._scripted: list[list[_FakeInstance]] = []
        self.execute_python_calls: list[dict[str, Any]] = []
        self._popens: list[Any] | None = None

    def feed(self, *snapshots: list[_FakeInstance]) -> None:
        self._scripted.extend(snapshots)

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

    def execute_python(self, code: str, *, instance: str | None = None) -> dict:
        self.execute_python_calls.append({"code": code, "instance": instance})
        return {}


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
    """launch_token による決定的 instance_id 解決（pid diff heuristic を置換）。"""

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

    def test_dangling_worker_remote_shutdown_attempted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_popen: list[_FakePopen],
    ) -> None:
        """teardown 時に Hub 側に instance が残っていれば remote exitQgis() を呼ぶ。"""
        monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
        client = _FakeAutomationClient()
        client.bind_popens(mock_popen)
        client.feed(
            [_FakeInstance(instance_id="inst-1", pid=12345)],  # register 検出
            [_FakeInstance(instance_id="inst-1", pid=12345)],  # teardown 時もまだ残ってる
        )
        with spawn_qgis(
            hub_port=9876,
            automation_client=client,  # type: ignore[arg-type]
            qgis_bin="qgis-bin.exe",
            register_timeout_s=2.0,
            graceful_shutdown_timeout_s=0.1,
        ):
            pass
        # remote exitQgis() が呼ばれている
        assert any(
            "exitQgis" in call["code"] and call["instance"] == "inst-1"
            for call in client.execute_python_calls
        )


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
