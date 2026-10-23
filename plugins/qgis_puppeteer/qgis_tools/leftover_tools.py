"""テストのあとに残った状態を読む（#28）。

同じ QGIS を次のテストも使うので、前のテストが残した状態（閉じ忘れの窓、編集中の
レイヤ、変わったままのマップツール、選択など）で次のテストの結果が変わる。
pytest 側はテストの始めと終わりに :func:`get_leftover_state` を呼んで比べ、始めに
無かったのに残ったものを知らせる。ここは「今の状態」を読むだけで、比べ方も片付けも
持たない（片付けは利用者のデータを壊しうるので、利用側の役目）。

読むのは頼まれた項目だけ（毎テスト 2 回呼ぶので、要らない項目は読まない）。
"""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtWidgets import QApplication, QDialog

ITEMS = (
    "dialogs",
    "popup",
    "editing",
    "editing_modified",
    "map_tool",
    "selection",
    "layer_tree",
    "layers",
    "active_layer",
    "project_dirty",
)


def get_leftover_state(params: dict, iface: Any = None) -> dict:
    """``qgis_get_leftover_state`` ハンドラ。

    params:
        - ``items``: 読む項目の list（:data:`ITEMS` から）。既定は全部

    Returns:
        ``{"success": True, "state": {項目: 値}}``。``editing`` と
        ``editing_modified`` は同じ値（編集中のレイヤの一覧。変更の有無は各レイヤの
        ``modified`` / ``undo`` で分かる）を返し、どちらで知らせるかは呼ぶ側が決める。
    """
    items = params.get("items")
    if items is None:
        items = list(ITEMS)
    if not isinstance(items, list) or not all(i in ITEMS for i in items):
        return {
            "success": False,
            "error": "invalid_argument",
            "message": f"items must be a list from {ITEMS}, not {items!r}",
        }
    readers = {
        "dialogs": _dialogs,
        "popup": _popup,
        "editing": _editing,
        "editing_modified": _editing,
        "map_tool": lambda: _map_tool(iface),
        "selection": _selection,
        "layer_tree": _layer_tree,
        "layers": _layers,
        "active_layer": lambda: _active_layer(iface),
        "project_dirty": _project_dirty,
    }
    return {"success": True, "state": {item: readers[item]() for item in items}}


# ============================================================
# 項目ごとの読み方
# ============================================================


def _window(widget: Any, *, modal: bool) -> dict:
    return {
        "class": type(widget).__name__,
        "object_name": widget.objectName(),
        "title": widget.windowTitle(),
        "modal": modal,
    }


def _dialogs() -> list[dict]:
    """見えているダイアログとモーダル（ドックは入らない）。"""
    app = QApplication.instance()
    if app is None:
        return []
    found: list[dict] = []
    seen: set[int] = set()
    modal = app.activeModalWidget()
    if modal is not None:
        found.append(_window(modal, modal=True))
        seen.add(id(modal))
    for top in app.topLevelWidgets():
        if id(top) in seen or not isinstance(top, QDialog) or not top.isVisible():
            continue
        found.append(_window(top, modal=top.isModal()))
    return found


def _popup() -> dict | None:
    app = QApplication.instance()
    popup = app.activePopupWidget() if app is not None else None
    if popup is None:
        return None
    info = {"class": type(popup).__name__, "object_name": popup.objectName()}
    title = popup.title() if hasattr(popup, "title") else popup.windowTitle()
    if title:
        info["title"] = title
    return info


def _editing() -> list[dict]:
    """編集中のベクタレイヤ。``modified`` は未保存の変更、``undo`` は取り消しの数。"""
    from qgis.core import QgsProject, QgsVectorLayer

    found = []
    for layer in QgsProject.instance().mapLayers().values():
        if isinstance(layer, QgsVectorLayer) and layer.isEditable():
            found.append(
                {
                    "id": layer.id(),
                    "name": layer.name(),
                    "modified": bool(layer.isModified()),
                    "undo": layer.undoStack().count() if layer.undoStack() else 0,
                }
            )
    return found


