"""click で、人と同じくフォーカスを移してから押す。

表のセルのエディタは、フォーカスが外れたときに値を確定する。押す前にフォーカスを
移さないと、編集中の値が確定しないまま「保存」が走る。

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
from qgis_puppeteer.qgis_tools.ui_tools import click_widget  # noqa: E402

QApplication = QtWidgets.QApplication
Qt = QtCore.Qt


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def host(app: Any) -> Iterator[Any]:
    window = QtWidgets.QWidget()
    window.setObjectName("host")
    window.setLayout(QtWidgets.QVBoxLayout())
    window.resize(400, 300)
    window.show()
    window.activateWindow()
    QApplication.processEvents()
    exception_recorder.clear()
    try:
        yield window
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()
        exception_recorder.clear()


def _add(host: Any, widget: Any, name: str) -> Any:
    widget.setObjectName(name)
    host.layout().addWidget(widget)
    QApplication.processEvents()
    return widget


def _sel(name: str) -> dict:
    return {"object_name": name, "root_object_name": "host"}


def _editing_table(host: Any) -> tuple[Any, Any]:
    """1 行目のセルを編集中にした表と、その item を返す。"""
    table = _add(host, QtWidgets.QTableWidget(2, 1), "table")
    for r in range(2):
        table.setItem(r, 0, QtWidgets.QTableWidgetItem(f"row{r}"))
    item = table.item(0, 0)
    table.setCurrentItem(item)
    table.editItem(item)
    QApplication.processEvents()
    editor = QApplication.focusWidget()
    assert isinstance(editor, QtWidgets.QLineEdit), editor
    editor.setText("edited")  # 人がキーを打ったのと同じ（まだ確定していない）
    return table, item


def test_clicking_a_button_commits_the_cell_being_edited(host: Any) -> None:
    _, item = _editing_table(host)
    seen: list[str] = []
    button = _add(host, QtWidgets.QPushButton("保存"), "save")
    button.clicked.connect(lambda: seen.append(item.text()))

    result = click_widget(_sel("save"))

    assert result["success"] is True
    assert seen == ["edited"]  # 押した時点で、もう確定している
    assert QApplication.focusWidget() is button


def test_a_button_that_takes_no_focus_leaves_the_editor_open(host: Any) -> None:
    """ツールバーのボタンなど（NoFocus）は、人が押してもフォーカスは動かない。"""
    _, item = _editing_table(host)
    button = _add(host, QtWidgets.QPushButton("x"), "nofocus")
    button.setFocusPolicy(Qt.NoFocus)

    assert click_widget(_sel("nofocus"))["success"] is True

    assert item.text() == "row0"
    assert isinstance(QApplication.focusWidget(), QtWidgets.QLineEdit)


def test_a_queued_click_moves_the_focus_when_it_runs(host: Any) -> None:
    _, item = _editing_table(host)
    seen: list[str] = []
    button = _add(host, QtWidgets.QPushButton("保存"), "save")
    button.clicked.connect(lambda: seen.append(item.text()))

    result = click_widget(_sel("save"), wait=False)
    assert result["queued"] is True
    assert item.text() == "row0"  # 積んだだけでは、まだ動かない

    QApplication.processEvents()
    assert seen == ["edited"]


def test_clicking_a_cell_of_another_view_commits_the_edit(host: Any) -> None:
    """マウスのイベントで押す経路（cell 付き）でも、人と同じくフォーカスが移る。"""
    _, item = _editing_table(host)
    other = _add(host, QtWidgets.QListWidget(), "other")
    other.addItems(["a", "b"])

    result = click_widget(_sel("other"), cell={"row": 1})

    assert result["success"] is True
    assert item.text() == "edited"
    assert QApplication.focusWidget() is other
    assert other.currentRow() == 1
