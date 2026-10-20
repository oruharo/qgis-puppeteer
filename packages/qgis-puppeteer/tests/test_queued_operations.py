"""操作を積んですぐ返す ``wait=False`` と、ボタンを押す dialog handler（#21）。

押すと ``exec()`` の確認を出すボタンを、実際の Qt で押して確かめる。実 Qt widget
を作るので、``qgis.PyQt`` が import できる環境（QGIS の Python、
``scripts/qt_tests.py``）でだけ走る。無ければ全件 skip。

確認が閉じないと ``exec()`` から戻らずテストが止まるので、各テストには
数秒で残ったモーダルを閉じる安全装置を付けている（閉じたらテストは落ちる）。
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

from qgis_puppeteer.qgis_tools import (  # noqa: E402
    dialog_handler_tools,
    exception_recorder,
    queued,
)
from qgis_puppeteer.qgis_tools.ui_tools import click_widget, set_widget_value  # noqa: E402

QApplication = QtWidgets.QApplication
QMessageBox = QtWidgets.QMessageBox

_SAFETY_MS = 5000
_YES_NO = QMessageBox.Yes | QMessageBox.No


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(app: Any) -> Iterator[dict[str, Any]]:
    """窓と、dialog handler の見回り（本番の plugin と同じく QTimer で tick）。"""
    exception_recorder.clear()
    dialog_handler_tools.get_registry().clear()
    window = QtWidgets.QWidget()
    window.setObjectName("host")
    layout = QtWidgets.QVBoxLayout(window)
    window.show()
    ticker = QtCore.QTimer()
    ticker.setInterval(20)
    ticker.timeout.connect(dialog_handler_tools.tick)
    ticker.start()
    safety = QtCore.QTimer()
    safety.setSingleShot(True)
    safety.timeout.connect(_close_leftover_modal)
    safety.start(_SAFETY_MS)
    try:
        yield {
            "app": app,
            "window": window,
            "layout": layout,
            "ticker": ticker,
            "rescued": _RESCUED,
        }
    finally:
        safety.stop()
        ticker.stop()
        _RESCUED.clear()
        dialog_handler_tools.get_registry().clear()
        window.close()
        window.deleteLater()
        # processEvents だけでは消えないことがある。閉じた "host" が残ると、次の
        # テストの root_object_name が 2 つの窓に当たる
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()
        exception_recorder.clear()


_RESCUED: list[str] = []


def _close_leftover_modal() -> None:
    modal = QApplication.activeModalWidget()
    if modal is not None:
        _RESCUED.append(type(modal).__name__)
        if hasattr(modal, "reject"):
            modal.reject()
        else:
            modal.close()


def _add(env: dict[str, Any], widget: Any) -> None:
    """表示中の窓に足した widget は、イベントを回すまで見えないので回す。"""
    env["layout"].addWidget(widget)
    env["app"].processEvents()


def _pump_until(app: Any, done: Callable[[], bool], timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if done():
            return True
        time.sleep(0.01)
    return done()


def _confirm_button(env: dict[str, Any], answers: list[Any]) -> Any:
    """押すと QMessageBox.question を exec() で出し、答えを ``answers`` に積むボタン。"""
    button = QtWidgets.QPushButton("delete")
    button.setObjectName("deleteButton")

    def on_click() -> None:
        answers.append(QMessageBox.question(env["window"], "Confirm", "Delete?", _YES_NO))

    button.clicked.connect(on_click)
    _add(env, button)
    return button


def test_click_wait_false_returns_before_the_confirmation_and_a_button_handler_answers(
    env: dict[str, Any],
) -> None:
    answers: list[Any] = []
    _confirm_button(env, answers)
    dialog_handler_tools.register_dialog_handler(
        {"name": "yes", "predicate": {"class": "QMessageBox"}, "action": "button", "button": "Yes"}
    )

    result = click_widget({"object_name": "deleteButton", "root_object_name": "host"}, wait=False)

    assert result["success"] is True
    assert result["queued"] is True
    assert answers == []  # まだ押していない（積んだだけ）
    assert _pump_until(env["app"], lambda: bool(answers))
    assert answers == [QMessageBox.Yes]
    assert env["rescued"] == []
    assert exception_recorder.get_recent() == []


@pytest.mark.parametrize(
    ("label", "button"),
    [
        ("Save as &new", "Save as new"),
        # 日本語の Qt の標準ボタンの文言の形
        ("いいえ(&N)", "いいえ"),
    ],
)
def test_button_handler_matches_the_button_text(
    env: dict[str, Any], label: str, button: str
) -> None:
    """標準ボタンでないボタンは文言で押す（``&`` と末尾の ``(&N)`` は無視）。"""
    chosen: list[str] = []

    def ask() -> None:
        box = QMessageBox(env["window"])
        box.setWindowTitle("Save")
        box.addButton(label, QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        chosen.append(box.clickedButton().text())

    dialog_handler_tools.register_dialog_handler(
        {"name": "new", "predicate": {"title": "Save"}, "action": "button", "button": button}
    )
    QtCore.QTimer.singleShot(0, ask)

    assert _pump_until(env["app"], lambda: bool(chosen))
    assert chosen == [label]
    assert env["rescued"] == []


def test_button_handler_presses_a_standard_button_of_a_dialog_button_box(
    env: dict[str, Any],
) -> None:
    """QMessageBox でない QDialog でも、QDialogButtonBox の標準ボタンを押せる。"""
    results: list[int] = []

    def ask() -> None:
        dialog = QtWidgets.QDialog(env["window"])
        dialog.setObjectName("optionsDialog")
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, dialog
        )
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        results.append(dialog.exec())

    dialog_handler_tools.register_dialog_handler(
        {
            "name": "ok",
            "predicate": {"object_name": "optionsDialog"},
            "action": "button",
            "button": "Ok",
        }
    )
    QtCore.QTimer.singleShot(0, ask)

    assert _pump_until(env["app"], lambda: bool(results))
    assert results == [QtWidgets.QDialog.Accepted]
    assert env["rescued"] == []


def test_a_missing_button_leaves_the_dialog_and_is_recorded(env: dict[str, Any]) -> None:
    answers: list[Any] = []
    _confirm_button(env, answers)
    dialog_handler_tools.register_dialog_handler(
        {"name": "ok", "predicate": {"class": "QMessageBox"}, "action": "button", "button": "Ok"}
    )

    click_widget({"object_name": "deleteButton", "root_object_name": "host"}, wait=False)

    # 確認は開いたまま（exec() の中）なので、安全装置が閉じるまで戻ってこない
    assert _pump_until(env["app"], lambda: bool(answers), timeout_s=_SAFETY_MS / 1000 + 2)
    assert env["rescued"] == ["QMessageBox"]
    records = exception_recorder.get_recent()
    assert len(records) == 1
    assert records[0]["type"] == queued.QUEUED_FAILURE_TYPE
    assert "'ok'" in records[0]["message"]
    assert "dialog_button_not_found" in records[0]["message"]


def test_a_modal_that_stays_open_is_handled_once(env: dict[str, Any]) -> None:
    """同じ modal には、Python の wrapper が替わっても 2 度目は発火しない。

    PyQt は ``activeModalWidget()`` のたびに別の wrapper を返しうる。間に別の
    wrapper を作って番地の使い回しを崩し、``id()`` が替わる状況を作る。
    """
    seconds: list[dict] = []

    def ask() -> None:
        QMessageBox.question(env["window"], "Stay", "Open?", _YES_NO)

    def probe() -> None:
        dialog_handler_tools.tick()
        junk = [QtCore.QObject() for _ in range(200)]
        seconds.append(dialog_handler_tools.tick())
        del junk
        QApplication.activeModalWidget().reject()

    dialog_handler_tools.register_dialog_handler(
        {"name": "ok", "predicate": {"title": "Stay"}, "action": "button", "button": "Ok"}
    )
    QtCore.QTimer.singleShot(0, ask)
    QtCore.QTimer.singleShot(100, probe)

    assert _pump_until(env["app"], lambda: bool(seconds))
    assert seconds[0]["applied"] is False
    env["app"].processEvents()
    assert len(exception_recorder.get_recent()) == 1


def test_a_modal_closed_before_the_queued_action_is_left_alone(env: dict[str, Any]) -> None:
    """積んでから動くまでの間に閉じた modal には何もせず、失敗にもしない。"""
    done: list[bool] = []

    def ask() -> None:
        QMessageBox.question(env["window"], "Gone", "Open?", _YES_NO)
        done.append(True)

    def probe() -> None:
        assert dialog_handler_tools.tick()["queued"] is True
        QApplication.activeModalWidget().reject()  # 積んだ action より先に閉じる

    env["ticker"].stop()  # tick はこのテストが自分で呼ぶ
    # 押すボタンが無い handler。動けば dialog_button_not_found が残るはず
    dialog_handler_tools.register_dialog_handler(
        {"name": "ok", "predicate": {"title": "Gone"}, "action": "button", "button": "Ok"}
    )
    env["app"].processEvents()
    QtCore.QTimer.singleShot(0, ask)
    QtCore.QTimer.singleShot(50, probe)

    assert _pump_until(env["app"], lambda: bool(done))
    env["app"].processEvents()
    assert exception_recorder.get_recent() == []


def test_accept_handler_is_queued_too(env: dict[str, Any]) -> None:
    """accept / reject / close も tick の中ではなく、積んでから行う。"""
    results: list[int] = []

    def ask() -> None:
        dialog = QtWidgets.QDialog(env["window"])
        dialog.setObjectName("plainDialog")
        results.append(dialog.exec())

    dialog_handler_tools.register_dialog_handler(
        {"name": "acc", "predicate": {"object_name": "plainDialog"}, "action": "accept"}
    )
    QtCore.QTimer.singleShot(0, ask)

    assert _pump_until(env["app"], lambda: bool(results))
    assert results == [QtWidgets.QDialog.Accepted]


def test_set_value_wait_false_reads_the_prior_value_and_applies_later(env: dict[str, Any]) -> None:
    combo = QtWidgets.QComboBox()
    combo.setObjectName("mode")
    combo.addItems(["a", "b"])
    _add(env, combo)
    answers: list[Any] = []
    combo.currentIndexChanged.connect(
        lambda _i: answers.append(
            QMessageBox.question(env["window"], "Switch", "Discard?", _YES_NO)
        )
    )
    dialog_handler_tools.register_dialog_handler(
        {"name": "no", "predicate": {"title": "Switch"}, "action": "button", "button": "No"}
    )

    result = set_widget_value({"object_name": "mode", "root_object_name": "host"}, "b", wait=False)

    assert result["success"] is True
    assert result["queued"] is True
    assert result["prior_value"] == "a"
    assert combo.currentText() == "a"
    assert _pump_until(env["app"], lambda: bool(answers))
    assert answers == [QMessageBox.No]
    assert combo.currentText() == "b"
    assert exception_recorder.get_recent() == []


def test_set_value_wait_false_records_a_failure_found_while_applying(env: dict[str, Any]) -> None:
    """入れたあとに分かる失敗（読み直した値が違う、#23）は記録に残る。"""
    spin = QtWidgets.QSpinBox()
    spin.setObjectName("count")
    spin.setRange(0, 9)
    _add(env, spin)

    result = set_widget_value({"object_name": "count", "root_object_name": "host"}, 50, wait=False)

    assert result["success"] is True
    assert _pump_until(env["app"], lambda: bool(exception_recorder.get_recent()))
    record = exception_recorder.get_recent()[0]
    assert record["type"] == queued.QUEUED_FAILURE_TYPE
    assert "qgis_set_widget_value" in record["message"]
    assert "value_not_applied" in record["message"]


