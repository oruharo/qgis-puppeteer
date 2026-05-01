"""QGIS レイヤー操作ツール群。

`QgsProject` / `QgsVectorLayer` に対する問い合わせ・選択・属性取得を
`qgis_puppet` の handler として公開する。戻り値はプロトコル層
(`_coerce_leaf`) が QVariant / QDate 等を JSON 化直前に coerce する。
"""

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgisInterface

# QVariant（NULL フィールド）や QDate / QByteArray 等の Qt 型は
# `protocol._coerce_leaf` が JSON 化直前にまとめて coerce する。
# ここでは feature.attribute(...) の戻り値をそのまま辞書に入れてよい。


def list_layers(iface: QgisInterface | None = None) -> dict:
    """Get list of all layers in the current project.

    Args:
        iface: QGIS interface instance (optional)

    Returns:
        Dictionary with layer information:
        - layers: List of layer dictionaries with name, type, source, etc.
        - count: Total number of layers
    """
    try:
        project = QgsProject.instance()
        layers_info = []

        for layer in project.mapLayers().values():
            layer_dict = {
                "name": layer.name(),
                "id": layer.id(),
                "type": (layer.type().name if hasattr(layer.type(), "name") else str(layer.type())),
                "valid": layer.isValid(),
            }

            # Add vector layer specific info
            if isinstance(layer, QgsVectorLayer):
                layer_dict.update(
                    {
                        "feature_count": layer.featureCount(),
                        "geometry_type": (
                            layer.geometryType().name
                            if hasattr(layer.geometryType(), "name")
                            else str(layer.geometryType())
                        ),
                        "crs": layer.crs().authid(),
                        "source": layer.source(),
                    }
                )

            layers_info.append(layer_dict)

        return {
            "success": True,
            "layers": layers_info,
            "count": len(layers_info),
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_layer_info(layer_name: str, iface: QgisInterface | None = None) -> dict:
    """Get detailed information about a specific layer.

    Args:
        layer_name: Name of the layer
        iface: QGIS interface instance (optional)

    Returns:
        Dictionary with detailed layer information
    """
    try:
        project = QgsProject.instance()
        layers = project.mapLayersByName(layer_name)

        if not layers:
            return {
                "success": False,
                "error": f"Layer '{layer_name}' not found",
            }

        layer = layers[0]

        info = {
            "success": True,
            "name": layer.name(),
            "id": layer.id(),
            "type": (layer.type().name if hasattr(layer.type(), "name") else str(layer.type())),
            "valid": layer.isValid(),
            "crs": layer.crs().authid() if hasattr(layer, "crs") else None,
            "extent": (
                {
                    "xmin": layer.extent().xMinimum(),
                    "ymin": layer.extent().yMinimum(),
                    "xmax": layer.extent().xMaximum(),
                    "ymax": layer.extent().yMaximum(),
                }
                if hasattr(layer, "extent")
                else None
            ),
        }

        # Add vector layer specific info
        if isinstance(layer, QgsVectorLayer):
            fields_info = []
            for field in layer.fields():
                fields_info.append(
                    {
                        "name": field.name(),
                        "type": field.typeName(),
                        "length": field.length(),
                    }
                )

            info.update(
                {
                    "feature_count": layer.featureCount(),
                    "geometry_type": (
                        layer.geometryType().name
                        if hasattr(layer.geometryType(), "name")
                        else str(layer.geometryType())
                    ),
                    "source": layer.source(),
                    "fields": fields_info,
                    "selected_count": layer.selectedFeatureCount(),
                }
            )

        return info

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def select_features(
    layer_name: str,
    expression: str,
    iface: QgisInterface | None = None,
) -> dict:
    """Select features in a layer using an expression.

    Args:
        layer_name: Name of the layer
        expression: QGIS expression to filter features
        iface: QGIS interface instance (optional)

    Returns:
        Dictionary with selection results
    """
    try:
        project = QgsProject.instance()
        layers = project.mapLayersByName(layer_name)

        if not layers:
            return {
                "success": False,
                "error": f"Layer '{layer_name}' not found",
            }

        layer = layers[0]

        if not isinstance(layer, QgsVectorLayer):
            return {
                "success": False,
                "error": f"Layer '{layer_name}' is not a vector layer",
            }

        # Select features
        layer.selectByExpression(expression)
        selected_count = layer.selectedFeatureCount()

        return {
            "success": True,
            "layer_name": layer_name,
            "expression": expression,
            "selected_count": selected_count,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_selected_features(
    layer_name: str,
    limit: int = 100,
    iface: QgisInterface | None = None,
) -> dict:
    """Get selected features from a layer.

    Args:
        layer_name: Name of the layer
        limit: Maximum number of features to return (default: 100)
        iface: QGIS interface instance (optional)

    Returns:
        Dictionary with selected features
    """
    try:
        project = QgsProject.instance()
        layers = project.mapLayersByName(layer_name)

        if not layers:
            return {
                "success": False,
                "error": f"Layer '{layer_name}' not found",
            }

        layer = layers[0]

        if not isinstance(layer, QgsVectorLayer):
            return {
                "success": False,
                "error": f"Layer '{layer_name}' is not a vector layer",
            }

        # Get selected features
        selected = layer.selectedFeatures()
        features_data = []

        for i, feature in enumerate(selected):
            if i >= limit:
                break

            feature_dict = {
                "id": feature.id(),
                "attributes": {
                    field.name(): feature.attribute(field.name()) for field in layer.fields()
                },
            }

            # Add geometry if available
            if feature.hasGeometry():
                geom = feature.geometry()
                feature_dict["geometry"] = {
                    "type": (
                        geom.type().name if hasattr(geom.type(), "name") else str(geom.type())
                    ),
                    "wkt": geom.asWkt(),
                }

            features_data.append(feature_dict)

        return {
            "success": True,
            "layer_name": layer_name,
            "total_selected": len(selected),
            "returned_count": len(features_data),
            "limit": limit,
            "features": features_data,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }
