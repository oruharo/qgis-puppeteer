"""表・ツリー・リストの行のチェックを付け外す（``set_widget_value`` の ``cell``）。

実 Qt widget を作るので、``qgis.PyQt`` が import できる環境（QGIS の Python、
``scripts/qt_tests.py``）でだけ走る。無ければ全件 skip。QGIS のレイヤツリーの凡例の
分類は、QGIS を初期化した子プロセスで確かめる（``qgis.core`` が無ければ skip）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("qgis.PyQt.QtWidgets")
QtCore = pytest.importorskip("qgis.PyQt.QtCore")
QtGui = pytest.importorskip("qgis.PyQt.QtGui")

from qgis_puppeteer.qgis_tools import exception_recorder  # noqa: E402
from qgis_puppeteer.qgis_tools.ui_tools import read_rows, set_widget_value  # noqa: E402

QApplication = QtWidgets.QApplication
Qt = QtCore.Qt
CHECKABLE = Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def host(app: Any) -> Iterator[Any]:
    window = QtWidgets.QWidget()
    window.setObjectName("host")
    window.setLayout(QtWidgets.QVBoxLayout())
    window.resize(400, 400)
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


def _sel() -> dict:
    return {"object_name": "w", "root_object_name": "host"}


def _item(parent: Any, text: str, *, checkable: bool = True) -> Any:
    item = QtWidgets.QTreeWidgetItem([text, f"{text}-id"])
    if checkable:
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(0, Qt.Unchecked)
    if isinstance(parent, QtWidgets.QTreeWidget):
        parent.addTopLevelItem(item)
    else:
        parent.addChild(item)
    return item


def _tree(host: Any) -> Any:
    """条件 ─ 道路 ─ 国道 / 県道、条件 ─ 建物（チェック無し）。すべて畳んだまま。"""
    tree = QtWidgets.QTreeWidget()
    tree.setObjectName("w")
    tree.setHeaderLabels(["名前", "ID"])
    top = _item(tree, "条件")
    roads = _item(top, "道路")
    _item(roads, "国道")
    _item(roads, "県道")
    _item(top, "建物", checkable=False)
    host.layout().addWidget(tree)
    QApplication.processEvents()
    return tree


def _find(tree: Any, text: str) -> Any:
    return tree.findItems(text, Qt.MatchExactly | Qt.MatchRecursive)[0]


def test_a_child_row_is_checked_after_opening_its_parents(host: Any) -> None:
    tree = _tree(host)
    changed: list[tuple[str, int]] = []
    tree.itemChanged.connect(lambda item, column: changed.append((item.text(0), column)))

    result = set_widget_value(_sel(), True, cell={"path": ["条件", "道路", "国道"]})

    assert result["success"] is True
    assert result["prior_value"] == "unchecked"
    assert result["value_after"] == "checked"
    assert result["cell"] == {"row": 0, "column": 0, "path": [0, 0, 0]}
    assert result["expanded"] == [[0], [0, 0]]
    assert _find(tree, "国道").checkState(0) == Qt.Checked
    assert ("国道", 0) in changed  # 人のクリックと同じく itemChanged が出る
    assert _find(tree, "条件").isExpanded() and _find(tree, "道路").isExpanded()

    result = set_widget_value(_sel(), False, cell={"path": ["条件", "道路", "国道"]})
    assert result["success"] is True
    assert (result["prior_value"], result["value_after"]) == ("checked", "unchecked")
    assert "expanded" not in result
    assert _find(tree, "国道").checkState(0) == Qt.Unchecked


def test_strings_true_and_false_are_taken(host: Any) -> None:
    tree = _tree(host)
    assert set_widget_value(_sel(), "true", cell={"path": ["条件"]})["value_after"] == "checked"
    assert _find(tree, "条件").checkState(0) == Qt.Checked
    result = set_widget_value(_sel(), "yes", cell={"path": ["条件"]})
    assert result["error"] == "invalid_value"


def test_an_auto_tristate_parent_reads_partial(host: Any) -> None:
    tree = _tree(host)
    roads = _find(tree, "道路")
    roads.setFlags(roads.flags() | Qt.ItemIsAutoTristate)

    set_widget_value(_sel(), True, cell={"path": ["条件", "道路", "県道"]})

    read = read_rows({"selector": _sel(), "cell": {"path": ["条件", "道路"]}, "role": "check"})
    assert read == {
        "success": True,
        "cell": {"row": 0, "column": 0, "path": [0, 0]},
        "value": "partial",
    }
    # 親を付けると子も付く（Qt の auto tristate）
    set_widget_value(_sel(), True, cell={"path": ["条件", "道路"]})
    assert _find(tree, "国道").checkState(0) == Qt.Checked


def test_rows_a_person_cannot_check_are_refused(host: Any) -> None:
    tree = _tree(host)

    no_box = set_widget_value(_sel(), True, cell={"path": ["条件", "建物"]})
    assert no_box["success"] is False
    assert no_box["error"] == "cell_not_checkable"
    assert no_box["cell"] == {"row": 1, "column": 0, "path": [0, 1]}
    assert no_box["check_state"] is None
    assert "column" in no_box["hint"]

    other_column = set_widget_value(_sel(), True, cell={"path": ["条件-id"], "column": "ID"})
    assert other_column["error"] == "cell_not_checkable"

    _find(tree, "県道").setDisabled(True)
    disabled = set_widget_value(_sel(), True, cell={"path": ["条件", "道路", "県道"]})
    assert disabled["error"] == "cell_disabled"
    assert _find(tree, "県道").checkState(0) == Qt.Unchecked

    tree.setColumnHidden(0, True)
    hidden = set_widget_value(_sel(), True, cell={"path": ["条件"], "column": 0})
    assert hidden["error"] == "column_hidden"
    tree.setColumnHidden(0, False)

    tree.setItemsExpandable(False)
    collapsed = set_widget_value(_sel(), True, cell={"path": ["条件", "道路"]})
    assert collapsed["error"] == "row_collapsed"

    missing = set_widget_value(_sel(), True, cell={"path": ["条件", "鉄道"]})
    assert (missing["error"], missing["depth"]) == ("row_not_found", 1)


def test_a_cell_needs_an_item_view(host: Any) -> None:
    edit = QtWidgets.QLineEdit()
    edit.setObjectName("w")
    host.layout().addWidget(edit)
    QApplication.processEvents()
    result = set_widget_value(_sel(), True, cell=0)
    assert result["error"] == "cell_needs_a_view"


class _Model(QtGui.QStandardItemModel):
    """setData で断る・黙って無視するモデル。"""

    mode = "refuse"

    def setData(self, index: Any, value: Any, role: int = Qt.EditRole) -> bool:  # noqa: N802
        if role == Qt.ItemDataRole.CheckStateRole:
            return self.mode != "refuse"
        return super().setData(index, value, role)


@pytest.mark.parametrize(
    ("mode", "error"), [("refuse", "set_value_failed"), ("ignore", "value_not_applied")]
)
def test_a_model_that_does_not_take_the_check_fails(host: Any, mode: str, error: str) -> None:
    model = _Model()
    model.mode = mode
    item = QtGui.QStandardItem("a")
    item.setFlags(CHECKABLE)
    item.setCheckState(Qt.Unchecked)
    model.appendRow(item)
    view = QtWidgets.QListView()
    view.setObjectName("w")
    view.setModel(model)
    host.layout().addWidget(view)
    QApplication.processEvents()

    result = set_widget_value(_sel(), True, cell={"text": "a"})

    assert result["success"] is False
    assert result["error"] == error
    if mode == "ignore":
        assert result["value_after"] == "unchecked"
        assert result["cell"] == {"row": 0, "column": 0}
        assert set_widget_value(_sel(), True, cell=0, verify=False)["success"] is True


def test_a_list_widget_row_is_checked_by_number(host: Any) -> None:
    widget = QtWidgets.QListWidget()
    widget.setObjectName("w")
    for text in ("a", "b"):
        item = QtWidgets.QListWidgetItem(text)
        item.setFlags(CHECKABLE)
        item.setCheckState(Qt.Unchecked)
        widget.addItem(item)
    host.layout().addWidget(widget)
    QApplication.processEvents()

    current = widget.currentRow()
    assert set_widget_value(_sel(), True, cell=1)["value_after"] == "checked"
    assert widget.item(1).checkState() == Qt.Checked
    assert widget.item(0).checkState() == Qt.Unchecked
    assert widget.currentRow() == current  # チェックは行を選ばない


def test_a_queued_check_runs_later(host: Any, app: Any) -> None:
    tree = _tree(host)

    result = set_widget_value(_sel(), True, wait=False, cell={"path": ["条件", "道路"]})

    assert result == {
        "success": True,
        "queued": True,
        "widget": result["widget"],
        "prior_value": "unchecked",
        "cell": {"row": 0, "column": 0, "path": [0, 0]},
        "expanded": [[0]],
    }
    assert _find(tree, "道路").checkState(0) == Qt.Unchecked
    for _ in range(3):
        app.processEvents()
    assert _find(tree, "道路").checkState(0) == Qt.Checked
    assert exception_recorder.get_recent() == []


def test_read_rows_reads_one_cell(host: Any) -> None:
    tree = _tree(host)
    _find(tree, "県道").setCheckState(0, Qt.Checked)

    read = read_rows(
        {"selector": _sel(), "cell": {"path": ["条件", "道路", "県道"]}, "role": "check"}
    )
    assert read["value"] == "checked"
    shown = read_rows(
        {"selector": _sel(), "cell": {"path": ["条件-id", "道路-id", "県道-id"], "column": "ID"}}
    )
    assert shown["value"] == "県道-id"
    mixed = read_rows({"selector": _sel(), "cell": 0, "path": ["条件"]})
    assert (mixed["error"], mixed["expected_form"]) == ("invalid_value", "cell without path")
    assert read_rows({"selector": _sel(), "cell": 0, "start": 0})["success"] is True


# QGIS のレイヤツリー：レイヤの下の凡例の分類（categorized renderer）を付け外す
_LEGEND = """
from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsFeature, QgsField, QgsGeometry, QgsLayerTreeModel,
    QgsMarkerSymbol, QgsProject, QgsRendererCategory, QgsVectorLayer,
)
from qgis.gui import QgsLayerTreeView
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QVBoxLayout, QWidget
from qgis_puppeteer.qgis_tools.ui_tools import read_rows, set_widget_value

