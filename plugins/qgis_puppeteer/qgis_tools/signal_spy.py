"""Qt 信号 spy（ADR-0002 Roadmap "Worker test handler 追加"）。

Worker 内で Qt の ``pyqtBoundSignal`` を購読し、emission 履歴を test 側から
照会できるようにする。Playwright の ``page.waitForEvent`` 風の API を
``test.signal_spy_*`` namespace で公開する。

## 提供する機能（pure Python レイヤー）

- ``SpyRegistry``: spy の生成 / 取得 / 解放を id ベースで管理
- ``Spy``: signal に connect される実体。emissions（dict のリスト）を保持
- ``serialize_args(args)``: Qt 側 emission の引数 tuple を JSON safe 化

Qt 依存（``QApplication`` / ``QObject`` / ``QEventLoop``）は本モジュールでは
最小化し、handler 側（``handlers.py``）で widget 解決と event loop spin を行う。

## test 側 API（``handlers.py`` 経由）

- ``test.signal_spy_start(selector, signal, scope?)`` → ``{spy_id, count}``
- ``test.signal_spy_get_emissions(spy_id, since_index?)`` → ``{emissions, count}``
- ``test.signal_spy_count(spy_id)`` → ``{count}``
- ``test.signal_spy_stop(spy_id)`` → ``{ok}``
- ``test.wait_for_signal(selector, signal, timeout_ms?, scope?)``
  → ``{fired, args}``（blocking）

冪等性 / 多重 connect 防止: ``signal_spy_start`` で同じ widget+signal の組に対し
2 回呼ぶと別 spy_id を返し、それぞれ独立に履歴を持つ。
"""

from __future__ import annotations

import contextlib
import itertools
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("qgis_puppeteer.signal_spy")


def serialize_args(args: tuple[Any, ...]) -> list[Any]:
    """Qt signal の args tuple を JSON safe な list に変換する。

    Qt の primitive（int / float / bool / str / None）はそのまま、QString は str
    化、それ以外は ``repr()`` で文字列化する（``QObject`` / ``QVariant`` 等の
    runtime オブジェクトを wire 上で安全に表現するため）。
    """
    out: list[Any] = []
    for a in args:
        if a is None or isinstance(a, (bool, int, float, str)):
            out.append(a)
        elif isinstance(a, (list, tuple)):
            out.append(list(serialize_args(tuple(a))))
        elif isinstance(a, dict):
            out.append({str(k): _safe_repr(v) for k, v in a.items()})
        else:
            out.append(_safe_repr(a))
    return out


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:  # noqa: BLE001
        return "<unrepresentable>"


class Spy:
    """1 つの Qt signal に connect された emission レコーダ。

    ``signal.connect(spy.handle)`` で connect される。``handle(*args, **kwargs)``
    の形なので、ほぼ任意の Qt signal に汎用接続できる。

    ``emissions`` は dict のリスト（each: ``{"args": [...], "kwargs": {...}}``）。
    数千 emission オーダーまでは普通に保持する想定（必要なら ``max_emissions``
    で上限を付けて古いものから drop することも可能）。
    """

    def __init__(
        self,
        *,
        spy_id: int,
        selector: dict[str, Any],
        signal_name: str,
        max_emissions: int | None = None,
    ) -> None:
        self.spy_id = spy_id
        self.selector = selector
        self.signal_name = signal_name
        self.max_emissions = max_emissions
        self.emissions: list[dict[str, Any]] = []
        # disconnect 用に bound signal を保持（None なら未接続）
        self._signal: Any | None = None

    def handle(self, *args: Any, **kwargs: Any) -> None:
        """signal が emit された時に呼ばれる slot 本体。"""
        record = {"args": serialize_args(args)}
        if kwargs:
            record["kwargs"] = {str(k): _safe_repr(v) for k, v in kwargs.items()}
        self.emissions.append(record)
        if self.max_emissions is not None and len(self.emissions) > self.max_emissions:
            # 先頭を drop（FIFO）。max_emissions=N なら N+1 になった瞬間 1 件捨てる
            self.emissions.pop(0)

    def attach(self, signal: Any) -> None:
        """``signal.connect(self.handle)`` を呼んで bound signal を覚える。"""
        signal.connect(self.handle)
        self._signal = signal

    def detach(self) -> None:
        """connect を解除する。disconnect 失敗（既に切れている / wrapped C++ object 削除済）
        は握り潰す（teardown は best-effort）。"""
        if self._signal is None:
            return
        with contextlib.suppress(TypeError, RuntimeError):
            self._signal.disconnect(self.handle)
        self._signal = None


