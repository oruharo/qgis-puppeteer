"""条件を固定して地図を画像に描画するツール（`qgis_render_map`）。

`qgis_screenshot` はキャンバスに今映っているものを保存するので、ウィンドウの
大きさや画面の表示スケールが変わると画像も変わる。画像スナップショットの比較
基準には使えない。こちらはキャンバスの見た目に依存せず、範囲・CRS・ピクセル
サイズ・DPI・レイヤを指定して描画する。

## 決定性

同じ入力・同じ環境なら同じ画像になるよう、次のようにしている。

- 逐次の描画ジョブ（`QgsMapRendererSequentialJob`）で描く。レイヤを 1 つずつ順に
  描き、並列ジョブのようにレイヤごとのスレッドに分けない
- `devicePixelRatio` を 1 に固定する（画面の表示スケールを持ち込まない）
- 編集中レイヤの頂点マーカーなど、編集状態の描画は切る
- 画像の DPI メタデータを `dpi` に合わせて書く

それでも画像が変わる要因は残る：ネットワーク越しのレイヤ（XYZ / WMS 等）、
`now()` など時刻に依存する式、シード無しのランダム配置、そしてスタイルを
固定していないレイヤ（QGIS は新しいベクタレイヤに毎回ランダムな色を付ける）。
フォントやアンチエイリアスの実装差があるので、別の機械では一致しない。基準
画像は環境ごとに持つこと。

## 範囲の扱い

`extent` は出力画像の縦横比に合わせて広げられる（地図は歪めない）。実際に
描画した範囲は戻り値の `extent` に入る。

描画が終わるまで、呼び出したスレッド（Worker では GUI スレッド）は待って止まる。
重い描画の間は Worker が Hub に応答しないので、一覧で一時的に unresponsive と
出ることがある。
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

# 1 辺の上限。誤って巨大な値を渡して GUI スレッドでメモリを食い潰さないため。
MAX_SIDE_PX = 10_000
DEFAULT_SIZE = (800, 600)
DEFAULT_DPI = 96.0
_INCH_IN_METERS = 0.0254


class RenderParamError(ValueError):
    """引数が不正。戻り値の ``error`` にそのまま載せる。"""


# ==============================================================
# 引数の検証（QGIS 非依存。venv の単体テストで回す）
# ==============================================================


def parse_extent(value: Any) -> tuple[float, float, float, float] | None:
    """``[xmin, ymin, xmax, ymax]`` か ``{"xmin": ..., ...}`` を受け付ける。"""
    if value is None:
        return None
    if isinstance(value, Mapping):
        try:
            raw = [value["xmin"], value["ymin"], value["xmax"], value["ymax"]]
        except KeyError as e:
            raise RenderParamError(f"extent is missing {e.args[0]!r}") from None
    elif isinstance(value, Sequence) and not isinstance(value, str):
        raw = list(value)
        if len(raw) != 4:
            raise RenderParamError("extent must be [xmin, ymin, xmax, ymax]")
    else:
        raise RenderParamError("extent must be [xmin, ymin, xmax, ymax] or a dict")
    try:
        xmin, ymin, xmax, ymax = (float(v) for v in raw)
    except (TypeError, ValueError):
        raise RenderParamError("extent values must be numbers") from None
    if not (xmin < xmax and ymin < ymax):
        raise RenderParamError("extent must have xmin < xmax and ymin < ymax")
    return xmin, ymin, xmax, ymax


def parse_size(width: Any, height: Any) -> tuple[int, int]:
    """出力サイズ。省略時は 800 x 600。片方だけの指定は受け付けない。"""
    if width is None and height is None:
        return DEFAULT_SIZE
    if width is None or height is None:
        raise RenderParamError("give both width and height, or neither")
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        raise RenderParamError("width and height must be integers") from None
    if not (0 < w <= MAX_SIDE_PX and 0 < h <= MAX_SIDE_PX):
        raise RenderParamError(f"width and height must be between 1 and {MAX_SIDE_PX}")
    return w, h


def parse_dpi(dpi: Any) -> float:
    if dpi is None:
        return DEFAULT_DPI
    try:
        value = float(dpi)
    except (TypeError, ValueError):
        raise RenderParamError("dpi must be a number") from None
    if not (1.0 <= value <= 2400.0):
        raise RenderParamError("dpi must be between 1 and 2400")
    return value


def dots_per_meter(dpi: float) -> int:
    """PNG の pHYs に書く値。"""
    return round(dpi / _INCH_IN_METERS)


# ==============================================================
# 描画
# ==============================================================


def render_map(
    output_path: str | None = None,
    *,
    extent: Any = None,
    crs: str | None = None,
    width: int | None = None,
    height: int | None = None,
    dpi: float | None = None,
    layers: Sequence[str] | None = None,
    background_color: str | None = None,
    antialiasing: bool = True,
    draw_labels: bool = True,
    draw_selection: bool = True,
    iface: Any = None,
) -> dict[str, Any]:
    """指定した条件で地図を描画し、PNG に保存する。

    Args:
        output_path: 保存先（省略時は一時ファイル）。
        extent: ``[xmin, ymin, xmax, ymax]``（``crs`` の座標）。省略時はキャンバスの
            範囲（キャンバスが無ければレイヤ全体）。``crs`` だけ変えた場合は、元の
            範囲をその CRS に変換して使う。
        crs: 出力 CRS（``"EPSG:3857"`` など）。省略時はキャンバス（無ければ
            プロジェクト）の CRS。
        width / height: 出力ピクセル。省略時は 800 x 600。
        dpi: 出力 DPI（シンボル・ラベルの大きさに効く）。省略時は 96。
        layers: 描画するレイヤ ID（先頭が最前面）。省略時はキャンバスに表示中の
            レイヤ（キャンバスが無ければレイヤツリーで表示中のもの）。
        background_color: ``"#RRGGBB"`` / ``"#AARRGGBB"`` / 色名。省略時は
            キャンバス（無ければプロジェクト）の背景色。
        antialiasing / draw_labels / draw_selection: 描画フラグ。
        iface: QGIS インターフェース（``None`` なら headless）。

    Returns:
        ``success``, ``path``, ``width``, ``height``, ``dpi``, ``crs``,
        ``extent``（実際に描画した範囲）, ``layers``（描画したレイヤ ID）,
        ``render_ms``, ``render_errors``（レイヤごとの描画エラー）。失敗時は
        ``success: False`` と ``error``。
    """
    try:
        size = parse_size(width, height)
        out_dpi = parse_dpi(dpi)
        bbox = parse_extent(extent)
        settings = _build_settings(
            iface=iface,
            bbox=bbox,
            crs=crs,
            size=size,
            dpi=out_dpi,
            layer_ids=layers,
            background_color=background_color,
            antialiasing=antialiasing,
            draw_labels=draw_labels,
            draw_selection=draw_selection,
        )
    except RenderParamError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - handler の戻り値で返す
        return {"success": False, "error": f"{type(e).__name__}: {e}"}

    return _render_and_save(settings, output_path, out_dpi)


def _build_settings(
    *,
    iface: Any,
    bbox: tuple[float, float, float, float] | None,
    crs: str | None,
    size: tuple[int, int],
    dpi: float,
    layer_ids: Sequence[str] | None,
    background_color: str | None,
    antialiasing: bool,
    draw_labels: bool,
    draw_selection: bool,
) -> Any:
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsMapSettings,
        QgsProject,
        QgsRectangle,
    )
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor

    project = QgsProject.instance()
    if iface is not None:
        # キャンバスの設定を土台にする：CRS・座標変換の設定・レイヤ順・時間範囲・
        # ラベルエンジンの設定がキャンバスと揃う。描画条件だけ下で差し替える。
        settings = QgsMapSettings(iface.mapCanvas().mapSettings())
    else:
        settings = _headless_base_settings(project)

    base_crs = settings.destinationCrs()
    base_extent = settings.extent()
    resolved: list[Any] | None = None

    if layer_ids is not None:
        resolved = []
        missing = []
        for layer_id in layer_ids:
            layer = project.mapLayer(str(layer_id))
            if layer is None:
                missing.append(str(layer_id))
            else:
                resolved.append(layer)
        if missing:
            raise RenderParamError(f"unknown layer ids: {missing}")
        settings.setLayers(resolved)

    if crs is not None:
        target = QgsCoordinateReferenceSystem(crs)
        if not target.isValid():
            raise RenderParamError(f"unknown crs: {crs!r}")
        settings.setDestinationCrs(target)
        if bbox is None and base_crs.isValid() and base_crs != target:
            transform = QgsCoordinateTransform(base_crs, target, settings.transformContext())
            base_extent = transform.transformBoundingBox(base_extent)

    if bbox is not None:
        extent = QgsRectangle(*bbox)
    elif iface is None and resolved is not None:
        # キャンバスが無くレイヤを指定したなら、そのレイヤ全体を描く（レイヤツリーで
        # 表示中のものの範囲ではなく）
        extent = _layers_extent(settings, resolved)
    else:
        extent = base_extent
    if extent.isNull() or extent.isEmpty():
        raise RenderParamError(
            "nothing to draw: the extent is empty (no layers with features, or an empty "
            "canvas); pass extent, or layers that have features"
        )
    settings.setExtent(extent)

    settings.setOutputSize(QSize(*size))
    settings.setOutputDpi(dpi)
    if hasattr(settings, "setDevicePixelRatio"):
        settings.setDevicePixelRatio(1.0)

    if background_color is not None:
        color = QColor(background_color)
        if not color.isValid():
            raise RenderParamError(f"invalid background_color: {background_color!r}")
        settings.setBackgroundColor(color)

    flag = _map_settings_flags()
    settings.setFlag(flag.Antialiasing, bool(antialiasing))
    settings.setFlag(flag.DrawLabeling, bool(draw_labels))
    settings.setFlag(flag.DrawSelection, bool(draw_selection))
    # 編集中レイヤの頂点マーカー等。スナップショットに編集状態を持ち込まない
    settings.setFlag(flag.DrawEditingInfo, False)
    settings.setFlag(flag.RenderPartialOutput, False)
    return settings


def _map_settings_flags() -> Any:
    """描画フラグの列挙。QGIS 3.22 以降は ``Qgis.MapSettingsFlag``。"""
    from qgis.core import Qgis, QgsMapSettings

    return getattr(Qgis, "MapSettingsFlag", QgsMapSettings)


def _headless_base_settings(project: Any) -> Any:
    """キャンバスが無いときの土台：レイヤツリーで表示中のレイヤを全体範囲で。"""
    from qgis.core import QgsMapSettings

    settings = QgsMapSettings()
    layers = project.layerTreeRoot().checkedLayers()
    settings.setLayers(layers)
    settings.setTransformContext(project.transformContext())
    crs = project.crs()
    if not crs.isValid() and layers:
        crs = layers[0].crs()
    settings.setDestinationCrs(crs)
    settings.setBackgroundColor(project.backgroundColor())
    settings.setExtent(_layers_extent(settings, layers))
    return settings


def _layers_extent(settings: Any, layers: Sequence[Any]) -> Any:
    """レイヤ全体の範囲（出力 CRS）。レイヤごとに CRS が違ってもよいように変換して合わせる。"""
    from qgis.core import QgsRectangle

    extent = QgsRectangle()
    extent.setMinimal()
    for layer in layers:
        extent.combineExtentWith(settings.layerExtentToOutputExtent(layer, layer.extent()))
    return extent


def _render_and_save(settings: Any, output_path: str | None, dpi: float) -> dict[str, Any]:
    from qgis.core import QgsMapRendererSequentialJob

    if output_path is None:
        # 固定名だと、同時に描いた別の QGIS の画像を読み違える
        output_path = os.path.join(
            tempfile.gettempdir(), f"qgis_render_map-{os.getpid()}-{uuid.uuid4().hex[:8]}.png"
        )
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    started = time.perf_counter()
    job = QgsMapRendererSequentialJob(settings)
    job.start()
    job.waitForFinished()
    render_ms = (time.perf_counter() - started) * 1000.0

    image = job.renderedImage()
    dpm = dots_per_meter(dpi)
    image.setDotsPerMeterX(dpm)
    image.setDotsPerMeterY(dpm)
    if not image.save(output_path, "PNG"):
        return {"success": False, "error": f"could not save the image to {output_path}"}

    visible = settings.visibleExtent()
    crs = settings.destinationCrs()
    return {
        "success": True,
        "path": output_path,
        "width": image.width(),
        "height": image.height(),
        "dpi": dpi,
        "crs": crs.authid() or crs.toWkt(),
        "extent": {
            "xmin": visible.xMinimum(),
            "ymin": visible.yMinimum(),
            "xmax": visible.xMaximum(),
            "ymax": visible.yMaximum(),
        },
        "layers": [layer.id() for layer in settings.layers()],
        "render_ms": round(render_ms, 1),
        "render_errors": [{"layer_id": e.layerID, "message": e.message} for e in job.errors()],
    }
