"""selector の部分一致・正規表現・role・attr のキーが、操作の解決（live）でも効く。

snapshot の解決（``record_matches_selector``）と同じ結果になること。実 Qt widget を
作るので、``qgis.PyQt`` が import できる環境（``scripts/qt_tests.py``）でだけ走る。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("qgis.PyQt.QtWidgets")
QtCore = pytest.importorskip("qgis.PyQt.QtCore")

from qgis_puppeteer.qgis_tools.ui_tools import (  # noqa: E402
    _describe_widget,
    _find_widget,
    click_widget,
)

QApplication = QtWidgets.QApplication


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(app: Any) -> Iterator[Any]:
    dlg = QtWidgets.QDialog()
    dlg.setObjectName("keys")
    dlg.setWindowTitle("レイヤのプロパティ — 建物")
    layout = QtWidgets.QVBoxLayout(dlg)
    for name, text in (("save", "保存(&S)"), ("delete", "削除"), ("count", "項目 12 件")):
        button = QtWidgets.QPushButton(text, dlg)
        button.setObjectName(name)
        layout.addWidget(button)
    dlg.show()
    QApplication.processEvents()
    try:
        yield dlg
    finally:
        dlg.close()
        dlg.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()


def _name(selector: dict) -> str | None:
    widget, _ = _find_widget({"scope": "any", "root_object_name": "keys", **selector})
    return widget.objectName() if widget is not None else None


def test_text_contains_and_text_re_pick_one_widget(dialog: Any) -> None:
    assert _name({"class": "QPushButton", "text_contains": "保存"}) == "save"
    assert _name({"class": "QPushButton", "text_re": r"\d+ 件"}) == "count"
    assert _name({"class": "QPushButton", "text_contains": "無い"}) is None


def test_title_contains_matches_part_of_a_window_title(dialog: Any) -> None:
    """#35"""
    assert _name({"class": "QDialog", "title_contains": "プロパティ"}) == "keys"
    assert _name({"class": "QDialog", "title_contains": "属性"}) is None


def test_attr_and_role_narrow_the_match(dialog: Any) -> None:
    dialog.findChild(QtWidgets.QPushButton, "delete").setEnabled(False)
    assert _name({"class": "QPushButton", "text_contains": "削除", "attr": {"enabled": False}}) == (
        "delete"
    )
    assert _name({"class": "QPushButton", "text": "削除", "attr": {"enabled": True}}) is None


def test_role_matches_what_the_snapshot_shows(dialog: Any) -> None:
    # role は snapshot に出る値と同じ（Qt の版で名前か番号かが違う。accessibility が
    # 効いていない環境では出ない）
    record = _describe_widget(dialog.findChild(QtWidgets.QPushButton, "save"), 0, True)
    if "role" not in record:
        pytest.skip("no accessible role in this environment")
    assert _name({"role": record["role"], "text_contains": "保存"}) == "save"


def test_a_click_reaches_the_widget_matched_by_text_contains(dialog: Any) -> None:
    clicked: list[str] = []
    dialog.findChild(QtWidgets.QPushButton, "save").clicked.connect(lambda: clicked.append("s"))

    result = click_widget({"scope": "any", "class": "QPushButton", "text_contains": "保存"})

    assert result["success"] is True
    assert clicked == ["s"]


