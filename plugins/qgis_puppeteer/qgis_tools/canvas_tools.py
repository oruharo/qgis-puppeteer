"""地図のキャンバスへのマウスとキーの操作（#26）。

QGIS のマップツール（作図・移動・選択など）は、キャンバスが受けたマウスとキーの
イベント（``canvasPressEvent`` / ``canvasMoveEvent`` / ``canvasReleaseEvent`` /
``canvasDoubleClickEvent`` / ``wheelEvent`` / ``keyPressEvent``）で動く。E2E で作図
などの流れを通すには、それを人の操作と同じ形で送る必要がある。

- 位置は**地図の座標**で受ける（既定はキャンバスの CRS。``crs`` を付ければそこから
  変換。``pixel=True`` なら viewport の画素）。画素への変換は、**実際に送る時点**の
  ``getCoordinateTransform()`` で行う（積んだあとに地図が動いても、その時点の位置に
  送る）。画素は ``QPointF`` のまま渡す
- マウスは ``canvas.viewport()`` に、キーは送り先（既定はキャンバス自身）に
  ``QApplication.sendEvent`` で送る
- click は「その位置へ move（ボタンなし）→ press → release」。人がマウスを動かして
  から押すのと同じにして、スナップの表示や輪郭の線（rubber band）、移動のツールで
  付いてくる地物を更新させる
- ``wait=False`` は #21 と同じ（右クリックで作図を完了すると属性フォームを
  ``exec()`` で出すツールがあるので、積んで返し、自動応答で答える）

どの操作を使うかは利用側が決める。ライブラリは、人がキャンバスにできる基本の操作
（クリック・ダブルクリック・押す・離す・動かす・ドラッグ・ホイール・キー・文字）を
一通り揃える。

送るのはイベントだけで、Qt のグローバルな状態（``QGuiApplication.keyboardModifiers()``
/ ``mouseButtons()``）は変わらない。修飾キーやボタンを、受けたイベントではなく
グローバルな状態から読むマップツールには届かない。
"""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt
from qgis.PyQt.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from qgis.PyQt.QtWidgets import QApplication

from qgis_puppeteer.qgis_tools import queued, ui_tools

_MOUSE_ACTIONS = ("click", "double", "press", "release", "move", "drag", "wheel")
_KEY_ACTIONS = ("tap", "press", "release")
_BUTTONS = {"left": Qt.LeftButton, "right": Qt.RightButton, "middle": Qt.MiddleButton}
_MODIFIERS = {
    "shift": Qt.ShiftModifier,
    "ctrl": Qt.ControlModifier,
    "alt": Qt.AltModifier,
    "meta": Qt.MetaModifier,
}
# ホイールの 1 ノッチ（Qt の angleDelta は 1/8 度単位で、1 ノッチ = 15 度）
_WHEEL_STEP = 120
_DEFAULT_DRAG_STEPS = 5
# drag の途中の move の上限（GUI スレッドでイベントを送り続けないように）
_MAX_DRAG_STEPS = 1000


# ============================================================
# 公開ハンドラ
# ============================================================


