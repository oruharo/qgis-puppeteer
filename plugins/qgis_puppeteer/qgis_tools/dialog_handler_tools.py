"""Dialog handler Qt-tied glue: handler 関数と tick 実装。

ADR-0002 Roadmap "Dialog handler（グローバル auto-respond）" の Qt 接続部。

純粋ロジックは :mod:`qgis_puppeteer.dialog_handlers` の
:class:`DialogHandlerRegistry` に集約。本モジュールは:

- registry の **module-level singleton** を保持する（Worker / handler 群が共有）
- ``register_dialog_handler`` 等の Tier 1 ハンドラ関数を提供
- :func:`tick` が Qt の ``QApplication.activeModalWidget()`` を読み、発火
  すべき handler の action を実行する

singleton は QGIS プロセス中で 1 つに集約。複数 Worker (≒ 通常無い) でも
QApplication は 1 つなので registry も 1 つで OK。
"""

from __future__ import annotations

import logging
from typing import Any

from qgis.PyQt.QtWidgets import QApplication, QDialog

from qgis_puppeteer.dialog_handlers import (
    DialogHandlerError,
    DialogHandlerRegistry,
)

logger = logging.getLogger("qgis_puppeteer.qgis_tools.dialog_handler_tools")

# Module-level singleton。テストでは ``get_registry().clear()`` で初期化する。
_REGISTRY = DialogHandlerRegistry()


def get_registry() -> DialogHandlerRegistry:
    """singleton ``DialogHandlerRegistry`` を取得する。"""
    return _REGISTRY


# ============================================================
# Handler 関数（qgis_register_dialog_handler 等）
# ============================================================


def register_dialog_handler(params: dict, iface: Any = None) -> dict:
    """``qgis_register_dialog_handler`` ハンドラ。

    params:
        - ``name`` (str): handler 識別名（既存名は上書き）
        - ``predicate`` (dict): modal の record と AND 一致比較する selector
          （``class`` / ``object_name`` / ``title`` を主に想定）
        - ``action`` (str): ``"accept"`` / ``"reject"`` / ``"close"``
        - ``once`` (bool, optional): True で 1 度発火したら自動 unregister
    """
    del iface  # unused
    try:
        name = params["name"]
        predicate = params["predicate"]
        action = params["action"]
        once = params.get("once", False)
    except KeyError as exc:
        return {"success": False, "error": "missing_param", "param": exc.args[0]}

    try:
        _REGISTRY.register(name, predicate, action, bool(once))
    except DialogHandlerError as exc:
        return {"success": False, "error": "invalid_argument", "message": str(exc)}

    return {"success": True, "name": name, "registered_count": len(_REGISTRY)}


def unregister_dialog_handler(params: dict, iface: Any = None) -> dict:
    """``qgis_unregister_dialog_handler`` ハンドラ。"""
    del iface
    name = params.get("name", "")
    found = _REGISTRY.unregister(name)
    return {"success": found, "name": name, "registered_count": len(_REGISTRY)}


def list_dialog_handlers(params: dict, iface: Any = None) -> dict:
    """``qgis_list_dialog_handlers`` ハンドラ。"""
    del params, iface
    return {"handlers": _REGISTRY.list_specs()}


def clear_dialog_handlers(params: dict, iface: Any = None) -> dict:
    """``qgis_clear_dialog_handlers`` ハンドラ — 全削除（テスト/teardown 用）。"""
    del params, iface
    _REGISTRY.clear()
    return {"success": True}


# ============================================================
# tick（QTimer から呼ばれる polling 本体）
# ============================================================


def _modal_to_record(modal: Any) -> dict | None:
    """active modal を selector_match 用 record dict に変換する。"""
    if modal is None:
        return None
    record: dict = {
        "class": type(modal).__name__,
        "object_name": modal.objectName() if hasattr(modal, "objectName") else "",
    }
    if hasattr(modal, "windowTitle"):
        try:
            title = modal.windowTitle()
            if title:
                record["title"] = title
        except Exception:  # noqa: BLE001
            pass
    return record


def _apply_action(modal: Any, action: str) -> bool:
    """Modal に action を適用。成功なら True。"""
    if modal is None:
        return False
    try:
        if isinstance(modal, QDialog):
            if action == "accept":
                modal.accept()
                return True
            if action == "reject":
                modal.reject()
                return True
            if action == "close":
                modal.close()
                return True
        # 非 QDialog modal への fallback
        if action == "close" and hasattr(modal, "close"):
            modal.close()
            return True
    except Exception:  # noqa: BLE001
        logger.exception("Failed to apply dialog action %r", action)
        return False
    logger.warning("Cannot apply action %r to modal of type %s", action, type(modal).__name__)
    return False


def tick(iface: Any = None) -> dict:
    """1 回分の polling サイクル。Worker 側 QTimer の timeout から呼ばれる。

    Returns:
        ``{"applied": bool, "handler": str | None, "action": str | None,
        "modal_class": str | None}``。発火が無かった場合 ``applied=False``。

    iface は handler 関数規約に合わせて受け取るが使わない。
    """
    del iface
    app = QApplication.instance()
    if app is None:
        return {"applied": False}

    modal = app.activeModalWidget()
    modal_id = id(modal) if modal is not None else None
    record = _modal_to_record(modal)

    spec = _REGISTRY.select_for_modal(modal_id, record)
    if spec is None:
        return {"applied": False}

    success = _apply_action(modal, spec.action)
    return {
        "applied": bool(success),
        "handler": spec.name,
        "action": spec.action,
        "modal_class": (record or {}).get("class"),
        "modal_object_name": (record or {}).get("object_name"),
    }
