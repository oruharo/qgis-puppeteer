"""QgsMessageLog のメッセージでテストを落とす判定（#5）。

``qgis_fail_on_message_level`` を指定すると、call 直前に Worker のメッセージを
消去し、call 後にそのレベル以上のメッセージがあれば pass を fail にする
（``qgis_fail_on_uncaught_exception`` と同じ流れ）。既知の無害なメッセージは
許可リストで外す。

## 許可リスト

- ini ``qgis_message_allowlist``（1 行 1 ルール）::

      qgis_message_allowlist =
          Could not load the style
          OGR::^Warning 1: .*

  1 行目は本文に正規表現が含まれるメッセージを、2 行目はタグが ``OGR`` で本文が
  正規表現に合うメッセージを除外する（``<タグ>::<正規表現>``）。``#`` で
  始まる行はコメント（正規表現の途中の ``#`` はそのまま使える）。

  最初の ``::`` がタグの区切りになる。本文の正規表現に ``::`` を書きたいとき
  （``QgsVectorLayer::`` など）は ``\\:\\:`` と書く。タグに見えない文字
  （正規表現の記号）が ``::`` の前にあると、取り違えを避けるためエラーにする。
- マーカー（テスト単位）::

      @pytest.mark.qgis_allow_message(r"slow provider", tag="WMS")
      @pytest.mark.qgis_allow_message(source="message_bar")

  ``pattern`` / ``tag`` / ``source`` を組み合わせる（指定したものすべてに合う
  メッセージを除外）。複数付けられる。

正規表現は本文に対する ``re.search``（部分一致）。

## 起動の間のメッセージ（#15）

QGIS の起動からテスト本体までに出たメッセージは、その QGIS を初めて使う
``qgis`` fixture の準備で取っておく（``qgis_startup_messages``）。判定は同じ
レベルと ini の許可リストで行い、引っかかったものはどのテストも落とさずに
最後のまとめに出して、終了コードを失敗にする。マーカーは使わない（どの
テストのものでもないため）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

LEVELS = ("info", "warning", "critical")
# メッセージの重さの順位（qgis_puppeteer.qgis_tools.message_recorder.SEVERITY と同じ）
_SEVERITY = {"none": -1, "info": 0, "success": 0, "warning": 1, "critical": 2}
# タグには出てこないはずの正規表現の記号（"<タグ>::<正規表現>" の取り違え検出用）
_REGEX_CHARS = re.compile(r"[\^$.*+?()\[\]{}|]")
_OFF = ("", "none", "off", "false", "0", "no")


@dataclass(frozen=True)
class AllowRule:
    """除外ルール。指定された条件すべてに合うメッセージを除外する。"""

    pattern: re.Pattern[str] | None = None
    tag: str | None = None
    source: str | None = None

    def matches(self, message: dict[str, Any]) -> bool:
        if self.tag is not None and message.get("tag") != self.tag:
            return False
        if self.source is not None and message.get("source") != self.source:
            return False
        return self.pattern is None or bool(self.pattern.search(str(message.get("message", ""))))


def parse_fail_level(raw: Any) -> str | None:
    """``qgis_fail_on_message_level`` を解釈する。無効化なら ``None``。"""
    value = "" if raw is None else str(raw).strip().lower()
    if value in _OFF:
        return None
    if value not in LEVELS:
        raise ValueError(
            f"qgis_fail_on_message_level must be one of {', '.join(LEVELS)} "
            f"(or empty to disable), got {raw!r}"
        )
    return value


def parse_allow_line(line: str) -> AllowRule | None:
    """ini の 1 行を読む。空行・コメントだけの行は ``None``。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    tag: str | None = None
    if "::" in text:
        tag, text = (part.strip() for part in text.split("::", 1))
        tag = tag or None
        if tag is not None and _REGEX_CHARS.search(tag):
            raise ValueError(
                f"qgis_message_allowlist: {line.strip()!r} reads as tag {tag!r}; "
                "to match a literal '::' in the message, write it as \\:\\:"
            )
    try:
        pattern = re.compile(text) if text else None
    except re.error as e:
        raise ValueError(f"qgis_message_allowlist: bad regex {text!r}: {e}") from None
    return AllowRule(pattern=pattern, tag=tag)


def rules_from_ini(lines: Iterable[str] | None) -> list[AllowRule]:
    rules = []
    for line in lines or ():
        rule = parse_allow_line(line)
        if rule is not None:
            rules.append(rule)
    return rules


def rules_from_markers(item: Any) -> list[AllowRule]:
    """``@pytest.mark.qgis_allow_message(...)`` を読む。"""
    rules = []
    for marker in item.iter_markers("qgis_allow_message"):
        raw = marker.args[0] if marker.args else marker.kwargs.get("pattern")
        tag = marker.kwargs.get("tag")
        source = marker.kwargs.get("source")
        if raw is None and tag is None and source is None:
            raise ValueError(f"{item.nodeid}: qgis_allow_message needs a pattern, tag= or source=")
        try:
            pattern = re.compile(raw) if raw is not None else None
        except re.error as e:
            raise ValueError(f"{item.nodeid}: qgis_allow_message: bad regex {raw!r}: {e}") from None
        rules.append(AllowRule(pattern=pattern, tag=tag, source=source))
    return rules


def unexpected(messages: Iterable[dict[str, Any]], rules: list[AllowRule]) -> list[dict[str, Any]]:
    """どのルールにも合わないメッセージ。"""
    return [m for m in messages if not any(rule.matches(m) for rule in rules)]


def at_or_above(messages: Iterable[dict[str, Any]], level: str) -> list[dict[str, Any]]:
    """``level`` 以上のメッセージ（info = success < warning < critical）。"""
    floor = _SEVERITY[level]
    return [m for m in messages if _SEVERITY.get(str(m.get("level", "none")), -1) >= floor]


def unexpected_messages(
    messages: Iterable[dict[str, Any]],
    config: Any,
    *,
    item: Any = None,
    min_level: str | None = None,
) -> list[dict[str, Any]]:
    """判定に引っかかるメッセージを返す（公開の窓口、#15）。

    ``min_level``（省略時は ini の ``qgis_fail_on_message_level``）以上のうち、
    ini の ``qgis_message_allowlist`` にも、``item`` を渡せばその
    ``@pytest.mark.qgis_allow_message`` にも合わないもの。conftest で自分で
    取ったメッセージを、テストと同じ規則で判定したいときに使う::

        from pytest_qgis_puppeteer import unexpected_messages

        bad = unexpected_messages(qgis.get_recent_messages(), request.config)

    Raises:
        ValueError: ``min_level`` も ini のレベルも無い。レベルや許可リストの書き方の誤り。
    """
    raw = min_level if min_level is not None else config.getini("qgis_fail_on_message_level")
    level = parse_fail_level(raw)
    if level is None:
        raise ValueError(
            "unexpected_messages needs min_level= or qgis_fail_on_message_level in the ini"
        )
    rules = rules_from_ini(config.getini("qgis_message_allowlist"))
    if item is not None:
        rules += rules_from_markers(item)
    return unexpected(at_or_above(messages, level), rules)


def format_messages(
    messages: list[dict[str, Any]],
    level: str,
    *,
    phase: str = "during the call phase",
    markers: bool = True,
) -> str:
    """pytest の longrepr 用テキスト。"""
    lines = [
        f"Worker logged {len(messages)} message(s) at level {level} or above "
        f"{phase} (qgis_fail_on_message_level):"
    ]
    for m in messages:
        source = " (message bar)" if m.get("source") == "message_bar" else ""
        lines.append(
            f"  [{m.get('level', '?')}] {m.get('tag') or '-'}{source} @ {m.get('ts', '?')}: "
            f"{m.get('message', '')}"
        )
    if markers:
        lines.append(
            "Allow known messages with the qgis_message_allowlist ini or "
            "@pytest.mark.qgis_allow_message(...)."
        )
    else:
        # 起動の間の分はどのテストのものでもないので、マーカーは効かない
        lines.append("Allow known messages with the qgis_message_allowlist ini.")
    return "\n".join(lines)