def canvas_mouse(params: dict, iface: Any = None) -> dict:  # noqa: C901 - 引数の検査の列挙
    """``qgis_canvas_mouse`` ハンドラ。

    params:
        - ``action``: ``"click"``（既定）/ ``"double"`` / ``"press"`` / ``"release"`` /
          ``"move"`` / ``"drag"`` / ``"wheel"``
        - ``point``: ``[x, y]``（``drag`` 以外）。``points``: ``[[x, y], ...]``（``drag``、
          2 点以上。間を ``steps`` 回の move で埋める）
        - ``button``: ``"left"``（既定）/ ``"right"`` / ``"middle"``
        - ``modifiers``: ``["shift", "ctrl", "alt", "meta"]`` の組み合わせ
        - ``crs``: 座標の CRS（``"EPSG:6677"`` など）。既定はキャンバスの CRS
        - ``pixel``: True なら ``point`` は viewport の画素
        - ``steps``: ``drag`` の途中の move の数（既定 5）
        - ``delta``: ``wheel`` のノッチ数（正で奥へ回す＝拡大。既定 1）
        - ``canvas``: 操作するキャンバスの selector。既定は ``iface.mapCanvas()``
        - ``wait``: False なら積んですぐ返す（#21）

    Returns:
        ``pixel`` / ``map_point``（マップツールが受け取る地図の点。画素に丸めるので、
        頼んだ座標から最大で半画素ずれる）、``map_units_per_pixel``、``map_tool``
        （そのときのマップツールのクラス名）。``drag`` は ``pixels`` / ``map_points``。
        ``wait=True`` なら、送ったあとの ``extent`` も。
    """
    canvas, error = _resolve_canvas(params.get("canvas"), iface)
    if error is not None:
        return error
    action = params.get("action", "click")
    if action not in _MOUSE_ACTIONS:
        return _invalid_argument(f"action must be one of {_MOUSE_ACTIONS}, not {action!r}")
    button_name = params.get("button", "left")
    if button_name not in _BUTTONS:
        return _invalid_argument(f"button must be one of {tuple(_BUTTONS)}, not {button_name!r}")
    modifiers, error = _parse_modifiers(params.get("modifiers"))
    if error is not None:
        return error
    crs = params.get("crs")
    pixel_mode = bool(params.get("pixel", False))
    if crs is not None and pixel_mode:
        return _invalid_argument("crs and pixel cannot be used together")
    _, error = _source_transform(canvas, crs)  # 知らない CRS は、積む前に断る
    if error is not None:
        return error

    if action == "drag":
        raw_points = params.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            return _invalid_argument("drag needs points: [[x, y], [x, y], ...] (2 or more)")
        steps = params.get("steps", _DEFAULT_DRAG_STEPS)
        if not ui_tools._is_index(steps) or not 0 <= steps <= _MAX_DRAG_STEPS:
            return _invalid_argument(
                f"steps must be an integer from 0 to {_MAX_DRAG_STEPS}, not {steps!r}"
            )
    else:
        raw_points = [params.get("point")]
        steps = 0
    points: list[tuple[float, float]] = []
    for raw in raw_points:
        point = _as_point(raw)
        if point is None:
            return _invalid_argument(f"a point must be [x, y], not {raw!r}")
        points.append(point)
    delta = params.get("delta", 1)
    if action == "wheel" and (not ui_tools._is_index(delta) or delta == 0):
        return _invalid_argument(f"delta must be a non-zero integer, not {delta!r}")

    def pixels_now() -> tuple[list[QPointF] | None, dict | None]:
        """今の地図で画素に直し、どの点も viewport の中にあるか確かめる。

        積んだ処理は、送る時点でもう一度呼ぶ（積んだあとに地図や CRS が変わりうる）。
        判定は、実際に届く丸めた画素（``toPoint()``）で行う。別の widget（メッセージバーの
        帯、キャンバスの上に浮かぶ入力欄、重なった窓など）に覆われた点は、人には押せない
        ので ``point_covered``（#33）。
        """
        viewport = canvas.viewport()
        if viewport.width() <= 0 or viewport.height() <= 0 or not canvas.isVisible():
            return None, {
                "success": False,
                "error": "canvas_not_ready",
                "canvas": _canvas_summary(canvas),
            }
        transform, error = _source_transform(canvas, crs)
        if error is not None:
            return None, error
        found = []
        for point in points:
            try:
                pos = _to_pixel(canvas, point, transform, pixel_mode)
            except Exception as exc:  # noqa: BLE001 - QgsCsException（CRS の範囲外など）
                return None, {
                    "success": False,
                    "error": "transform_failed",
                    "point": list(point),
                    "message": str(exc),
                }
            if not _inside(viewport, pos):
                return None, {
                    "success": False,
                    "error": "point_outside_canvas",
                    "point": list(point),
                    "pixel": [pos.x(), pos.y()],
                    "size": [viewport.width(), viewport.height()],
                    "extent": _extent(canvas),
                }
            covered = ui_tools.covered_point(viewport, pos)
            if covered is not None:
                covered["point"] = list(point)
                return None, covered
            found.append(pos)
        return found, None

    # 入れる前の確認：今の地図で、どの点も viewport の中にあること（人には外は押せない）
    pixels, error = pixels_now()
    if error is not None:
        return error
    button = _BUTTONS[button_name]

    def send() -> dict | None:
        # 画素への変換と範囲の確かめは、実際に送るこの時点の地図で
        at_send, failure = pixels_now()
        if failure is not None:
            return failure  # 積んだ処理なら recent_exceptions に残る
        _send_mouse(canvas, action, at_send, button, modifiers, steps=steps, delta=delta)
        return None

    result: dict[str, Any] = {"success": True, "action": action}
    _describe_points(canvas, pixels, action, result)
    result["map_units_per_pixel"] = canvas.mapUnitsPerPixel()
    result["map_tool"] = _map_tool_name(canvas)
    if not params.get("wait", True):
        # pixel / map_point は頼んだ時点の地図での値。送る時点で地図が動いていれば変わる
        queued.run_later(send, what="qgis_canvas_mouse", target=canvas)
        result["queued"] = True
        return result
    failure = send()
    if failure is not None:
        return failure
    result["extent"] = _extent(canvas)
    return result


