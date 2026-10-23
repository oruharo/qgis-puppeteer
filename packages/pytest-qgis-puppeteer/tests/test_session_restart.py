"""``qgis_auto_fresh_after_failure = session``：失敗の後に session の QGIS を起こし直す（#11）。

実 QGIS は使わない。``SessionQgis`` は spawn の代役で、flag の立ち方は pytester で
確かめる。実機での確認は user-guide / CHANGELOG を参照。
"""

from __future__ import annotations

import contextlib
import logging
import textwrap
from collections.abc import Iterator
from typing import Any

import pytest
from pytest_qgis_puppeteer.plugin import (
    _CONFIG_ATTR_RESTART_SESSION_QGIS,
    SessionQgis,
    _restart_session_qgis,
)

pytest_plugins = ["pytester"]


class _FakeSpawn:
    """``spawn_qgis`` の代役。起こすたびに番号付きの worker を返し、出入りを記録する。"""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.count = 0
        self.fail_next = False

    @contextlib.contextmanager
    def __call__(self) -> Iterator[Any]:
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("did not register")
        self.count += 1
        n = self.count
        self.events.append(f"start {n}")
        try:
            yield type("W", (), {"instance_id": f"w-{n}"})()
        finally:
            self.events.append(f"stop {n}")


class _Config:
    """flag を持たせるだけの config の代役。"""


class TestSessionQgis:
    def test_restart_closes_the_old_qgis_before_starting_a_new_one(self) -> None:
        spawn = _FakeSpawn()
        session = SessionQgis(spawn)
        session.start()
        assert session.worker.instance_id == "w-1"
        session.restart()
        assert spawn.events == ["start 1", "stop 1", "start 2"]
        assert session.worker.instance_id == "w-2"
        session.stop()
        assert spawn.events[-1] == "stop 2"
        assert session.worker is None

    def test_stop_and_start_are_idempotent(self) -> None:
        spawn = _FakeSpawn()
        session = SessionQgis(spawn)
        session.stop()  # 起こす前でも何もしない
        session.start()
        session.start()  # 2 本目は起こさない
        session.stop()
        session.stop()
        assert spawn.events == ["start 1", "stop 1"]


