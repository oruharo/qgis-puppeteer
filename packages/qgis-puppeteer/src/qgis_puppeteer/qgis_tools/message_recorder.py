"""QgsMessageLog に流れたメッセージを記録するリングバッファ（#5、ロードマップ B5c）。

QGIS プラグインは、例外を捕まえて ``QgsMessageLog.logMessage(..., Qgis.Critical)``
に流す書き方をよくする。これは ``sys.excepthook`` を通らないので
`exception_recorder` では拾えず、E2E では「画面は正常、裏ではエラー」を見逃す。
`QgsApplication.messageLog().messageReceived` を購読して溜め、テストや
エージェントから照会できるようにする。

## 公開 API

- ``install()`` — 1 度だけ呼ぶ（冪等）。QGIS が要るのはここだけ
- ``get_recent(min_level=, tags=, sources=, after_seq=, limit=)`` — 古い順のコピー
- ``clear()`` / ``size_limit()`` / ``last_seq()``
- ``record(message, tag, level)`` — 購読先の本体。QGIS 無しのテストからも呼べる

## レコード形

```python
{
    "seq": 42,                       # 記録順の通し番号（clear しても戻らない）
    "ts": "2026-10-11T12:34:56.789012",
    "level": "critical",             # info / warning / critical / success / none
    "tag": "MyPlugin",
    "message": "failed to load ...",
    "source": "log",                 # "message_bar" = メッセージバーに出たもの
    "thread": "MainThread",
}
```

## レベルの順位

QGIS の数値は Info=0, Warning=1, Critical=2 の後に **Success=3, NoLevel=4** が
続く。数値で ``>= Critical`` と比べると Success まで拾うので、重さは
``SEVERITY`` の順位で比べる（success は info と同じ、none は最も軽い）。

## メッセージバー

`QgsMessageBar` は表示したメッセージを ``tr("Messages")`` のタグで
QgsMessageLog にも書く（「タイトル : 本文」）。タグは翻訳されるので、日本語の
QGIS では "Messages" ではない。``install()`` 時に翻訳後のタグを求めておき、
そのタグのレコードを ``source: "message_bar"`` にする（言語に依らず
``sources=["message_bar"]`` で絞れる）。同じタグを自分で使うプラグインの
メッセージも message_bar 扱いになる。

## 取れないもの

購読を始められるのは、このプラグインが読み込まれてから。QGIS はプラグインを
名前順に読むので、それより前に読まれるプラグインの初期化中のメッセージは
入らない。テスト本体（call）の判定には影響しない。

## 大量の info でエラーが押し出されない

バッファは 1000 件。info が大量に流れる処理（Processing の進捗等）で、その前の
critical が押し出されると、判定が黙って素通りする。warning 以上は別に 1000 件の
バッファにも持ち、``min_level`` が warning / critical の照会はそちらから返す。

## スレッド

``messageReceived`` は ``logMessage`` を呼んだスレッドで発火する（描画スレッド、
Processing のタスク等）。購読先はロックしてバッファに積むだけで、Qt の
オブジェクトには触らない。購読先から ``logMessage`` を呼ぶと無限に回るので
呼ばない。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from datetime import datetime
from typing import Any

_MAXLEN = 1000

# 重さの順位。success は info と同じ扱い、none（NoLevel）は最も軽い。
SEVERITY: dict[str, int] = {"none": -1, "info": 0, "success": 0, "warning": 1, "critical": 2}
# Qgis.MessageLevel の数値 → 名前
_LEVEL_NAMES = {0: "info", 1: "warning", 2: "critical", 3: "success", 4: "none"}

SOURCE_LOG = "log"
SOURCE_MESSAGE_BAR = "message_bar"

_LOCK = threading.Lock()
_BUF: deque[dict[str, Any]] = deque(maxlen=_MAXLEN)
# warning 以上だけ（info の洪水で押し出されないように）
_SEVERE: deque[dict[str, Any]] = deque(maxlen=_MAXLEN)
_SEVERE_RANK = 1
_SEQ = 0
_INSTALLED = False
_MESSAGE_BAR_TAGS: frozenset[str] = frozenset({"Messages"})


def level_name(level: Any) -> str:
    """``Qgis.MessageLevel``（enum / int / 名前）を小文字の名前にする。"""
    if isinstance(level, str):
        name = level.strip().lower()
        return name if name in SEVERITY else "none"
    try:
        number = int(level)
    except (TypeError, ValueError):
        number = int(getattr(level, "value", 4))
    return _LEVEL_NAMES.get(number, "none")


def parse_min_level(value: Any) -> str | None:
    """``min_level`` 引数を検証する。``None`` / 空はフィルタ無し。"""
    if value is None or value == "":
        return None
    name = str(value).strip().lower()
    if name not in SEVERITY:
        raise ValueError(f"min_level must be one of {sorted(SEVERITY)}, got {value!r}")
    return name


def is_at_least(level: str, min_level: str | None) -> bool:
    if min_level is None:
        return True
    return SEVERITY.get(level, -1) >= SEVERITY[min_level]


def install() -> bool:
    """``QgsMessageLog.messageReceived`` を購読する。冪等。

    Returns:
        ``True``: 今回購読した / ``False``: 既に購読済み
    """
    global _INSTALLED, _MESSAGE_BAR_TAGS
    if _INSTALLED:
        return False
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QCoreApplication

    translated = QCoreApplication.translate("QgsMessageBar", "Messages")
    _MESSAGE_BAR_TAGS = frozenset({"Messages", translated})
    QgsApplication.messageLog().messageReceived.connect(record)
    _INSTALLED = True
    return True


def record(message: Any, tag: Any, level: Any) -> None:
    """1 件記録する（``messageReceived`` の購読先）。例外は外に出さない。"""
    global _SEQ
    try:
        tag_text = str(tag or "")
        entry = {
            "ts": datetime.now().isoformat(timespec="microseconds"),
            "level": level_name(level),
            "tag": tag_text,
            "message": str(message),
            "source": SOURCE_MESSAGE_BAR if tag_text in _MESSAGE_BAR_TAGS else SOURCE_LOG,
            "thread": threading.current_thread().name,
        }
    except Exception:  # noqa: BLE001 - 購読先で落ちると QGIS 側に例外が出る
        entry = {
            "ts": datetime.now().isoformat(timespec="microseconds"),
            "level": "none",
            "tag": "",
            "message": "<message_recorder failed to format the record>",
            "source": SOURCE_LOG,
            "thread": threading.current_thread().name,
        }
    with _LOCK:
        _SEQ += 1
        entry["seq"] = _SEQ
        _BUF.append(entry)
        if SEVERITY.get(entry["level"], -1) >= _SEVERE_RANK:
            _SEVERE.append(entry)


def get_recent(
    *,
    min_level: str | None = None,
    tags: Iterable[str] | None = None,
    sources: Iterable[str] | None = None,
    after_seq: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """記録を古い順に返す（コピー）。

    Args:
        min_level: これ以上の重さだけ（info / warning / critical）。
        tags: このタグのどれかに一致するものだけ。
        sources: ``"log"`` / ``"message_bar"`` で絞る。
        after_seq: この通し番号より後のものだけ（差分の取得に使う）。
        limit: 絞り込んだ後の末尾 N 件。
    """
    level = parse_min_level(min_level)
    tag_set = set(tags) if tags is not None else None
    source_set = set(sources) if sources is not None else None
    use_severe = level is not None and SEVERITY[level] >= _SEVERE_RANK
    with _LOCK:
        items = [dict(e) for e in (_SEVERE if use_severe else _BUF)]
    out = [
        e
        for e in items
        if is_at_least(e["level"], level)
        and (tag_set is None or e["tag"] in tag_set)
        and (source_set is None or e["source"] in source_set)
        and (after_seq is None or e["seq"] > after_seq)
    ]
    if limit is not None and limit >= 0:
        out = out[-limit:] if limit else []
    return out


def clear() -> None:
    """バッファを空にする。通し番号は戻さない（差分取得が壊れないように）。"""
    with _LOCK:
        _BUF.clear()
        _SEVERE.clear()


def last_seq() -> int:
    """これまでに振った最後の通し番号（何も無ければ 0）。"""
    with _LOCK:
        return _SEQ


def size_limit() -> int:
    return _MAXLEN


def is_installed() -> bool:
    return _INSTALLED


def _reset_for_tests() -> None:
    """テスト用：バッファ・通し番号・購読状態を初期化する。"""
    global _SEQ, _INSTALLED, _MESSAGE_BAR_TAGS
    with _LOCK:
        _BUF.clear()
        _SEVERE.clear()
        _SEQ = 0
    _INSTALLED = False
    _MESSAGE_BAR_TAGS = frozenset({"Messages"})