def canvas_key(params: dict, iface: Any = None) -> dict:
    """``qgis_canvas_key`` ハンドラ。

    params:
        - ``key``: キーの名前（Qt の ``Qt.Key_<名前>`` の ``<名前>``。``"Escape"`` /
          ``"Backspace"`` / ``"Delete"`` / ``"Return"`` / ``"Enter"`` / ``"Tab"`` /
          ``"Space"`` / ``"Left"`` / ``"F2"`` など）か、1 文字
        - ``text``: 打つ文字（1 文字ずつ、text 付きの ``QKeyEvent`` の press / release）。
          ``key`` とどちらか一方
        - ``action``: ``key`` のとき ``"tap"``（既定。press と release）/ ``"press"`` /
          ``"release"``（押しっぱなし。QGIS のスペースキーを押している間のパンなど）
        - ``modifiers``: ``canvas_mouse`` と同じ
        - ``target``: 送り先。``"canvas"``（既定）/ ``"focus"``（フォーカスのある widget。
          無ければキャンバス）/ selector dict
        - ``canvas``: ``target="canvas"`` のときのキャンバスの selector
        - ``wait``: ``canvas_mouse`` と同じ
    """
    key_name = params.get("key")
    text = params.get("text")
    if (key_name is None) == (text is None):
        return _invalid_argument("give exactly one of key or text")
    action = params.get("action", "tap")
    if action not in _KEY_ACTIONS:
        return _invalid_argument(f"action must be one of {_KEY_ACTIONS}, not {action!r}")
    if text is not None and action != "tap":
        return _invalid_argument("text is typed key by key; action must be 'tap'")
    modifiers, error = _parse_modifiers(params.get("modifiers"))
    if error is not None:
        return error

    if key_name is not None:
        key = _key_code(key_name)
        if key is None:
            return _invalid_argument(f"unknown key: {key_name!r}")
        strokes = [(key, _key_text(key_name), modifiers)]
    else:
        if not isinstance(text, str) or not text:
            return _invalid_argument("text must be a non-empty string")
        strokes = [_char_stroke(ch, modifiers) for ch in text]

    target_spec = params.get("target", "canvas")
    target, error = _resolve_key_target(target_spec, params.get("canvas"), iface)
    if error is not None:
        return error

    def send() -> None:
        receiver = target() if callable(target) else target
        for key, key_text, key_modifiers in strokes:
            if action in ("tap", "press"):
                QApplication.sendEvent(
                    receiver, QKeyEvent(QEvent.KeyPress, key, key_modifiers, key_text)
                )
            if action in ("tap", "release"):
                QApplication.sendEvent(
                    receiver, QKeyEvent(QEvent.KeyRelease, key, key_modifiers, key_text)
                )

    receiver_now = target() if callable(target) else target
    result: dict[str, Any] = {
        "success": True,
        "target": ui_tools._widget_summary(receiver_now),
        "keys": len(strokes),
    }
    if callable(target):
        # "focus" は送る時点のフォーカスに送る。target は頼んだ時点のもの
        result["target_mode"] = "focus"
    canvas = _canvas_of(receiver_now)
    if canvas is not None:
        result["map_tool"] = _map_tool_name(canvas)
    if not params.get("wait", True):
        queued.run_later(send, what="qgis_canvas_key", target=receiver_now)
        result["queued"] = True
        return result
    send()
    return result


# ============================================================
# キャンバスと座標
# ============================================================


def _map_canvas_class() -> Any:
    from qgis.gui import QgsMapCanvas

    return QgsMapCanvas


def _resolve_canvas(selector: Any, iface: Any) -> tuple[Any, dict | None]:
    """操作するキャンバス。selector があればそれ、無ければ ``iface.mapCanvas()``。"""
    if selector is not None:
        if not isinstance(selector, dict):
            return None, _invalid_argument("canvas must be a selector dict")
        widget, diagnostics = ui_tools._find_widget({"scope": "any", **selector})
        if widget is None:
            return None, {
                "success": False,
                "error": "canvas_not_found",
                "diagnostics": diagnostics,
            }
        if not isinstance(widget, _map_canvas_class()):
            return None, {
                "success": False,
                "error": "not_a_map_canvas",
                "widget": ui_tools._widget_summary(widget),
            }
        canvas = widget
    else:
        if iface is None:
            return None, {"success": False, "error": "canvas_not_found", "message": "no iface"}
        canvas = iface.mapCanvas()
    if not canvas.isEnabled():
        return None, {
            "success": False,
            "error": "widget_disabled",
            "widget": ui_tools._widget_summary(canvas),
        }
    return canvas, None


