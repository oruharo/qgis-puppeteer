"""表・ツリー・リストの行を上限なしに読む（#36）。

実 Qt widget を作るので、``qgis.PyQt`` が import できる環境（``scripts/qt_tests.py``）
でだけ走る。
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

from qgis_puppeteer.qgis_tools.ui_tools import read_rows  # noqa: E402

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
    window.show()
    try:
        yield window
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()


def _add(host: Any, widget: Any) -> Any:
    widget.setObjectName("w")
    host.layout().addWidget(widget)
    QApplication.processEvents()
    return widget


def _read(**params: Any) -> dict:
    return read_rows({"selector": {"object_name": "w", "root_object_name": "host"}, **params})


def _big_table(host: Any, rows: int) -> Any:
    model = QtGui.QStandardItemModel(rows, 2)
    model.setHorizontalHeaderLabels(["名前", "数"])
    for r in range(rows):
        name = QtGui.QStandardItem(f"n{r}")
        name.setData(f"id-{r}", Qt.UserRole)
        model.setItem(r, 0, name)
        model.setItem(r, 1, QtGui.QStandardItem(str(r)))
    view = QtWidgets.QTableView()
    view.setModel(model)
    view._model = model  # 寿命を view に合わせる
    return _add(host, view)


def test_rows_past_the_snapshot_limit_are_read_in_pages(host: Any) -> None:
    _big_table(host, 2500)

    first = _read()
    assert first["columns"] == ["名前", "数"]
    assert first["row_count"] == 2500
    assert len(first["rows"]) == 1000  # 1 回は 1000 行まで
    assert first["rows"][0] == ["n0", "0"]
    last = _read(start=2400, max_rows=500)
    assert last["start"] == 2400
    assert len(last["rows"]) == 100
    assert last["rows"][-1] == ["n2499", "2499"]


def test_role_user_and_a_disabled_table(host: Any) -> None:
    view = _big_table(host, 3)
    view.setEnabled(False)  # 灰色の表も読める

    result = _read(role="user")

    assert [row[0] for row in result["rows"]] == ["id-0", "id-1", "id-2"]
    assert result["rows"][0][1] is None  # UserRole の無いセル
    assert _read(role=int(Qt.UserRole))["rows"][2][0] == "id-2"


def test_check_states_of_a_tree_level(host: Any) -> None:
    model = QtGui.QStandardItemModel()
    group = QtGui.QStandardItem("道路")
    for name, state in (
        ("国道", Qt.Checked),
        ("県道", Qt.Unchecked),
        ("市道", Qt.PartiallyChecked),
    ):
        item = QtGui.QStandardItem(name)
        item.setCheckable(True)
        item.setCheckState(state)
        group.appendRow(item)
    model.appendRow(group)
    view = QtWidgets.QTreeView()
    view.setModel(model)
    view._model = model
    _add(host, view)

    checks = _read(path=["道路"], role="check")
    assert checks["rows"] == [["checked"], ["unchecked"], ["partial"]]
    assert _read(path=["道路"])["rows"] == [["国道"], ["県道"], ["市道"]]
    assert _read(role="check")["rows"] == [[None]]  # チェックの無い行
    assert _read(path=["無い"])["error"] == "row_not_found"


def test_a_list_reads_its_model_column(host: Any) -> None:
    model = QtGui.QStandardItemModel(2, 2)
    for r in range(2):
        model.setItem(r, 0, QtGui.QStandardItem(f"hidden{r}"))
        model.setItem(r, 1, QtGui.QStandardItem(f"shown{r}"))
    view = QtWidgets.QListView()
    view.setModel(model)
    view.setModelColumn(1)
    view._model = model
    _add(host, view)

    result = _read()
    assert result["rows"] == [["shown0"], ["shown1"]]
    assert "columns" not in result


def test_bad_arguments_are_refused(host: Any) -> None:
    _big_table(host, 2)
    assert _read(role="colour")["error"] == "invalid_value"
    assert _read(start=-1)["error"] == "invalid_value"
    label = QtWidgets.QLabel("x")
    label.setObjectName("label")
    host.layout().addWidget(label)
    QApplication.processEvents()
    not_view = read_rows({"selector": {"object_name": "label", "root_object_name": "host"}})
    assert not_view["error"] == "not_an_item_view"
