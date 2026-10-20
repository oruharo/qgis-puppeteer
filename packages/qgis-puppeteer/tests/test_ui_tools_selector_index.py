"""selector の index と root_object_name（#20）。

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

from qgis_puppeteer.qgis_tools.ui_tools import (  # noqa: E402
    _find_widget,
    set_widget_value,
)

QApplication = QtWidgets.QApplication
Qt = pytest.importorskip("qgis.PyQt.QtCore").Qt


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app: Any) -> Iterator[dict[str, Any]]:
    """名前の無い QTabWidget が 2 つある窓（1 つは名前付きのドックの中）。"""
    main = QtWidgets.QMainWindow()
    main.setObjectName("hostMain")
    central = QtWidgets.QTabWidget()
    for name in ("a", "b"):
        central.addTab(QtWidgets.QWidget(), name)
    main.setCentralWidget(central)
    dock = QtWidgets.QDockWidget("panel", main)
    dock.setObjectName("hostDock")
    docked = QtWidgets.QTabWidget()
    for name in ("x", "y"):
        docked.addTab(QtWidgets.QWidget(), name)
    dock.setWidget(docked)
    main.addDockWidget(Qt.LeftDockWidgetArea, dock)
    main.show()
    app.processEvents()
    try:
        yield {"main": main, "central": central, "docked": docked}
    finally:
        main.close()
        main.deleteLater()
        app.processEvents()


def test_index_picks_the_nth_match_even_when_the_summaries_are_equal(window: Any) -> None:
    base = {"class": "QTabWidget", "root_object_name": "hostMain"}
    first, _ = _find_widget({**base, "index": 0})
    second, _ = _find_widget({**base, "index": 1})
    assert first is not None and second is not None
    assert first is not second  # 以前は index=1 でも 1 つ目に戻っていた
    assert {id(first), id(second)} == {id(window["central"]), id(window["docked"])}


def test_set_widget_value_acts_on_the_chosen_one(window: Any) -> None:
    base = {"class": "QTabWidget", "root_object_name": "hostMain"}
    second, _ = _find_widget({**base, "index": 1})
    other = window["central"] if second is window["docked"] else window["docked"]
    assert set_widget_value({**base, "index": 1}, 1)["success"] is True
    assert second.currentIndex() == 1
    assert other.currentIndex() == 0


def test_root_object_name_can_name_a_widget_inside_a_window(window: Any) -> None:
    """ドックの中の widget を根にして、重複を根で絞り込める。"""
    found, diagnostics = _find_widget({"class": "QTabWidget", "root_object_name": "hostDock"})
    assert found is window["docked"], diagnostics


def test_root_object_name_naming_several_widgets_is_ambiguous(window: Any, app: Any) -> None:
    window["central"].widget(0).setObjectName("same")
    window["docked"].widget(0).setObjectName("same")
    found, diagnostics = _find_widget({"class": "QWidget", "root_object_name": "same"})
    assert found is None
    assert diagnostics["reason"] == "selector_ambiguous"
    assert diagnostics["match_count"] == 2


def test_a_hidden_widget_is_not_a_root(window: Any, app: Any) -> None:
    """閉じたドック（や、閉じたあとまだ消えていない窓の中の widget）は根にしない。"""
    window["main"].findChild(QtWidgets.QDockWidget, "hostDock").hide()
    app.processEvents()
    found, diagnostics = _find_widget({"class": "QTabWidget", "root_object_name": "hostDock"})
    assert found is None
    assert diagnostics["reason"] == "no_root_widget"