def _source_transform(canvas: Any, crs: Any) -> tuple[Any, dict | None]:
    """``crs`` の座標をキャンバスの CRS に直す変換（``crs`` が無ければ None）。"""
    if crs is None:
        return None, None
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject

    source = QgsCoordinateReferenceSystem(str(crs))
    if not source.isValid():
        return None, _invalid_argument(f"unknown crs: {crs!r}")
    destination = canvas.mapSettings().destinationCrs()
    return QgsCoordinateTransform(source, destination, QgsProject.instance()), None


def _to_pixel(canvas: Any, point: tuple[float, float], transform: Any, pixel_mode: bool) -> QPointF:
    """地図の点（か画素）を、今の地図で viewport の画素にする。"""
    if pixel_mode:
        return QPointF(point[0], point[1])
    from qgis.core import QgsPointXY

    xy = QgsPointXY(point[0], point[1])
    if transform is not None:
        xy = transform.transform(xy)
    pixel = canvas.getCoordinateTransform().transform(xy)
    return QPointF(pixel.x(), pixel.y())


def _inside(viewport: Any, pos: QPointF) -> bool:
    # マップツールが受けるのは丸めた整数の画素（QMouseEvent::pos()）なので、それで見る
    point = pos.toPoint()
    return 0 <= point.x() < viewport.width() and 0 <= point.y() < viewport.height()


def _describe_points(canvas: Any, pixels: list[QPointF], action: str, result: dict) -> None:
    """送る画素と、マップツールが受け取る地図の点を戻り値に入れる。

    マップツールは ``QgsMapMouseEvent`` の整数の画素から地図の点を求めるので、
    ``toPoint()`` してから ``toMapCoordinates``（利用側の確かめと同じ計算）。
    """

    def map_point(pos: QPointF) -> list[float]:
        xy = canvas.getCoordinateTransform().toMapCoordinates(pos.toPoint())
        return [xy.x(), xy.y()]

    if action == "drag":
        result["pixels"] = [[p.x(), p.y()] for p in pixels]
        result["map_points"] = [map_point(p) for p in pixels]
    else:
        result["pixel"] = [pixels[0].x(), pixels[0].y()]
        result["map_point"] = map_point(pixels[0])


def _extent(canvas: Any) -> list[float]:
    e = canvas.extent()
    return [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]


def _map_tool_name(canvas: Any) -> str | None:
    tool = canvas.mapTool()
    return type(tool).__name__ if tool is not None else None


def _canvas_summary(canvas: Any) -> dict:
    summary = ui_tools._widget_summary(canvas)
    summary["size"] = [canvas.viewport().width(), canvas.viewport().height()]
    return summary


def _canvas_of(widget: Any) -> Any:
    """``widget`` がキャンバス（かその viewport）なら、そのキャンバス。"""
    cls = _map_canvas_class()
    if isinstance(widget, cls):
        return widget
    parent = widget.parent() if widget is not None else None
    return parent if isinstance(parent, cls) else None


# ============================================================
# イベントを送る
# ============================================================


def _send_mouse(
    canvas: Any,
    action: str,
    pixels: list[QPointF],
    button: Any,
    modifiers: Any,
    *,
    steps: int,
    delta: int,
) -> None:
    viewport = canvas.viewport()

    def mouse(kind: Any, pos: QPointF, which: Any, held: Any) -> None:
        # globalPos は整数の点から（global の位置で出るメニューなどのため）
        global_pos = QPointF(viewport.mapToGlobal(pos.toPoint()))
        QApplication.sendEvent(viewport, QMouseEvent(kind, pos, global_pos, which, held, modifiers))

    def move(pos: QPointF, held: Any = Qt.NoButton) -> None:
        mouse(QEvent.MouseMove, pos, Qt.NoButton, held)

    def press(pos: QPointF) -> None:
        mouse(QEvent.MouseButtonPress, pos, button, button)

    def release(pos: QPointF) -> None:
        mouse(QEvent.MouseButtonRelease, pos, button, Qt.NoButton)

    first = pixels[0]
    if action == "move":
        move(first)
    elif action == "press":
        move(first)
        press(first)
    elif action == "release":
        release(first)
    elif action == "click":
        move(first)
        press(first)
        release(first)
    elif action == "double":
        move(first)
        press(first)
        release(first)
        mouse(QEvent.MouseButtonDblClick, first, button, button)
        release(first)
    elif action == "drag":
        move(first)
        press(first)
        for start, end in zip(pixels, pixels[1:], strict=False):
            for i in range(1, steps + 1):
                t = i / (steps + 1)
                move(
                    QPointF(
                        start.x() + (end.x() - start.x()) * t, start.y() + (end.y() - start.y()) * t
                    ),
                    button,
                )
            move(end, button)
        release(pixels[-1])
    elif action == "wheel":
        move(first)
        global_pos = QPointF(viewport.mapToGlobal(first.toPoint()))
        QApplication.sendEvent(
            viewport,
            QWheelEvent(
                first,
                global_pos,
                QPoint(0, 0),
                QPoint(0, _WHEEL_STEP * delta),
                Qt.NoButton,
                modifiers,
                Qt.NoScrollPhase,
                False,
            ),
        )


