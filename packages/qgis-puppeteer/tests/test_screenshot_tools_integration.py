"""`qgis_screenshot` を本物の QGIS で確かめる（#7）。

- headless では描画結果が保存される（以前は透明な画像だった）
- GUI では `width` / `height` は使われず、戻り値は保存した画像の実サイズ

QGIS の Python でしか走らない（`scripts/qt_tests.py`）。venv と CI では skip。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("qgis.core")

from tests._qgis_child import RED_BOX_LAYER, run_in_qgis  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def shots(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("shot")
    return run_in_qgis(
        RED_BOX_LAYER
        + f"""
from qgis.PyQt.QtGui import QColor, QImage
from qgis_puppeteer.qgis_tools.screenshot_tools import take_screenshot

OUT = {str(out)!r}


class _Canvas:
    \"\"\"saveAsImage だけを持つキャンバスの代役。書く画像の大きさを決められる。\"\"\"

    def __init__(self, size):
        self.size = size

    def saveAsImage(self, path):
        if self.size is None:
            return  # 何も書かない（保存に失敗したキャンバス）
        img = QImage(self.size[0], self.size[1], QImage.Format_ARGB32)
        img.fill(QColor("#123456"))
        img.save(path)


class _Iface:
    def __init__(self, size):
        self.canvas = _Canvas(size)

    def mapCanvas(self):
        return self.canvas


# 前回の画像が残っている場所に、何も書かないキャンバスで撮る
stale = os.path.join(OUT, "stale.png")
QImage(10, 10, QImage.Format_ARGB32).save(stale)

headless = take_screenshot(os.path.join(OUT, "headless.png"), 120, 80)
img = QImage(headless["path"])
headless["center_rgba"] = hex(img.pixel(img.width() // 2, img.height() // 2))

RESULT = {{
    "headless": headless,
    "headless_one_side": take_screenshot(os.path.join(OUT, "one.png"), 120, None),
    "gui": take_screenshot(os.path.join(OUT, "gui.png"), 800, 600, _Iface((123, 45))),
    "gui_not_written": take_screenshot(os.path.join(OUT, "none.png"), None, None, _Iface(None)),
    "gui_stale": take_screenshot(stale, None, None, _Iface(None)),
    "default_1": take_screenshot(None, 8, 8),
    "default_2": take_screenshot(None, 8, 8),
}}
"""
    )


def test_headless_saves_the_rendered_map_not_a_transparent_image(shots: dict[str, Any]) -> None:
    res = shots["headless"]
    assert res["success"], res
    assert (res["mode"], res["width"], res["height"]) == ("headless", 120, 80)
    assert res["center_rgba"] == "0xffff0000"  # 赤いポリゴン。以前は 0x0（透明）
    assert res["render_errors"] == []


def test_headless_with_only_one_side_falls_back_to_the_default_size(
    shots: dict[str, Any],
) -> None:
    res = shots["headless_one_side"]
    assert (res["width"], res["height"]) == (800, 600)


def test_gui_reports_the_size_of_the_saved_image_not_the_arguments(
    shots: dict[str, Any],
) -> None:
    res = shots["gui"]
    assert res["success"], res
    assert (res["mode"], res["width"], res["height"]) == ("gui", 123, 45)


def test_gui_says_so_when_nothing_was_written(shots: dict[str, Any]) -> None:
    res = shots["gui_not_written"]
    assert res["success"] is False
    assert "was not written" in res["error"]


def test_a_stale_file_is_not_reported_as_the_new_image(shots: dict[str, Any]) -> None:
    """saveAsImage が何も書かなかったら、古いファイルのサイズで成功にしない。"""
    assert shots["gui_stale"]["success"] is False


def test_default_paths_do_not_collide(shots: dict[str, Any]) -> None:
    assert shots["default_1"]["path"] != shots["default_2"]["path"]
