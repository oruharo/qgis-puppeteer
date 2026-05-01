"""Selector matching の共通ロジック（Qt 非依存・snapshot/live tree 共通）。

ADR-0002 §8.1 / Roadmap "Selector resolver の統合" を実装する。
qgis_tools.ui_tools._find_widget（live Qt tree 走査）と
pytest_qgis_puppeteer.automation_client._find_in_snapshot（snapshot DFS）が
同じ selector 仕様を採用できるよう、純粋関数として切り出す。

## 公開 API

- ``record_matches_selector(record, selector)`` — 1 件の record を判定
- ``resolve_with_index(records, selector)`` — match list から `index` /
  strict mode の解釈を含めて確定 record を返す

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

live tree 側では QWidget から record を抽出し、snapshot 側ではノード辞書を
そのまま record として扱う。
"""

from __future__ import annotations

import re
from typing import Any


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
    if text is not None and record.get("text") != text:
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
