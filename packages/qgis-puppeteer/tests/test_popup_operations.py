"""ポップアップのメニュー（``QMenu.exec()``）を E2E で扱う（#22）。

snapshot の ``active_popup``、``scope="popup"`` の項目の click、ポップアップに
答える dialog handler を、実際の Qt で確かめる。``qgis.PyQt`` が import できる
環境（QGIS の Python、``scripts/qt_tests.py``）でだけ走る。無ければ全件 skip。

メニューが閉じないと ``exec()`` から戻らずテストが止まるので、各テストには
数秒で残ったポップアップ・モーダルを閉じる安全装置を付けている（閉じたら
テストは落ちる）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("qgis.PyQt.QtWidgets")
QtCore = pytest.importorskip("qgis.PyQt.QtCore")

from qgis_puppeteer.qgis_tools import dialog_handler_tools, exception_recorder  # noqa: E402
from qgis_puppeteer.qgis_tools.ui_tools import (  # noqa: E402
    check_actionability,
    click_widget,
    snapshot_ui,
)

QApplication = QtWidgets.QApplication
QMessageBox = QtWidgets.QMessageBox

_SAFETY_MS = 5000


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


_RESCUED: list[str] = []


def _close_leftovers() -> None:
    for get in (QApplication.activePopupWidget, QApplication.activeModalWidget):
        widget = get()
        if widget is not None:
            _RESCUED.append(type(widget).__name__)
            widget.close()


@pytest.fixture
def env(app: Any) -> Iterator[dict[str, Any]]:
    """窓と、dialog handler の見回り（本番の plugin と同じく QTimer で tick）。"""
    exception_recorder.clear()
    dialog_handler_tools.get_registry().clear()
    window = QtWidgets.QWidget()
    window.setObjectName("host")
    window.show()
    ticker = QtCore.QTimer()
    ticker.setInterval(20)
    ticker.timeout.connect(dialog_handler_tools.tick)
    ticker.start()
    safety = QtCore.QTimer()
    safety.setSingleShot(True)
    safety.timeout.connect(_close_leftovers)
    safety.start(_SAFETY_MS)
    try:
        yield {"app": app, "window": window, "rescued": _RESCUED}
    finally:
        safety.stop()
        ticker.stop()
        _RESCUED.clear()
        dialog_handler_tools.get_registry().clear()
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()
        exception_recorder.clear()


def _pump_until(app: Any, done: Callable[[], bool], timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if done():
            return True
        time.sleep(0.01)
    return done()


def _menu(env: dict[str, Any], *labels: str) -> Any:
    menu = QtWidgets.QMenu("Layer", env["window"])
    menu.setObjectName("layerMenu")
    for label in labels:
        if label == "-":
            menu.addSeparator()
        else:
            menu.addAction(label)
    return menu


def _open(menu: Any, chosen: list[str]) -> None:
    """``exec()`` でメニューを開き、返った項目の文言（閉じたら None）を積む。"""

    def run() -> None:
        action = menu.exec(QtCore.QPoint(10, 10))
        chosen.append(action.text() if action is not None else None)

    QtCore.QTimer.singleShot(0, run)


def _while_open(probe: Callable[[], None]) -> None:
    """メニューが開いてから（``exec()`` の中で）``probe`` を呼ぶ。"""
    QtCore.QTimer.singleShot(100, probe)


def test_snapshot_shows_the_open_menu_and_its_items(env: dict[str, Any]) -> None:
    menu = _menu(env, "&Copy", "-", "Delete")
    menu.actions()[-1].setEnabled(False)
    check = menu.addAction("Visible")
    check.setCheckable(True)
    check.setChecked(True)
    menu.addMenu("More")
    chosen: list[str] = []
    snaps: list[dict] = []

    def probe() -> None:
        snaps.append(snapshot_ui())
        menu.close()

    _open(menu, chosen)
    _while_open(probe)

    assert _pump_until(env["app"], lambda: bool(chosen))
    popup = snaps[0]["active_popup"]
    assert popup["class"] == "QMenu"
    assert popup["object_name"] == "layerMenu"
    assert popup["title"] == "Layer"  # QMenu.title()（#27）
    items = popup["menu_items"]
    assert items[0]["text"] == "&Copy"
    assert items[0]["enabled"] is True
    assert items[1] == {"class": "QAction", "separator": True}
    assert items[2]["enabled"] is False
    assert items[3]["checkable"] is True
    assert items[3]["checked"] is True
    assert items[4]["submenu"] is True
    assert chosen == [None]
    assert env["rescued"] == []


@pytest.mark.parametrize(("label", "text"), [("&Copy", "Copy"), ("コピー(&C)", "コピー")])
def test_clicking_a_popup_item_chooses_it_in_the_menu(
    env: dict[str, Any], label: str, text: str
) -> None:
    """``exec()`` が選んだ項目を返す（trigger() では返らない）。"""
    menu = _menu(env, label, "Delete")
    chosen: list[str] = []
    results: list[dict] = []

    def probe() -> None:
        results.append(click_widget({"scope": "popup", "class": "QAction", "text": text}))

    _open(menu, chosen)
    _while_open(probe)

    assert _pump_until(env["app"], lambda: bool(chosen))
    assert results[0]["success"] is True
    assert chosen == [label]
    assert env["rescued"] == []


def test_items_of_a_closed_submenu_are_not_candidates(env: dict[str, Any]) -> None:
    """開いているメニューの項目だけを見る。下位メニューの同じ文言と重ならない。"""
    menu = _menu(env, "Copy")
    sub = menu.addMenu("More")
    sub.addAction("Copy")
    chosen: list[str] = []
    checks: list[dict] = []

    def probe() -> None:
        checks.append(check_actionability({"scope": "popup", "text": "Copy"}))
        menu.close()

    _open(menu, chosen)
    _while_open(probe)

    assert _pump_until(env["app"], lambda: bool(chosen))
    assert checks[0]["actionable"] is True  # selector_ambiguous にならない


def test_a_popup_item_is_actionable_and_a_disabled_one_is_not(env: dict[str, Any]) -> None:
    menu = _menu(env, "Copy", "Delete")
    menu.actions()[1].setEnabled(False)
    chosen: list[str] = []
    checks: list[dict] = []

    def probe() -> None:
        checks.append(check_actionability({"scope": "popup", "text": "Copy"}))
        checks.append(check_actionability({"scope": "popup", "text": "Delete"}))
        menu.close()

    _open(menu, chosen)
    _while_open(probe)

    assert _pump_until(env["app"], lambda: bool(chosen))
    assert checks[0]["actionable"] is True
    assert checks[1]["exists"] is True
    assert checks[1]["actionable"] is False


def test_popup_scope_without_a_popup(env: dict[str, Any]) -> None:
    result = click_widget({"scope": "popup", "text": "Copy"})
    assert result["success"] is False
    assert result["diagnostics"]["reason"] == "no_popup"


def test_a_popup_handler_chooses_an_item_and_a_dialog_handler_answers_what_follows(
    env: dict[str, Any],
) -> None:
    """右クリックのメニューで「削除」を選び、続く確認に「はい」で答える流れ。"""
    menu = _menu(env, "Copy", "削除(&D)")
    answers: list[Any] = []

    def run() -> None:
        action = menu.exec(QtCore.QPoint(10, 10))
        if action is not None and action.text() == "削除(&D)":
            answers.append(
                QMessageBox.question(
                    env["window"], "Confirm", "Delete?", QMessageBox.Yes | QMessageBox.No
                )
            )

    dialog_handler_tools.register_dialog_handler(
        {
            "name": "menu",
            "predicate": {"popup": True, "title": "Layer"},
            "action": "button",
            "button": "削除",
        }
    )
    dialog_handler_tools.register_dialog_handler(
        {"name": "yes", "predicate": {"title": "Confirm"}, "action": "button", "button": "Yes"}
    )
    QtCore.QTimer.singleShot(0, run)

    assert _pump_until(env["app"], lambda: bool(answers))
    assert answers == [QMessageBox.Yes]
    assert env["rescued"] == []
    assert exception_recorder.get_recent() == []


def test_a_popup_title_selector_matches_the_menu_title(env: dict[str, Any]) -> None:
    """実ツリーの selector の title も QMenu.title()（snapshot と dialog handler と同じ）。"""
    menu = _menu(env, "Copy")
    chosen: list[str] = []
    checks: list[dict] = []

    def probe() -> None:
        checks.append(check_actionability({"scope": "popup", "title": "Layer", "class": "QMenu"}))
        menu.close()

    _open(menu, chosen)
    _while_open(probe)

    assert _pump_until(env["app"], lambda: bool(chosen))
    assert checks[0]["exists"] is True


def test_a_hidden_menu_item_is_not_a_candidate(env: dict[str, Any]) -> None:
    menu = _menu(env, "Copy", "Copy")
    menu.actions()[0].setVisible(False)
    chosen: list[str] = []
    results: list[dict] = []

    def probe() -> None:
        results.append(click_widget({"scope": "popup", "class": "QAction", "text": "Copy"}))

    _open(menu, chosen)
    _while_open(probe)

    assert _pump_until(env["app"], lambda: bool(chosen))
    assert results[0]["success"] is True  # 見えない方と重ならない
    assert chosen == ["Copy"]


def test_a_once_handler_without_its_item_stays_for_the_right_menu(env: dict[str, Any]) -> None:
    """項目が無くて当てられなかった once の handler は、使い切らずに残る。"""
    first = _menu(env, "Copy")
    chosen: list[str] = []
    dialog_handler_tools.register_dialog_handler(
        {
            "name": "m",
            "predicate": {"popup": True},
            "action": "button",
            "button": "Paste",
            "once": True,
        }
    )

    def probe() -> None:
        _pump_until(env["app"], lambda: bool(exception_recorder.get_recent()), timeout_s=1.0)
        first.close()

    _open(first, chosen)
    _while_open(probe)
    assert _pump_until(env["app"], lambda: bool(chosen))

    second = _menu(env, "Copy", "Paste")
    chosen.clear()
    _open(second, chosen)
    assert _pump_until(env["app"], lambda: bool(chosen))
    assert chosen == ["Paste"]
    assert dialog_handler_tools.list_dialog_handlers({})["handlers"] == []  # 今度は使い切った


def test_no_handlers_means_nothing_is_written_to_the_modal(env: dict[str, Any]) -> None:
    """handler が無ければ、ホストの窓に印の property を書かない。"""
    dialog = QtWidgets.QDialog(env["window"])
    seen: list[Any] = []

    def probe() -> None:
        dialog_handler_tools.tick()
        seen.append(dialog.property("_qpuppeteer_modal_key"))
        dialog.reject()

    QtCore.QTimer.singleShot(0, lambda: dialog.exec())
    QtCore.QTimer.singleShot(50, probe)

    assert _pump_until(env["app"], lambda: bool(seen))
    assert seen == [None]


def test_a_catch_all_handler_leaves_popups_alone(env: dict[str, Any]) -> None:
    """``{}`` の handler は、ポップアップ（コンボのドロップダウンなど）を閉じない。"""
    menu = _menu(env, "Copy")
    chosen: list[str] = []
    still_open: list[bool] = []
    dialog_handler_tools.register_dialog_handler(
        {"name": "any", "predicate": {}, "action": "close"}
    )

    def probe() -> None:
        still_open.append(QApplication.activePopupWidget() is menu)
        menu.close()

    _open(menu, chosen)
    QtCore.QTimer.singleShot(300, probe)  # tick が何度も回ったあと

    assert _pump_until(env["app"], lambda: bool(chosen))
    assert still_open == [True]


def test_a_missing_menu_item_is_recorded(env: dict[str, Any]) -> None:
    menu = _menu(env, "Copy")
    chosen: list[str] = []
    dialog_handler_tools.register_dialog_handler(
        {"name": "m", "predicate": {"popup": True}, "action": "button", "button": "Paste"}
    )

    def recorded() -> bool:
        return bool(exception_recorder.get_recent())

    def probe() -> None:
        _pump_until(env["app"], recorded, timeout_s=1.0)
        menu.close()

    _open(menu, chosen)
    _while_open(probe)

    assert _pump_until(env["app"], lambda: bool(chosen))
    records = exception_recorder.get_recent()
    assert len(records) == 1
    assert "menu_item_not_found" in records[0]["message"]
    assert "'Copy'" in records[0]["message"]
