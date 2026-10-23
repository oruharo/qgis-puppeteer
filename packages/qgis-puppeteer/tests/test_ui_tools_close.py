"""窓を × と同じく閉じる（#34）。

実 Qt widget を作るので、``qgis.PyQt`` が import できる環境（QGIS の Python、
``scripts/qt_tests.py``）でだけ走る。無ければ全件 skip。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("qgis.PyQt.QtWidgets")
QtCore = pytest.importorskip("qgis.PyQt.QtCore")

from qgis_puppeteer.qgis_tools import exception_recorder  # noqa: E402
from qgis_puppeteer.qgis_tools.ui_tools import close_widget  # noqa: E402

QApplication = QtWidgets.QApplication


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def windows(app: Any) -> Iterator[list[Any]]:
    made: list[Any] = []
    exception_recorder.clear()
    try:
        yield made
    finally:
        for w in made:
            w.hide()
            w.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()
        exception_recorder.clear()


def _dialog(windows: list[Any], title: str) -> Any:
    dlg = QtWidgets.QDialog()
    dlg.setWindowTitle(title)
    QtWidgets.QVBoxLayout(dlg)
    ok = QtWidgets.QPushButton("OK", dlg)
    ok.setObjectName("ok")
    dlg.layout().addWidget(ok)
    dlg.show()
    QApplication.processEvents()
    windows.append(dlg)
    return dlg


def test_a_dialog_is_closed_through_its_close_event(windows: list[Any]) -> None:
    dlg = _dialog(windows, "属性")
    rejected: list[bool] = []
    dlg.rejected.connect(lambda: rejected.append(True))

    result = close_widget({"scope": "any", "class": "QDialog", "title": "属性"})

    assert result["success"] is True
    assert result["closed"] is True
    assert result["window"]["title"] == "属性"
    assert not dlg.isVisible()
    assert rejected == [True]  # QDialog は reject 相当


def test_a_widget_inside_closes_its_window(windows: list[Any]) -> None:
    dlg = _dialog(windows, "検索")

    result = close_widget({"scope": "any", "object_name": "ok"})

    assert result["window"]["title"] == "検索"
    assert not dlg.isVisible()


class _Stubborn(QtWidgets.QWidget):
    """閉じるのを断る窓（未保存の変更で「キャンセル」されたときのような）。"""

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        event.ignore()


def test_a_window_that_refuses_stays_open(windows: list[Any]) -> None:
    win = _Stubborn()
    win.setWindowTitle("編集中")
    win.show()
    QApplication.processEvents()
    windows.append(win)

    result = close_widget({"scope": "any", "title": "編集中"})

    assert result["success"] is True
    assert result["closed"] is False
    assert win.isVisible()


def test_a_queued_close_runs_later(windows: list[Any], app: Any) -> None:
    dlg = _dialog(windows, "後で")

    result = close_widget({"scope": "any", "class": "QDialog", "title": "後で"}, wait=False)

    assert result["queued"] is True
    assert dlg.isVisible()
    for _ in range(5):
        app.processEvents()
    assert not dlg.isVisible()


def test_the_main_window_is_not_closed(windows: list[Any]) -> None:
    main = QtWidgets.QMainWindow()
    main.setWindowTitle("QGIS")
    main.show()
    QApplication.processEvents()
    windows.append(main)

    class Iface:
        def mainWindow(self) -> Any:  # noqa: N802 - QgisInterface の名前
            return main

    result = close_widget({"scope": "any", "title": "QGIS"}, Iface())

    assert result["error"] == "widget_not_closable"
    assert main.isVisible()
