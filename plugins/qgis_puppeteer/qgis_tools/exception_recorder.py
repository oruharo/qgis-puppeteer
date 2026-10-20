"""未捕捉 Qt / Python 例外を記録するリングバッファ（ADR-0002 §17.x）。

QGIS / Qt の slot 内で発生した未処理例外は、PyQt5 では `sys.excepthook` 経由で
stderr に出るだけで「テストの pass/fail」には反映されない（slot は C++ 側から
呼ばれるので Python の except に届かない）。これを Worker プロセス内で hook して
リングバッファに溜め、test 側から照会できるようにする。

## 公開 API

- ``install_excepthook()`` — 1 度だけ呼ぶ。previous hook を chain して保持する
- ``get_recent(limit=None)`` — 蓄積した例外レコードを古い順に返す
- ``record(type_name, message, traceback_text="")`` — hook を通らない失敗を積む
  （後で動かす処理の中で握った例外など。``queued.run_later`` が使う）
- ``clear()`` — バッファをクリア（test setup でリセットする想定）
- ``size_limit()`` — リングバッファの maxlen

## レコード形

```python
{
    "ts": "2026-05-01T12:34:56.789012",  # ISO 8601 (local time)
    "type": "ValueError",                # exc_type.__name__
    "message": "negative value",         # str(exc_value)
    "traceback": "Traceback (most...)",  # traceback.format_exception 連結
    "thread": "MainThread",               # threading.current_thread().name
}
```

## 設計方針

- maxlen=200 のリングバッファで「同じ slot から発火する大量例外」が来ても OOM
  にならない（古いものから捨てる）
- previous hook を chain することで、QGIS 標準の stderr 出力 / GDAL 側の追加
  hook を壊さない
- Lock で list 操作を保護（GIL 下でも `deque.append` は atomic だが iteration 中の
  mutation を避けるため）
- import 時点では何もせず、`install_excepthook()` の明示呼出が必要（test 環境
  以外で勝手に hook を奪わないため）
"""

from __future__ import annotations

import contextlib
import sys
import threading
import traceback
from collections import deque
from datetime import datetime
from types import TracebackType
from typing import Any

_MAXLEN = 200

_LOCK = threading.Lock()
_BUF: deque[dict[str, Any]] = deque(maxlen=_MAXLEN)
_PREV_HOOK: Any = None
_INSTALLED = False


def install_excepthook() -> bool:
    """``sys.excepthook`` を hook して未捕捉例外を内部バッファに積むようにする。

    冪等：2 回目以降は何もせず ``False`` を返す。previous hook は内部に保持し、
    本 hook の処理が終わった後に chain して呼ぶ（stderr 出力等の既存挙動を壊さない）。

    Returns:
        ``True``: 今回新規にインストールした
        ``False``: 既にインストール済み
    """
    global _PREV_HOOK, _INSTALLED
    if _INSTALLED:
        return False
    _PREV_HOOK = sys.excepthook
    sys.excepthook = _our_hook
    _INSTALLED = True
    return True


def _our_hook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: TracebackType | None,
) -> None:
    """内部 hook。レコード化して buffer に積み、previous hook に chain する。"""
    try:
        record = _make_record(
            exc_type.__name__ if exc_type is not None else "<unknown>",
            _safe_str(exc_value),
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
    except Exception:  # noqa: BLE001 - hook 自身が落ちると無限再帰になるので握る
        record = _make_record(
            "<recorder_error>", "exception_recorder failed to format the record", ""
        )
    with _LOCK:
        _BUF.append(record)
    # previous hook に chain（stderr 出力 / KeyboardInterrupt 標準処理を壊さない）
    if _PREV_HOOK is not None:
        with contextlib.suppress(Exception):
            _PREV_HOOK(exc_type, exc_value, exc_tb)


def record(type_name: str, message: str, traceback_text: str = "") -> None:
    """Hook を通らない失敗を 1 件積む。レコードの形は hook と同じ。

    後で動かす処理（``QTimer`` で積んだ click など）は、もう呼び出し元へ結果を
    返せない。そこで握った例外や ``success: false`` を未捕捉例外と同じ場所に残し、
    テストの判定（``qgis_fail_on_uncaught_exception``）に乗せるために使う。
    """
    item = _make_record(type_name, message, traceback_text)
    with _LOCK:
        _BUF.append(item)


def _make_record(type_name: str, message: str, traceback_text: str) -> dict[str, Any]:
    return {
        "ts": datetime.now().isoformat(timespec="microseconds"),
        "type": type_name,
        "message": message,
        "traceback": traceback_text,
        "thread": threading.current_thread().name,
    }


def get_recent(limit: int | None = None) -> list[dict[str, Any]]:
    """蓄積された例外レコードを古い順に返す（コピー）。

    Args:
        limit: ``None`` で全件、整数なら末尾 N 件。

    Returns:
        list of dict（``ts`` / ``type`` / ``message`` / ``traceback`` / ``thread``）。
    """
    with _LOCK:
        items = list(_BUF)
    if limit is not None and limit >= 0:
        items = items[-limit:]
    return items


def clear() -> None:
    """バッファを空にする（test 開始時にリセットする想定）。"""
    with _LOCK:
        _BUF.clear()


def size_limit() -> int:
    """リングバッファの maxlen を返す。"""
    return _MAXLEN


def is_installed() -> bool:
    """``install_excepthook()`` が呼ばれたかを返す（テスト用）。"""
    return _INSTALLED


def _safe_str(value: BaseException | None) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return "<unrepresentable>"
