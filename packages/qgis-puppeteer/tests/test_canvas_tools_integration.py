"""地図のキャンバスへのマウスとキー（#26）。

``QgsMapCanvas`` と、受けたイベントを記録する ``QgsMapTool`` を、QGIS を初期化した
子プロセス（``tests._qgis_child.run_in_qgis``）に置いて確かめる。``qgis.core`` が
無ければ全件 skip。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("qgis.core")

from tests._qgis_child import run_in_qgis  # noqa: E402

pytestmark = pytest.mark.integration

_BODY = """
from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
from qgis.gui import QgsMapCanvas, QgsMapTool
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QLineEdit, QVBoxLayout, QWidget
from qgis_puppeteer.qgis_tools.canvas_tools import canvas_key, canvas_mouse

BUTTONS = {int(Qt.LeftButton): "left", int(Qt.RightButton): "right", int(Qt.MiddleButton): "middle"}

class Recorder(QgsMapTool):
    def __init__(self, canvas):
        super().__init__(canvas)
        self.log = []
    def _mouse(self, kind, e):
        p = e.mapPoint()
        self.log.append([kind, BUTTONS.get(int(e.button()), "none"), [p.x(), p.y()],
                         int(e.modifiers()), int(e.buttons())])
    def canvasPressEvent(self, e): self._mouse("press", e)
    def canvasReleaseEvent(self, e): self._mouse("release", e)
    def canvasMoveEvent(self, e): self._mouse("move", e)
    def canvasDoubleClickEvent(self, e): self._mouse("double", e)
    def keyPressEvent(self, e):
        self.log.append(["key_press", int(e.key()), e.text(), int(e.modifiers())])
    def keyReleaseEvent(self, e): self.log.append(["key_release", int(e.key()), e.text()])
    def wheelEvent(self, e):
        self.log.append(["wheel", e.angleDelta().y()])
        e.ignore()  # キャンバスに拡大縮小させる

host = QWidget()
host.setObjectName("host")
lay = QVBoxLayout(host)
canvas = QgsMapCanvas(host)
canvas.setObjectName("main")
mini = QgsMapCanvas(host)
mini.setObjectName("mini")
edit = QLineEdit(host)
edit.setObjectName("angle")
for c in (canvas, mini):
    lay.addWidget(c)
lay.addWidget(edit)
host.resize(420, 720)
host.show()
for c in (canvas, mini):
    c.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    c.setExtent(QgsRectangle(0, 0, 400, 300))
for _ in range(5):
    _app.processEvents()
tool = Recorder(canvas)
canvas.setMapTool(tool)
mini_tool = Recorder(mini)
mini.setMapTool(mini_tool)

class Iface:
    def mapCanvas(self):
        return canvas
iface = Iface()

def run(fn, params, target_tool=tool):
    target_tool.log = []
    result = fn(params, iface)
    for _ in range(5):
        _app.processEvents()
    return {"result": result, "log": list(target_tool.log)}

