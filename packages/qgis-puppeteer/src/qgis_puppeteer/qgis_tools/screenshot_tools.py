"""QGIS キャンバス / メインウィンドウのスクリーンショット取得ツール群。

`iface.mapCanvas()` や `iface.mainWindow()` をレンダリングして PNG に落とす。
"""

import os
import tempfile

from qgis.core import QgsProject
from qgis.gui import QgisInterface


def take_screenshot(
    output_path: str | None = None,
    width: int | None = None,
    height: int | None = None,
    iface: QgisInterface | None = None,
) -> dict:
    """Capture a screenshot of the QGIS map canvas.

    Args:
        output_path: Path to save the screenshot (optional, uses temp file if not provided)
        width: Width of the screenshot in pixels (optional, uses canvas size if not provided)
        height: Height of the screenshot in pixels (optional, uses canvas size if not provided)
        iface: QGIS interface instance (optional)

    Returns:
        Dictionary with screenshot information:
        - success: Whether screenshot was successful
        - path: Path to saved screenshot
        - width: Width of screenshot
        - height: Height of screenshot
    """
    try:
        # Get map canvas
        if iface is not None:
            canvas = iface.mapCanvas()
        else:
            # In headless mode, create a QgsMapSettings and render
            from qgis.core import QgsMapRendererParallelJob, QgsMapSettings
            from qgis.PyQt.QtCore import QSize
            from qgis.PyQt.QtGui import QImage, QPainter

            # Create map settings
            settings = QgsMapSettings()
            project = QgsProject.instance()

            # Set layers
            layers = [layer for layer in project.mapLayers().values()]
            settings.setLayers(layers)

            # Set extent
            if layers:
                extent = layers[0].extent()
                for layer in layers[1:]:
                    extent.combineExtentWith(layer.extent())
                settings.setExtent(extent)

            # Set output size
            if width and height:
                settings.setOutputSize(QSize(width, height))
            else:
                settings.setOutputSize(QSize(800, 600))

            # Render map
            image = QImage(settings.outputSize(), QImage.Format_ARGB32)
            image.fill(0)

            painter = QPainter(image)
            job = QgsMapRendererParallelJob(settings)
            job.start()
            job.waitForFinished()

            painter.end()

            # Save image
            if output_path is None:
                output_path = os.path.join(tempfile.gettempdir(), "qgis_screenshot.png")

            # Ensure directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            image.save(output_path)

            return {
                "success": True,
                "path": output_path,
                "width": settings.outputSize().width(),
                "height": settings.outputSize().height(),
                "mode": "headless",
            }

        # GUI mode - use map canvas
        if output_path is None:
            output_path = os.path.join(tempfile.gettempdir(), "qgis_screenshot.png")

        # Ensure directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Get canvas size
        if width is None:
            width = canvas.width()
        if height is None:
            height = canvas.height()

        # Save screenshot
        canvas.saveAsImage(output_path)

        return {
            "success": True,
            "path": output_path,
            "width": width,
            "height": height,
            "mode": "gui",
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


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
) -> dict:
    """Set the extent of the map canvas.

    Args:
        xmin: Minimum X coordinate
        ymin: Minimum Y coordinate
        xmax: Maximum X coordinate
        ymax: Maximum Y coordinate
        iface: QGIS interface instance (required for GUI mode)

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
        canvas.setExtent(extent)
        canvas.refresh()

        return {
            "success": True,
            "extent": {
                "xmin": xmin,
                "ymin": ymin,
                "xmax": xmax,
                "ymax": ymax,
            },
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }
