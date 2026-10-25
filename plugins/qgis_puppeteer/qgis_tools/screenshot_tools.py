"""QGIS マップキャンバスの画像を保存するツール群。

撮れるのは**マップキャンバスの描画画像だけ**。メインウィンドウやダイアログ、
メッセージバーは写らない。サイズ・DPI・範囲を固定した地図画像が要るときは
`qgis_render_map` を使う（こちらはキャンバスの見た目に依存しない）。
"""

import contextlib
import os
import tempfile
import uuid

from qgis.core import QgsProject
from qgis.gui import QgisInterface, QgsMapCanvas

from . import queued


def take_screenshot(
    output_path: str | None = None,
    width: int | None = None,
    height: int | None = None,
    iface: QgisInterface | None = None,
) -> dict:
    """Save an image of the QGIS map canvas.

    GUI (``iface`` given): saves what the canvas currently shows, via
    ``QgsMapCanvas.saveAsImage``. The image size is the canvas's own, so it
    changes with the window size and the display scale; ``width`` / ``height``
    are ignored, and the returned ``width`` / ``height`` are read back from the
    saved file. For a fixed size, DPI or extent use ``qgis_render_map``.
    A canvas that has drawn nothing yet (no layer was ever added, so its
    extent is empty) is saved as its background colour. Fails while the
    canvas is not shown (QGIS shows its welcome page until a project or a
    layer is opened).

    Headless (no ``iface``): renders the layers shown in the layer tree over
    their combined extent at ``width`` x ``height`` (default 800 x 600), with
    ``qgis_render_map``'s renderer.

    Args:
        output_path: Where to save the PNG (a temp file if omitted).
        width: Headless only: image width in pixels.
        height: Headless only: image height in pixels.
        iface: QGIS interface instance (``None`` = headless).

    Returns:
        ``success``, ``path``, ``width`` / ``height`` (of the saved image),
        ``mode`` (``"gui"`` / ``"headless"``); headless also ``render_errors``.
    """
    try:
        if output_path is None:
            # 固定名だと、同時に撮った別の QGIS の画像を読み違える
            output_path = os.path.join(
                tempfile.gettempdir(), f"qgis_screenshot-{os.getpid()}-{uuid.uuid4().hex[:8]}.png"
            )
        directory = os.path.dirname(output_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        if iface is None:
            return _render_project_headless(output_path, width, height)

        canvas = iface.mapCanvas()
        if not canvas.isVisible():
            # プロジェクトもレイヤも開いていない QGIS は、キャンバスの代わりに
            # ようこそ画面を出している。隠れたキャンバスの大きさは意味が無い
            return {
                "success": False,
                "error": (
                    "the map canvas is not shown (QGIS shows its welcome page "
                    "until a project or a layer is opened)"
                ),
            }

        # saveAsImage は失敗しても何も言わない。古いファイルが残っていると、その
        # サイズを成功として返してしまうので先に消す
        with contextlib.suppress(FileNotFoundError):
            os.remove(output_path)
        if canvas.mapSettings().hasValidSettings():
            canvas.saveAsImage(output_path)
        else:
            # レイヤを一度も足していないキャンバスは範囲が空で、一度も描かない。
            # saveAsImage は空の画像を書けずに .pgw だけ残すので、画面と同じ
            # 背景色だけの画像を自分で書く
            _save_blank_canvas(canvas, output_path)
        size = _saved_image_size(output_path)
        if size is None:
            return {
                "success": False,
                "error": f"the canvas image was not written to {output_path}",
            }
        return {
            "success": True,
            "path": output_path,
            "width": size[0],
            "height": size[1],
            "mode": "gui",
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def _render_project_headless(output_path: str, width: int | None, height: int | None) -> dict:
    """キャンバスが無いときの撮影：レイヤツリーで表示中のレイヤを全体範囲で描く。

    描画は `render_tools.render_map` に任せる。以前はここで描画ジョブを回しながら
    結果を画像に描かずに保存していて、透明な画像になっていた（#7）。
    """
    from qgis_puppeteer.qgis_tools.render_tools import render_map

    # 以前と同じく、片方だけの指定は既定サイズ扱い
    size = {"width": width, "height": height} if width and height else {}
    result = render_map(output_path, **size)
    if not result.get("success"):
        return result
    return {
        "success": True,
        "path": result["path"],
        "width": result["width"],
        "height": result["height"],
        "mode": "headless",
        "render_errors": result["render_errors"],
    }


def _save_blank_canvas(canvas: QgsMapCanvas, path: str) -> None:
    """まだ何も描いていないキャンバスの見た目（背景色だけ）を PNG で書く。

    大きさは描いたときに saveAsImage が書く画像と同じ（デバイスピクセル）。
    """
    from qgis.PyQt.QtGui import QImage

    settings = canvas.mapSettings()
    size = settings.deviceOutputSize()
    image = QImage(size.width(), size.height(), QImage.Format_ARGB32)
    image.fill(settings.backgroundColor())
    image.save(path, "PNG")


def _saved_image_size(path: str) -> tuple[int, int] | None:
    """保存された画像の実サイズ。読めなければ None。"""
    from qgis.PyQt.QtGui import QImageReader

    reader = QImageReader(path)
    size = reader.size()
    if not size.isValid():
        return None
    return size.width(), size.height()


def get_canvas_extent(iface: QgisInterface | None = None) -> dict:
    """Get the current extent of the map canvas.

    Args:
        iface: QGIS interface instance (optional)

    Returns:
        Dictionary with extent information
    """
    try:
        if iface is not None:
            canvas = iface.mapCanvas()
            extent = canvas.extent()
        else:
            # In headless mode, get extent from project layers
            project = QgsProject.instance()
            layers = [layer for layer in project.mapLayers().values()]

            if not layers:
                return {
                    "success": False,
                    "error": "No layers in project",
                }

            extent = layers[0].extent()
            for layer in layers[1:]:
                extent.combineExtentWith(layer.extent())

        return {
            "success": True,
            "xmin": extent.xMinimum(),
            "ymin": extent.yMinimum(),
            "xmax": extent.xMaximum(),
            "ymax": extent.yMaximum(),
            "width": extent.width(),
            "height": extent.height(),
            "center": {
                "x": extent.center().x(),
                "y": extent.center().y(),
            },
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def set_canvas_extent(
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    iface: QgisInterface | None = None,
    *,
    wait: bool = True,
) -> dict:
    """Set the extent of the map canvas.

    Args:
        xmin: Minimum X coordinate
        ymin: Minimum Y coordinate
        xmax: Maximum X coordinate
        ymax: Maximum Y coordinate
        iface: QGIS interface instance (required for GUI mode)
        wait: False なら、範囲の変更を Qt のイベントループに積んですぐ返す（#21）。
            ``extentsChanged`` に結ばれた処理が ``exec()`` の確認を出す場合用。
            中で出た例外は recent_exceptions に残る。

    Returns:
        Dictionary with success status
    """
    try:
        if iface is None:
            return {
                "success": False,
                "error": "Cannot set extent in headless mode (no canvas available)",
            }

        from qgis.core import QgsRectangle

        canvas = iface.mapCanvas()
        extent = QgsRectangle(xmin, ymin, xmax, ymax)

        def apply() -> None:
            canvas.setExtent(extent)
            canvas.refresh()

        result: dict = {
            "success": True,
            "extent": {
                "xmin": xmin,
                "ymin": ymin,
                "xmax": xmax,
                "ymax": ymax,
            },
        }
        if not wait:
            queued.run_later(apply, what="qgis_set_canvas_extent", target=canvas)
            result["queued"] = True
            return result
        apply()
        return result

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }
