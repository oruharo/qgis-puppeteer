"""ツリーの子の行を、上の段からの文字か番号の並び（``{"path": [...]}``）で指す（#30）。

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
QtGui = pytest.importorskip("qgis.PyQt.QtGui")

from qgis_puppeteer.qgis_tools import exception_recorder  # noqa: E402
from qgis_puppeteer.qgis_tools.ui_tools import click_widget, set_widget_value  # noqa: E402

QApplication = QtWidgets.QApplication

# 道路 ─ 国道 / 県道、建物 ─ 現存 / 滅失済 ─ A
TREE = {"道路": {"国道": {}, "県道": {}}, "建物": {"現存": {}, "滅失済": {"A": {}}}}


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


def _fill(parent: Any, tree: dict) -> None:
    for name, children in tree.items():
        item = QtWidgets.QTreeWidgetItem([name])
        if isinstance(parent, QtWidgets.QTreeWidget):
            parent.addTopLevelItem(item)
        else:
            parent.addChild(item)
        _fill(item, children)


def _tree(host: Any) -> Any:
    tree = QtWidgets.QTreeWidget()
    tree.setObjectName("w")
    tree.setHeaderLabels(["名前"])
    _fill(tree, TREE)
    host.layout().addWidget(tree)
    QApplication.processEvents()
    return tree


def _sel() -> dict:
    return {"object_name": "w", "root_object_name": "host"}


def _expanded(tree: Any) -> list[str]:
    found = []

    def walk(item: Any) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            if child.isExpanded():
                found.append(child.text(0))
            walk(child)

    walk(tree.invisibleRootItem())
    return found


def test_a_child_row_is_selected_by_path_after_opening_its_parents(host: Any) -> None:
    tree = _tree(host)

    result = set_widget_value(_sel(), {"path": ["建物", 1, "A"]})

    assert result["success"] is True
    assert tree.currentItem().text(0) == "A"
    assert result["value_after"] == [1, 1, 0]  # 子の行は上の段からの番号
    assert result["expanded"] == [[1], [1, 1]]
    assert _expanded(tree) == ["建物", "滅失済"]
    # 一番上の段の行は今まで通り番号
    assert set_widget_value(_sel(), {"path": ["道路"]})["value_after"] == 0


def test_a_double_click_reaches_a_child_row_in_a_collapsed_group(host: Any) -> None:
    tree = _tree(host)
    seen: list[str] = []
    tree.itemDoubleClicked.connect(lambda item, _c: seen.append(item.text(0)))

    result = click_widget(_sel(), kind="double", cell={"path": ["道路", "県道"]})

    assert result["success"] is True
    assert result["cell"] == {"row": 1, "column": 0, "path": [0, 1]}
    assert result["expanded"] == [[0]]
    assert seen == ["県道"]


def test_a_queued_click_opens_the_parents_when_it_runs(host: Any, app: Any) -> None:
    tree = _tree(host)
    seen: list[str] = []
    tree.itemDoubleClicked.connect(lambda item, _c: seen.append(item.text(0)))

    result = click_widget(_sel(), kind="double", cell={"path": ["建物", "現存"]}, wait=False)
    assert result["queued"] is True
    assert _expanded(tree) == []  # まだ開かない
    for _ in range(5):
        app.processEvents()

    assert seen == ["現存"]
    assert _expanded(tree) == ["建物"]


def test_a_missing_step_says_where_and_opens_nothing(host: Any) -> None:
    tree = _tree(host)

    missing = set_widget_value(_sel(), {"path": ["道路", "市道"]})
    assert missing["error"] == "row_not_found"
    assert missing["depth"] == 1
    assert missing["path"] == ["道路", "市道"]
    assert _expanded(tree) == []
    out_of_range = set_widget_value(_sel(), {"path": [1, 5]})
    assert out_of_range["error"] == "index_out_of_range"
    assert out_of_range["depth"] == 1
    assert "hint" not in out_of_range  # 遅れて読むモデルではない
    tree.setRowHidden(0, tree.model().index(0, 0), True)  # 国道を隠す
    assert set_widget_value(_sel(), {"path": ["道路", "国道"]})["error"] == "row_not_found"
    hidden = set_widget_value(_sel(), {"path": ["道路", 0]})
    assert hidden["error"] == "row_hidden"
    assert hidden["depth"] == 1


def test_an_ambiguous_step_is_refused(host: Any) -> None:
    tree = _tree(host)
    tree.topLevelItem(0).addChild(QtWidgets.QTreeWidgetItem(["国道"]))

    result = set_widget_value(_sel(), {"path": ["道路", "国道"]})

    assert result["error"] == "row_ambiguous"
    assert result["depth"] == 1
    assert result["rows"] == [0, 2]


def test_a_tree_that_cannot_be_expanded_is_refused_while_collapsed(host: Any) -> None:
    tree = _tree(host)
    tree.setItemsExpandable(False)

    collapsed = set_widget_value(_sel(), {"path": ["道路", "国道"]})
    assert collapsed["error"] == "row_collapsed"
    assert collapsed["path"] == [0]
    tree.expandItem(tree.topLevelItem(0))  # ホストが開いていれば選べる
    assert set_widget_value(_sel(), {"path": ["道路", "国道"]})["value_after"] == [0, 0]


@pytest.mark.parametrize("path", [[], "道路", [1.5], [None], [True]])
def test_a_malformed_path_is_refused(host: Any, path: Any) -> None:
    _tree(host)
    assert set_widget_value(_sel(), {"path": path})["error"] == "invalid_value"
    assert set_widget_value(_sel(), {"path": ["道路"], "row": 0})["error"] == "invalid_value"


def test_column_narrows_every_step(host: Any) -> None:
    tree = QtWidgets.QTreeWidget()
    tree.setObjectName("w")
    tree.setHeaderLabels(["名前", "種別"])
    road = QtWidgets.QTreeWidgetItem(["道路", "group"])
    road.addChild(QtWidgets.QTreeWidgetItem(["国道", "line"]))
    road.addChild(QtWidgets.QTreeWidgetItem(["line", "point"]))
    tree.addTopLevelItem(road)
    host.layout().addWidget(tree)
    QApplication.processEvents()

    assert set_widget_value(_sel(), {"path": ["道路", "line"]})["error"] == "row_ambiguous"
    by_name = set_widget_value(_sel(), {"path": ["道路", "line"], "column": "名前"})
    assert by_name["value_after"] == [0, 1]


class _LazyModel(QtGui.QStandardItemModel):
    """子を fetchMore で初めて読むモデル（ファイルやデータベースの木のような）。"""

    def __init__(self) -> None:
        super().__init__()
        self.fetched: list[str] = []
        for name in ("a", "b"):
            item = QtGui.QStandardItem(name)
            self.appendRow(item)

    def hasChildren(self, parent: Any = QtCore.QModelIndex()) -> bool:  # noqa: B008, N802
        return not parent.isValid() or not parent.parent().isValid()

    def canFetchMore(self, parent: Any) -> bool:  # noqa: N802
        return parent.isValid() and parent.data() not in self.fetched

    def fetchMore(self, parent: Any) -> None:  # noqa: N802
        name = parent.data()
        self.fetched.append(name)
        self.itemFromIndex(parent).appendRow(QtGui.QStandardItem(f"{name}-1"))


def test_a_lazy_model_is_asked_to_load_the_children(host: Any) -> None:
    view = QtWidgets.QTreeView()
    view.setObjectName("w")
    model = _LazyModel()
    view.setModel(model)
    host.layout().addWidget(view)
    QApplication.processEvents()

    result = set_widget_value(_sel(), {"path": ["b", "b-1"]})

    assert result["value_after"] == [1, 0]
    assert "b" in model.fetched
    missing = set_widget_value(_sel(), {"path": ["a", "a-9"]})
    assert missing["error"] == "row_not_found"
    assert "lazily" in missing["hint"]
