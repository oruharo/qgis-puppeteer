"""snapshot_ui がリスト・表・ツリーの中身を返す（#12）。

実 Qt widget を作るので、``qgis.PyQt`` が import できる環境（QGIS の Python、
``scripts/qt_tests.py``）でだけ走る。無ければ全件 skip。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("qgis.PyQt.QtWidgets")
QtCore = pytest.importorskip("qgis.PyQt.QtCore")
QtGui = pytest.importorskip("qgis.PyQt.QtGui")

from qgis_puppeteer.qgis_tools.ui_tools import (  # noqa: E402
    _MAX_ITEM_COLUMNS,
    _MAX_ITEM_ROWS,
    snapshot_ui,
)

QApplication = QtWidgets.QApplication
QDialog = QtWidgets.QDialog
QVBoxLayout = QtWidgets.QVBoxLayout
Qt = QtCore.Qt


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(app: Any) -> Iterator[Any]:
    dlg = QDialog()
    dlg.setObjectName("viewsDialog")
    QVBoxLayout(dlg)
    try:
        yield dlg
    finally:
        dlg.close()
        dlg.deleteLater()
        app.processEvents()


def _add(dialog: Any, widget: Any, name: str) -> Any:
    widget.setObjectName(name)
    dialog.layout().addWidget(widget)
    return widget


def _node(dialog: Any, app: Any, name: str) -> dict:
    dialog.show()
    app.processEvents()
    snap = snapshot_ui()
    [root] = [d for d in snap["visible_dialogs"] if d.get("object_name") == "viewsDialog"]
    found = _find(root, name)
    assert found is not None, name
    return found


def _find(node: Any, name: str) -> dict | None:
    if not isinstance(node, dict):
        return None
    if node.get("object_name") == name:
        return node
    for child in node.get("children") or ():
        hit = _find(child, name)
        if hit is not None:
            return hit
    return None


def _all(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for child in node.get("children") or ():
            yield from _all(child)


def test_list_widget_items_and_current_row(dialog: Any, app: Any) -> None:
    lw = _add(dialog, QtWidgets.QListWidget(), "layers")
    lw.addItems(["roads", "rivers", "parcels"])
    lw.setCurrentRow(1)
    node = _node(dialog, app, "layers")
    assert node["items"] == ["roads", "rivers", "parcels"]
    assert node["current_row"] == 1
    assert node["row_count"] == 3
    assert "truncated" not in node


def test_empty_list_has_no_current_row(dialog: Any, app: Any) -> None:
    _add(dialog, QtWidgets.QListWidget(), "empty")
    node = _node(dialog, app, "empty")
    assert node["items"] == []
    assert node["current_row"] is None


def test_table_widget_columns_rows_and_current(dialog: Any, app: Any) -> None:
    tw = _add(dialog, QtWidgets.QTableWidget(2, 2), "attrs")
    tw.setHorizontalHeaderLabels(["name", "area"])
    for r, (name, area) in enumerate([("A", 12), ("B", 3.5)]):
        tw.setItem(r, 0, QtWidgets.QTableWidgetItem(name))
        item = QtWidgets.QTableWidgetItem()
        item.setData(Qt.ItemDataRole.DisplayRole, area)
        tw.setItem(r, 1, item)
    tw.setCurrentCell(1, 0)
    node = _node(dialog, app, "attrs")
    assert node["columns"] == ["name", "area"]
    assert node["rows"] == [["A", 12], ["B", 3.5]]
    assert node["current"] == {"row": 1, "column": 0}
    assert node["row_count"] == 2
    # 表の見出し（QHeaderView）は同じモデルを持つが、中身を二重に出さない
    headers = [n for n in _all(node) if n.get("class") == "QHeaderView"]
    assert headers
    assert all("rows" not in h and "items" not in h for h in headers)


def test_tree_widget_rows_are_the_top_level(dialog: Any, app: Any) -> None:
    tree = _add(dialog, QtWidgets.QTreeWidget(), "tree")
    tree.setHeaderLabels(["group"])
    top = QtWidgets.QTreeWidgetItem(["Base maps"])
    top.addChild(QtWidgets.QTreeWidgetItem(["OSM"]))
    tree.addTopLevelItem(top)
    tree.addTopLevelItem(QtWidgets.QTreeWidgetItem(["Data"]))
    node = _node(dialog, app, "tree")
    assert node["columns"] == ["group"]
    assert node["rows"] == [["Base maps"], ["Data"]]


def test_tree_widget_shows_the_rows_of_open_groups(dialog: Any, app: Any) -> None:
    """開いている段の子の行は child_rows、子があって畳まれた行は collapsed_rows（#30）。"""
    tree = _add(dialog, QtWidgets.QTreeWidget(), "tree")
    tree.setHeaderLabels(["group"])
    base = QtWidgets.QTreeWidgetItem(["Base maps"])
    osm = QtWidgets.QTreeWidgetItem(["OSM"])
    osm.addChild(QtWidgets.QTreeWidgetItem(["tiles"]))
    base.addChild(osm)
    base.addChild(QtWidgets.QTreeWidgetItem(["Aerial"]))
    data = QtWidgets.QTreeWidgetItem(["Data"])
    data.addChild(QtWidgets.QTreeWidgetItem(["roads"]))
    tree.addTopLevelItem(base)
    tree.addTopLevelItem(data)
    tree.addTopLevelItem(QtWidgets.QTreeWidgetItem(["Empty"]))
    tree.expandItem(base)
    tree.setCurrentItem(osm)

    node = _node(dialog, app, "tree")

    assert node["rows"] == [["Base maps"], ["Data"], ["Empty"]]  # rows の形は変えない
    assert node["child_rows"] == [
        {"path": [0], "rows": [["OSM"], ["Aerial"]], "row_count": 2},
    ]
    assert node["collapsed_rows"] == [[0, 0], [1]]
    assert node["current"] == {"row": 0, "column": 0, "path": [0, 0]}
    assert "truncated" not in node


def test_tree_child_rows_share_the_row_limit(dialog: Any, app: Any) -> None:
    tree = _add(dialog, QtWidgets.QTreeWidget(), "tree")
    top = QtWidgets.QTreeWidgetItem(["top"])
    for i in range(150):
        top.addChild(QtWidgets.QTreeWidgetItem([f"c{i}"]))
    tree.addTopLevelItem(top)
    tree.expandItem(top)

    node = _node(dialog, app, "tree")

    [block] = node["child_rows"]
    assert block["row_count"] == 150
    assert len(block["rows"]) == 99  # rows の 1 行と合わせて 100 行
    assert node["truncated"] is True


def test_hidden_header_gives_no_columns(dialog: Any, app: Any) -> None:
    """見出しを隠したツリー（QGIS のレイヤパネルなど）の見出しは Qt の既定の連番なので出さない。"""
    tree = _add(dialog, QtWidgets.QTreeWidget(), "layer_tree")
    tree.setHeaderHidden(True)
    tree.addTopLevelItem(QtWidgets.QTreeWidgetItem(["parcels"]))
    node = _node(dialog, app, "layer_tree")
    assert "columns" not in node
    assert node["rows"] == [["parcels"]]


def test_model_values_become_json(dialog: Any, app: Any) -> None:
    model = QtGui.QStandardItemModel(1, 3)
    date_item = QtGui.QStandardItem()
    date_item.setData(QtCore.QDate(2026, 10, 20), Qt.ItemDataRole.DisplayRole)
    model.setItem(0, 0, date_item)
    flag_item = QtGui.QStandardItem()
    flag_item.setData(True, Qt.ItemDataRole.DisplayRole)
    model.setItem(0, 1, flag_item)
    # (0, 2) は空のまま
    view = _add(dialog, QtWidgets.QTableView(), "model_view")
    view.setModel(model)
    node = _node(dialog, app, "model_view")
    assert node["rows"] == [["2026-10-20", True, None]]
    json.dumps(node)  # 全部 JSON にできる


def test_large_views_are_truncated(dialog: Any, app: Any) -> None:
    lw = _add(dialog, QtWidgets.QListWidget(), "big_list")
    lw.addItems([f"item {i}" for i in range(_MAX_ITEM_ROWS + 50)])
    wide = _add(dialog, QtWidgets.QTableWidget(1, _MAX_ITEM_COLUMNS + 5), "wide")
    del wide
    big = _node(dialog, app, "big_list")
    assert len(big["items"]) == _MAX_ITEM_ROWS
    assert big["row_count"] == _MAX_ITEM_ROWS + 50
    assert big["truncated"] is True
    wide_node = _node(dialog, app, "wide")
    assert len(wide_node["columns"]) == _MAX_ITEM_COLUMNS
    assert wide_node["column_count"] == _MAX_ITEM_COLUMNS + 5
    assert wide_node["truncated"] is True