def _resolve_key_target(spec: Any, canvas_selector: Any, iface: Any) -> tuple[Any, dict | None]:
    """キーの送り先。``"focus"`` は送る時点のフォーカスで決めるので関数を返す。"""
    if spec == "canvas":
        return _resolve_canvas(canvas_selector, iface)
    if spec == "focus":
        canvas = None
        if canvas_selector is not None or iface is not None:
            canvas, error = _resolve_canvas(canvas_selector, iface)
            if error is not None and canvas_selector is not None:
                return None, error  # 指したキャンバスが無いことを、そのまま知らせる

        def focused() -> Any:
            widget = QApplication.focusWidget()
            return widget if widget is not None else canvas

        if focused() is None:
            return None, {"success": False, "error": "no_focus_widget"}
        return focused, None
    if isinstance(spec, dict):
        widget, error = ui_tools._resolve_actionable_widget(spec)
        if error is not None:
            return None, error
        return widget, None
    return None, _invalid_argument(f"target must be 'canvas', 'focus' or a selector, not {spec!r}")


def _key_code(name: Any) -> Any:
    if not isinstance(name, str) or not name:
        return None
    if len(name) == 1:
        return _char_key(name)
    return getattr(Qt, f"Key_{name}", None)


def _char_key(ch: str) -> int:
    """1 文字のキーの番号。ASCII の印字できる文字は Qt の番号が大文字の番号と同じ。

    それ以外（かな・漢字など）は 0（キーの番号なし）で、文字は text で届く。
    ``Qt.Key_unknown`` は 0x01ffffff で、0 とは別。
    """
    upper = ch.upper()
    if len(upper) == 1 and 0x20 <= ord(upper) < 0x7F:
        return ord(upper)
    return 0


# 打つ文字のうち、キーで送るもの（改行は Return、タブは Tab）
_CONTROL_CHARS = {"\n": ("Key_Return", "\r"), "\r": ("Key_Return", "\r"), "\t": ("Key_Tab", "\t")}


def _char_stroke(ch: str, modifiers: Any) -> tuple[int, str, Any]:
    """打つ 1 文字を ``(キーの番号, 文字, 修飾キー)`` にする。

    改行・タブはそのキーにする（``returnPressed`` を待つ欄に届くように）。大文字は、
    人が打つときと同じく Shift を付ける。
    """
    if ch in _CONTROL_CHARS:
        name, key_text = _CONTROL_CHARS[ch]
        return getattr(Qt, name), key_text, modifiers
    if ch.isalpha() and ch.isupper() and ch.isascii():
        return _char_key(ch), ch, modifiers | Qt.ShiftModifier
    return _char_key(ch), ch, modifiers


def _key_text(name: str) -> str:
    """名前で押すキーが文字を持つなら、その文字（1 文字の名前と Space）。"""
    if len(name) == 1:
        return name
    if name == "Space":
        return " "
    return ""


# ============================================================
# 引数
# ============================================================


def _parse_modifiers(raw: Any) -> tuple[Any, dict | None]:
    combined = Qt.NoModifier
    if raw is None:
        return combined, None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return None, _invalid_argument(f"modifiers must be a list, not {raw!r}")
    for name in raw:
        if name not in _MODIFIERS:
            return None, _invalid_argument(
                f"modifiers must be from {tuple(_MODIFIERS)}, not {name!r}"
            )
        combined = combined | _MODIFIERS[name]
    return combined, None


def _as_point(raw: Any) -> tuple[float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    x, y = raw
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return float(x), float(y)


def _invalid_argument(message: str) -> dict:
    return {"success": False, "error": "invalid_argument", "message": message}