layer = QgsVectorLayer("Point?crs=EPSG:4326&field=kind:string", "pts", "memory")
features = []
for i, kind in enumerate(["a", "b"]):
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
    f.setAttribute("kind", kind)
    features.append(f)
layer.dataProvider().addFeatures(features)
categories = [
    QgsRendererCategory(k, QgsMarkerSymbol.createSimple({}), k, True, k) for k in ("a", "b")
]
layer.setRenderer(QgsCategorizedSymbolRenderer("kind", categories))
QgsProject.instance().addMapLayer(layer)

host = QWidget()
host.setObjectName("host")
QVBoxLayout(host)
VISIBILITY = QgsLayerTreeModel.ShowLegend | QgsLayerTreeModel.AllowNodeChangeVisibility
FLAGS = {"full": VISIBILITY | QgsLayerTreeModel.AllowLegendChangeState, "locked": VISIBILITY}
views = {}
for name, flags in FLAGS.items():
    model = QgsLayerTreeModel(QgsProject.instance().layerTreeRoot(), host)
    model.setFlags(flags)
    view = QgsLayerTreeView(host)
    view.setObjectName(name)
    view.setModel(model)
    host.layout().addWidget(view)
    views[name] = view
views["full"].collapseAll()
host.resize(300, 400)
host.show()
for _ in range(5):
    _app.processEvents()

