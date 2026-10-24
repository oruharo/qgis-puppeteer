"""Selector matching の共通ロジック（Qt 非依存・snapshot/live tree 共通）。

ADR-0002 §8.1 / Roadmap "Selector resolver の統合" を実装する。
qgis_tools.ui_tools._find_widget（live Qt tree 走査）と
pytest_qgis_puppeteer.automation_client._find_in_snapshot（snapshot DFS）が
同じ selector 仕様を採用できるよう、純粋関数として切り出す。

## 公開 API

- ``record_matches_selector(record, selector)`` — 1 件の record を判定
- ``resolve_with_index(records, selector)`` — match list から `index` /
  strict mode の解釈を含めて確定 record を返す
- ``label_forms(text)`` — ボタン・メニュー項目の文言を、指定と比べる形にする
- ``live_only_keys(selector)`` — snapshot では判定できないキー（``window`` /
  ``class_inherits``）のうち、selector が使っているもの

## record の形

dict で以下のキーを持つ（無いものは None / 欠落で OK）:

- ``class``: Python クラス名 (str)
- ``object_name``: Qt objectName (str)
- ``text``: button/label/action のテキスト (str | None)
- ``title``: window title (str | None)
- ``label``: QLabel.buddy() で対応付けられた label テキスト (str | None)
- ``placeholder``: LineEdit の placeholderText() (str | None)
- ``role``: ARIA / Qt accessible role 名 (str | None) — Worker 側 widget summary に
  ``QAccessibleInterface`` 経由で埋める想定（実 record 生成は次フェーズ）
- ``menu_item``: 開いているメニューの項目なら True。``text`` を
  :func:`label_forms` で比べる（``"Copy"`` で ``"&Copy"`` に当たる、#22）
- ``is_window``: ``QWidget.isWindow()``（selector の ``window``、#38）
- ``class_lineage``: Qt のメタオブジェクトのクラス名を、自分から基底へ並べたもの
  （selector の ``class_inherits``。``QObject.inherits()`` と同じ判定、#38）

``is_window`` と ``class_lineage`` は snapshot の node には無い（その場の widget
から作る record だけが持つ）。

live tree 側では QWidget から record を抽出し、snapshot 側ではノード辞書を
そのまま record として扱う。
"""

from __future__ import annotations

import re
from typing import Any

# 日本語などの Qt が付ける「いいえ(&N)」のようなニーモニックの括弧
_MNEMONIC_SUFFIX_RE = re.compile(r"\s*\(&.\)$")

# その場の Qt の widget からしか判定できない selector のキー（#38）。snapshot の
# node には型の系譜も isWindow も入っていない
LIVE_ONLY_KEYS = ("window", "class_inherits")


class LiveOnlySelectorError(ValueError):
    """snapshot で探す API に、snapshot では判定できないキーを渡した（#38）。

    黙って「当たらない」にすると、``wait_for_widget`` は時間切れまで待ち、値の
    ``expect`` は値が無いと言うだけで、原因が分からない。
    """


def live_only_keys(selector: dict[str, Any]) -> list[str]:
    """selector（``_scope_chain`` の各段も含む）が使っている :data:`LIVE_ONLY_KEYS`。"""
    found = [key for key in LIVE_ONLY_KEYS if key in selector]
    for step in selector.get("_scope_chain") or ():
        if isinstance(step, dict):
            found.extend(key for key in live_only_keys(step) if key not in found)
    return found


def check_snapshot_selector(selector: dict[str, Any]) -> None:
    """snapshot で探す前に呼ぶ。:data:`LIVE_ONLY_KEYS` があれば :class:`LiveOnlySelectorError`。"""
    keys = live_only_keys(selector)
    if keys:
        raise LiveOnlySelectorError(
            f"selector keys {keys} are not in snapshots and work only where the widget is "
            "looked up in QGIS: actions, close(), and expect's to_be_visible / to_be_hidden / "
            "to_be_enabled / to_be_disabled / to_have_count. They cannot be used with "
            "snapshot(), get_text(), get_value(), wait_for_widget, or expect's to_have_text / "
            f"to_contain_text / to_have_value / to_be_checked / to_have_row_count: {selector!r}"
        )


