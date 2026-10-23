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
