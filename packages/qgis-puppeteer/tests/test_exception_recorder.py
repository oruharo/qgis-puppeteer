"""``qgis_tools.exception_recorder`` の pure-python ユニットテスト。

Qt も QGIS も触らない（``sys.excepthook`` の機能だけを使う）。
"""

from __future__ import annotations

import sys

import pytest
from qgis_puppeteer.qgis_tools import exception_recorder as er


@pytest.fixture(autouse=True)
def _reset_module_state():
    """各テスト後に hook を巻き戻し、buffer / installed flag をクリアする。

    モジュール変数を外から書き戻す形になるが、test 間の独立性を保つために必要。
    """
    saved_hook = sys.excepthook
    er.clear()
    yield
    sys.excepthook = saved_hook
    er._INSTALLED = False  # type: ignore[attr-defined]
    er._PREV_HOOK = None  # type: ignore[attr-defined]
    er.clear()


def _trigger(exc: BaseException) -> None:
    """``sys.excepthook`` 経由で例外を投げて recorder が拾うかを試す。"""
    try:
        raise exc
    except BaseException:  # noqa: BLE001 - excepthook の呼び方として明示
        sys.excepthook(*sys.exc_info())  # type: ignore[arg-type]


def test_install_is_idempotent():
    assert er.install_excepthook() is True
    assert er.is_installed() is True
    # 2 回目は False
    assert er.install_excepthook() is False


def test_records_uncaught_exception():
    er.install_excepthook()
    _trigger(ValueError("boom"))
    items = er.get_recent()
    assert len(items) == 1
    rec = items[0]
    assert rec["type"] == "ValueError"
    assert rec["message"] == "boom"
    assert "ValueError" in rec["traceback"]
    assert "ts" in rec and "thread" in rec


def test_clear_empties_buffer():
    er.install_excepthook()
    _trigger(RuntimeError("a"))
    _trigger(RuntimeError("b"))
    assert len(er.get_recent()) == 2
    er.clear()
    assert er.get_recent() == []


def test_get_recent_limit_returns_tail():
    er.install_excepthook()
    for i in range(5):
        _trigger(ValueError(f"e{i}"))
    items = er.get_recent(limit=2)
    assert [r["message"] for r in items] == ["e3", "e4"]


def test_ringbuffer_drops_old_entries():
    er.install_excepthook()
    # MAXLEN を超えるまで投げる（heavy だが 200 程度なので OK）
    n = er.size_limit() + 5
    for i in range(n):
        _trigger(ValueError(f"v{i}"))
    items = er.get_recent()
    assert len(items) == er.size_limit()
    # 先頭は drop されているはず
    assert items[0]["message"] != "v0"
    assert items[-1]["message"] == f"v{n - 1}"


def test_previous_hook_is_chained():
    """install すると previous hook が chain 呼び出しされる。"""
    called: list[str] = []

    def prev_hook(exc_type, exc_value, exc_tb):
        called.append(f"{exc_type.__name__}:{exc_value}")

    sys.excepthook = prev_hook
    er.install_excepthook()
    _trigger(KeyError("x"))
    # recorder 側に積まれて、かつ previous hook も呼ばれている
    assert len(er.get_recent()) == 1
    assert called == ["KeyError:'x'"]


def test_size_limit_returns_int():
    assert isinstance(er.size_limit(), int)
    assert er.size_limit() > 0