class SpyRegistry:
    """``Spy`` の生成と id 管理。プロセス内 singleton 想定（thread safe ではない、
    Qt の event loop スレッドからのみアクセス）。"""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._by_id: dict[int, Spy] = {}

    def create(
        self,
        *,
        selector: dict[str, Any],
        signal_name: str,
        max_emissions: int | None = None,
    ) -> Spy:
        spy_id = next(self._counter)
        spy = Spy(
            spy_id=spy_id,
            selector=selector,
            signal_name=signal_name,
            max_emissions=max_emissions,
        )
        self._by_id[spy_id] = spy
        return spy

    def get(self, spy_id: int) -> Spy | None:
        return self._by_id.get(int(spy_id))

    def remove(self, spy_id: int) -> bool:
        spy = self._by_id.pop(int(spy_id), None)
        if spy is not None:
            spy.detach()
            return True
        return False

    def clear(self) -> None:
        for spy in list(self._by_id.values()):
            spy.detach()
        self._by_id.clear()

    def __len__(self) -> int:
        return len(self._by_id)


# ============================================================
# プロセス内 singleton（handlers.py から参照される）
# ============================================================

_REGISTRY = SpyRegistry()


def get_registry() -> SpyRegistry:
    return _REGISTRY


# ============================================================
# 公開 helper（handlers.py から呼ぶ薄い wrapper）
# ============================================================


def start_spy(
    *,
    widget_resolver: Callable[[dict[str, Any]], Any],
    selector: dict[str, Any],
    signal_name: str,
    max_emissions: int | None = None,
) -> dict[str, Any]:
    """selector で widget を解決し、``signal_name`` 属性に Spy を connect する。

    ``widget_resolver`` は ``ui_tools._find_widget(selector)`` 相当を期待する
    （戻り値は ``(widget, diagnostics)``）。テスト容易性のため依存注入。

    Returns:
        ``{"spy_id": int, "matched": bool, "diagnostics": dict | None}``
        widget 解決失敗時は ``matched=False`` で diagnostics を返す（spy_id は -1）。
        signal 属性が無い場合は ``ValueError`` を raise する。
    """
    widget, diag = widget_resolver(selector)
    if widget is None:
        return {
            "spy_id": -1,
            "matched": False,
            "diagnostics": dict(diag) if diag is not None else None,
        }
    signal = getattr(widget, signal_name, None)
    if signal is None or not hasattr(signal, "connect"):
        raise ValueError(
            f"widget {widget!r} does not expose a connectable signal named {signal_name!r}"
        )
    spy = _REGISTRY.create(
        selector=selector,
        signal_name=signal_name,
        max_emissions=max_emissions,
    )
    spy.attach(signal)
    return {"spy_id": spy.spy_id, "matched": True, "diagnostics": None}


def get_emissions(spy_id: int, *, since_index: int = 0) -> dict[str, Any]:
    """spy_id の emissions のうち ``since_index`` 以降のものを返す。

    ``since_index`` を使うことで前回照会以降の差分だけを取れる。
    """
    spy = _REGISTRY.get(spy_id)
    if spy is None:
        return {"found": False, "emissions": [], "count": 0}
    since = max(int(since_index), 0)
    emissions = list(spy.emissions[since:])
    return {
        "found": True,
        "count": len(spy.emissions),
        "emissions": emissions,
        "next_index": len(spy.emissions),
    }


def stop_spy(spy_id: int) -> dict[str, Any]:
    """spy を解除して registry から外す。"""
    ok = _REGISTRY.remove(spy_id)
    return {"ok": ok}
