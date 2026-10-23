"""Dialog handler Qt-tied glue: handler 関数と tick 実装。

ADR-0002 Roadmap "Dialog handler（グローバル auto-respond）" の Qt 接続部。

純粋ロジックは :mod:`qgis_puppeteer.dialog_handlers` の
:class:`DialogHandlerRegistry` に集約。本モジュールは:

- registry の **module-level singleton** を保持する（Worker / handler 群が共有）
- ``register_dialog_handler`` 等の Tier 1 ハンドラ関数を提供
- :func:`tick` が Qt の ``QApplication.activePopupWidget()`` /
  ``activeModalWidget()`` を読み、発火すべき handler の action を Qt のイベント
  ループに積む（#21。action の中でホストのコードが ``exec()`` の確認を出しても、
  tick が止まらないように）。ポップアップ（``QMenu.exec()`` のメニューなど）は、
  predicate に ``"popup": true`` を書いた handler だけが扱う（#22）

singleton は QGIS プロセス中で 1 つに集約。複数 Worker (≒ 通常無い) でも
QApplication は 1 つなので registry も 1 つで OK。
"""

from __future__ import annotations

import logging
from typing import Any

from qgis.PyQt.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMenu

from qgis_puppeteer.dialog_handlers import (
    DialogHandlerError,
    DialogHandlerRegistry,
)
from qgis_puppeteer.qgis_tools import queued
from qgis_puppeteer.qgis_tools.ui_tools import activate_menu_action
from qgis_puppeteer.selector_match import label_forms

logger = logging.getLogger("qgis_puppeteer.qgis_tools.dialog_handler_tools")

# Module-level singleton。テストでは ``get_registry().clear()`` で初期化する。
_REGISTRY = DialogHandlerRegistry()