def label_forms(text: str) -> tuple[str, ...]:
    """ボタン・メニュー項目の文言を、指定と比べる形にする。

    そのまま・``&`` のニーモニック抜き・末尾の ``(&N)`` 抜きの 3 つ（``"No"`` で
    ``"&No"``、``"いいえ"`` で ``"いいえ(&N)"`` に当たる）。``&&`` は文字としての
    ``&``（``"Cut && Paste"`` は ``"Cut & Paste"`` と表示される）。
    """

    def strip(value: str) -> str:
        return value.replace("&&", "\0").replace("&", "").replace("\0", "&")

    return (text, strip(text), strip(_MNEMONIC_SUFFIX_RE.sub("", text)))


def record_matches_selector(record: dict[str, Any], selector: dict[str, Any]) -> bool:
    """1 件の record が selector の AND 条件を満たすかを判定。

    selector に与えられたキーのみ評価する（キー無し = フィルタしない）。
    値の比較は完全一致。

    Args:
        record: 判定対象の record dict（class / object_name / text / title /
            label / placeholder のいずれかを含み得る）。
        selector: 判定条件 dict。`class` / `object_name` / `text` / `title` /
            `label` / `placeholder` のいずれかを含む。それ以外のキー
            （`index` / `scope` / `root_object_name` 等）はここでは無視する。

    Returns:
        全条件を満たすなら True。
    """
    cls = selector.get("class")
    if cls is not None and record.get("class") != cls:
        return False
    obj = selector.get("object_name")
    if obj is not None and record.get("object_name") != obj:
        return False
    text = selector.get("text")
    if text is not None:
        rec_text = record.get("text")
        if record.get("menu_item") and isinstance(rec_text, str):
            if text not in label_forms(rec_text):
                return False
        elif rec_text != text:
            return False
    title = selector.get("title")
    if title is not None and record.get("title") != title:
        return False
    label = selector.get("label")
    if label is not None and record.get("label") != label:
        return False
    placeholder = selector.get("placeholder")
    if placeholder is not None and record.get("placeholder") != placeholder:
        return False
    role = selector.get("role")
    if role is not None and record.get("role") != role:
        return False
    # ADR-0002 §8.2 拡張: 部分一致 / 正規表現 / 任意属性
    text_contains = selector.get("text_contains")
    if text_contains is not None:
        rec_text = record.get("text")
        if not isinstance(rec_text, str) or text_contains not in rec_text:
            return False
    title_contains = selector.get("title_contains")
    if title_contains is not None:
        rec_title = record.get("title")
        if not isinstance(rec_title, str) or title_contains not in rec_title:
            return False
    text_re = selector.get("text_re")
    if text_re is not None:
        rec_text = record.get("text")
        if not isinstance(rec_text, str):
            return False
        try:
            if re.search(text_re, rec_text) is None:
                return False
        except re.error:
            # 不正な正規表現はマッチしない扱い（silently skip。呼び出し側で
            # バリデートする想定で、selector 全体を fail させない）。
            return False
    window = selector.get("window")
    if window is not None and record.get("is_window") is not bool(window):
        return False
    class_inherits = selector.get("class_inherits")
    if class_inherits is not None and class_inherits not in (record.get("class_lineage") or ()):
        return False
    attr = selector.get("attr")
    if attr is not None:
        if not isinstance(attr, dict):
            return False
        for k, v in attr.items():
            if record.get(k) != v:
                return False
    return True


def resolve_with_index(
    records: list[dict[str, Any]], selector: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """既にフィルタ済みの match list から、`index` / strict mode を解釈して 1 件確定する。

    呼び出し側は ``record_matches_selector`` で絞った後の list をそのまま渡す。

    Strict mode (ADR-0002 §8.1):
        - `index` が selector に **明示** されていない & len(records) > 1 →
          `(None, {"reason": "selector_ambiguous", ...})` を返す
        - `index` が指定されていれば該当 N 番目を返す（範囲外なら
          `(None, {"reason": "index_out_of_range", ...})`）
        - 単一マッチなら index 未指定でもそれを返す（旧挙動と互換）

    Returns:
        (確定 record or None, diagnostics dict)
    """
    if not records:
        return None, {"reason": "no_match"}

    explicit_index = "index" in selector
    if not explicit_index and len(records) > 1:
        return None, {
            "reason": "selector_ambiguous",
            "match_count": len(records),
            # 上位 10 件を返す（呼び出し側で widget summary 化済の想定だが、
            # snapshot 側ではそのまま node が記録される）
            "candidates": records[:10],
            "hint": (
                "Add 'index' to disambiguate, or refine selector with "
                "'object_name' / 'root_object_name' / 'scope'."
            ),
        }

    index = selector.get("index", 0)
    if index >= len(records):
        return None, {"reason": "index_out_of_range", "match_count": len(records)}

    return records[index], {"match_count": len(records)}
