"""pytest-qgis-puppeteer plugin の設定解決ロジックの単体テスト。

`_resolve_qgis_command()` 等は純粋に config / env を参照するだけなので、
fake config / monkeypatch で挙動を検証する。fixture 自体（subprocess を
spawn するもの）は実 QGIS が要るので別経路（integration test）で扱う。
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_qgis_puppeteer.plugin import (
    ENV_QGIS_ARGS,
    ENV_QGIS_BIN,
    ENV_QGIS_COMMAND,
    ENV_QGIS_PYTHON,
    _format_uncaught_exceptions,
    _pending_screenshot_dir,
    _resolve_auto_fresh_after_failure,
    _resolve_diag_capture_screenshots,
    _resolve_fail_on_uncaught_exception,
    _resolve_qgis_args,
    _resolve_qgis_command,
    _resolve_setting,
)


class _FakeConfig:
    """pytest.Config の最低限互換 stub（getoption / getini のみ）。"""

    def __init__(
        self,
        *,
        options: dict[str, Any] | None = None,
        ini: dict[str, Any] | None = None,
    ) -> None:
        self._options = options or {}
        self._ini = ini or {}

    def getoption(self, name: str, *, default: Any = None) -> Any:
        return self._options.get(name, default)

    def getini(self, name: str) -> Any:
        return self._ini.get(name, "")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """親プロセスの env が漏れ込まないよう毎テスト delete。"""
    for key in (
        ENV_QGIS_BIN,
        ENV_QGIS_PYTHON,
        ENV_QGIS_ARGS,
        ENV_QGIS_COMMAND,
    ):
        monkeypatch.delenv(key, raising=False)


# ============================================================
# _resolve_qgis_command
# ============================================================


class TestResolveQgisCommand:
    """`qgis_command` の優先順 (CLI > env > ini) を検証。"""

    def test_returns_empty_when_nothing_set(self) -> None:
        config = _FakeConfig()
        assert _resolve_qgis_command(config) == []  # type: ignore[arg-type]

    def test_cli_string_is_split(self) -> None:
        config = _FakeConfig(options={"qgis_command": "launcher.bat --config abc"})
        assert _resolve_qgis_command(config) == [  # type: ignore[arg-type]
            "launcher.bat",
            "--config",
            "abc",
        ]

    def test_cli_list_passes_through(self) -> None:
        config = _FakeConfig(options={"qgis_command": ["launcher.bat", "--config", "abc"]})
        assert _resolve_qgis_command(config) == [  # type: ignore[arg-type]
            "launcher.bat",
            "--config",
            "abc",
        ]

    def test_env_used_when_cli_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_QGIS_COMMAND, 'launcher.bat --config "ab c"')
        config = _FakeConfig()
        assert _resolve_qgis_command(config) == [  # type: ignore[arg-type]
            "launcher.bat",
            "--config",
            "ab c",
        ]

    def test_ini_used_when_cli_and_env_unset(self) -> None:
        config = _FakeConfig(ini={"qgis_command": ["launcher.bat", "--config", "abc"]})
        assert _resolve_qgis_command(config) == [  # type: ignore[arg-type]
            "launcher.bat",
            "--config",
            "abc",
        ]

    def test_cli_wins_over_env_and_ini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_QGIS_COMMAND, "from-env.bat")
        config = _FakeConfig(
            options={"qgis_command": "from-cli.bat"},
            ini={"qgis_command": ["from-ini.bat"]},
        )
        assert _resolve_qgis_command(config) == ["from-cli.bat"]  # type: ignore[arg-type]

    def test_env_wins_over_ini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_QGIS_COMMAND, "from-env.bat")
        config = _FakeConfig(ini={"qgis_command": ["from-ini.bat"]})
        assert _resolve_qgis_command(config) == ["from-env.bat"]  # type: ignore[arg-type]


# ============================================================
# _resolve_qgis_args / _resolve_setting
# ============================================================


class TestResolveQgisArgs:
    """`qgis_args` の挙動が `qgis_command` 追加で壊れていないことを確認。"""

    def test_empty_returns_empty(self) -> None:
        config = _FakeConfig()
        assert _resolve_qgis_args(config) == []  # type: ignore[arg-type]

    def test_env_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_QGIS_ARGS, "--profile foo --noversioncheck")
        config = _FakeConfig()
        assert _resolve_qgis_args(config) == [  # type: ignore[arg-type]
            "--profile",
            "foo",
            "--noversioncheck",
        ]


class TestResolveSetting:
    """`_resolve_setting` の三段優先（CLI > env > ini）を確認。"""

    def test_cli_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_ENV", "from-env")
        config = _FakeConfig(
            options={"k": "from-cli"},
            ini={"k": "from-ini"},
        )
        result = _resolve_setting(
            config,  # type: ignore[arg-type]
            cli_key="k",
            env_key="MY_ENV",
            ini_key="k",
        )
        assert result == "from-cli"

    def test_env_wins_over_ini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_ENV", "from-env")
        config = _FakeConfig(ini={"k": "from-ini"})
        result = _resolve_setting(
            config,  # type: ignore[arg-type]
            cli_key="k",
            env_key="MY_ENV",
            ini_key="k",
        )
        assert result == "from-env"

    def test_ini_used_when_others_empty(self) -> None:
        config = _FakeConfig(ini={"k": "from-ini"})
        result = _resolve_setting(
            config,  # type: ignore[arg-type]
            cli_key="k",
            env_key="UNSET_VAR",
            ini_key="k",
        )
        assert result == "from-ini"

    def test_returns_none_when_all_empty(self) -> None:
        config = _FakeConfig()
        result = _resolve_setting(
            config,  # type: ignore[arg-type]
            cli_key="k",
            env_key="UNSET_VAR",
            ini_key="k",
        )
        assert result is None


# ============================================================
# B5b: _resolve_diag_capture_screenshots / _pending_screenshot_dir
# ============================================================


class TestResolveDiagCaptureScreenshots:
    """``qgis_diag_capture_screenshots`` ini の bool 解釈ルール。"""

    def test_default_true_when_unset(self) -> None:
        config = _FakeConfig(ini={})
        # 未設定（_FakeConfig.getini が "" を返す）→ 既定 True
        assert _resolve_diag_capture_screenshots(config) is True  # type: ignore[arg-type]

    def test_empty_string_is_default_true(self) -> None:
        config = _FakeConfig(ini={"qgis_diag_capture_screenshots": ""})
        assert _resolve_diag_capture_screenshots(config) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "value",
        ["true", "True", "TRUE", "1", "yes", "YES", "on", "ON", "  true  "],
    )
    def test_truthy_values(self, value: str) -> None:
        config = _FakeConfig(ini={"qgis_diag_capture_screenshots": value})
        assert _resolve_diag_capture_screenshots(config) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "value",
        ["false", "False", "0", "no", "off", "anything-else", "FALSE"],
    )
    def test_falsy_values(self, value: str) -> None:
        config = _FakeConfig(ini={"qgis_diag_capture_screenshots": value})
        assert _resolve_diag_capture_screenshots(config) is False  # type: ignore[arg-type]


class TestPendingScreenshotDir:
    """pending dir の path 構造（``<diag_root>/_pending/<safe>_<ts>``）。"""

    def test_path_structure(self, tmp_path: Any) -> None:
        config = _FakeConfig(ini={"qgis_diag_dir": str(tmp_path / "diag")})

        class _StubNode:
            nodeid = "tests/test_x.py::test_y"

        path = _pending_screenshot_dir(config, _StubNode())  # type: ignore[arg-type]
        # 親は <diag_root>/_pending
        assert path.parent == (tmp_path / "diag" / "_pending")
        # leaf は safe_filename(nodeid) + "_" + timestamp
        assert path.name.startswith("tests_test_x.py__test_y_")

    def test_safe_filename_strips_unsafe_chars(self, tmp_path: Any) -> None:
        config = _FakeConfig(ini={"qgis_diag_dir": str(tmp_path / "diag")})

        class _StubNode:
            # コロン / スラッシュ / 山括弧などは _ に置換される
            nodeid = "tests/foo<bar>::baz[param=1]"

        path = _pending_screenshot_dir(config, _StubNode())  # type: ignore[arg-type]
        # path-safe な leaf 名（特殊記号は _ に置換）
        leaf = path.name
        for ch in (":", "<", ">", "[", "]", "="):
            assert ch not in leaf


# ============================================================
# ADR-0002 §17.5: 失敗連鎖防止 自動 fresh_qgis（ini parse + hook）
# ============================================================


class TestResolveAutoFreshAfterFailure:
    """``qgis_auto_fresh_after_failure`` ini の bool 解釈ルール。"""

    def test_default_false_when_unset(self) -> None:
        config = _FakeConfig(ini={})
        # 未設定（_FakeConfig.getini が "" を返す）→ 既定 False（opt-in）
        assert _resolve_auto_fresh_after_failure(config) is False  # type: ignore[arg-type]

    def test_empty_string_is_false(self) -> None:
        config = _FakeConfig(ini={"qgis_auto_fresh_after_failure": ""})
        assert _resolve_auto_fresh_after_failure(config) is False  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", "  TRUE  "])
    def test_truthy_values(self, value: str) -> None:
        config = _FakeConfig(ini={"qgis_auto_fresh_after_failure": value})
        assert _resolve_auto_fresh_after_failure(config) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "anything-else"])
    def test_falsy_values(self, value: str) -> None:
        config = _FakeConfig(ini={"qgis_auto_fresh_after_failure": value})
        assert _resolve_auto_fresh_after_failure(config) is False  # type: ignore[arg-type]


class TestPytestRuntestSetupAutoFresh:
    """``pytest_runtest_setup`` hook が flag を読んで fresh_qgis marker を付与する。"""

    def _make_item(self, *, has_marker: bool) -> Any:
        """pytest.Item の最小スタブ。get_closest_marker / add_marker / config を持つ。"""

        class _StubItem:
            def __init__(self, has_marker: bool) -> None:
                self._has_marker = has_marker
                self._added_markers: list[Any] = []
                self.nodeid = "tests/test_x.py::test_y"
                self.config = _FakeConfig()

            def get_closest_marker(self, name: str) -> Any:
                return object() if (name == "fresh_qgis" and self._has_marker) else None

            def add_marker(self, marker: Any) -> None:
                self._added_markers.append(marker)

        return _StubItem(has_marker)

    def test_no_op_when_flag_not_set(self) -> None:
        from pytest_qgis_puppeteer.plugin import pytest_runtest_setup

        item = self._make_item(has_marker=False)
        pytest_runtest_setup(item)  # type: ignore[arg-type]
        assert item._added_markers == []

    def test_adds_marker_when_flag_true(self) -> None:
        from pytest_qgis_puppeteer.plugin import (
            _CONFIG_ATTR_PRIOR_TEST_FAILED,
            pytest_runtest_setup,
        )

        item = self._make_item(has_marker=False)
        setattr(item.config, _CONFIG_ATTR_PRIOR_TEST_FAILED, True)
        pytest_runtest_setup(item)  # type: ignore[arg-type]
        assert len(item._added_markers) == 1
        # flag はクリアされる（次の test で再付与しない）
        assert getattr(item.config, _CONFIG_ATTR_PRIOR_TEST_FAILED) is False

    def test_no_op_when_marker_already_present(self) -> None:
        """既に明示 fresh_qgis marker が付いている test では追加しない。"""
        from pytest_qgis_puppeteer.plugin import (
            _CONFIG_ATTR_PRIOR_TEST_FAILED,
            pytest_runtest_setup,
        )

        item = self._make_item(has_marker=True)
        setattr(item.config, _CONFIG_ATTR_PRIOR_TEST_FAILED, True)
        pytest_runtest_setup(item)  # type: ignore[arg-type]
        # 既存 marker があるので追加されない
        assert item._added_markers == []
        # flag はクリアされる（明示 marker でも flag は消費）
        assert getattr(item.config, _CONFIG_ATTR_PRIOR_TEST_FAILED) is False


# ============================================================
# ADR-0002 §17 / Roadmap "Qt 未捕捉例外 → fail 連動"
# ============================================================


class TestResolveFailOnUncaughtException:
    """``qgis_fail_on_uncaught_exception`` ini の bool 解釈（既定 ON）。"""

    def test_default_true_when_unset(self) -> None:
        config = _FakeConfig(ini={})
        # 未設定（_FakeConfig.getini が "" を返す）→ 既定 True（opt-out 方式）
        assert _resolve_fail_on_uncaught_exception(config) is True  # type: ignore[arg-type]

    def test_empty_string_is_true(self) -> None:
        config = _FakeConfig(ini={"qgis_fail_on_uncaught_exception": ""})
        assert _resolve_fail_on_uncaught_exception(config) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on"])
    def test_truthy_values(self, value: str) -> None:
        config = _FakeConfig(ini={"qgis_fail_on_uncaught_exception": value})
        assert _resolve_fail_on_uncaught_exception(config) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "anything-else"])
    def test_falsy_values(self, value: str) -> None:
        config = _FakeConfig(ini={"qgis_fail_on_uncaught_exception": value})
        assert _resolve_fail_on_uncaught_exception(config) is False  # type: ignore[arg-type]


class TestFormatUncaughtExceptions:
    """``_format_uncaught_exceptions`` の出力形式。"""

    def test_single_exception_renders_header_and_traceback(self) -> None:
        excs = [
            {
                "ts": "2026-05-01T10:00:00.000",
                "thread": "MainThread",
                "type": "ValueError",
                "message": "boom",
                "traceback": "Traceback...\nValueError: boom",
            }
        ]
        out = _format_uncaught_exceptions(excs)
        assert "1 uncaught exception(s)" in out
        assert "[1] ValueError @ 2026-05-01T10:00:00.000 (MainThread)" in out
        assert "boom" in out
        assert "Traceback..." in out

    def test_multiple_exceptions_indexed(self) -> None:
        excs = [
            {"ts": "t1", "thread": "T1", "type": "A", "message": "m1", "traceback": "tb1"},
            {"ts": "t2", "thread": "T2", "type": "B", "message": "m2", "traceback": "tb2"},
        ]
        out = _format_uncaught_exceptions(excs)
        assert "[1] A @ t1 (T1)" in out
        assert "[2] B @ t2 (T2)" in out

    def test_missing_fields_use_placeholders(self) -> None:
        excs = [{}]
        out = _format_uncaught_exceptions(excs)
        assert "<unknown>" in out
        assert "<no-ts>" in out
        assert "<no-thread>" in out