# modal に振るキーを入れる dynamic property（`_modal_key`）
_MODAL_KEY_PROPERTY = "_qpuppeteer_modal_key"
_next_modal_key = 0


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
        - ``action`` (str): ``"accept"`` / ``"reject"`` / ``"close"`` / ``"button"``
        - ``once`` (bool, optional): True で 1 度発火したら自動 unregister
        - ``button`` (str, optional): ``action="button"`` で押すボタン。
          ``QMessageBox`` / ``QDialogButtonBox`` の標準ボタン名（``"Ok"`` /
          ``"Yes"`` / ``"No"`` / ``"Cancel"`` など）か、ボタンの文言。
          ポップアップのメニューでは、選ぶ項目の文言
    """
    del iface  # unused
    try:
        name = params["name"]
        predicate = params["predicate"]
        action = params["action"]
        once = params.get("once", False)
        button = params.get("button")
    except KeyError as exc:
        return {"success": False, "error": "missing_param", "param": exc.args[0]}

    try:
        _REGISTRY.register(name, predicate, action, bool(once), button)
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


def _modal_to_record(modal: Any, *, popup: bool = False) -> dict | None:
    """active modal / popup を selector_match 用 record dict に変換する。

    ポップアップには ``popup: True`` を付ける（handler の opt-in の判定用）。
    ``QMenu`` の ``title`` はメニューの題名（``windowTitle`` は空のことが多い）。
    """
    if modal is None:
        return None
    record: dict = {
        "class": type(modal).__name__,
        "object_name": modal.objectName() if hasattr(modal, "objectName") else "",
    }
    if popup:
        record["popup"] = True
    if isinstance(modal, QMenu) and modal.title():
        record["title"] = modal.title()
    elif hasattr(modal, "windowTitle"):
        try:
            title = modal.windowTitle()
            if title:
                record["title"] = title
        except Exception:  # noqa: BLE001
            pass
    return record


def _modal_key(modal: Any) -> int:
    """modal ごとに一意で、同じ modal なら変わらないキーを返す（#21）。

    ``id(modal)`` は使えない。PyQt は ``activeModalWidget()`` のたびに別の Python
    wrapper を返すことがあり、同じ modal でも tick ごとに変わる（action で閉じない
    modal に handler が毎回発火する）。逆に、閉じた modal の wrapper の番地が次の
    modal に使い回されると、新しい modal を処理済みと見なして飛ばす。キーは
    modal 自身の dynamic property に置くので、modal と一緒に消える。
    """
    global _next_modal_key
    key = modal.property(_MODAL_KEY_PROPERTY)
    if key is None:
        _next_modal_key += 1
        key = _next_modal_key
        modal.setProperty(_MODAL_KEY_PROPERTY, key)
    return int(key)


def _apply_action(modal: Any, spec: Any) -> bool:
    """Modal への action を Qt のイベントループに積む。積めたら True。

    その場で行うと、ホストが上書きした ``reject()`` の中で ``exec()`` の確認を出す
    ような場合に tick が止まる。積んだ処理の失敗（押すボタンが無い、など）は
    recent_exceptions に残る。
    """
    if modal is None:
        return False
    action = spec.action
    if not (
        isinstance(modal, QDialog)
        or action == "button"
        or (action == "close" and hasattr(modal, "close"))
    ):
        logger.warning("Cannot apply action %r to modal of type %s", action, type(modal).__name__)
        return False

    def run() -> dict:
        outcome = _run_action(modal, action, spec.button)
        if outcome.get("success") is False and spec.once:
            # 押すボタン・項目が無かった once の handler は使い切らない
            _REGISTRY.restore(spec)
        return outcome

    queued.run_later(
        run,
        what=f"dialog handler {spec.name!r} ({action})",
        target=modal,
        gone_is_failure=False,  # その間に閉じて消えたなら、することが無いだけ
    )
    return True


def _run_action(modal: Any, action: str, button: str | None) -> dict:
    """積んだ action の本体。modal が消えていれば、ここには来ない（``run_later``）。"""
    if not modal.isVisible():
        # 積んでから動くまでの間に閉じた（まだ消えてはいない）。することは無く、
        # 失敗でもない
        return {"success": True, "skipped": "modal_closed"}
    if action == "button":
        if isinstance(modal, QMenu):
            return _choose_menu_item(modal, button or "")
        return _click_button(modal, button or "")
    if isinstance(modal, QDialog) and action in ("accept", "reject"):
        getattr(modal, action)()
    else:
        modal.close()
    return {"success": True}


def _click_button(modal: Any, button: str) -> dict:
    """``button``（標準ボタン名か文言）に当たるボタンを押す。

    ``QMessageBox`` は中に ``QDialogButtonBox`` を持つので、どちらも
    ``QDialogButtonBox`` から探す。標準ボタン名を先に見て、無ければ文言で探す。
    文言は ``&`` のニーモニックと、末尾の ``(&N)`` を除いても比べる（``"No"`` で
    ``"&No"``、``"いいえ"`` で ``"いいえ(&N)"`` に当たる）。標準ボタンの文言は
    QGIS の言語で変わるので、標準ボタンは名前で指すのがよい。押すと
    ``QMessageBox`` の ``clickedButton()`` が決まり、``exec()`` がそのボタンを返す。
    """
    boxes = modal.findChildren(QDialogButtonBox)
    standard = getattr(QDialogButtonBox.StandardButton, button, None)
    if isinstance(standard, QDialogButtonBox.StandardButton):
        for box in boxes:
            target = box.button(standard)
            if target is not None:
                target.click()
                return {"success": True}
    available: list[str] = []
    for box in boxes:
        for target in box.buttons():
            text = target.text()
            available.append(text)
            if button in label_forms(text):
                target.click()
                return {"success": True}
    return {
        "success": False,
        "error": "dialog_button_not_found",
        "button": button,
        "available": available,
        "modal_class": type(modal).__name__,
    }


def _choose_menu_item(menu: QMenu, item: str) -> dict:
    """ポップアップのメニューで、文言が ``item`` の項目を選ぶ（#22）。

    文言の比べ方はボタンと同じ（``&`` と末尾の ``(&N)`` を除いても比べる）。
    選ぶと ``QMenu.exec()`` がその項目を返す（:func:`activate_menu_action`）。
    """
    available: list[str] = []
    for action in menu.actions():
        if action.isSeparator() or not action.isVisible():
            continue
        text = action.text()
        available.append(text)
        if action.isEnabled() and item in label_forms(text):
            activate_menu_action(menu, action)
            return {"success": True}
    return {
        "success": False,
        "error": "menu_item_not_found",
        "item": item,
        "available": available,
        "menu": menu.title() or menu.objectName(),
    }


def tick(iface: Any = None) -> dict:
    """1 回分の polling サイクル。Worker 側 QTimer の timeout から呼ばれる。

    Returns:
        ``{"applied": bool, "queued": bool, "handler": str | None,
        "action": str | None, "modal_class": str | None}``。発火が無かった場合
        ``applied=False``。action は積むだけなので、``applied`` は「積んだ」の意味。

    iface は handler 関数規約に合わせて受け取るが使わない。
    """
    del iface
    app = QApplication.instance()
    if app is None or len(_REGISTRY) == 0:
        # handler が無ければ何もしない（ホストの窓に印の property を書かない）
        return {"applied": False}

    # ポップアップは、開いていれば一番上にある（モーダルの上に開くこともある）
    popup = app.activePopupWidget()
    modal = popup if popup is not None else app.activeModalWidget()
    modal_id = _modal_key(modal) if modal is not None else None
    record = _modal_to_record(modal, popup=popup is not None)

    spec = _REGISTRY.select_for_modal(modal_id, record)
    if spec is None:
        return {"applied": False}

    success = _apply_action(modal, spec)
    if not success and spec.once:
        # 当てられなかった once の handler は使い切らない（本来の窓のために残す）
        _REGISTRY.restore(spec)
    return {
        "applied": bool(success),
        "queued": bool(success),
        "handler": spec.name,
        "action": spec.action,
        "modal_class": (record or {}).get("class"),
        "modal_object_name": (record or {}).get("object_name"),
    }