def test_title_is_only_the_title_a_person_sees(app: Any) -> None:
    """#37: 窓でない部品の windowTitle（Designer の既定の "Form" など）には当てない。"""
    main = QtWidgets.QMainWindow()
    main.setObjectName("titles")
    main.setWindowTitle("検索")
    central = QtWidgets.QWidget()
    central.setWindowTitle("検索")  # 埋め込んだ .ui の部品（人には見えない題名）
    layout = QtWidgets.QVBoxLayout(central)
    for _ in range(2):
        form = QtWidgets.QWidget()
        form.setWindowTitle("Form")
        layout.addWidget(form)
    box = QtWidgets.QGroupBox("条件")
    layout.addWidget(box)
    main.setCentralWidget(central)
    dock = QtWidgets.QDockWidget("凡例", main)
    main.addDockWidget(QtCore.Qt.LeftDockWidgetArea, dock)
    main.show()
    QApplication.processEvents()
    try:

        def find(selector: dict) -> Any:
            widget, _ = _find_widget({"scope": "any", "root_object_name": "titles", **selector})
            return widget

        assert find({"title": "検索"}) is main  # 中の部品の同じ題名には当たらない
        assert find({"title": "Form"}) is None
        assert find({"title_contains": "Fo"}) is None
        assert find({"title": "条件"}) is box  # 枠の題名
        assert find({"title": "凡例"}) is dock  # ドックしていても題名の帯に出る
        assert "title" not in _describe_widget(central, 0, True)
        assert _describe_widget(box, 0, True)["title"] == "条件"
    finally:
        main.close()
        main.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()


def test_window_matches_only_windows(dialog: Any) -> None:
    """#38"""
    assert _name({"window": True}) == "keys"
    assert _name({"class": "QPushButton", "window": True}) is None
    assert _name({"object_name": "save", "window": False}) == "save"


def test_window_and_class_inherits_count_every_open_dialog(dialog: Any) -> None:
    """#38: どの窓も開いていない、を数える指定。窓の中の部品と、窓でない部品は数えない。"""
    child = QtWidgets.QMessageBox(dialog)  # 親のある窓（topLevelWidgets にも親の子にも出る）
    child.setObjectName("child")
    child.setModal(False)
    child.show()
    inner = QtWidgets.QDialog(dialog)  # QDialog だが窓でない（埋め込み）
    # コンストラクタのフラグが 0 だと QDialog は Qt::Dialog を足すので、あとで外す
    inner.setWindowFlags(QtCore.Qt.Widget)
    inner.setObjectName("inner")
    assert not inner.isWindow()
    QApplication.processEvents()
    try:
        selector = {"scope": "any", "window": True, "class_inherits": "QDialog"}
        widget, diagnostics = _find_widget(selector)
        assert widget is None
        assert diagnostics["reason"] == "selector_ambiguous"
        # 親のある窓を、トップレベルとしてと親の子としての 2 回は数えない
        assert diagnostics["match_count"] == 2
        assert {c["object_name"] for c in diagnostics["candidates"]} == {"keys", "child"}
    finally:
        child.close()
        child.deleteLater()


class _PythonDialog(QtWidgets.QDialog):
    pass


def test_class_inherits_matches_a_python_subclass_by_either_name(app: Any) -> None:
    """#38"""
    dlg = _PythonDialog()
    dlg.setObjectName("py")
    dlg.show()
    QApplication.processEvents()
    try:

        def find(selector: dict) -> Any:
            widget, _ = _find_widget({"scope": "any", "root_object_name": "py", **selector})
            return widget

        assert find({"class_inherits": "_PythonDialog"}) is dlg
        assert find({"class_inherits": "QDialog", "window": True}) is dlg
        assert find({"class_inherits": "QMessageBox"}) is None
    finally:
        dlg.close()
        dlg.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()


def test_class_inherits_sees_a_cpp_type_that_python_sees_as_its_base(dialog: Any) -> None:
    """#38: C++ だけの型（sip は基底の型で包む）は class では当たらず、class_inherits で当たる。

    QComboBox のポップアップの入れ物は Qt の内部の型 QComboBoxPrivateContainer で、
    Python からは QFrame に見える（QGIS の QgsAttributeDialog が QDialog に見えるのと同じ）。
    """
    combo = QtWidgets.QComboBox(dialog)
    container = combo.view().parentWidget()
    assert type(container).__name__ == "QFrame"
    assert _name({"class": "QComboBoxPrivateContainer"}) is None
    widget, _ = _find_widget(
        {"scope": "any", "root_object_name": "keys", "class_inherits": "QComboBoxPrivateContainer"}
    )
    assert widget is container
