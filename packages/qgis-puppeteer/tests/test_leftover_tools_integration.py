"""テストのあとに残った状態を読む（#28）。

QGIS を初期化した子プロセス（``tests._qgis_child.run_in_qgis``）で、レイヤ・編集・
選択・グループ・分類の凡例・ダイアログを作り、``get_leftover_state`` が読むものを
確かめる。``qgis.core`` が無ければ全件 skip。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("qgis.core")

from tests._qgis_child import run_in_qgis  # noqa: E402

pytestmark = pytest.mark.integration

_BODY = """
from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRendererCategory, QgsSymbol, QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapToolPan
from qgis.PyQt.QtWidgets import QDialog
from qgis_puppeteer.qgis_tools.leftover_tools import get_leftover_state

project = QgsProject.instance()
pts = QgsVectorLayer("Point?crs=EPSG:3857&field=kind:string", "建物", "memory")
feats = []
for i, kind in enumerate(["現存", "滅失済", "現存"]):
    f = QgsFeature(pts.fields())
    f.setAttributes([kind])
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(i, i)))
    feats.append(f)
pts.dataProvider().addFeatures(feats)
SYMBOL = QgsSymbol.defaultSymbol(pts.geometryType())
cats = [QgsRendererCategory(v, SYMBOL.clone(), v) for v in ("現存", "滅失済")]
pts.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))
roads = QgsVectorLayer("LineString?crs=EPSG:3857", "道路", "memory")
project.addMapLayer(pts)
root = project.layerTreeRoot()
group = root.addGroup("基図")
project.addMapLayer(roads, False)
group.addLayer(roads)

canvas = QgsMapCanvas()
pan = QgsMapToolPan(canvas)
canvas.setMapTool(pan)


class Iface:
    def mapCanvas(self):
        return canvas
    def activeLayer(self):
        return ACTIVE[0]


ACTIVE = [pts]
iface = Iface()

def read(items=None):
    params = {} if items is None else {"items": items}
    return get_leftover_state(params, iface)

R = {}
project.setDirty(False)
R["before"] = read()["state"]

# テストが残しうるもの
pts.startEditing()
pts.changeAttributeValue(pts.getFeatures().__next__().id(), 0, "滅失済")
roads.startEditing()  # 変更なしで編集中
pts.selectByIds([f.id() for f in pts.getFeatures()][:2])
root.findLayer(roads.id()).setItemVisibilityChecked(False)
pts.renderer().checkLegendSymbolItem(pts.renderer().legendSymbolItems()[1].ruleKey(), False)
root.addGroup("作業")
ACTIVE[0] = roads
project.setDirty(True)
dlg = QDialog()
dlg.setWindowTitle("検索")
dlg.show()
_app.processEvents()

R["after"] = read()["state"]
R["only"] = sorted(read(["dialogs", "map_tool"])["state"])
R["bad"] = get_leftover_state({"items": ["dialog"]}, iface)
pts.rollBack()
roads.rollBack()
dlg.close()
RESULT = R
"""


@pytest.fixture(scope="module")
def r() -> dict[str, Any]:
    return run_in_qgis(_BODY, gui=True)


def test_editing_layers_with_and_without_changes(r: dict[str, Any]) -> None:
    assert r["before"]["editing"] == []
    editing = {layer["name"]: layer for layer in r["after"]["editing"]}
    assert editing["建物"]["modified"] is True
    assert editing["建物"]["undo"] >= 1
    assert editing["道路"]["modified"] is False
    assert editing["道路"]["undo"] == 0
    assert r["after"]["editing_modified"] == r["after"]["editing"]


def test_selection_counts_by_layer(r: dict[str, Any]) -> None:
    assert r["before"]["selection"] == {}
    [entry] = r["after"]["selection"].values()
    assert entry["name"] == "建物"
    assert entry["count"] == 2
    assert len(entry["ids"]) == 16  # 選んだ地物の id の要約（入れ替わりが分かる）


def test_layer_tree_visibility_and_legend_checks(r: dict[str, Any]) -> None:
    before, after = r["before"]["layer_tree"], r["after"]["layer_tree"]
    roads = [n for n in after["nodes"].values() if n["label"] == "基図 › 道路"]
    assert roads == [{"label": "基図 › 道路", "checked": False}]
    assert any(n == {"label": "基図", "checked": True} for n in after["nodes"].values())
    legend = {v["label"]: v["checked"] for v in after["legend"].values()}
    assert legend == {"建物 › 現存": True, "建物 › 滅失済": False}
    assert {v["label"]: v["checked"] for v in before["legend"].values()} == {
        "建物 › 現存": True,
        "建物 › 滅失済": True,
    }


def test_layers_groups_active_layer_and_map_tool(r: dict[str, Any]) -> None:
    assert sorted(r["after"]["layers"]["layers"].values()) == ["建物", "道路"]
    assert r["before"]["layers"]["groups"] == ["基図"]
    assert r["after"]["layers"]["groups"] == ["基図", "作業"]
    assert r["before"]["active_layer"]["name"] == "建物"
    assert r["after"]["active_layer"]["name"] == "道路"
    assert r["after"]["map_tool"] == "QgsMapToolPan"
    assert r["before"]["project_dirty"] is False
    assert r["after"]["project_dirty"] is True


def test_dialogs_and_popup(r: dict[str, Any]) -> None:
    assert r["before"]["dialogs"] == []
    assert r["after"]["dialogs"] == [
        {"class": "QDialog", "object_name": "", "title": "検索", "modal": False}
    ]
    assert r["after"]["popup"] is None


def test_only_the_asked_items_are_read(r: dict[str, Any]) -> None:
    assert r["only"] == ["dialogs", "map_tool"]
    assert r["bad"]["error"] == "invalid_argument"
