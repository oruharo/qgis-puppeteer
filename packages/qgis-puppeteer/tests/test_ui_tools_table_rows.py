"""表・ツリーの行の選択、ダブルクリック、右クリックのメニュー（#24）。

実 Qt widget を作るので、``qgis.PyQt`` が import できる環境（QGIS の Python、
``scripts/qt_tests.py``）でだけ走る。無ければ全件 skip。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("qgis.PyQt.QtWidgets")
QtCore = pytest.importorskip("qgis.PyQt.QtCore")
QtGui = pytest.importorskip("qgis.PyQt.QtGui")

from qgis_puppeteer.qgis_tools import exception_recorder  # noqa: E402
from qgis_puppeteer.qgis_tools.ui_tools import click_widget, set_widget_value  # noqa: E402

QApplication = QtWidgets.QApplication
Qt = QtCore.Qt

ROWS = [("alpha", "point"), ("beta", "line"), ("gamma", "point")]
HEADERS = ["名前", "種別"]


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
    exception_recorder.clear()
    try:
        yield window
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()
        exception_recorder.clear()


def _add(host: Any, widget: Any, name: str = "w") -> Any:
    widget.setObjectName(name)
    host.layout().addWidget(widget)
    QApplication.processEvents()
    return widget


def _table(host: Any, name: str = "w") -> Any:
    table = QtWidgets.QTableWidget(len(ROWS), len(HEADERS))
    table.setHorizontalHeaderLabels(HEADERS)
    for r, cells in enumerate(ROWS):
        for c, text in enumerate(cells):
            table.setItem(r, c, QtWidgets.QTableWidgetItem(text))
    return _add(host, table, name)


def _sel(name: str = "w") -> dict:
    return {"object_name": name, "root_object_name": "host"}


def _selected_rows(view: Any) -> list[int]:
    return sorted({i.row() for i in view.selectionModel().selectedRows()})


# ------------------------------------------------------------
# 行を選ぶ
# ------------------------------------------------------------


def test_a_row_number_selects_the_whole_row_and_fires_the_signals(host: Any) -> None:
    table = _table(host)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectItems)  # それでも行全体
    seen: list[Any] = []
    table.currentCellChanged.connect(lambda r, c, pr, pc: seen.append(("current", r)))
    table.itemSelectionChanged.connect(lambda: seen.append("selection"))

    result = set_widget_value(_sel(), 1)

    assert result["success"] is True
    assert result["value_after"] == 1
    assert _selected_rows(table) == [1]
    assert ("current", 1) in seen
    assert "selection" in seen


@pytest.mark.parametrize(
    "value",
    [{"text": "beta"}, {"text": "beta", "column": "名前"}, {"text": "beta", "column": 0}],
)
def test_a_row_is_chosen_by_a_cell_text(host: Any, value: dict) -> None:
    table = _table(host)

    result = set_widget_value(_sel(), value)

    assert result["success"] is True
    assert result["value_after"] == 1
    assert _selected_rows(table) == [1]


def test_a_text_in_several_rows_is_ambiguous_unless_narrowed(host: Any) -> None:
    _table(host)

    result = set_widget_value(_sel(), {"text": "point"})

    assert result["error"] == "row_ambiguous"
    assert result["rows"] == [0, 2]
    assert set_widget_value(_sel(), {"text": "point", "column": "種別"})["error"] == "row_ambiguous"


def test_a_missing_text_or_column_is_refused(host: Any) -> None:
    _table(host)

    missing = set_widget_value(_sel(), {"text": "delta"})
    assert missing["error"] == "row_not_found"
    wrong_column = set_widget_value(_sel(), {"text": "beta", "column": "座標"})
    assert wrong_column["error"] == "column_not_found"
    assert wrong_column["available"] == HEADERS
    assert set_widget_value(_sel(), {"text": "beta", "column": 5})["error"] == "column_not_found"
    assert set_widget_value(_sel(), "beta")["error"] == "invalid_value"


def test_hidden_rows_are_skipped_and_refused(host: Any) -> None:
    table = _table(host)
    table.setRowHidden(1, True)

    assert set_widget_value(_sel(), {"text": "beta"})["error"] == "row_not_found"
    by_number = set_widget_value(_sel(), {"row": 1})
    assert by_number["error"] == "row_hidden"
    assert set_widget_value(_sel(), {"text": "gamma"})["value_after"] == 2


def test_row_is_the_displayed_order_after_sorting(host: Any) -> None:
    table = _table(host)
    table.setSortingEnabled(True)
    table.sortItems(0, Qt.DescendingOrder)  # gamma, beta, alpha
    QApplication.processEvents()

    result = set_widget_value(_sel(), 0)

    assert result["value_after"] == 0
    assert table.item(table.currentRow(), 0).text() == "gamma"
    assert set_widget_value(_sel(), {"text": "alpha"})["value_after"] == 2


def test_a_tree_row_is_chosen_at_the_top_level(host: Any) -> None:
    tree = QtWidgets.QTreeWidget()
    tree.setHeaderLabels(["layer"])
    for name in ("roads", "rivers"):
        item = QtWidgets.QTreeWidgetItem([name])
        item.addChild(QtWidgets.QTreeWidgetItem(["child"]))
        tree.addTopLevelItem(item)
    _add(host, tree)

    result = set_widget_value(_sel(), {"text": "rivers"})

    assert result["value_after"] == 1
    assert tree.currentItem().text(0) == "rivers"
    assert set_widget_value(_sel(), {"text": "child"})["error"] == "row_not_found"


def test_a_view_with_a_model_takes_the_same_forms(host: Any) -> None:
    model = QtGui.QStandardItemModel(0, 2)
    model.setHorizontalHeaderLabels(HEADERS)
    for cells in ROWS:
        model.appendRow([QtGui.QStandardItem(t) for t in cells])
    view = _add(host, QtWidgets.QTableView())
    view.setModel(model)
    QApplication.processEvents()

    result = set_widget_value(_sel(), {"text": "line", "column": "種別"})

    assert result["value_after"] == 1
    assert _selected_rows(view) == [1]


# ------------------------------------------------------------
# ダブルクリック・右クリック
# ------------------------------------------------------------


def test_a_double_click_on_a_cell_selects_first_then_double_clicks(host: Any) -> None:
    """人の操作と同じく、最初の押しで行が選ばれてからダブルクリックが出る。"""
    table = _table(host)
    seen: list[Any] = []
    table.currentCellChanged.connect(lambda r, c, pr, pc: seen.append(("current", r)))
    table.cellDoubleClicked.connect(lambda r, c: seen.append(("double", r, c)))

    result = click_widget(_sel(), kind="double", cell={"text": "beta"})

    assert result["success"] is True
    assert result["cell"] == {"row": 1, "column": 0}
    assert seen == [("current", 1), ("double", 1, 0)]
    assert table.currentRow() == 1  # マウスはセルを選ぶ（行全体の選択は set_widget_value）


def test_a_right_click_on_a_cell_requests_the_context_menu(host: Any) -> None:
    table = _table(host)
    table.setContextMenuPolicy(Qt.CustomContextMenu)
    seen: list[Any] = []
    table.customContextMenuRequested.connect(lambda pos: seen.append(table.indexAt(pos).row()))

    result = click_widget(_sel(), kind="right", cell={"row": 2, "column": "種別"})

    assert result["success"] is True
    assert result["cell"] == {"row": 2, "column": 1}
    assert seen == [2]
    assert table.currentRow() == 2  # 右クリックでも行が選ばれる


def test_a_view_double_click_emits_double_clicked(host: Any) -> None:
    model = QtCore.QStringListModel(["a", "b", "c"])
    view = _add(host, QtWidgets.QListView())
    view.setModel(model)
    QApplication.processEvents()
    seen: list[int] = []
    view.doubleClicked.connect(lambda index: seen.append(index.row()))

    result = click_widget(_sel(), kind="double", cell=2)

    assert result["cell"] == {"row": 2, "column": 0}
    assert seen == [2]


def test_a_right_click_without_a_cell_goes_to_the_widget(host: Any) -> None:
    label = _add(host, QtWidgets.QLabel("x"))
    label.setContextMenuPolicy(Qt.CustomContextMenu)
    seen: list[Any] = []
    label.customContextMenuRequested.connect(lambda pos: seen.append(pos))

    result = click_widget(_sel(), kind="right")

    assert result["success"] is True
    assert "cell" not in result
    assert len(seen) == 1


def test_a_plain_left_click_still_uses_click(host: Any) -> None:
    button = _add(host, QtWidgets.QPushButton("go"))
    seen: list[bool] = []
    button.clicked.connect(lambda: seen.append(True))

    assert click_widget(_sel())["success"] is True
    assert seen == [True]


def test_a_left_click_on_a_cell_selects_it_with_the_mouse(host: Any) -> None:
    """kind="left" に cell を付けるとマウスで押す（後で修飾キーを足す形）。"""
    table = _table(host)

    result = click_widget(_sel(), cell={"row": 2})

    assert result["cell"] == {"row": 2, "column": 0}
    assert table.currentRow() == 2


def test_misuse_is_refused(host: Any) -> None:
    button = _add(host, QtWidgets.QPushButton("go"))
    assert click_widget(_sel(), kind="middle")["error"] == "invalid_argument"
    assert click_widget(_sel(), cell={"row": 0})["error"] == "cell_needs_a_view"
    _table(host, "t")
    assert click_widget(_sel("t"), kind="double", cell={"row": 9})["error"] == "index_out_of_range"
    action = QtWidgets.QAction("act", button)
    action.setObjectName("act")
    button.addAction(action)
    assert click_widget(_sel("act"), kind="double")["error"] == "widget_not_clickable"


def test_wait_false_queues_the_double_click(host: Any, app: Any) -> None:
    table = _table(host)
    seen: list[int] = []
    table.cellDoubleClicked.connect(lambda r, c: seen.append(r))

    result = click_widget(_sel(), kind="double", cell={"text": "gamma"}, wait=False)

    assert result["queued"] is True
    assert result["cell"] == {"row": 2, "column": 0}
    assert seen == []
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not seen:
        app.processEvents()
        time.sleep(0.01)
    assert seen == [2]
    assert exception_recorder.get_recent() == []
