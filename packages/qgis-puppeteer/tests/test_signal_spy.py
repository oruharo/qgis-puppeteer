"""``qgis_tools.signal_spy`` の pure-python ユニットテスト。

Qt 非依存の Signal スタブで Spy / SpyRegistry / start_spy / get_emissions /
stop_spy の挙動を検証する。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from qgis_puppeteer.qgis_tools import signal_spy


class _FakeSignal:
    """Qt の ``pyqtBoundSignal`` を最小限に模した stub。

    ``connect(slot)`` で slot を覚え、``emit(*args, **kwargs)`` で同期的に呼ぶ。
    複数 slot を connect 可能。``disconnect(slot)`` で 1 件ずつ外す。
    """

    def __init__(self) -> None:
        self._slots: list[Callable[..., Any]] = []

    def connect(self, slot: Callable[..., Any]) -> None:
        self._slots.append(slot)

    def disconnect(self, slot: Callable[..., Any]) -> None:
        if slot not in self._slots:
            raise TypeError("slot was not connected")
        self._slots.remove(slot)

    def emit(self, *args: Any, **kwargs: Any) -> None:
        for s in list(self._slots):
            s(*args, **kwargs)


class _FakeWidget:
    """signal 属性を持つだけの stub widget（Qt 非依存）。"""

    def __init__(self, **signals: _FakeSignal) -> None:
        for name, signal in signals.items():
            setattr(self, name, signal)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    signal_spy.get_registry().clear()
    yield
    signal_spy.get_registry().clear()


# ============================================================
# serialize_args
# ============================================================


class TestSerializeArgs:
    def test_passes_primitives_through(self) -> None:
        assert signal_spy.serialize_args((1, "x", True, 3.14, None)) == [
            1,
            "x",
            True,
            3.14,
            None,
        ]

    def test_lists_recurse(self) -> None:
        assert signal_spy.serialize_args(([1, 2], (3, 4))) == [[1, 2], [3, 4]]

    def test_dict_keys_str_values_repr(self) -> None:
        out = signal_spy.serialize_args(({"k": object()},))
        assert isinstance(out[0], dict)
        assert "k" in out[0]
        assert isinstance(out[0]["k"], str)

    def test_unknown_objects_to_repr(self) -> None:
        class Custom:
            def __repr__(self) -> str:
                return "<Custom>"

        out = signal_spy.serialize_args((Custom(),))
        assert out == ["<Custom>"]


# ============================================================
# Spy / SpyRegistry
# ============================================================


class TestSpyDirect:
    def test_handle_records_args(self) -> None:
        spy = signal_spy.Spy(spy_id=1, selector={}, signal_name="clicked")
        spy.handle(42, "x")
        assert spy.emissions == [{"args": [42, "x"]}]

    def test_handle_records_kwargs(self) -> None:
        spy = signal_spy.Spy(spy_id=1, selector={}, signal_name="x")
        spy.handle(value=5)
        assert spy.emissions[0]["kwargs"] == {"value": "5"}

    def test_max_emissions_drops_oldest(self) -> None:
        spy = signal_spy.Spy(spy_id=1, selector={}, signal_name="x", max_emissions=3)
        for i in range(5):
            spy.handle(i)
        # 最初の 2 件が drop され、残り 3 件
        assert [r["args"][0] for r in spy.emissions] == [2, 3, 4]

    def test_attach_then_detach(self) -> None:
        sig = _FakeSignal()
        spy = signal_spy.Spy(spy_id=1, selector={}, signal_name="x")
        spy.attach(sig)
        sig.emit("hello")
        assert spy.emissions == [{"args": ["hello"]}]
        spy.detach()
        sig.emit("after-detach")
        assert spy.emissions == [{"args": ["hello"]}]  # 増えない

    def test_detach_idempotent(self) -> None:
        spy = signal_spy.Spy(spy_id=1, selector={}, signal_name="x")
        # 未 attach でも例外なし
        spy.detach()
        sig = _FakeSignal()
        spy.attach(sig)
        spy.detach()
        spy.detach()  # 2 回目も noop


class TestSpyRegistry:
    def test_create_increments_id(self) -> None:
        reg = signal_spy.SpyRegistry()
        a = reg.create(selector={}, signal_name="x")
        b = reg.create(selector={}, signal_name="x")
        assert a.spy_id != b.spy_id

    def test_get_returns_none_for_unknown(self) -> None:
        reg = signal_spy.SpyRegistry()
        assert reg.get(999) is None

    def test_remove_detaches_and_returns_true(self) -> None:
        reg = signal_spy.SpyRegistry()
        spy = reg.create(selector={}, signal_name="x")
        sig = _FakeSignal()
        spy.attach(sig)
        assert reg.remove(spy.spy_id) is True
        assert reg.get(spy.spy_id) is None
        # detach 後は emit しても spy には届かない
        sig.emit(1)
        assert spy.emissions == []

    def test_remove_unknown_returns_false(self) -> None:
        reg = signal_spy.SpyRegistry()
        assert reg.remove(999) is False


# ============================================================
# start_spy / get_emissions / stop_spy（高レベル API）
# ============================================================


class TestHighLevelApi:
    def test_start_spy_attaches_to_named_signal(self) -> None:
        clicked = _FakeSignal()
        widget = _FakeWidget(clicked=clicked)

        def resolver(_sel: dict[str, Any]) -> tuple[Any, Any]:
            return widget, {}

        result = signal_spy.start_spy(
            widget_resolver=resolver,
            selector={"class": "QPushButton"},
            signal_name="clicked",
        )
        assert result["matched"] is True
        spy_id = result["spy_id"]
        assert spy_id > 0

        clicked.emit()
        clicked.emit("payload")
        emissions = signal_spy.get_emissions(spy_id)
        assert emissions["found"] is True
        assert emissions["count"] == 2
        assert emissions["emissions"][0] == {"args": []}
        assert emissions["emissions"][1] == {"args": ["payload"]}
        assert emissions["next_index"] == 2

    def test_get_emissions_with_since_index(self) -> None:
        sig = _FakeSignal()
        widget = _FakeWidget(triggered=sig)

        result = signal_spy.start_spy(
            widget_resolver=lambda _s: (widget, {}),
            selector={},
            signal_name="triggered",
        )
        spy_id = result["spy_id"]
        sig.emit(1)
        sig.emit(2)
        sig.emit(3)
        # 最初の 2 件を skip して残り 1 件
        delta = signal_spy.get_emissions(spy_id, since_index=2)
        assert delta["count"] == 3  # total count
        assert [e["args"][0] for e in delta["emissions"]] == [3]
        assert delta["next_index"] == 3

    def test_start_spy_returns_diagnostics_when_widget_not_found(self) -> None:
        diag = {"reason": "no_match"}

        def resolver(_sel: dict[str, Any]) -> tuple[Any, Any]:
            return None, diag

        result = signal_spy.start_spy(
            widget_resolver=resolver,
            selector={"class": "Nope"},
            signal_name="clicked",
        )
        assert result["matched"] is False
        assert result["spy_id"] == -1
        assert result["diagnostics"] == diag

    def test_start_spy_raises_when_signal_attribute_missing(self) -> None:
        widget = _FakeWidget()  # no signals

        with pytest.raises(ValueError, match="connectable signal"):
            signal_spy.start_spy(
                widget_resolver=lambda _s: (widget, {}),
                selector={},
                signal_name="nonexistent",
            )

    def test_stop_spy_removes_and_disconnects(self) -> None:
        sig = _FakeSignal()
        widget = _FakeWidget(triggered=sig)
        result = signal_spy.start_spy(
            widget_resolver=lambda _s: (widget, {}),
            selector={},
            signal_name="triggered",
        )
        spy_id = result["spy_id"]
        assert signal_spy.stop_spy(spy_id) == {"ok": True}
        # 解除後の emit は記録されない
        sig.emit(1)
        emissions = signal_spy.get_emissions(spy_id)
        assert emissions["found"] is False

    def test_stop_unknown_spy_returns_ok_false(self) -> None:
        assert signal_spy.stop_spy(999) == {"ok": False}

    def test_get_emissions_unknown_spy(self) -> None:
        assert signal_spy.get_emissions(999) == {"found": False, "emissions": [], "count": 0}