R = {}
R["mupp"] = canvas.mapUnitsPerPixel()
R["size"] = [canvas.viewport().width(), canvas.viewport().height()]
e = canvas.extent()
R["extent"] = [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]
cx, cy = (e.xMinimum() + e.xMaximum()) / 2, (e.yMinimum() + e.yMaximum()) / 2
R["center"] = [cx, cy]
R["click"] = run(canvas_mouse, {"point": [cx + 0.3, cy + 0.7]})
R["right"] = run(canvas_mouse, {"point": [cx, cy], "button": "right"})
R["double"] = run(canvas_mouse, {"action": "double", "point": [cx, cy]})
R["press"] = run(canvas_mouse, {"action": "press", "point": [cx, cy]})
R["release"] = run(canvas_mouse, {"action": "release", "point": [cx, cy]})
R["move"] = run(canvas_mouse, {"action": "move", "point": [cx, cy]})
DRAG = [[cx - 50, cy], [cx + 50, cy], [cx + 50, cy + 40]]
R["drag"] = run(canvas_mouse, {"action": "drag", "points": DRAG, "steps": 2})
R["shift"] = run(canvas_mouse, {"point": [cx, cy], "modifiers": ["shift", "ctrl"]})
R["outside"] = run(canvas_mouse, {"point": [e.xMaximum() + 1000, cy]})
R["pixel"] = run(canvas_mouse, {"point": [10, 20], "pixel": True})
# 0.001 度は、赤道付近で約 111.3 m（EPSG:3857）
R["crs"] = run(canvas_mouse, {"point": [0.001, 0.001], "crs": "EPSG:4326"})
R["bad_crs"] = run(canvas_mouse, {"point": [0, 0], "crs": "EPSG:999999"})
R["bad_action"] = run(canvas_mouse, {"action": "tap", "point": [cx, cy]})
R["bad_point"] = run(canvas_mouse, {"point": [cx]})
R["bad_drag"] = run(canvas_mouse, {"action": "drag", "points": [[cx, cy]]})
R["wheel"] = run(canvas_mouse, {"action": "wheel", "point": [cx, cy], "delta": 1})
canvas.setExtent(QgsRectangle(0, 0, 400, 300))
_app.processEvents()
tool.log = []
R["mini"] = run(canvas_mouse, {"point": [cx, cy], "canvas": {"object_name": "mini"}}, mini_tool)
R["mini_log_main"] = list(tool.log)
R["not_canvas"] = run(canvas_mouse, {"point": [cx, cy], "canvas": {"object_name": "angle"}})

queued = run(canvas_mouse, {"point": [cx, cy], "wait": False})
R["queued"] = queued

R["crs_and_pixel"] = run(canvas_mouse, {"point": [1, 1], "crs": "EPSG:4326", "pixel": True})
R["too_many_steps"] = run(canvas_mouse, {"action": "drag", "points": DRAG, "steps": 100000})
W = canvas.viewport().width()
R["edge"] = run(canvas_mouse, {"point": [W - 0.3, 10], "pixel": True})  # 丸めると W で外
R["edge_in"] = run(canvas_mouse, {"point": [-0.4, 10], "pixel": True})  # 丸めると 0 で中
from qgis_puppeteer.qgis_tools import exception_recorder
exception_recorder.clear()
tool.log = []
moved = canvas_mouse({"point": [cx, cy], "wait": False}, iface)
canvas.setExtent(QgsRectangle(10000, 10000, 10400, 10300))  # 積んだあとに地図が動く
for _ in range(5):
    _app.processEvents()
R["moved"] = {"result": moved, "log": list(tool.log),
              "recorded": [r["message"] for r in exception_recorder.get_recent()]}
canvas.setExtent(QgsRectangle(0, 0, 400, 300))
_app.processEvents()
R["typed"] = run(canvas_key, {"text": "aB\\n"})
NOPE = {"object_name": "nope"}
R["focus_bad_canvas"] = run(canvas_key, {"text": "x", "target": "focus", "canvas": NOPE})

# キャンバスの上端に重なる帯（メッセージバーのような、キャンバスの上に重なる子）
from qgis.PyQt.QtWidgets import QLabel
band = QLabel("帯", canvas)
band.setObjectName("band")
band.setGeometry(0, 0, canvas.width(), 28)
band.show()
band.raise_()
_app.processEvents()
R["covered"] = run(canvas_mouse, {"point": [10, 10], "pixel": True})
R["below_band"] = run(canvas_mouse, {"point": [10, 40], "pixel": True})
band.setAttribute(Qt.WA_TransparentForMouseEvents, True)
R["see_through"] = run(canvas_mouse, {"point": [10, 10], "pixel": True})
band.setAttribute(Qt.WA_TransparentForMouseEvents, False)
band.hide()
exception_recorder.clear()
queued_cover = canvas_mouse({"point": [10, 10], "pixel": True, "wait": False}, iface)
band.show()  # 積んだあとに帯が出る
tool.log = []
for _ in range(5):
    _app.processEvents()
R["covered_later"] = {"result": queued_cover, "log": list(tool.log),
                      "recorded": [r["message"] for r in exception_recorder.get_recent()]}
band.hide()
_app.processEvents()

