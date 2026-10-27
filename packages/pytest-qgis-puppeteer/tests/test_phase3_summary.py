"""ADR-0003 Phase 3 (summary.json + env ごとの JUnit XML のまとめ + --maxfail 累計) の単体テスト。

実 pytest invocation を使わず、internal helper に対するユニットテスト中心。
``_run_envs_and_aggregate`` のフルフロー検証は integration スコープ（`pytest.main`
を入れ子で動かす）になるため、本ファイルでは pure 関数 + 部分結合をテストする。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from pytest_qgis_puppeteer._environments import (
    EnvironmentSpec,
)
from pytest_qgis_puppeteer.plugin import (
    _CONFIG_ATTR_ACTIVE_ENV,
    _CONFIG_ATTR_ENV_STATS,
    _SUMMARY_SCHEMA_VERSION,
    _child_junit_path,
    _empty_env_stats,
    _ensure_env_session_start,
    _env_output_dir,
    _EnvJunitEntry,
    _junit_target,
    _merge_env_junit_reports,
    _not_run_reason,
    _outputs_root,
    _read_child_stats,
    _write_env_stats,
    _write_summary_json,
)


class _FakeOption:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _FakeConfig:
    """``pytest_qgis_puppeteer.plugin`` の Phase 3 ヘルパが必要とする最小 Config。

    - getini("qgis_diag_dir"): outputs/diagnostics の親を ``outputs/`` にする
    - option: ``xmlpath`` / ``maxfail`` 等の argparse 結果
    - 任意の attribute を持てる（active_env / env_stats を直接 setattr する）
    """

    def __init__(self, *, diag_dir: str | None = None, option: _FakeOption | None = None) -> None:
        self._ini = {"qgis_diag_dir": diag_dir or "outputs/diagnostics"}
        self.option = option or _FakeOption()
        self.pluginmanager = _FakePluginManager()

    def getini(self, key: str) -> str:
        return self._ini.get(key, "")


class _FakePluginManager:
    def __init__(self) -> None:
        self._terminalreporter: Any | None = None

    def get_plugin(self, name: str) -> Any | None:
        if name == "terminalreporter":
            return self._terminalreporter
        return None


class _FakeTerminalReporter:
    def __init__(self, stats: dict[str, list[Any]]) -> None:
        self.stats = stats


class _FakeReport:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


def _spec(name: str) -> EnvironmentSpec:
    return EnvironmentSpec(name=name)


# ============================================================
# _outputs_root / _env_output_dir
# ============================================================


class TestOutputsPaths:
    def test_outputs_root_from_default_diag_dir(self, tmp_path: Path) -> None:
        config = _FakeConfig(diag_dir=str(tmp_path / "outputs" / "diagnostics"))
        assert _outputs_root(config) == tmp_path / "outputs"  # type: ignore[arg-type]

    def test_env_output_dir(self, tmp_path: Path) -> None:
        config = _FakeConfig(diag_dir=str(tmp_path / "outputs" / "diagnostics"))
        assert _env_output_dir(config, "smoke") == tmp_path / "outputs" / "smoke"  # type: ignore[arg-type]


# ============================================================
# _empty_env_stats
# ============================================================


class TestEmptyEnvStats:
    def test_skipped_due_to_maxfail_shape(self) -> None:
        stats = _empty_env_stats("auth", exitcode=0, reason="not_run_due_to_cumulative_maxfail")
        assert stats["env_name"] == "auth"
        assert stats["exitcode"] == 0
        assert stats["counts"]["passed"] == 0
        assert stats["counts"]["failed"] == 0
        assert stats["failed_tests"] == []
        assert stats["skipped_reason"] == "not_run_due_to_cumulative_maxfail"
        assert stats["schema_version"] == _SUMMARY_SCHEMA_VERSION


# ============================================================
# _write_env_stats / _read_child_stats
# ============================================================


class TestWriteEnvStats:
    def test_no_active_env_writes_nothing(self, tmp_path: Path) -> None:
        config = _FakeConfig(diag_dir=str(tmp_path / "outputs" / "diagnostics"))
        # active_env 未設定（getattr default = None）
        _write_env_stats(config, exitstatus=0)  # type: ignore[arg-type]
        assert not (tmp_path / "outputs").exists()

    def test_writes_json_for_active_env(self, tmp_path: Path) -> None:
        config = _FakeConfig(diag_dir=str(tmp_path / "outputs" / "diagnostics"))
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, _spec("smoke"))
        # session start を初期化（duration_s 計算に使う）
        _ensure_env_session_start(config)  # type: ignore[arg-type]
        # terminalreporter を inject して count を作る
        config.pluginmanager._terminalreporter = _FakeTerminalReporter(  # type: ignore[attr-defined]
            stats={
                "passed": [_FakeReport("a")],
                "failed": [_FakeReport("tests/test_x.py::test_y")],
                "error": [_FakeReport("tests/test_z.py::test_w")],
            }
        )
        _write_env_stats(config, exitstatus=1)  # type: ignore[arg-type]
        out_path = tmp_path / "outputs" / "smoke" / "_env_stats.json"
        assert out_path.is_file()
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["env_name"] == "smoke"
        assert payload["exitcode"] == 1
        assert payload["counts"]["passed"] == 1
        assert payload["counts"]["failed"] == 1
        assert payload["counts"]["errored"] == 1
        # failed_tests は call-failed と setup/teardown errored の両方
        assert "tests/test_x.py::test_y" in payload["failed_tests"]
        assert "tests/test_z.py::test_w" in payload["failed_tests"]
        assert payload["schema_version"] == _SUMMARY_SCHEMA_VERSION

    def test_read_child_stats_returns_none_if_missing(self, tmp_path: Path) -> None:
        config = _FakeConfig(diag_dir=str(tmp_path / "outputs" / "diagnostics"))
        assert _read_child_stats(config, "smoke") is None  # type: ignore[arg-type]

    def test_read_child_stats_round_trip(self, tmp_path: Path) -> None:
        config = _FakeConfig(diag_dir=str(tmp_path / "outputs" / "diagnostics"))
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, _spec("smoke"))
        _ensure_env_session_start(config)  # type: ignore[arg-type]
        config.pluginmanager._terminalreporter = _FakeTerminalReporter(  # type: ignore[attr-defined]
            stats={"passed": [_FakeReport("a"), _FakeReport("b")]},
        )
        _write_env_stats(config, exitstatus=0)  # type: ignore[arg-type]
        stats = _read_child_stats(config, "smoke")  # type: ignore[arg-type]
        assert stats is not None
        assert stats["env_name"] == "smoke"
        assert stats["counts"]["passed"] == 2


# ============================================================
# _write_summary_json
# ============================================================


class TestWriteSummaryJson:
    def test_aggregates_totals_across_envs(self, tmp_path: Path) -> None:
        config = _FakeConfig(diag_dir=str(tmp_path / "outputs" / "diagnostics"))
        env_results = [
            {
                "env_name": "smoke",
                "exitcode": 0,
                "counts": {
                    "passed": 5,
                    "failed": 0,
                    "skipped": 1,
                    "errored": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                },
                "failed_tests": [],
            },
            {
                "env_name": "auth",
                "exitcode": 1,
                "counts": {
                    "passed": 3,
                    "failed": 2,
                    "skipped": 0,
                    "errored": 1,
                    "xfailed": 0,
                    "xpassed": 0,
                },
                "failed_tests": ["tests/test_login.py::test_x"],
            },
        ]
        _write_summary_json(
            config,  # type: ignore[arg-type]
            env_results=env_results,
            aggregated_exit=1,
            started_at="2026-05-01T00:00:00+00:00",
        )
        out = tmp_path / "outputs" / "summary.json"
        assert out.is_file()
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == _SUMMARY_SCHEMA_VERSION
        assert payload["exit_code"] == 1
        assert len(payload["envs"]) == 2
        assert payload["totals"]["passed"] == 8
        assert payload["totals"]["failed"] == 2
        assert payload["totals"]["skipped"] == 1
        assert payload["totals"]["errored"] == 1
        assert payload["started_at"] == "2026-05-01T00:00:00+00:00"


# ============================================================
# _merge_env_junit_reports
# ============================================================


def _write_child_junit(path: Path, classnames: list[str], *, wrapped: bool = True) -> None:
    """pytest の junitxml と同じ形（``<testsuites><testsuite>``）の子のレポートを書く。"""
    suite = ET.Element(
        "testsuite",
        name="pytest",
        errors="0",
        failures="1",
        skipped="0",
        tests=str(len(classnames)),
        time="1.500",
    )
    for i, cls in enumerate(classnames):
        testcase = ET.SubElement(suite, "testcase", classname=cls, name=f"test_{i}")
        if i == 0:
            ET.SubElement(testcase, "failure", message="boom")
    root = suite
    if wrapped:
        root = ET.Element("testsuites", name="pytest tests")
        root.append(suite)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


class TestMergeEnvJunitReports:
    def test_one_testsuite_per_env_with_prefixed_classnames(self, tmp_path: Path) -> None:
        _write_child_junit(tmp_path / "smoke.xml", ["tests.test_x", "tests.test_y"])
        _write_child_junit(tmp_path / "auth.xml", ["tests.test_login"])
        root = _merge_env_junit_reports(
            [
                _EnvJunitEntry("smoke", tmp_path / "smoke.xml", exitcode=1),
                _EnvJunitEntry("auth", tmp_path / "auth.xml", exitcode=1),
            ]
        ).getroot()

        assert root.tag == "testsuites"
        assert [s.get("name") for s in root] == ["smoke", "auth"]
        assert [tc.get("classname") for tc in root.iter("testcase")] == [
            "smoke::tests.test_x",
            "smoke::tests.test_y",
            "auth::tests.test_login",
        ]
        # 子の結果（failure）はそのまま残る
        assert len(list(root.iter("failure"))) == 2

    def test_totals_on_testsuites(self, tmp_path: Path) -> None:
        _write_child_junit(tmp_path / "smoke.xml", ["a", "b"])
        _write_child_junit(tmp_path / "auth.xml", ["c"])
        root = _merge_env_junit_reports(
            [
                _EnvJunitEntry("smoke", tmp_path / "smoke.xml", exitcode=1),
                _EnvJunitEntry("auth", tmp_path / "auth.xml", exitcode=1),
            ]
        ).getroot()

        assert root.get("tests") == "3"
        assert root.get("failures") == "2"
        assert root.get("errors") == "0"
        assert root.get("skipped") == "0"
        assert root.get("time") == "3.000"

    def test_child_with_bare_testsuite_root(self, tmp_path: Path) -> None:
        _write_child_junit(tmp_path / "smoke.xml", ["tests.test_x"], wrapped=False)
        root = _merge_env_junit_reports(
            [_EnvJunitEntry("smoke", tmp_path / "smoke.xml", exitcode=1)]
        ).getroot()

        assert [s.get("name") for s in root] == ["smoke"]
        assert [tc.get("classname") for tc in root.iter("testcase")] == ["smoke::tests.test_x"]

    def test_empty_classname_becomes_env_name(self, tmp_path: Path) -> None:
        _write_child_junit(tmp_path / "smoke.xml", [""])
        root = _merge_env_junit_reports(
            [_EnvJunitEntry("smoke", tmp_path / "smoke.xml", exitcode=1)]
        ).getroot()

        assert [tc.get("classname") for tc in root.iter("testcase")] == ["smoke"]

    @pytest.mark.parametrize("content", [None, "<testsuites><testsuite"])
    def test_missing_or_broken_report_becomes_error(
        self, tmp_path: Path, content: str | None
    ) -> None:
        path = tmp_path / "smoke.xml"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        root = _merge_env_junit_reports([_EnvJunitEntry("smoke", path, exitcode=3)]).getroot()

        (suite,) = list(root)
        assert suite.get("name") == "smoke"
        assert suite.get("errors") == "1"
        (error,) = list(suite.iter("error"))
        assert "'smoke'" in error.get("message", "")
        assert "code 3" in error.get("message", "")
        assert root.get("errors") == "1"
        assert root.get("tests") == "1"

    def test_not_run_env_becomes_skipped(self, tmp_path: Path) -> None:
        # 回さなかった env は、古いファイルが残っていても読まない
        _write_child_junit(tmp_path / "auth.xml", ["stale"])
        root = _merge_env_junit_reports(
            [
                _EnvJunitEntry(
                    "auth",
                    tmp_path / "auth.xml",
                    exitcode=0,
                    not_run_reason="not_run_due_to_cumulative_maxfail",
                )
            ]
        ).getroot()

        (suite,) = list(root)
        assert suite.get("name") == "auth"
        assert suite.get("skipped") == "1"
        (skipped,) = list(suite.iter("skipped"))
        assert "not_run_due_to_cumulative_maxfail" in skipped.get("message", "")
        assert [tc.get("classname") for tc in root.iter("testcase")] == ["auth"]
        assert root.get("skipped") == "1"


class TestNotRunReason:
    def test_not_run_reason_is_returned(self) -> None:
        stats = _empty_env_stats("auth", exitcode=0, reason="not_run_due_to_cumulative_maxfail")
        assert _not_run_reason(stats) == "not_run_due_to_cumulative_maxfail"

    @pytest.mark.parametrize("reason", [None, "env_stats_missing"])
    def test_other_reasons_are_none(self, reason: str | None) -> None:
        stats: dict[str, Any] = {"env_name": "smoke", "exitcode": 0}
        if reason is not None:
            stats["skipped_reason"] = reason
        assert _not_run_reason(stats) is None


class TestJunitPaths:
    def test_junit_target_none_without_xmlpath(self) -> None:
        assert _junit_target(_FakeConfig()) is None  # type: ignore[arg-type]

    def test_junit_target_is_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        config = _FakeConfig(option=_FakeOption(xmlpath="reports/junit.xml"))
        assert _junit_target(config) == tmp_path / "reports" / "junit.xml"  # type: ignore[arg-type]

    def test_child_junit_path_is_under_env_output_dir(self, tmp_path: Path) -> None:
        config = _FakeConfig(diag_dir=str(tmp_path / "outputs" / "diagnostics"))
        path = _child_junit_path(config, "smoke")  # type: ignore[arg-type]
        assert path == tmp_path / "outputs" / "smoke" / "junit.xml"


# Smoke test for _CONFIG_ATTR_ENV_STATS being set after _ensure_env_session_start.
def test_ensure_env_session_start_idempotent(tmp_path: Path) -> None:
    config = _FakeConfig()
    a = _ensure_env_session_start(config)  # type: ignore[arg-type]
    b = _ensure_env_session_start(config)  # type: ignore[arg-type]
    assert a is b  # 同じ dict が返る
    assert "started_at" in a and "started_monotonic" in a
    # config attribute としても保存されている
    assert getattr(config, _CONFIG_ATTR_ENV_STATS) is a


def _unused() -> Any:
    # ruff F401 防止のため pytest を使う 1 行（fixture を使う書き換えを後で入れる場合の保険）
    return pytest


# ============================================================
# Phase 4 smoke: --list-envs (uses pytest.exit, hard to test in-process)
# ============================================================


class TestPrintEnvsAndExit:
    """``_print_envs_and_exit`` は pytest.exit を呼ぶので catch する形でテスト。"""

    def test_prints_name_and_description(self, capsys: pytest.CaptureFixture[str]) -> None:
        from pytest_qgis_puppeteer._environments import (
            DefaultEnvironmentConfig,
            EnvironmentsConfig,
        )
        from pytest_qgis_puppeteer.plugin import _print_envs_and_exit

        config = EnvironmentsConfig(
            environments=(
                EnvironmentSpec(name="smoke", description="quick run"),
                EnvironmentSpec(name="auth", description=""),
            ),
            default=DefaultEnvironmentConfig(strategy="named", default_name="smoke"),
        )
        with pytest.raises(pytest.exit.Exception):  # type: ignore[attr-defined]
            _print_envs_and_exit(config)
        captured = capsys.readouterr()
        # description ありは tab 区切り、無しは name のみ
        assert "smoke\tquick run" in captured.out
        assert "\nauth\n" in captured.out or captured.out.endswith("auth\n")
