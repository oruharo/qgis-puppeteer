"""`qgis_render_map` を本物の QGIS で描画して確かめる（#6）。

QGIS の Python でしか走らない（`scripts/qt_tests.py`）。venv と CI では skip。
QgsApplication は子プロセスで立てる（`_qgis_child` 参照）。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("qgis.core")

from tests._qgis_child import RED_BOX_LAYER, run_in_qgis  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def renders(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """1 回の QGIS 起動でまとめて描画し、結果を返す（起動が数秒かかるため）。"""
    out = tmp_path_factory.mktemp("render")
    return run_in_qgis(
        RED_BOX_LAYER
        + f"""
from qgis.PyQt.QtGui import QImage
from qgis_puppeteer.qgis_tools.render_tools import render_map

OUT = {str(out)!r}

def shot(name, **kw):
    res = render_map(os.path.join(OUT, name + ".png"), **kw)
    if res.get("success"):
        img = QImage(res["path"])
        res["center_rgba"] = hex(img.pixel(img.width() // 2, img.height() // 2))
        res["corner_rgba"] = hex(img.pixel(1, 1))
        res["dpm"] = img.dotsPerMeterX()
        with open(res["path"], "rb") as fh:
            res["bytes"] = fh.read().hex()
    return res

box = [0, 0, 10, 10]

# レイヤツリーで非表示にした別のレイヤと、地物の無いレイヤ
from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
far = QgsVectorLayer("Polygon?crs=EPSG:4326", "far", "memory")
_g = QgsFeature()
_g.setGeometry(QgsGeometry.fromWkt("POLYGON((100 100, 110 100, 110 110, 100 110, 100 100))"))
far.dataProvider().addFeatures([_g])
QgsProject.instance().addMapLayer(far)
QgsProject.instance().layerTreeRoot().findLayer(far.id()).setItemVisibilityChecked(False)
empty = QgsVectorLayer("Polygon?crs=EPSG:4326", "empty", "memory")
QgsProject.instance().addMapLayer(empty)

RESULT = {{
    "far": shot("far", layers=[far.id()], width=64, height=64),
    "empty": shot("x", layers=[empty.id()]),
    "default_path_1": render_map(width=8, height=8),
    "default_path_2": render_map(width=8, height=8),
    "a": shot("a", extent=box, width=200, height=100, background_color="#ffffff"),
    "b": shot("b", extent=box, width=200, height=100, background_color="#ffffff"),
    "dpi": shot("dpi", extent=box, width=200, height=100, dpi=300),
    "bg": shot("bg", extent=[20, 20, 30, 30], width=50, height=50, background_color="#00ff00"),
    "only": shot("only", layers=[layer.id()], width=64, height=64),
    "mercator": shot("merc", crs="EPSG:3857", width=64, height=64),
    "bad_layer": shot("x", layers=["no-such-layer"]),
    "bad_crs": shot("x", crs="EPSG:999999"),
    "bad_extent": shot("x", extent=[1, 0, 0, 1]),
}}
"""
    )


def test_same_input_gives_byte_identical_png(renders: dict[str, Any]) -> None:
    a, b = renders["a"], renders["b"]
    assert a["success"] and b["success"], (a, b)
    assert a["bytes"] == b["bytes"]


def test_size_and_dpi_are_what_was_asked(renders: dict[str, Any]) -> None:
    a = renders["a"]
    assert (a["width"], a["height"], a["dpi"]) == (200, 100, 96.0)
    assert a["dpm"] == 3780
    assert renders["dpi"]["dpm"] == 11811


def test_the_map_is_actually_drawn(renders: dict[str, Any]) -> None:
    """以前の headless 撮影は透明な画像を保存していた（#7）。中身があること。"""
    assert renders["a"]["center_rgba"] == "0xffff0000"  # 赤いポリゴンの中
    assert renders["a"]["corner_rgba"] == "0xffffffff"  # 背景


def test_extent_is_widened_to_the_image_aspect(renders: dict[str, Any]) -> None:
    """10x10 の範囲を 200x100 に描くと、横に広げて歪めない。"""
    e = renders["a"]["extent"]
    width, height = e["xmax"] - e["xmin"], e["ymax"] - e["ymin"]
    assert height == pytest.approx(10.0)
    assert width == pytest.approx(20.0)


def test_background_color(renders: dict[str, Any]) -> None:
    assert renders["bg"]["center_rgba"] == "0xff00ff00"


def test_layers_and_crs_overrides(renders: dict[str, Any]) -> None:
    only = renders["only"]
    assert only["success"], only
    assert len(only["layers"]) == 1
    merc = renders["mercator"]
    assert merc["success"], merc
    assert merc["crs"] == "EPSG:3857"
    # 範囲を指定せず CRS だけ変えたら、元の範囲（度）をメートルに変換して使う
    assert merc["extent"]["xmax"] > 1_000_000


def test_render_errors_are_reported(renders: dict[str, Any]) -> None:
    assert renders["a"]["render_errors"] == []


def test_headless_layers_without_extent_draw_those_layers(renders: dict[str, Any]) -> None:
    """キャンバスが無いとき、範囲はレイヤツリーで表示中のものではなく指定レイヤから。"""
    far = renders["far"]
    assert far["success"], far
    assert far["extent"]["xmin"] >= 99 and far["extent"]["xmax"] <= 111
    assert far["center_rgba"] != "0x0"


def test_default_output_paths_do_not_collide(renders: dict[str, Any]) -> None:
    """同時に描いた別の QGIS の画像を読み違えないよう、既定の保存先は毎回別。"""
    assert renders["default_path_1"]["path"] != renders["default_path_2"]["path"]


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("bad_layer", "unknown layer ids"),
        ("bad_crs", "unknown crs"),
        ("bad_extent", "xmin < xmax"),
        ("empty", "nothing to draw"),
    ],
)
def test_bad_parameters_fail_with_a_reason(renders: dict[str, Any], key: str, message: str) -> None:
    res = renders[key]
    assert res["success"] is False
    assert message in res["error"]