R["key"] = run(canvas_key, {"key": "Escape"})
R["key_shift"] = run(canvas_key, {"key": "Backspace", "modifiers": ["shift"]})
# Space の離しはキャンバスが自分で受ける（パンの終わり）ので、マップツールに渡る文字で
R["key_press_only"] = run(canvas_key, {"key": "x", "action": "press"})
R["key_release_only"] = run(canvas_key, {"key": "x", "action": "release"})
R["key_char"] = run(canvas_key, {"key": "a"})
R["text_focus"] = run(canvas_key, {"text": "90", "target": "focus"})
ANGLE = {"object_name": "angle", "root_object_name": "host"}
R["text_selector"] = run(canvas_key, {"text": "45.5", "target": ANGLE})
R["edit_text"] = edit.text()
R["bad_key"] = run(canvas_key, {"key": "NoSuchKey"})
R["both"] = run(canvas_key, {"key": "Escape", "text": "x"})
R["text_press"] = run(canvas_key, {"text": "x", "action": "press"})
RESULT = R
"""


@pytest.fixture(scope="module")
def r() -> dict[str, Any]:
    return run_in_qgis(_BODY, gui=True)


def _kinds(log: list) -> list[str]:
    return [entry[0] for entry in log]


def test_a_click_moves_first_then_presses_and_releases(r: dict[str, Any]) -> None:
    click = r["click"]
    assert click["result"]["success"] is True
    assert _kinds(click["log"]) == ["move", "press", "release"]
    assert click["log"][1][1] == "left"
    # マップツールが受けた点 = 戻り値の map_point。頼んだ点からは半画素以内
    received = click["log"][1][2]
    assert received == pytest.approx(click["result"]["map_point"])
    cx, cy = r["center"]
    half = r["mupp"] / 2 + 1e-9
    assert abs(received[0] - (cx + 0.3)) <= half
    assert abs(received[1] - (cy + 0.7)) <= half
    assert click["result"]["map_units_per_pixel"] == pytest.approx(r["mupp"])
    assert click["result"]["map_tool"] == "Recorder"
    assert click["result"]["extent"] == pytest.approx(r["extent"])


def test_buttons_and_the_other_actions(r: dict[str, Any]) -> None:
    assert [e[1] for e in r["right"]["log"] if e[0] != "move"] == ["right", "right"]
    assert _kinds(r["double"]["log"]) == ["move", "press", "release", "double", "release"]
    assert _kinds(r["press"]["log"]) == ["move", "press"]
    assert _kinds(r["release"]["log"]) == ["release"]
    assert _kinds(r["move"]["log"]) == ["move"]


def test_a_drag_holds_the_button_through_the_moves(r: dict[str, Any]) -> None:
    drag = r["drag"]
    kinds = _kinds(drag["log"])
    # move → press → (途中 2 回 + 終点) × 2 区間 → release
    assert kinds == ["move", "press"] + ["move"] * 6 + ["release"]
    held = [e[4] for e in drag["log"][2:-1]]
    assert all(b != 0 for b in held)  # 押したまま動かす
    assert drag["log"][-1][2] == pytest.approx(drag["result"]["map_points"][-1])
    assert len(drag["result"]["pixels"]) == 3


def test_modifiers_reach_the_map_tool(r: dict[str, Any]) -> None:
    press = r["shift"]["log"][1]
    shift, ctrl = 0x02000000, 0x04000000
    assert press[3] & shift and press[3] & ctrl


def test_a_point_outside_the_canvas_is_refused_before_sending(r: dict[str, Any]) -> None:
    out = r["outside"]
    assert out["result"]["error"] == "point_outside_canvas"
    assert out["log"] == []


def test_pixels_and_another_crs(r: dict[str, Any]) -> None:
    assert r["pixel"]["result"]["pixel"] == [10.0, 20.0]
    received = r["crs"]["log"][1][2]
    assert received == pytest.approx([111.32, 111.33], abs=r["mupp"] + 0.1)
    assert r["bad_crs"]["result"]["error"] == "invalid_argument"


def test_misuse_is_refused(r: dict[str, Any]) -> None:
    for name in ("bad_action", "bad_point", "bad_drag"):
        assert r[name]["result"]["error"] == "invalid_argument", name
        assert r[name]["log"] == [], name


def test_the_wheel_reaches_the_tool_and_zooms_the_canvas(r: dict[str, Any]) -> None:
    wheel = r["wheel"]
    assert ["wheel", 120] in wheel["log"]
    before = r["extent"]
    after = wheel["result"]["extent"]
    assert (after[2] - after[0]) < (before[2] - before[0])  # 奥へ回して拡大


def test_another_canvas_is_chosen_by_selector(r: dict[str, Any]) -> None:
    assert _kinds(r["mini"]["log"]) == ["move", "press", "release"]
    assert r["mini_log_main"] == []
    assert r["not_canvas"]["result"]["error"] == "not_a_map_canvas"


def test_wait_false_sends_later(r: dict[str, Any]) -> None:
    q = r["queued"]
    assert q["result"]["queued"] is True
    assert _kinds(q["log"]) == ["move", "press", "release"]  # processEvents のあとに届く


def test_keys_reach_the_map_tool(r: dict[str, Any]) -> None:
    escape = 0x01000000
    assert r["key"]["log"] == [["key_press", escape, "", 0], ["key_release", escape, ""]]
    assert r["key_shift"]["log"][0][3] == 0x02000000
    assert _kinds(r["key_press_only"]["log"]) == ["key_press"]
    assert _kinds(r["key_release_only"]["log"]) == ["key_release"]
    assert r["key_char"]["log"][0][1:3] == [ord("A"), "a"]


def test_text_is_typed_key_by_key(r: dict[str, Any]) -> None:
    # フォーカスのある widget が無ければキャンバスへ
    typed = [e[2] for e in r["text_focus"]["log"] if e[0] == "key_press"]
    assert typed == ["9", "0"]
    assert r["text_selector"]["result"]["keys"] == 4
    assert r["edit_text"] == "45.5"


def test_a_covered_point_is_not_clicked(r: dict[str, Any]) -> None:
    """人は上に重なった帯に遮られて押せない（#33）。"""
    covered = r["covered"]["result"]
    assert covered["error"] == "point_covered"
    assert covered["covered_by"]["object_name"] == "band"
    assert covered["point"] == [10, 10]
    assert r["covered"]["log"] == []
    assert r["below_band"]["result"]["success"] is True
    assert r["see_through"]["result"]["success"] is True  # マウスを通す widget は覆わない
    later = r["covered_later"]
    assert later["result"]["queued"] is True
    assert later["log"] == []
    assert any("point_covered" in m for m in later["recorded"])


