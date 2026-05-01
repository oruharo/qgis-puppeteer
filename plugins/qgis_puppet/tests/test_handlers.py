"""handlers.py のアダプタ単体テスト。

`qgis_puppeteer.qgis_tools.*` をフェイクモジュールで置き換えて、各 Handler が
params dict からの引数分解 / iface の合流 / 結果の透過返却を正しく行うかを
確認する。QGIS / PyQt5 は不要。
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# ==============================================================
# テスト前準備：フェイク qgis_puppeteer.qgis_tools モジュールを sys.modules に注入
# ==============================================================


class _CallRecorder:
    """どう呼ばれたかを `(args, kwargs)` として記録するだけのフェイク。"""

    def __init__(self, return_value: Any = None) -> None:
        self.return_value = return_value
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.return_value


class _FakePermissionManager:
    def __init__(self) -> None:
        self.whitelist: list[str] = ["print('hi')"]
        self.session: list[str] = ["x = 1"]
        self.clear_session_called: bool = False

    def get_whitelist(self) -> list[str]:
        return list(self.whitelist)

    def get_session_allowed(self) -> list[str]:
        return list(self.session)

    def clear_session(self) -> None:
        self.clear_session_called = True
        self.session.clear()


def _install_fake_tools() -> dict[str, Any]:
    """sys.modules に `qgis_puppeteer.qgis_tools.*` の最小フェイクを流し込む。

    戻り値は各フェイクモジュール・関数への参照を持つ dict。テストから
    呼び出し記録を検証するのに使う。

    注意: `qgis_puppeteer` 本体は pure-python として実在するので、
    親パッケージは上書きせず `qgis_tools` サブパッケージだけ差し替える。

    注意: `build_handlers()` が import するモジュールは **1 つ残らず** ここに
    並べること。フェイクの親は `__path__` を持たない `ModuleType` なので、
    属性の無いモジュールは `from qgis_puppeteer.qgis_tools import x` の時点で
    ImportError になり、`build_handlers()` が丸ごと空 dict を返してしまう。
    """
    qgis_tools_pkg = types.ModuleType("qgis_puppeteer.qgis_tools")

    layer_tools = types.ModuleType("qgis_puppeteer.qgis_tools.layer_tools")
    layer_tools.list_layers = _CallRecorder({"layers": []})
    layer_tools.get_layer_info = _CallRecorder({"name": "L"})
    layer_tools.select_features = _CallRecorder({"selected_count": 3})
    layer_tools.get_selected_features = _CallRecorder({"features": []})

    fake_pm = _FakePermissionManager()
    python_executor = types.ModuleType("qgis_puppeteer.qgis_tools.python_executor")
    python_executor.execute_python = _CallRecorder({"stdout": "ok"})
    python_executor.execute_with_permission = _CallRecorder({"stdout": "ok"})
    python_executor.get_permission_manager = lambda: fake_pm

    screenshot_tools = types.ModuleType("qgis_puppeteer.qgis_tools.screenshot_tools")
    screenshot_tools.take_screenshot = _CallRecorder({"path": "/tmp/x.png"})
    screenshot_tools.get_canvas_extent = _CallRecorder({"xmin": 0.0})
    screenshot_tools.set_canvas_extent = _CallRecorder({"success": True})

    ui_tools = types.ModuleType("qgis_puppeteer.qgis_tools.ui_tools")
    ui_tools.snapshot_ui = _CallRecorder({"active_modal": None})
    ui_tools.click_widget = _CallRecorder({"success": True})
    ui_tools.set_widget_value = _CallRecorder({"success": True})
    ui_tools.check_actionability = _CallRecorder({"actionable": True})

    dialog_handler_tools = types.ModuleType("qgis_puppeteer.qgis_tools.dialog_handler_tools")
    dialog_handler_tools.register_dialog_handler = _CallRecorder({"success": True})
    dialog_handler_tools.unregister_dialog_handler = _CallRecorder({"success": True})
    dialog_handler_tools.list_dialog_handlers = _CallRecorder({"handlers": []})
    dialog_handler_tools.clear_dialog_handlers = _CallRecorder({"success": True})

    exception_recorder = types.ModuleType("qgis_puppeteer.qgis_tools.exception_recorder")
    exception_recorder.install_excepthook = _CallRecorder(True)
    exception_recorder.get_recent = _CallRecorder([{"type": "ValueError", "message": "boom"}])
    # 実装の既定値（200）とは別の値にして、size_limit() を呼ばず直書きしても
    # 通ってしまう偶然の一致を防ぐ
    exception_recorder.size_limit = _CallRecorder(7)
    exception_recorder.clear = _CallRecorder(None)

    # サブモジュール属性としても参照できるようにしておく（`from pkg import x` 相当）
    qgis_tools_pkg.layer_tools = layer_tools
    qgis_tools_pkg.python_executor = python_executor
    qgis_tools_pkg.screenshot_tools = screenshot_tools
    qgis_tools_pkg.ui_tools = ui_tools
    qgis_tools_pkg.dialog_handler_tools = dialog_handler_tools
    qgis_tools_pkg.exception_recorder = exception_recorder

    sys.modules["qgis_puppeteer.qgis_tools"] = qgis_tools_pkg
    sys.modules["qgis_puppeteer.qgis_tools.layer_tools"] = layer_tools
    sys.modules["qgis_puppeteer.qgis_tools.python_executor"] = python_executor
    sys.modules["qgis_puppeteer.qgis_tools.screenshot_tools"] = screenshot_tools
    sys.modules["qgis_puppeteer.qgis_tools.ui_tools"] = ui_tools
    sys.modules["qgis_puppeteer.qgis_tools.dialog_handler_tools"] = dialog_handler_tools
    sys.modules["qgis_puppeteer.qgis_tools.exception_recorder"] = exception_recorder

    return {
        "layer": layer_tools,
        "py": python_executor,
        "pm": fake_pm,
        "shot": screenshot_tools,
        "ui": ui_tools,
        "dialog": dialog_handler_tools,
        "exc": exception_recorder,
    }


@pytest.fixture
def fake_tools() -> dict[str, Any]:
    """各テスト前にフェイクを注入し、終了後にクリーンアップする。"""
    refs = _install_fake_tools()
    yield refs
    for mod_name in [
        "qgis_puppeteer.qgis_tools.layer_tools",
        "qgis_puppeteer.qgis_tools.python_executor",
        "qgis_puppeteer.qgis_tools.screenshot_tools",
        "qgis_puppeteer.qgis_tools.ui_tools",
        "qgis_puppeteer.qgis_tools.dialog_handler_tools",
        "qgis_puppeteer.qgis_tools.exception_recorder",
        "qgis_puppeteer.qgis_tools",
    ]:
        sys.modules.pop(mod_name, None)


# build_handlers は実モジュールから import するため、plugins ディレクトリが
# sys.path に入っている必要がある（conftest.py で対応）。
def _import_build_handlers() -> Any:
    from qgis_puppet.handlers import build_handlers  # type: ignore

    return build_handlers


# ==============================================================
# Layer Tools
# ==============================================================


class TestLayerHandlers:
    def test_list_layers_ignores_params_and_passes_iface(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        iface = object()
        handlers = build_handlers(iface)
        result = handlers["qgis_list_layers"]({"ignored": "x"})
        assert result == {"layers": []}
        calls = fake_tools["layer"].list_layers.calls
        assert calls == [((iface,), {})]

    def test_get_layer_info_extracts_layer_name(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        iface = object()
        handlers = build_handlers(iface)
        handlers["qgis_get_layer_info"]({"layer_name": "Rivers"})
        args, _ = fake_tools["layer"].get_layer_info.calls[0]
        assert args == ("Rivers", iface)

    def test_select_features_passes_both_args(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        handlers["qgis_select_features"]({"layer_name": "L", "expression": '"n" > 0'})
        args, _ = fake_tools["layer"].select_features.calls[0]
        assert args == ("L", '"n" > 0', None)

    def test_get_selected_features_uses_default_limit(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        handlers["qgis_get_selected_features"]({"layer_name": "L"})
        args, _ = fake_tools["layer"].get_selected_features.calls[0]
        assert args == ("L", 100, None)

    def test_get_selected_features_honors_limit_override(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        handlers["qgis_get_selected_features"]({"layer_name": "L", "limit": 5})
        args, _ = fake_tools["layer"].get_selected_features.calls[0]
        assert args == ("L", 5, None)


# ==============================================================
# Python Executor
# ==============================================================


class TestPythonHandlers:
    def test_execute_python_passes_code(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        iface = object()
        handlers = build_handlers(iface)
        handlers["qgis_execute_python"]({"code": "1+1"})
        args, _ = fake_tools["py"].execute_python.calls[0]
        assert args == ("1+1", iface)

    def test_execute_with_permission_passes_both(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        handlers["qgis_execute_with_permission"]({"code": "x=1", "permission": "once"})
        args, _ = fake_tools["py"].execute_with_permission.calls[0]
        assert args == ("x=1", "once", None)

    def test_get_whitelist_projects_permission_manager(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        result = handlers["qgis_get_whitelist"]({})
        assert result == {
            "whitelist": ["print('hi')"],
            "session_allowed": ["x = 1"],
        }

    def test_clear_session_permissions_invokes_manager(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        result = handlers["qgis_clear_session_permissions"]({})
        assert result == {
            "success": True,
            "message": "Session permissions cleared",
        }
        assert fake_tools["pm"].clear_session_called is True


# ==============================================================
# Screenshot & Canvas
# ==============================================================


class TestScreenshotHandlers:
    def test_screenshot_passes_all_optional_params(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        iface = object()
        handlers = build_handlers(iface)
        handlers["qgis_screenshot"]({"output_path": "/tmp/a.png", "width": 800, "height": 600})
        args, _ = fake_tools["shot"].take_screenshot.calls[0]
        assert args == ("/tmp/a.png", 800, 600, iface)

    def test_screenshot_defaults_all_to_none(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        handlers["qgis_screenshot"]({})
        args, _ = fake_tools["shot"].take_screenshot.calls[0]
        assert args == (None, None, None, None)

    def test_get_canvas_extent_passes_only_iface(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        iface = object()
        handlers = build_handlers(iface)
        handlers["qgis_get_canvas_extent"]({})
        args, _ = fake_tools["shot"].get_canvas_extent.calls[0]
        assert args == (iface,)

    def test_set_canvas_extent_passes_four_floats(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        handlers["qgis_set_canvas_extent"]({"xmin": 1.0, "ymin": 2.0, "xmax": 3.0, "ymax": 4.0})
        args, _ = fake_tools["shot"].set_canvas_extent.calls[0]
        assert args == (1.0, 2.0, 3.0, 4.0, None)


# ==============================================================
# UI Tools
# ==============================================================


class TestUiHandlers:
    def test_snapshot_ui_defaults(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        handlers["qgis_snapshot_ui"]({})
        _, kwargs = fake_tools["ui"].snapshot_ui.calls[0]
        assert kwargs == {
            "max_depth": 8,
            "include_invisible": False,
            "include_main_window": False,
        }

    def test_snapshot_ui_explicit(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        handlers["qgis_snapshot_ui"](
            {
                "max_depth": 3,
                "include_invisible": True,
                "include_main_window": True,
            }
        )
        _, kwargs = fake_tools["ui"].snapshot_ui.calls[0]
        assert kwargs == {
            "max_depth": 3,
            "include_invisible": True,
            "include_main_window": True,
        }

    def test_click_widget_forwards_selector(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        iface = object()
        handlers = build_handlers(iface)
        selector = {"name": "ok_button"}
        handlers["qgis_click_widget"]({"selector": selector})
        args, _ = fake_tools["ui"].click_widget.calls[0]
        assert args == (selector, iface)

    def test_set_widget_value_forwards_selector_and_value(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        selector = {"name": "input_x"}
        handlers["qgis_set_widget_value"]({"selector": selector, "value": "hello"})
        args, _ = fake_tools["ui"].set_widget_value.calls[0]
        assert args == (selector, "hello", None)

    def test_check_actionability_forwards_selector_and_iface(
        self, fake_tools: dict[str, Any]
    ) -> None:
        build_handlers = _import_build_handlers()
        iface = object()
        handlers = build_handlers(iface)
        selector = {"object_name": "ok_button", "scope": "modal"}
        result = handlers["qgis_check_actionability"]({"selector": selector})
        assert result == {"actionable": True}
        args, _ = fake_tools["ui"].check_actionability.calls[0]
        assert args == (selector, iface)


# ==============================================================
# Uncaught exception recorder（ADR-0002 §17）
# ==============================================================


class TestExceptionRecorderHandlers:
    def test_build_handlers_installs_excepthook(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        build_handlers(None)
        assert fake_tools["exc"].install_excepthook.calls == [((), {})]

    def test_get_recent_exceptions_passes_int_limit(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        result = handlers["qgis_get_recent_exceptions"]({"limit": 5})
        _, kwargs = fake_tools["exc"].get_recent.calls[0]
        assert kwargs == {"limit": 5}
        assert result["exceptions"] == [{"type": "ValueError", "message": "boom"}]
        assert fake_tools["exc"].size_limit.calls == [((), {})]
        assert result["max_buffer_size"] == 7

    def test_get_recent_exceptions_ignores_non_int_limit(self, fake_tools: dict[str, Any]) -> None:
        """limit が int でなければ None（＝全件）に落とす。"""
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        handlers["qgis_get_recent_exceptions"]({"limit": "10"})
        _, kwargs = fake_tools["exc"].get_recent.calls[0]
        assert kwargs == {"limit": None}

    def test_clear_recent_exceptions_calls_clear(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        assert handlers["qgis_clear_recent_exceptions"]({}) == {"ok": True}
        assert fake_tools["exc"].clear.calls == [((), {})]


# ==============================================================
# Inventory / ImportError path
# ==============================================================


class TestHandlerInventory:
    def test_all_expected_commands_registered(self, fake_tools: dict[str, Any]) -> None:
        build_handlers = _import_build_handlers()
        handlers = build_handlers(None)
        expected = {
            # layer
            "qgis_list_layers",
            "qgis_get_layer_info",
            "qgis_select_features",
            "qgis_get_selected_features",
            # python
            "qgis_execute_python",
            "qgis_execute_with_permission",
            "qgis_get_whitelist",
            "qgis_clear_session_permissions",
            # screenshot / canvas
            "qgis_screenshot",
            "qgis_get_canvas_extent",
            "qgis_set_canvas_extent",
            # ui
            "qgis_snapshot_ui",
            "qgis_click_widget",
            "qgis_set_widget_value",
            "qgis_check_actionability",
            # dialog handler
            "qgis_register_dialog_handler",
            "qgis_unregister_dialog_handler",
            "qgis_list_dialog_handlers",
            "qgis_clear_dialog_handlers",
            # uncaught exception recorder
            "qgis_get_recent_exceptions",
            "qgis_clear_recent_exceptions",
        }
        assert set(handlers.keys()) == expected


def test_build_handlers_returns_empty_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qgis_puppeteer.qgis_tools が import 不可の場合、空 dict を返す。

    QGIS 非依存環境（`qgis_puppeteer` 単体テスト等）や未インストール環境
    を想定。実モジュールが解決可能でも確実に失敗させるため
    `builtins.__import__` をフックして ImportError を注入する。
    """
    # 実モジュールを sys.modules から外す（次の import 試行を確実に走らせる）
    for mod_name in [
        "qgis_puppeteer.qgis_tools.layer_tools",
        "qgis_puppeteer.qgis_tools.python_executor",
        "qgis_puppeteer.qgis_tools.screenshot_tools",
        "qgis_puppeteer.qgis_tools.ui_tools",
        "qgis_puppeteer.qgis_tools",
    ]:
        monkeypatch.delitem(sys.modules, mod_name, raising=False)

    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("qgis_puppeteer.qgis_tools"):
            raise ImportError(f"blocked by test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    build_handlers = _import_build_handlers()
    handlers = build_handlers(None)
    assert handlers == {}