def test_set_value_wait_false_returns_a_missing_item_at_once(env: dict[str, Any]) -> None:
    """候補に無い文字は、入れる前に分かるのでその場で返る（#23）。"""
    combo = QtWidgets.QComboBox()
    combo.setObjectName("mode")
    combo.addItems(["a", "b"])
    _add(env, combo)

    result = set_widget_value(
        {"object_name": "mode", "root_object_name": "host"}, "missing", wait=False
    )

    assert result["success"] is False
    assert result["error"] == "combo_item_not_found"


def test_set_value_wait_false_still_refuses_what_it_can_check_now(env: dict[str, Any]) -> None:
    edit = QtWidgets.QLineEdit()
    edit.setObjectName("ro")
    edit.setReadOnly(True)
    _add(env, edit)

    result = set_widget_value({"object_name": "ro", "root_object_name": "host"}, "x", wait=False)

    assert result["success"] is False
    assert result["error"] == "widget_readonly"


def test_a_widget_deleted_before_the_queued_click_is_recorded_not_touched(
    env: dict[str, Any],
) -> None:
    """積んでから動くまでに消えた widget には触らない（触ると落ちる）。記録は残す。"""
    sip = pytest.importorskip("qgis.PyQt.sip")
    button = QtWidgets.QPushButton("x")
    button.setObjectName("doomed")
    _add(env, button)

    result = click_widget({"object_name": "doomed", "root_object_name": "host"}, wait=False)
    sip.delete(button)

    assert result["queued"] is True
    assert _pump_until(env["app"], lambda: bool(exception_recorder.get_recent()))
    record = exception_recorder.get_recent()[0]
    assert record["type"] == queued.QUEUED_FAILURE_TYPE
    assert "deleted before it ran" in record["message"]


def test_run_later_records_an_exception(env: dict[str, Any]) -> None:
    def boom() -> None:
        raise ValueError("bad")

    queued.run_later(boom, what="qgis_click_widget")

    assert _pump_until(env["app"], lambda: bool(exception_recorder.get_recent()))
    record = exception_recorder.get_recent()[0]
    assert record["type"] == "ValueError"
    assert record["message"] == "queued qgis_click_widget: bad"
    assert "boom" in record["traceback"]


def test_wait_true_is_unchanged(env: dict[str, Any]) -> None:
    clicked: list[bool] = []
    button = QtWidgets.QPushButton("plain")
    button.setObjectName("plain")
    button.clicked.connect(lambda: clicked.append(True))
    _add(env, button)

    result = click_widget({"object_name": "plain", "root_object_name": "host"})

    assert result == {"success": True, "widget": result["widget"]}
    assert clicked == [True]