def _map_tool(iface: Any) -> str | None:
    if iface is None:
        return None
    tool = iface.mapCanvas().mapTool()
    return type(tool).__name__ if tool is not None else None


def _selection() -> dict[str, dict]:
    """選択のあるベクタレイヤの、選択の数（``{layer_id: {"name", "count"}}``）。"""
    from qgis.core import QgsProject, QgsVectorLayer

    counts = {}
    for layer_id, layer in QgsProject.instance().mapLayers().items():
        if isinstance(layer, QgsVectorLayer):
            n = layer.selectedFeatureCount()
            if n:
                counts[layer_id] = {"name": layer.name(), "count": n}
    return counts


def _layer_tree() -> dict:
    """レイヤツリーの表示のチェックと、凡例の項目のチェック。

    - ``nodes``: ``{鍵: {"label", "checked"}}``。レイヤは ``layer:<id>``、グループは
      ``group:<親/…/名前>``。``label`` はツリーの道筋（``グループ › レイヤ``）
    - ``legend``: 凡例の項目にチェックがあるレイヤ（分類の表示など）の、すべての項目。
      鍵は ``<layer_id>/<項目の鍵>``、``label`` は ``レイヤ › 項目``
    """
    from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer, QgsProject, QgsVectorLayer

    nodes: dict[str, dict] = {}

    def walk(group: Any, path: str, labels: list[str]) -> None:
        for child in group.children():
            if isinstance(child, QgsLayerTreeLayer):
                nodes[f"layer:{child.layerId()}"] = {
                    "label": " › ".join([*labels, child.name()]),
                    "checked": bool(child.itemVisibilityChecked()),
                }
            elif isinstance(child, QgsLayerTreeGroup):
                child_path = f"{path}/{child.name()}" if path else child.name()
                nodes[f"group:{child_path}"] = {
                    "label": " › ".join([*labels, child.name()]),
                    "checked": bool(child.itemVisibilityChecked()),
                }
                walk(child, child_path, [*labels, child.name()])

    walk(QgsProject.instance().layerTreeRoot(), "", [])

    legend: dict[str, dict] = {}
    for layer_id, layer in QgsProject.instance().mapLayers().items():
        renderer = layer.renderer() if isinstance(layer, QgsVectorLayer) else None
        if renderer is None or not renderer.legendSymbolItemsCheckable():
            continue
        for item in renderer.legendSymbolItems():
            key = item.ruleKey()
            if not key:
                continue
            legend[f"{layer_id}/{key}"] = {
                "label": f"{layer.name()} › {item.label()}",
                "checked": bool(renderer.legendSymbolItemChecked(key)),
            }
    return {"nodes": nodes, "legend": legend}


def _layers() -> dict:
    """プロジェクトのレイヤ（``{id: 名前}``）とグループ（``親/…/名前`` の list）。"""
    from qgis.core import QgsLayerTreeGroup, QgsProject

    groups: list[str] = []

    def walk(group: Any, path: str) -> None:
        for child in group.children():
            if isinstance(child, QgsLayerTreeGroup):
                child_path = f"{path}/{child.name()}" if path else child.name()
                groups.append(child_path)
                walk(child, child_path)

    walk(QgsProject.instance().layerTreeRoot(), "")
    layers = {lid: layer.name() for lid, layer in QgsProject.instance().mapLayers().items()}
    return {"layers": layers, "groups": groups}


def _active_layer(iface: Any) -> dict | None:
    if iface is None:
        return None
    layer = iface.activeLayer()
    return {"id": layer.id(), "name": layer.name()} if layer is not None else None


def _project_dirty() -> bool:
    from qgis.core import QgsProject

    return bool(QgsProject.instance().isDirty())
