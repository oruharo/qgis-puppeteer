"""``QgsFileWidget`` にパスを入れる（#25）。

``QgsFileWidget`` は ``qgis.gui`` の部品で、``QgsApplication`` が要るので、
``tests._qgis_child.run_in_qgis`` で QGIS の Python の子プロセスの中で確かめる。
``qgis.core`` が無ければ全件 skip。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("qgis.core")

from tests._qgis_child import run_in_qgis  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    return run_in_qgis(
        """
from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout
from qgis.gui import QgsFileWidget
from qgis_puppeteer.qgis_tools.ui_tools import check_actionability, set_widget_value

host = QWidget()
host.setObjectName("host")
lay = QVBoxLayout(host)
widgets = {}
for name, mode in (
    ("save", QgsFileWidget.SaveFile),
    ("folder", QgsFileWidget.GetDirectory),
    ("open", QgsFileWidget.GetFile),
    ("many", QgsFileWidget.GetMultipleFiles),
):
    w = QgsFileWidget(host)
    w.setObjectName(name)
    w.setStorageMode(mode)
    lay.addWidget(w)
    widgets[name] = w
host.show()
_app.processEvents()

def sel(name):
    return {"object_name": name, "root_object_name": "host"}

RESULT = {}
# まだ無い保存先。区切りは Windows の両方の書き方で
RESULT["save_back"] = set_widget_value(sel("save"), "C:\\\\out\\\\report.pdf")
RESULT["save_path"] = widgets["save"].filePath()
RESULT["save_fwd"] = set_widget_value(sel("save"), "C:/out/report.pdf")
RESULT["folder"] = set_widget_value(sel("folder"), "D:/exports/blocks")
RESULT["folder_path"] = widgets["folder"].filePath()
RESULT["open"] = set_widget_value(sel("open"), "/data/trace.gpkg")
RESULT["open_prior"] = set_widget_value(sel("open"), "/data/other.gpkg")["prior_value"]
RESULT["list_refused"] = set_widget_value(sel("many"), ["a", "b"])
RESULT["number_refused"] = set_widget_value(sel("open"), 5)
# setReadOnly(True) は（3.34 では）中の入力欄とボタンを無効にするだけで、部品自身は
# 有効なまま。それでも断ること
widgets["open"].setReadOnly(True)
RESULT["readonly"] = set_widget_value(sel("open"), "/x")
RESULT["readonly_editable"] = check_actionability(sel("open"))["editable"]
widgets["open"].setReadOnly(False)

def find(node, name):
    if isinstance(node, dict):
        if node.get("object_name") == name:
            return node
        for c in node.get("children") or ():
            hit = find(c, name)
            if hit is not None:
                return hit
    return None
# host は QDialog ではないので、生の widget ツリーから直接読む
from qgis_puppeteer.qgis_tools.ui_tools import _describe_widget
RESULT["snapshot"] = find(_describe_widget(host, 3, False), "folder")
"""
    )


def test_a_path_goes_in_through_set_file_path_and_reads_back(results: dict[str, Any]) -> None:
    assert results["save_back"]["success"] is True
    assert results["save_back"]["value_after"] == "C:\\out\\report.pdf"
    assert results["save_path"] == "C:\\out\\report.pdf"
    assert results["save_fwd"]["success"] is True
    assert results["save_fwd"]["prior_value"] == "C:\\out\\report.pdf"


def test_a_folder_and_a_missing_path_are_accepted(results: dict[str, Any]) -> None:
    """まだ無い場所（保存先・出力先）も入れる。"""
    assert results["folder"]["success"] is True
    assert results["folder_path"] == "D:/exports/blocks"
    assert results["open"]["success"] is True
    assert results["open_prior"] == "/data/trace.gpkg"


def test_only_a_single_path_string_is_accepted(results: dict[str, Any]) -> None:
    assert results["list_refused"]["error"] == "invalid_value"
    assert results["number_refused"]["error"] == "invalid_value"


def test_a_read_only_file_widget_is_refused(results: dict[str, Any]) -> None:
    assert results["readonly"]["error"] == "widget_readonly"
    # fill() の auto-wait が、読み取り専用が解けるのを待てるように
    assert results["readonly_editable"] is False


def test_snapshot_shows_the_path_and_the_mode(results: dict[str, Any]) -> None:
    node = results["snapshot"]
    assert node["class"] == "QgsFileWidget"
    assert node["file_path"] == "D:/exports/blocks"
    assert node["storage_mode"] == "GetDirectory"
