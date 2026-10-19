"""パスワード欄の文字を外に出さない（#10）。

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

from qgis_puppeteer.qgis_tools.ui_tools import (  # noqa: E402
    _collect_candidates,
    check_actionability,
    click_widget,
    set_widget_value,
    snapshot_ui,
)

QApplication = QtWidgets.QApplication
QDialog = QtWidgets.QDialog
QLineEdit = QtWidgets.QLineEdit
QVBoxLayout = QtWidgets.QVBoxLayout

SECRET = "s3cret-Pa55"


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(app: Any) -> Iterator[Any]:
    dlg = QDialog()
    dlg.setObjectName("loginDialog")
    layout = QVBoxLayout(dlg)
    user = QLineEdit(dlg)
    user.setObjectName("user")
    user.setText("alice")
    layout.addWidget(user)
    for name, mode in (
        ("password", QLineEdit.EchoMode.Password),
        ("password_on_edit", QLineEdit.EchoMode.PasswordEchoOnEdit),
        ("no_echo", QLineEdit.EchoMode.NoEcho),
    ):
        edit = QLineEdit(dlg)
        edit.setObjectName(name)
        edit.setEchoMode(mode)
        edit.setText(SECRET)
        layout.addWidget(edit)
    dlg.show()
    app.processEvents()
    try:
        yield dlg
    finally:
        dlg.close()
        dlg.deleteLater()
        app.processEvents()


def _find(node: Any, object_name: str) -> dict | None:
    if not isinstance(node, dict):
        return None
    if node.get("object_name") == object_name:
        return node
    for child in node.get("children") or ():
        found = _find(child, object_name)
        if found is not None:
            return found
    return None


def _dialog_node(snap: dict) -> dict:
    for top in snap["visible_dialogs"]:
        if top.get("object_name") == "loginDialog":
            return top
    raise AssertionError("loginDialog not in the snapshot")


def test_snapshot_masks_every_secret_echo_mode(dialog: Any) -> None:
    snap = snapshot_ui()
    root = _dialog_node(snap)
    for name in ("password", "password_on_edit", "no_echo"):
        node = _find(root, name)
        assert node is not None
        assert node["masked"] is True
        assert "text" not in node
    assert _find(root, "user")["text"] == "alice"  # 普通の欄はそのまま
    assert SECRET not in json.dumps(snap, ensure_ascii=False)


def test_text_selector_does_not_match_the_contents(dialog: Any) -> None:
    assert _collect_candidates([dialog], {"text": SECRET}) == []
    assert _collect_candidates([dialog], {"text": "alice"}) != []


def test_set_widget_value_masks_the_result(dialog: Any) -> None:
    result = set_widget_value({"object_name": "password"}, "n3w-Pa55")
    assert result["success"] is True
    assert result["masked"] is True
    assert result["prior_value"] is None
    assert result["widget"]["masked"] is True
    dumped = json.dumps(result, ensure_ascii=False)
    assert SECRET not in dumped
    assert "n3w-Pa55" not in dumped
    assert dialog.findChild(QLineEdit, "password").text() == "n3w-Pa55"  # 入ってはいる


def test_ordinary_field_reports_its_prior_value(dialog: Any) -> None:
    result = set_widget_value({"object_name": "user"}, "bob")
    assert result["prior_value"] == "alice"
    assert "masked" not in result


def test_summaries_in_other_results_hide_the_text(dialog: Any) -> None:
    for result in (
        check_actionability({"object_name": "password"}),
        click_widget({"object_name": "password"}),
    ):
        assert result["widget"]["masked"] is True
        assert SECRET not in json.dumps(result, ensure_ascii=False)


def test_qgs_password_line_edit_is_masked_even_when_shown(dialog: Any) -> None:
    """QgsPasswordLineEdit は目のボタンで Normal にできるので、クラスで伏せる。"""
    gui = pytest.importorskip("qgis.gui")
    edit = gui.QgsPasswordLineEdit(dialog)
    edit.setObjectName("qgs_password")
    edit.setText(SECRET)
    edit.setEchoMode(QLineEdit.EchoMode.Normal)
    dialog.layout().addWidget(edit)
    edit.show()
    node = _find(_dialog_node(snapshot_ui()), "qgs_password")
    assert node is not None
    assert node["masked"] is True
    assert "text" not in node
