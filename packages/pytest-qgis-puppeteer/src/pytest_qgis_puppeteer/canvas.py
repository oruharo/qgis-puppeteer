"""地図のキャンバスを操作する道具（#26）。

``qgis.canvas()`` で作る。座標は地図の座標（既定はキャンバスの CRS）で渡す::

    canvas = qgis.canvas()
    canvas.click((-41000.0, 12000.0))                  # 頂点を置く
    canvas.press_key("Backspace")                      # 最後の頂点を取り消す
    canvas.right_click((-40900.0, 12100.0), wait=False)  # 作図を終える（フォームが開く）
    canvas.type_text("90")                             # フォーカスのある欄に打つ
    canvas.press_key("Return", target="focus")

どの操作も ``wait=False``（積んですぐ返す、#21）と ``modifiers``（``["shift"]``
など）を受ける。戻り値の ``map_point`` は、マップツールが受け取る地図の点（画素に
丸めるので、頼んだ点から ``map_units_per_pixel`` の半分までずれる）。失敗（地図の
外の点など）は ``WidgetOperationError``。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

Point = Sequence[float]


class Canvas:
    """地図のキャンバスへのマウスとキーの操作。``E2EAutomationClient.canvas()`` が作る。"""

    def __init__(
        self,
        client: Any,
        selector: dict[str, Any] | None = None,
        *,
        crs: str | None = None,
        instance: str | None = None,
    ) -> None:
        self._client = client
        self._selector = selector
        self._crs = crs
        self._instance = instance

    # ------------------------------------------------------------
    # マウス
    # ------------------------------------------------------------

    def click(
        self,
        point: Point,
        *,
        button: str = "left",
        modifiers: Sequence[str] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """その位置へ動かしてから押して離す。"""
        return self._mouse("click", point, button=button, modifiers=modifiers, wait=wait)

    def right_click(
        self, point: Point, *, modifiers: Sequence[str] | None = None, wait: bool = True
    ) -> dict[str, Any]:
        """右クリック（``click(point, button="right")``）。作図を終えるツールが多い。"""
        return self.click(point, button="right", modifiers=modifiers, wait=wait)

    def dblclick(
        self,
        point: Point,
        *,
        button: str = "left",
        modifiers: Sequence[str] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """ダブルクリック（press → release → doubleClick → release）。"""
        return self._mouse("double", point, button=button, modifiers=modifiers, wait=wait)

    def press(
        self,
        point: Point,
        *,
        button: str = "left",
        modifiers: Sequence[str] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """その位置へ動かしてから押す（離さない）。"""
        return self._mouse("press", point, button=button, modifiers=modifiers, wait=wait)

    def release(
        self,
        point: Point,
        *,
        button: str = "left",
        modifiers: Sequence[str] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """その位置で離す。"""
        return self._mouse("release", point, button=button, modifiers=modifiers, wait=wait)

    def move(
        self, point: Point, *, modifiers: Sequence[str] | None = None, wait: bool = True
    ) -> dict[str, Any]:
        """マウスを動かす（ボタンなし）。スナップの表示や輪郭の線が更新される。"""
        return self._mouse("move", point, modifiers=modifiers, wait=wait)

    def drag(
        self,
        points: Sequence[Point],
        *,
        button: str = "left",
        modifiers: Sequence[str] | None = None,
        steps: int | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """最初の点で押し、点から点へ ``steps`` 回（既定 5）ずつ動かして、最後の点で離す。"""
        return self._client.canvas_mouse(
            "drag",
            points=points,
            button=button,
            modifiers=modifiers,
            steps=steps,
            crs=self._crs,
            canvas=self._selector,
            wait=wait,
            instance=self._instance,
        )

    def wheel(
        self,
        point: Point,
        delta: int = 1,
        *,
        modifiers: Sequence[str] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """その位置でホイールを ``delta`` ノッチ回す（正で奥へ＝拡大）。

        戻り値の ``extent`` は回したあとの範囲（``wait=True`` のとき）。
        """
        return self._client.canvas_mouse(
            "wheel",
            point,
            delta=delta,
            modifiers=modifiers,
            crs=self._crs,
            canvas=self._selector,
            wait=wait,
            instance=self._instance,
        )

    # ------------------------------------------------------------
    # キー
    # ------------------------------------------------------------

    def press_key(
        self,
        key: str,
        *,
        action: str = "tap",
        modifiers: Sequence[str] | None = None,
        target: str | dict[str, Any] = "canvas",
        wait: bool = True,
    ) -> dict[str, Any]:
        """キーを送る（既定はキャンバスへ）。``key`` は ``"Escape"`` などの名前か 1 文字。

        ``action="press"`` / ``"release"`` で押しっぱなし。``target`` は ``"canvas"`` /
        ``"focus"`` / selector。
        """
        return self._client.canvas_key(
            key,
            action=action,
            modifiers=modifiers,
            target=target,
            canvas=self._selector,
            wait=wait,
            instance=self._instance,
        )

    def type_text(
        self,
        text: str,
        *,
        target: str | dict[str, Any] = "focus",
        wait: bool = True,
    ) -> dict[str, Any]:
        """文字を 1 文字ずつ打つ（既定はフォーカスのある widget へ）。

        キーのイベントでしか入らない欄（マップツールがキャンバスの上に出す数値の欄
        など）用。普通の入力欄には ``Locator.fill()`` の方が確か。
        """
        return self._client.canvas_key(
            text=text,
            target=target,
            canvas=self._selector,
            wait=wait,
            instance=self._instance,
        )

    # ------------------------------------------------------------

    def _mouse(
        self,
        action: str,
        point: Point,
        *,
        button: str = "left",
        modifiers: Sequence[str] | None = None,
        wait: bool,
    ) -> dict[str, Any]:
        return self._client.canvas_mouse(
            action,
            point,
            button=button,
            modifiers=modifiers,
            crs=self._crs,
            canvas=self._selector,
            wait=wait,
            instance=self._instance,
        )
