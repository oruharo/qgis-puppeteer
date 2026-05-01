"""Worker handler アダプタ：`qgis_puppeteer.qgis_tools.*` を Worker 用に wrap。

## 設計

`qgis_puppeteer.Worker` の Handler シグネチャは `(params: dict) -> Any` の
同期関数。一方、`qgis_puppeteer/qgis_tools/*.py` に実装されたツール関数は
各々個別の positional / keyword 引数を取る。両者をつなぐ薄いアダプタ群を
ここに集約する。

## なぜ別モジュールに分けたか

- `plugin.py` は Qt / iface ライフサイクルに集中させたい
- アダプタ単体でユニットテスト可能にしたい（PyQt5 / QGIS 不要のフェイク対象）
- 新ツール追加時の編集箇所を 1 ファイルに留めたい

## 依存

- 実行時：`qgis_puppeteer.qgis_tools` が import 可能である前提（QGIS 環境）
- `build_handlers()` は import 失敗を握って空 dict を返す設計（通信レイヤーだけ
  立ち上げたい headless test や、qgis 非依存環境で useful）
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

# Handler の型エイリアス（`qgis_puppeteer.worker_state.Handler` と同じ）。
# 循環依存を避けるためここでは自前定義。
Handler = Callable[[dict[str, Any]], Any]

logger = logging.getLogger("qgis_puppet.handlers")


# ==============================================================
# アダプタ構築：各ツール関数を `(params: dict) -> Any` に整形
# ==============================================================


def build_handlers(iface: Any | None = None) -> dict[str, Handler]:
    """ADR-0001 §5 の 14 ツールに対応する handler 群を返す。

    `qgis_puppeteer.qgis_tools.*` の import に失敗した場合は空 dict を返す。
    呼び出し側は戻り値をそのまま `Worker.register_handler(...)` に渡す想定。

    :param iface: QGIS の `QgisInterface`（GUI モード時）。headless なら None。
    :return: command 名 → Handler のマッピング。
    """
    try:
        from qgis_puppeteer.qgis_tools import (  # type: ignore[import-not-found]
            dialog_handler_tools,
            exception_recorder,
            layer_tools,
            python_executor,
            screenshot_tools,
            ui_tools,
        )
    except ImportError as e:
        logger.warning(
            "Could not import qgis_puppeteer.qgis_tools: %s. "
            "Handlers will not be registered; Worker will respond with "
            "INVALID_COMMAND to QGIS tool calls.",
            e,
        )
        return {}

    handlers: dict[str, Handler] = {}

    # ------------------------------------------------------------
    # Layer Tools
    # ------------------------------------------------------------

    handlers["qgis_list_layers"] = lambda _params: layer_tools.list_layers(iface)

    handlers["qgis_get_layer_info"] = lambda params: layer_tools.get_layer_info(
        params["layer_name"], iface
    )

    handlers["qgis_select_features"] = lambda params: layer_tools.select_features(
        params["layer_name"], params["expression"], iface
    )

    handlers["qgis_get_selected_features"] = lambda params: layer_tools.get_selected_features(
        params["layer_name"], params.get("limit", 100), iface
    )

    # ------------------------------------------------------------
    # Python Executor（3-tier permission system）
    # ------------------------------------------------------------

    handlers["qgis_execute_python"] = lambda params: python_executor.execute_python(
        params["code"], iface
    )

    handlers["qgis_execute_with_permission"] = lambda params: (
        python_executor.execute_with_permission(params["code"], params["permission"], iface)
    )

    def _get_whitelist(_params: dict[str, Any]) -> dict[str, Any]:
        pm = python_executor.get_permission_manager()
        return {
            "whitelist": pm.get_whitelist(),
            "session_allowed": pm.get_session_allowed(),
        }

    handlers["qgis_get_whitelist"] = _get_whitelist

    def _clear_session(_params: dict[str, Any]) -> dict[str, Any]:
        pm = python_executor.get_permission_manager()
        pm.clear_session()
        return {"success": True, "message": "Session permissions cleared"}

    handlers["qgis_clear_session_permissions"] = _clear_session

    # ------------------------------------------------------------
    # Screenshot & Canvas
    # ------------------------------------------------------------

    handlers["qgis_screenshot"] = lambda params: screenshot_tools.take_screenshot(
        params.get("output_path"),
        params.get("width"),
        params.get("height"),
        iface,
    )

    handlers["qgis_get_canvas_extent"] = lambda _params: screenshot_tools.get_canvas_extent(iface)

    handlers["qgis_set_canvas_extent"] = lambda params: screenshot_tools.set_canvas_extent(
        params["xmin"],
        params["ymin"],
        params["xmax"],
        params["ymax"],
        iface,
    )

    # ------------------------------------------------------------
    # UI Tools（iface 不要：QApplication をグローバル探索）
    # ------------------------------------------------------------

    handlers["qgis_snapshot_ui"] = lambda params: ui_tools.snapshot_ui(
        max_depth=params.get("max_depth", 8),
        include_invisible=params.get("include_invisible", False),
        include_main_window=params.get("include_main_window", False),
    )

    handlers["qgis_click_widget"] = lambda params: ui_tools.click_widget(params["selector"], iface)

    handlers["qgis_set_widget_value"] = lambda params: ui_tools.set_widget_value(
        params["selector"], params["value"], iface
    )

    handlers["qgis_check_actionability"] = lambda params: ui_tools.check_actionability(
        params["selector"], iface
    )

    # ------------------------------------------------------------
    # Dialog Handlers（ADR-0002 Roadmap "Dialog handler"）
    # ------------------------------------------------------------

    handlers["qgis_register_dialog_handler"] = lambda params: (
        dialog_handler_tools.register_dialog_handler(params, iface)
    )
    handlers["qgis_unregister_dialog_handler"] = lambda params: (
        dialog_handler_tools.unregister_dialog_handler(params, iface)
    )
    handlers["qgis_list_dialog_handlers"] = lambda params: (
        dialog_handler_tools.list_dialog_handlers(params, iface)
    )
    handlers["qgis_clear_dialog_handlers"] = lambda params: (
        dialog_handler_tools.clear_dialog_handlers(params, iface)
    )

    # ------------------------------------------------------------
    # Uncaught exception recorder（ADR-0002 §17 / Roadmap "Qt 未捕捉例外"）
    # ------------------------------------------------------------
    # plugin が build_handlers() を呼んだ時点で sys.excepthook を hook する。
    # 冪等なので fresh_qgis 経由の再 init でも安全。

    exception_recorder.install_excepthook()

    def _get_recent_exceptions(params: dict[str, Any]) -> dict[str, Any]:
        limit = params.get("limit") if isinstance(params, dict) else None
        if not isinstance(limit, int):
            limit = None
        return {
            "exceptions": exception_recorder.get_recent(limit=limit),
            "max_buffer_size": exception_recorder.size_limit(),
        }

    def _clear_recent_exceptions(_params: dict[str, Any]) -> dict[str, Any]:
        exception_recorder.clear()
        return {"ok": True}

    handlers["qgis_get_recent_exceptions"] = _get_recent_exceptions
    handlers["qgis_clear_recent_exceptions"] = _clear_recent_exceptions

    return handlers


def build_test_handlers(iface: Any | None = None) -> dict[str, Handler]:
    """ADR-0002 Roadmap "Worker test handler 追加": ``test.*`` namespace handlers。

    本関数は ``QPUPPETEER_ALLOW_TEST_HANDLERS=1`` の Worker でのみ呼ばれる前提
    （plugin.py 側で env を check して dispatch）。Worker.register_handler は
    ``test.*`` プレフィックスを env なしで登録しようとすると拒否する（ADR-0001
    §12.4）。

    現状の handler セット:

    - ``test.signal_spy_start(selector, signal, max_emissions?)`` →
      ``{spy_id, matched, diagnostics}``
    - ``test.signal_spy_get_emissions(spy_id, since_index?)`` →
      ``{found, count, emissions, next_index}``
    - ``test.signal_spy_count(spy_id)`` → ``{found, count}``
    - ``test.signal_spy_stop(spy_id)`` → ``{ok}``
    - ``test.wait_for_signal(selector, signal, timeout_ms?)`` →
      ``{fired, args, timed_out}``
    """
    try:
        from qgis_puppeteer.qgis_tools import (  # type: ignore[import-not-found]
            signal_spy,
            ui_tools,
        )
    except ImportError as e:
        logger.warning(
            "Could not import qgis_tools for test handlers: %s. "
            "test.signal_spy_* / test.wait_for_signal will not be registered.",
            e,
        )
        return {}

    handlers: dict[str, Handler] = {}

    def _resolve_widget(selector: dict[str, Any]) -> tuple[Any, Any]:
        # ui_tools._find_widget は ``(widget, diag)`` を返すので互換させる
        return ui_tools._find_widget(selector)  # noqa: SLF001 - 内部 API 利用

    def _signal_spy_start(params: dict[str, Any]) -> dict[str, Any]:
        return signal_spy.start_spy(
            widget_resolver=_resolve_widget,
            selector=params["selector"],
            signal_name=params["signal"],
            max_emissions=params.get("max_emissions"),
        )

    def _signal_spy_get_emissions(params: dict[str, Any]) -> dict[str, Any]:
        return signal_spy.get_emissions(
            int(params["spy_id"]),
            since_index=int(params.get("since_index", 0)),
        )

    def _signal_spy_count(params: dict[str, Any]) -> dict[str, Any]:
        result = signal_spy.get_emissions(int(params["spy_id"]))
        return {"found": result["found"], "count": result["count"]}

    def _signal_spy_stop(params: dict[str, Any]) -> dict[str, Any]:
        return signal_spy.stop_spy(int(params["spy_id"]))

    def _wait_for_signal(params: dict[str, Any]) -> dict[str, Any]:
        # blocking wait（local QEventLoop）。timeout_ms 既定 5000ms。
        from qgis.PyQt.QtCore import QEventLoop, QTimer  # type: ignore[import-not-found]

        widget, diag = _resolve_widget(params["selector"])
        if widget is None:
            return {
                "fired": False,
                "args": None,
                "timed_out": False,
                "matched": False,
                "diagnostics": dict(diag) if diag is not None else None,
            }
        signal = getattr(widget, params["signal"], None)
        if signal is None or not hasattr(signal, "connect"):
            raise ValueError(f"widget does not expose connectable signal {params['signal']!r}")
        timeout_ms = int(params.get("timeout_ms", 5000))
        captured: dict[str, Any] = {"fired": False, "args": None}
        loop = QEventLoop()

        def _on_emit(*args: Any) -> None:
            captured["fired"] = True
            captured["args"] = signal_spy.serialize_args(args)
            loop.quit()

        signal.connect(_on_emit)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec_()
        with contextlib.suppress(TypeError, RuntimeError):
            signal.disconnect(_on_emit)
        return {
            "fired": captured["fired"],
            "args": captured["args"],
            "timed_out": not captured["fired"],
            "matched": True,
        }

    handlers["test.signal_spy_start"] = _signal_spy_start
    handlers["test.signal_spy_get_emissions"] = _signal_spy_get_emissions
    handlers["test.signal_spy_count"] = _signal_spy_count
    handlers["test.signal_spy_stop"] = _signal_spy_stop
    handlers["test.wait_for_signal"] = _wait_for_signal
    return handlers
