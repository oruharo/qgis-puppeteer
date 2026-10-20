"""QgsMessageLog のメッセージでテストを落とす判定（#5）のテスト。

判定の部品（`_message_check`）と、それを使う pytest hook（call 直前の消去・
makereport での格上げ・setup でのマーカー検証）を stub の item / report で回す。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from pytest_qgis_puppeteer import _message_check as mc
from pytest_qgis_puppeteer import plugin

# ============================================================
# 部品
# ============================================================


class TestParseFailLevel:
    @pytest.mark.parametrize("raw", [None, "", "none", "off", "false", " NONE "])
    def test_disabled(self, raw: Any) -> None:
        assert mc.parse_fail_level(raw) is None

    @pytest.mark.parametrize("raw", ["info", "warning", "Critical"])
    def test_levels(self, raw: str) -> None:
        assert mc.parse_fail_level(raw) == raw.lower()

    def test_typo_is_an_error_not_silently_off(self) -> None:
        with pytest.raises(ValueError, match="qgis_fail_on_message_level"):
            mc.parse_fail_level("error")


class TestAllowRules:
    def test_ini_lines(self) -> None:
        rules = mc.rules_from_ini(
            ["", "# comment", "Could not load", "OGR::^Warning 1: .*", "colour #fff"]
        )
        assert [(r.tag, r.pattern.pattern if r.pattern else None) for r in rules] == [
            (None, "Could not load"),
            ("OGR", "^Warning 1: .*"),
            (None, "colour #fff"),  # 途中の # は正規表現の一部
        ]

    def test_bad_regex_in_ini(self) -> None:
        with pytest.raises(ValueError, match="bad regex"):
            mc.rules_from_ini(["(unclosed"])

    def test_a_regex_that_looks_like_a_tag_prefix_is_an_error(self) -> None:
        """`^QgsVectorLayer::.*` をタグ `^QgsVectorLayer` + `.*` と読むと全部許してしまう。"""
        with pytest.raises(ValueError, match="reads as tag"):
            mc.parse_allow_line("^QgsVectorLayer::.*")

    def test_escaped_colons_stay_in_the_regex(self) -> None:
        rule = mc.parse_allow_line(r"QgsVectorLayer\:\:load failed")
        assert rule is not None and rule.tag is None
        assert rule.matches({"tag": "x", "message": "QgsVectorLayer::load failed"})

    def test_tags_may_contain_spaces(self) -> None:
        rule = mc.parse_allow_line("Python warning::deprecated")
        assert rule is not None and rule.tag == "Python warning"

    def test_rule_matching(self) -> None:
        msg = {"tag": "OGR", "message": "Warning 1: x", "source": "log"}
        assert mc.AllowRule(pattern=re.compile("Warning"), tag="OGR").matches(msg)
        assert not mc.AllowRule(pattern=re.compile("Warning"), tag="WMS").matches(msg)
        assert mc.AllowRule(source="log").matches(msg)
        assert not mc.AllowRule(source="message_bar").matches(msg)

    def test_unexpected_keeps_only_unmatched(self) -> None:
        msgs = [
            {"tag": "A", "message": "known noise", "source": "log"},
            {"tag": "A", "message": "real failure", "source": "log"},
        ]
        rules = [mc.AllowRule(pattern=re.compile("noise"))]
        assert mc.unexpected(msgs, rules) == [msgs[1]]

    def test_format_mentions_level_and_how_to_allow(self) -> None:
        text = mc.format_messages(
            [
                {
                    "level": "critical",
                    "tag": "P",
                    "message": "boom",
                    "ts": "t",
                    "source": "message_bar",
                }
            ],
            "critical",
        )
        assert "1 message(s) at level critical" in text
        assert "[critical] P (message bar) @ t: boom" in text
        assert "qgis_allow_message" in text


# ============================================================
# hook（stub）
# ============================================================


class _Marker:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


class _Config:
    def __init__(self, ini: dict[str, Any]) -> None:
        self._ini = {"qgis_fail_on_uncaught_exception": "false", **ini}

    def getini(self, name: str) -> Any:
        return self._ini.get(name, [] if name == "qgis_message_allowlist" else "")


class _Client:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages = messages or []
        self.asked_levels: list[str | None] = []
        self.cleared = 0

    def get_recent_messages(self, *, min_level: str | None = None) -> list[dict[str, Any]]:
        self.asked_levels.append(min_level)
        return [m for m in self.messages if min_level is None or m["level"] == min_level]

    def clear_recent_messages(self) -> None:
        self.cleared += 1


class _Item:
    def __init__(self, ini: dict[str, Any], client: _Client, markers: list[_Marker] = ()) -> None:
        self.nodeid = "tests/test_x.py::test_y"
        self.config = _Config(ini)
        self.fixturenames = ["automation_client"]
        self.funcargs = {"automation_client": client}
        self._markers = list(markers)

    def iter_markers(self, name: str) -> Iterator[_Marker]:
        return iter(self._markers if name == "qgis_allow_message" else [])

    def get_closest_marker(self, name: str) -> Any:
        return None


class _Report:
    def __init__(self, outcome: str = "passed") -> None:
        self.when = "call"
        self.outcome = outcome
        self.longrepr: Any = None
        self.sections: list[tuple[str, str]] = []

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"


class _Outcome:
    def __init__(self, report: _Report) -> None:
        self._report = report

    def get_result(self) -> _Report:
        return self._report


@pytest.fixture
def dumped(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(plugin, "_dump_diagnostic_bundle", lambda item, **kw: calls.append(item))
    monkeypatch.delenv(plugin.ENV_DEV_MODE, raising=False)
    return calls


def _makereport(item: _Item, report: _Report) -> _Report:
    gen = plugin.pytest_runtest_makereport(item, None)  # type: ignore[arg-type]
    next(gen)
    with pytest.raises(StopIteration):
        gen.send(_Outcome(report))
    return report


CRITICAL = {"seq": 1, "level": "critical", "tag": "MyPlugin", "message": "boom", "source": "log"}
LEVEL = {"qgis_fail_on_message_level": "critical"}


class TestMakereport:
    def test_critical_message_turns_a_pass_into_a_failure(self, dumped: list[Any]) -> None:
        client = _Client([CRITICAL])
        item = _Item(LEVEL, client)
        report = _makereport(item, _Report())
        assert report.failed
        assert "boom" in report.longrepr
        assert client.asked_levels == ["critical"]
        assert dumped == [item]

    def test_off_by_default(self, dumped: list[Any]) -> None:
        client = _Client([CRITICAL])
        report = _makereport(_Item({}, client), _Report())
        assert report.passed
        assert client.asked_levels == []

    def test_ini_allowlist(self, dumped: list[Any]) -> None:
        item = _Item({**LEVEL, "qgis_message_allowlist": ["MyPlugin::^boom$"]}, _Client([CRITICAL]))
        assert _makereport(item, _Report()).passed

    def test_marker_allowlist(self, dumped: list[Any]) -> None:
        item = _Item(LEVEL, _Client([CRITICAL]), [_Marker("bo+m", tag="MyPlugin")])
        assert _makereport(item, _Report()).passed

    def test_marker_for_another_tag_does_not_hide_it(self, dumped: list[Any]) -> None:
        item = _Item(LEVEL, _Client([CRITICAL]), [_Marker("boom", tag="Other")])
        assert _makereport(item, _Report()).failed

    def test_already_failed_test_gets_a_section(self, dumped: list[Any]) -> None:
        report = _Report("failed")
        report.longrepr = "AssertionError"
        _makereport(_Item(LEVEL, _Client([CRITICAL])), report)
        assert report.longrepr == "AssertionError"
        assert report.sections[0][0] == "Worker log messages"

    def test_fetch_failure_does_not_fail_the_test_but_says_so(self, dumped: list[Any]) -> None:
        """古い Worker 等で照会できなくても落とさない。ただし黙って緑にもしない。"""
        client = _Client()

        def broken(**_kw: Any) -> list[dict[str, Any]]:
            raise ConnectionError("hub gone")

        client.get_recent_messages = broken  # type: ignore[method-assign]
        report = _makereport(_Item(LEVEL, client), _Report())
        assert report.passed
        [(title, text)] = report.sections
        assert title == "Worker log messages"
        assert "could not run" in text and "hub gone" in text

    def test_the_configured_level_is_what_the_worker_is_asked_for(self, dumped: list[Any]) -> None:
        """重さの順位（success < warning 等）は Worker 側。hook は指定どおりに渡す。"""
        client = _Client()
        _makereport(_Item({"qgis_fail_on_message_level": "info"}, client), _Report())
        assert client.asked_levels == ["info"]

    def test_exceptions_and_messages_together(self, dumped: list[Any]) -> None:
        """例外で fail に格上げされた後、メッセージはセクションに足される。"""

        class _Both(_Client):
            def get_recent_exceptions(self) -> list[dict[str, Any]]:
                return [{"type": "ValueError", "message": "slot blew up"}]

        item = _Item({**LEVEL, "qgis_fail_on_uncaught_exception": "true"}, _Both([CRITICAL]))
        report = _makereport(item, _Report())
        assert report.failed
        assert "slot blew up" in report.longrepr
        assert [title for title, _ in report.sections] == ["Worker log messages"]

    def test_dev_mode_skips_the_check(
        self, dumped: list[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(plugin.ENV_DEV_MODE, "1")
        client = _Client([CRITICAL])
        assert _makereport(_Item(LEVEL, client), _Report()).passed
        assert client.asked_levels == []


class TestRuntestCall:
    def _run(self, item: _Item) -> None:
        gen = plugin.pytest_runtest_call(item)  # type: ignore[arg-type]
        next(gen)
        with pytest.raises(StopIteration):
            next(gen)

    def test_clears_messages_before_the_call_when_enabled(self, dumped: list[Any]) -> None:
        client = _Client()
        self._run(_Item(LEVEL, client))
        assert client.cleared == 1

    def test_leaves_them_alone_when_disabled(self, dumped: list[Any]) -> None:
        client = _Client()
        self._run(_Item({}, client))
        assert client.cleared == 0

    def test_leaves_them_alone_in_dev_mode(
        self, dumped: list[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(plugin.ENV_DEV_MODE, "1")
        client = _Client()
        self._run(_Item(LEVEL, client))
        assert client.cleared == 0


class TestSetupValidatesMarkers:
    def test_bad_regex_in_marker_is_a_setup_error(self) -> None:
        item = _Item(LEVEL, _Client(), [_Marker("(unclosed")])
        with pytest.raises(ValueError, match="bad regex"):
            plugin.pytest_runtest_setup(item)  # type: ignore[arg-type]

    def test_markers_are_not_checked_when_the_gate_is_off(self) -> None:
        item = _Item({}, _Client(), [_Marker("(unclosed")])
        plugin.pytest_runtest_setup(item)  # type: ignore[arg-type]

    def test_marker_without_any_condition_is_rejected(self) -> None:
        item = _Item(LEVEL, _Client(), [_Marker()])
        with pytest.raises(ValueError, match="needs a pattern"):
            plugin.pytest_runtest_setup(item)  # type: ignore[arg-type]


# ============================================================
# 起動の間のメッセージ（#15）
# ============================================================

WARNING = {"seq": 2, "level": "warning", "tag": "Startup", "message": "slow", "source": "log"}
INFO = {"seq": 3, "level": "info", "tag": "MyPlugin", "message": "logged in", "source": "log"}
SUCCESS = {"seq": 4, "level": "success", "tag": "MyPlugin", "message": "ok", "source": "log"}


class TestUnexpectedMessages:
    """公開の窓口 ``unexpected_messages``：テストと同じ規則で判定する。"""

    def test_uses_the_ini_level_and_allowlist(self) -> None:
        config = _Config(
            {"qgis_fail_on_message_level": "warning", "qgis_message_allowlist": ["slow"]}
        )
        bad = mc.unexpected_messages([CRITICAL, WARNING, INFO], config)
        assert bad == [CRITICAL]

    def test_min_level_overrides_the_ini(self) -> None:
        config = _Config({})
        assert mc.unexpected_messages(
            [CRITICAL, WARNING, INFO, SUCCESS], config, min_level="info"
        ) == [
            CRITICAL,
            WARNING,
            INFO,
            SUCCESS,
        ]

    def test_success_ranks_with_info_not_above_critical(self) -> None:
        assert mc.at_or_above([SUCCESS, CRITICAL], "warning") == [CRITICAL]

    def test_markers_of_the_item_apply(self) -> None:
        item = _Item(LEVEL, _Client(), [_Marker("boom")])
        assert mc.unexpected_messages([CRITICAL], item.config, item=item) == []

    def test_without_a_level_it_refuses(self) -> None:
        with pytest.raises(ValueError, match="min_level"):
            mc.unexpected_messages([CRITICAL], _Config({}))

    def test_is_public(self) -> None:
        import pytest_qgis_puppeteer

        assert pytest_qgis_puppeteer.unexpected_messages is mc.unexpected_messages


class _StartupClient(_Client):
    def __init__(self, messages: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        super().__init__(messages)
        self.fail = fail
        self.instances: list[str | None] = []
        self.unrecorded_calls = 0

    def get_recent_messages(  # type: ignore[override]
        self, *, min_level: str | None = None, instance: str | None = None
    ) -> list[dict[str, Any]]:
        self.instances.append(instance)
        if self.fail:
            raise RuntimeError("old worker")
        return super().get_recent_messages(min_level=min_level)

    def unrecorded(self) -> Any:
        import contextlib

        self.unrecorded_calls += 1
        return contextlib.nullcontext()


class TestCaptureStartupMessages:
    @pytest.fixture(autouse=True)
    def _not_dev(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(plugin.ENV_DEV_MODE, raising=False)

    def _capture(self, config: _Config, client: _StartupClient, key: str = "w-1") -> Any:
        return plugin._capture_startup_messages(
            config,  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            key=key,
            description=f"session QGIS ({key})",
            instance=None,
        )

    def test_read_once_per_qgis_and_left_out_of_the_timeline(self) -> None:
        config = _Config({})
        client = _StartupClient([INFO])
        assert self._capture(config, client) == [INFO]
        assert self._capture(config, client) == [INFO]
        assert client.instances == [None]  # 2 回目は読まない
        assert client.unrecorded_calls == 1
        self._capture(config, client, key="w-2")  # 起こし直した QGIS は別
        assert client.instances == [None, None]

    def test_problems_are_kept_for_the_summary_when_the_check_is_on(self) -> None:
        config = _Config(
            {"qgis_fail_on_message_level": "warning", "qgis_message_allowlist": ["slow"]}
        )
        self._capture(config, _StartupClient([CRITICAL, WARNING, INFO]))
        [problem] = getattr(config, plugin._CONFIG_ATTR_STARTUP_PROBLEMS)
        assert problem["messages"] == [CRITICAL]
        assert problem["level"] == "warning"

    def test_nothing_is_judged_when_the_check_is_off(self) -> None:
        config = _Config({})
        self._capture(config, _StartupClient([CRITICAL]))
        assert getattr(config, plugin._CONFIG_ATTR_STARTUP_PROBLEMS, None) is None

    def test_a_failed_read_does_not_stop_the_test(self, caplog: pytest.LogCaptureFixture) -> None:
        config = _Config(LEVEL)
        assert self._capture(config, _StartupClient(fail=True)) == []
        assert "Could not read the startup messages" in caplog.text

    def test_dev_mode_has_no_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(plugin.ENV_DEV_MODE, "1")
        client = _StartupClient([CRITICAL])
        assert self._capture(_Config(LEVEL), client) == []
        assert client.instances == []


pytest_plugins = ["pytester"]


def test_startup_problems_fail_the_session_without_failing_a_test(
    pytester: pytest.Pytester,
) -> None:
    """A の形：どのテストも落とさず、最後のまとめに出して終了コードを失敗にする。"""
    pytester.makeconftest(
        f"""
        def pytest_configure(config):
            setattr(config, {plugin._CONFIG_ATTR_STARTUP_PROBLEMS!r}, [
                {{"qgis": "session QGIS (w-1)", "level": "warning",
                  "messages": [{{"level": "critical", "tag": "Proj", "message": "could not open",
                                 "ts": "t", "source": "log"}}]}}
            ])
        """
    )
    pytester.makeini("[pytest]\nqgis_fail_on_uncaught_exception = false\n")
    pytester.makepyfile("def test_ok():\n    pass\n")
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        ["*QGIS startup messages*", "*session QGIS (w-1)*", "*could not open*", "*allowlist ini.*"]
    )
    # 起動の間の分にマーカーは効かないので、マーカーは案内しない
    assert "qgis_allow_message" not in result.stdout.str()


def test_no_startup_problems_keep_the_session_green(pytester: pytest.Pytester) -> None:
    pytester.makeini("[pytest]\nqgis_fail_on_uncaught_exception = false\n")
    pytester.makepyfile("def test_ok():\n    pass\n")
    result = pytester.runpytest("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.OK
    assert "QGIS startup messages" not in result.stdout.str()