class TestRestartSessionQgis:
    def test_restarts_and_clears_the_flag(self) -> None:
        spawn = _FakeSpawn()
        session = SessionQgis(spawn)
        session.start()
        config = _Config()
        setattr(config, _CONFIG_ATTR_RESTART_SESSION_QGIS, True)
        _restart_session_qgis(config, session, "t.py::test_b")  # type: ignore[arg-type]
        assert session.worker.instance_id == "w-2"
        assert getattr(config, _CONFIG_ATTR_RESTART_SESSION_QGIS) is False

    def test_failed_restart_keeps_the_flag_for_the_next_test(self) -> None:
        spawn = _FakeSpawn()
        session = SessionQgis(spawn)
        session.start()
        config = _Config()
        setattr(config, _CONFIG_ATTR_RESTART_SESSION_QGIS, True)
        spawn.fail_next = True
        with pytest.raises(TimeoutError):
            _restart_session_qgis(config, session, "t.py::test_b")  # type: ignore[arg-type]
        assert getattr(config, _CONFIG_ATTR_RESTART_SESSION_QGIS) is True
        assert session.worker is None  # 古い QGIS は閉じた
        # 次のテストで起こし直せる
        _restart_session_qgis(config, session, "t.py::test_c")  # type: ignore[arg-type]
        assert session.worker.instance_id == "w-2"

    @pytest.mark.parametrize("session", [None, object()], ids=["dev-mode", "overridden"])
    def test_unrestartable_session_warns_and_drops_the_flag(
        self, session: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _Config()
        setattr(config, _CONFIG_ATTR_RESTART_SESSION_QGIS, True)
        with caplog.at_level(logging.WARNING, logger="pytest_qgis_puppeteer.plugin"):
            _restart_session_qgis(config, session, "t.py::test_b")  # type: ignore[arg-type]
        assert getattr(config, _CONFIG_ATTR_RESTART_SESSION_QGIS) is False
        assert "has no effect" in caplog.text


def test_failure_sets_the_restart_flag_unless_the_test_ran_on_a_fresh_qgis(
    pytester: pytest.Pytester,
) -> None:
    """失敗で flag が立つ。fresh_qgis のテストは session の QGIS を使わないので立てない。"""
    pytester.makeini(
        textwrap.dedent(
            """
            [pytest]
            qgis_auto_fresh_after_failure = session
            qgis_fail_on_uncaught_exception = false
            """
        )
    )
    pytester.makepyfile(
        textwrap.dedent(
            f"""
            import pytest

            FLAG = {_CONFIG_ATTR_RESTART_SESSION_QGIS!r}

            @pytest.mark.fresh_qgis
            def test_1_fresh_fails():
                assert False

            def test_2_not_flagged(request):
                assert getattr(request.config, FLAG, False) is False

            def test_3_fails():
                assert False

            def test_4_flagged(request):
                assert getattr(request.config, FLAG, False) is True
            """
        )
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, failed=2)


# ============================================================
# #19: pytest_qgis_ready フックと qgis_restart_before 印
# ============================================================


class TestReadyHookAndRestartMarker:
    def test_start_count_tells_a_restart_from_the_first_start(self) -> None:
        spawn = _FakeSpawn()
        session = SessionQgis(spawn)
        session.start()
        assert session.start_count == 1
        session.restart()
        assert session.start_count == 2

    def test_marker_asks_for_a_restart_unless_the_test_is_fresh(self) -> None:
        from pytest_qgis_puppeteer.plugin import _wants_session_restart

        class _Item:
            def __init__(self, marked: bool) -> None:
                self.marked = marked

            def get_closest_marker(self, name: str) -> Any:
                return object() if self.marked and name == "qgis_restart_before" else None

        assert _wants_session_restart(None, _Item(True), fresh=False)  # type: ignore[arg-type]
        assert not _wants_session_restart(None, _Item(True), fresh=True)  # type: ignore[arg-type]
        assert not _wants_session_restart(None, _Item(False), fresh=False)  # type: ignore[arg-type]


class _HookConfig:
    """``config.hook.pytest_qgis_ready`` と ini を持つ config の代役。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail
        outer = self

        class _Hook:
            def pytest_qgis_ready(self, **kwargs: Any) -> None:
                outer.calls.append(kwargs)
                kwargs["client"].events.append("ready")
                if outer.fail:
                    raise RuntimeError("login screen did not go away")

        self.hook = _Hook()

    def getini(self, name: str) -> Any:
        return [] if name == "qgis_message_allowlist" else ""


class _Client:
    def __init__(self) -> None:
        self.events: list[str] = []

    def unrecorded(self) -> Any:
        return contextlib.nullcontext()

    def get_recent_messages(self, *, instance: str | None = None) -> list[dict[str, Any]]:
        self.events.append("read")
        return [{"level": "info", "message": "logged in"}]


def _capture(config: _HookConfig, client: _Client, kind: str = "restart") -> Any:
    from pytest_qgis_puppeteer.plugin import _capture_startup_messages

    return _capture_startup_messages(
        config,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        key="w-2",
        description="session QGIS (w-2)",
        instance=None,
        ready_instance_id="w-2",
        kind=kind,
    )


def test_ready_hook_runs_once_before_the_startup_messages_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
    config, client = _HookConfig(), _Client()
    assert _capture(config, client) == [{"level": "info", "message": "logged in"}]
    assert client.events == ["ready", "read"]
    assert config.calls == [
        {"config": config, "client": client, "instance_id": "w-2", "kind": "restart"}
    ]
    _capture(config, client)  # 同じ QGIS では 2 回目は呼ばない
    assert len(config.calls) == 1


def test_a_failing_ready_hook_errors_the_test_and_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QPUPPETEER_E2E_USE_RUNNING_QGIS", raising=False)
    config, client = _HookConfig(fail=True), _Client()
    with pytest.raises(RuntimeError, match="login screen"):
        _capture(config, client)
    assert client.events == ["ready"]  # フックは呼んだが、まだ取っていない
    config.fail = False
    _capture(config, client)  # 次のテストでもう一度呼ばれ、今度は取る
    assert len(config.calls) == 2
    assert client.events == ["ready", "ready", "read"]


def test_conftest_can_implement_the_hook_and_use_the_marker(pytester: pytest.Pytester) -> None:
    """フックと印が登録されている（未知のフック・未知の印として弾かれない）。"""
    pytester.makeconftest(
        """
        def pytest_qgis_ready(config, client, instance_id, kind):
            pass
        """
    )
    pytester.makeini("[pytest]\nqgis_fail_on_uncaught_exception = false\n")
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.qgis_restart_before
        def test_ok():
            pass
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider", "--strict-markers")
    result.assert_outcomes(passed=1)


# ============================================================
# #29: pytest_qgis_before_spawn フック
# ============================================================


class _SpawnHookConfig:
    """``config.hook.pytest_qgis_before_spawn`` を持つ config の代役。

    pluggy と同じく、各 conftest の戻り値の list を返す。
    """

    def __init__(self, *results: Any) -> None:
        self.calls: list[str] = []
        self.results = results
        outer = self

        class _Hook:
            def pytest_qgis_before_spawn(self, *, config: Any, kind: str) -> list[Any]:
                outer.calls.append(kind)
                return list(outer.results)

        self.hook = _Hook()


def _extras(config: Any, spawn_kwargs: dict[str, Any], env: dict[str, str] | None = None) -> Any:
    from pytest_qgis_puppeteer.plugin import _with_spawn_extras

    return _with_spawn_extras(config, "session", spawn_kwargs, env)


class TestBeforeSpawnHook:
    def test_args_go_after_qgis_args_and_env_is_merged(self) -> None:
        config = _SpawnHookConfig(
            {"args": ["--profiles-path", "/tmp/p", "--profile", "e2e"]},
            None,
            {"env": {"QGIS_CUSTOM_CONFIG_PATH": "/tmp/c"}},
        )
        base = {"qgis_bin": "qgis.exe", "args": ["--nologo"]}
        kwargs, env = _extras(config, base, {"LANG": "ja"})
        assert kwargs == {
            "qgis_bin": "qgis.exe",
            "args": ["--nologo", "--profiles-path", "/tmp/p", "--profile", "e2e"],
        }
        assert env == {"LANG": "ja", "QGIS_CUSTOM_CONFIG_PATH": "/tmp/c"}
        assert base == {"qgis_bin": "qgis.exe", "args": ["--nologo"]}  # 元は変えない
        assert config.calls == ["session"]

    def test_an_explicit_env_wins_over_the_hook(self) -> None:
        """環境や fresh_qgis の印で決めた変数は、フックより勝つ（明示した設定を優先）。"""
        config = _SpawnHookConfig({"env": {"QGIS_CUSTOM_CONFIG_PATH": "/hook", "B": "2"}})
        _, env = _extras(config, {"qgis_bin": "q"}, {"QGIS_CUSTOM_CONFIG_PATH": "/marker"})
        assert env == {"QGIS_CUSTOM_CONFIG_PATH": "/marker", "B": "2"}

    def test_args_go_after_a_whole_command(self) -> None:
        config = _SpawnHookConfig({"args": ["--profile", "e2e"]})
        kwargs, env = _extras(config, {"command": ["launch.bat", "--nologo"]})
        assert kwargs == {"command": ["launch.bat", "--nologo", "--profile", "e2e"]}
        assert env is None

    def test_nothing_returned_changes_nothing(self) -> None:
        kwargs, env = _extras(_SpawnHookConfig(None), {"qgis_bin": "q", "args": []})
        assert kwargs == {"qgis_bin": "q", "args": []}
        assert env is None

    @pytest.mark.parametrize(
        "bad",
        [
            ["--profile"],
            {"argv": ["x"]},
            {"args": "--profile e2e"},
            {"args": [1]},
            {"env": {"A": 1}},
        ],
    )
    def test_a_wrong_return_value_is_a_usage_error(self, bad: Any) -> None:
        with pytest.raises(pytest.UsageError, match="pytest_qgis_before_spawn"):
            _extras(_SpawnHookConfig(bad), {"qgis_bin": "q"})

    def test_two_hooks_disagreeing_on_an_env_var_is_a_usage_error(self) -> None:
        config = _SpawnHookConfig({"env": {"A": "1"}}, {"env": {"A": "2"}})
        with pytest.raises(pytest.UsageError, match="different values"):
            _extras(config, {"qgis_bin": "q"})
        same = _SpawnHookConfig({"env": {"A": "1"}}, {"env": {"A": "1"}})
        assert _extras(same, {"qgis_bin": "q"})[1] == {"A": "1"}

    def test_session_spawn_asks_session_then_restart(self) -> None:
        """起こすたびに聞き、2 回目からは kind="restart"。"""
        from pytest_qgis_puppeteer.plugin import _with_spawn_extras

        config = _SpawnHookConfig({"args": ["--x"]})
        seen: list[tuple[str, Any]] = []
        spawn = _FakeSpawn()
        session: SessionQgis

        def spawn_session() -> Any:
            kind = "restart" if session.start_count > 0 else "session"
            kwargs, _ = _with_spawn_extras(config, kind, {"qgis_bin": "q", "args": []}, None)  # type: ignore[arg-type]
            seen.append((kind, kwargs["args"]))
            return spawn()

        session = SessionQgis(spawn_session)
        session.start()
        session.restart()
        assert seen == [("session", ["--x"]), ("restart", ["--x"])]

    def test_a_failing_hook_starts_no_qgis(self) -> None:
        class _Failing:
            class hook:  # noqa: N801 - config.hook の代役
                @staticmethod
                def pytest_qgis_before_spawn(**_: Any) -> list[Any]:
                    raise RuntimeError("could not make the profile")

        spawn = _FakeSpawn()
        from pytest_qgis_puppeteer.plugin import _with_spawn_extras

        def spawn_session() -> Any:
            _with_spawn_extras(_Failing(), "session", {"qgis_bin": "q"}, None)  # type: ignore[arg-type]
            return spawn()

        session = SessionQgis(spawn_session)
        with pytest.raises(RuntimeError, match="profile"):
            session.start()
        assert spawn.events == []


def test_conftest_can_implement_the_before_spawn_hook(pytester: pytest.Pytester) -> None:
    """フックが登録されている（未知のフックとして弾かれない）。"""
    pytester.makeconftest(
        """
        def pytest_qgis_before_spawn(config, kind):
            return {"args": ["--profile", "e2e"]}
        """
    )
    pytester.makeini("[pytest]\nqgis_fail_on_uncaught_exception = false\n")
    pytester.makepyfile("def test_ok():\n    pass\n")
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
