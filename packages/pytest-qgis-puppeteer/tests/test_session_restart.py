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
