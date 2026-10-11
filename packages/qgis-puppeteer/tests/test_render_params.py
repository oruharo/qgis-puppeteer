"""`render_tools` の引数検証（QGIS 非依存の部分）。

描画そのものは QGIS が要るので `test_render_tools_integration.py`
（`scripts/qt_tests.py` で QGIS の Python から回す）で見る。
"""

from __future__ import annotations

import pytest
from qgis_puppeteer.qgis_tools.render_tools import (
    DEFAULT_DPI,
    DEFAULT_SIZE,
    MAX_SIDE_PX,
    RenderParamError,
    dots_per_meter,
    parse_dpi,
    parse_extent,
    parse_size,
)


class TestParseExtent:
    def test_list_and_dict_forms(self) -> None:
        assert parse_extent([0, 1, 2, 3]) == (0.0, 1.0, 2.0, 3.0)
        assert parse_extent({"xmin": 0, "ymin": 1, "xmax": 2, "ymax": 3}) == (0.0, 1.0, 2.0, 3.0)

    def test_none_means_use_the_default(self) -> None:
        assert parse_extent(None) is None

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ([0, 1, 2], r"\[xmin, ymin, xmax, ymax\]"),
            ("0,1,2,3", r"\[xmin, ymin, xmax, ymax\]"),
            ({"xmin": 0, "ymin": 1, "xmax": 2}, "ymax"),
            ([0, 1, "a", 3], "numbers"),
            ([2, 0, 1, 3], "xmin < xmax"),
            ([0, 3, 1, 3], "ymin < ymax"),
        ],
    )
    def test_rejects_malformed_extents(self, value: object, message: str) -> None:
        with pytest.raises(RenderParamError, match=message):
            parse_extent(value)


class TestParseSize:
    def test_default(self) -> None:
        assert parse_size(None, None) == DEFAULT_SIZE

    def test_both_or_neither(self) -> None:
        with pytest.raises(RenderParamError, match="both"):
            parse_size(100, None)

    @pytest.mark.parametrize(("w", "h"), [(0, 10), (10, -1), (MAX_SIDE_PX + 1, 10)])
    def test_bounds(self, w: int, h: int) -> None:
        with pytest.raises(RenderParamError, match="between"):
            parse_size(w, h)

    def test_accepts_numeric_strings_from_json(self) -> None:
        assert parse_size("320", "240") == (320, 240)


class TestParseDpi:
    def test_default_and_value(self) -> None:
        assert parse_dpi(None) == DEFAULT_DPI
        assert parse_dpi(192) == 192.0

    @pytest.mark.parametrize("dpi", [0, -5, 5000, "x"])
    def test_rejects(self, dpi: object) -> None:
        with pytest.raises(RenderParamError):
            parse_dpi(dpi)


def test_dots_per_meter_matches_the_png_convention() -> None:
    # 96 dpi = 3780 dots/m（PNG の pHYs で一般的な値）
    assert dots_per_meter(96) == 3780
    assert dots_per_meter(300) == 11811