def test_reviewed_edges(r: dict[str, Any]) -> None:
    assert r["crs_and_pixel"]["result"]["error"] == "invalid_argument"
    assert r["too_many_steps"]["result"]["error"] == "invalid_argument"
    # 範囲の判定は、実際に届く丸めた画素で
    assert r["edge"]["result"]["error"] == "point_outside_canvas"
    assert r["edge_in"]["result"]["success"] is True
    # 積んだあとに地図が動いて外になった点は、送らずに記録する
    assert r["moved"]["result"]["queued"] is True
    assert r["moved"]["log"] == []
    assert any("point_outside_canvas" in m for m in r["moved"]["recorded"])
    assert r["focus_bad_canvas"]["result"]["error"] == "canvas_not_found"


def test_typing_sends_return_for_a_newline_and_shift_for_capitals(r: dict[str, Any]) -> None:
    presses = [e for e in r["typed"]["log"] if e[0] == "key_press"]
    shift = 0x02000000
    return_key = 0x01000004
    assert [(e[1], e[2]) for e in presses] == [(ord("A"), "a"), (ord("B"), "B"), (return_key, "\r")]
    assert presses[0][3] & shift == 0
    assert presses[1][3] & shift


def test_key_misuse_is_refused(r: dict[str, Any]) -> None:
    for name in ("bad_key", "both", "text_press"):
        assert r[name]["result"]["error"] == "invalid_argument", name