def sel(name):
    return {"object_name": name, "root_object_name": "host"}

renderer = layer.renderer()
R = {}
R["uncheck"] = set_widget_value(sel("full"), False, cell={"path": ["pts", "b"]})
R["b_after_uncheck"] = renderer.legendSymbolItemChecked("b")
R["a_after_uncheck"] = renderer.legendSymbolItemChecked("a")
R["read"] = read_rows({"selector": sel("full"), "cell": {"path": ["pts", "b"]}, "role": "check"})
R["check"] = set_widget_value(sel("full"), True, cell={"path": ["pts", "b"]})
R["b_after_check"] = renderer.legendSymbolItemChecked("b")
R["layer_off"] = set_widget_value(sel("full"), False, cell={"path": ["pts"]})
node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
R["layer_visible"] = node.isVisible()
R["locked"] = set_widget_value(sel("locked"), False, cell={"path": ["pts", "a"]})
R["a_after_locked"] = renderer.legendSymbolItemChecked("a")
for value in R.values():
    if isinstance(value, dict):
        value.pop("widget", None)
RESULT = R
"""


@pytest.mark.integration
def test_a_legend_class_under_a_layer_is_checked_in_the_layer_tree() -> None:
    pytest.importorskip("qgis.core")
    from tests._qgis_child import run_in_qgis

    r = run_in_qgis(_LEGEND, gui=True)

    assert r["uncheck"]["success"] is True, r["uncheck"]
    assert (r["uncheck"]["prior_value"], r["uncheck"]["value_after"]) == ("checked", "unchecked")
    assert r["uncheck"]["cell"]["path"] == [0, 1]
    assert r["uncheck"]["expanded"] == [[0]]
    assert (r["b_after_uncheck"], r["a_after_uncheck"]) == (False, True)
    assert r["read"]["value"] == "unchecked"
    assert r["check"]["success"] is True
    assert r["b_after_check"] is True
    assert r["layer_off"]["value_after"] == "unchecked"
    assert r["layer_visible"] is False
    # 凡例の付け外しを許さないツリーでは、人にも押せない
    assert r["locked"]["success"] is False
    assert r["locked"]["error"] == "cell_not_checkable"
    assert r["a_after_locked"] is True
