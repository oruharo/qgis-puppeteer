"""selector_match.py の単体テスト。

純粋関数なので Qt 不要。snapshot 側 (`_find_in_snapshot`) と live tree 側
(`_find_widget`) の両方から使われる共通ロジックの挙動を網羅する。
"""

from __future__ import annotations

from qgis_puppeteer.selector_match import (
    record_matches_selector,
    resolve_with_index,
)

# ============================================================
# record_matches_selector
# ============================================================


class TestRecordMatchesSelector:
    def test_empty_selector_matches_anything(self) -> None:
        rec = {"class": "QPushButton", "object_name": "ok"}
        assert record_matches_selector(rec, {}) is True

    def test_single_key_match(self) -> None:
        rec = {"class": "QPushButton", "object_name": "ok"}
        assert record_matches_selector(rec, {"class": "QPushButton"}) is True
        assert record_matches_selector(rec, {"class": "QLineEdit"}) is False

    def test_and_semantics(self) -> None:
        rec = {"class": "QPushButton", "object_name": "ok"}
        # 両方一致 → True
        assert record_matches_selector(rec, {"class": "QPushButton", "object_name": "ok"}) is True
        # 片方ミスマッチ → False
        assert (
            record_matches_selector(rec, {"class": "QPushButton", "object_name": "cancel"}) is False
        )

    def test_label_and_placeholder_match(self) -> None:
        # 拡張 selector key の前方互換
        rec = {
            "class": "QLineEdit",
            "object_name": "",
            "label": "Username",
            "placeholder": "Enter your name",
        }
        assert record_matches_selector(rec, {"label": "Username"}) is True
        assert record_matches_selector(rec, {"placeholder": "Enter your name"}) is True
        assert record_matches_selector(rec, {"label": "Password"}) is False

    def test_missing_field_treated_as_mismatch(self) -> None:
        # selector が要求するキーが record に無ければミスマッチ扱い
        rec = {"class": "QLineEdit"}  # label 欠落
        assert record_matches_selector(rec, {"label": "Username"}) is False

    def test_ignores_non_match_keys(self) -> None:
        # `index` / `scope` / `root_object_name` 等は match 判定では無視
        rec = {"class": "QPushButton"}
        assert (
            record_matches_selector(rec, {"class": "QPushButton", "index": 5, "scope": "modal"})
            is True
        )


# ============================================================
# resolve_with_index
# ============================================================


class TestResolveWithIndex:
    def test_no_match_returns_none(self) -> None:
        result, diag = resolve_with_index([], {"class": "QPushButton"})
        assert result is None
        assert diag["reason"] == "no_match"

    def test_single_match_without_index_returns_it(self) -> None:
        rec = {"class": "QPushButton", "object_name": "ok"}
        result, diag = resolve_with_index([rec], {"class": "QPushButton"})
        assert result == rec
        assert diag["match_count"] == 1

    def test_multiple_matches_without_index_is_ambiguous(self) -> None:
        records = [
            {"class": "QPushButton", "object_name": "ok"},
            {"class": "QPushButton", "object_name": "cancel"},
        ]
        result, diag = resolve_with_index(records, {"class": "QPushButton"})
        assert result is None
        assert diag["reason"] == "selector_ambiguous"
        assert diag["match_count"] == 2
        assert len(diag["candidates"]) == 2
        assert "hint" in diag

    def test_multiple_matches_with_explicit_index_picks_nth(self) -> None:
        records = [
            {"class": "QPushButton", "object_name": "ok"},
            {"class": "QPushButton", "object_name": "cancel"},
            {"class": "QPushButton", "object_name": "apply"},
        ]
        result, diag = resolve_with_index(records, {"class": "QPushButton", "index": 1})
        assert result == records[1]
        assert diag["match_count"] == 3

    def test_explicit_index_zero_opts_out_of_strict(self) -> None:
        # `index=0` を明示すれば旧挙動（先頭採用）に戻る
        records = [
            {"class": "QPushButton", "object_name": "ok"},
            {"class": "QPushButton", "object_name": "cancel"},
        ]
        result, diag = resolve_with_index(records, {"class": "QPushButton", "index": 0})
        assert result == records[0]

    def test_index_out_of_range(self) -> None:
        records = [{"class": "QPushButton"}]
        result, diag = resolve_with_index(records, {"class": "QPushButton", "index": 5})
        assert result is None
        assert diag["reason"] == "index_out_of_range"
        assert diag["match_count"] == 1

    def test_candidates_capped_at_10(self) -> None:
        # 候補が 30 件あっても diagnostics には上位 10 件のみ
        records = [{"class": "QPushButton", "object_name": f"btn_{i}"} for i in range(30)]
        result, diag = resolve_with_index(records, {"class": "QPushButton"})
        assert result is None
        assert diag["reason"] == "selector_ambiguous"
        assert diag["match_count"] == 30
        assert len(diag["candidates"]) == 10


# ============================================================
# ADR-0002 §8.2: text_re / text_contains / attr
# ============================================================


class TestTextContains:
    def test_partial_match(self) -> None:
        rec = {"class": "QPushButton", "text": "保存(&S)"}
        assert record_matches_selector(rec, {"text_contains": "保存"}) is True
        assert record_matches_selector(rec, {"text_contains": "(&S)"}) is True
        assert record_matches_selector(rec, {"text_contains": "削除"}) is False

    def test_with_other_keys_anded(self) -> None:
        rec = {"class": "QPushButton", "text": "保存(&S)"}
        # AND セマンティクスで他キーと併用可能
        assert (
            record_matches_selector(rec, {"class": "QPushButton", "text_contains": "保存"}) is True
        )
        assert record_matches_selector(rec, {"class": "QLabel", "text_contains": "保存"}) is False

    def test_no_text_field_is_false(self) -> None:
        rec = {"class": "QFrame"}
        # text フィールドが無い record は text_contains で False
        assert record_matches_selector(rec, {"text_contains": "anything"}) is False

    def test_non_string_text_is_false(self) -> None:
        rec = {"class": "X", "text": None}
        assert record_matches_selector(rec, {"text_contains": "x"}) is False


class TestTextRe:
    def test_basic_regex_match(self) -> None:
        rec = {"class": "QLabel", "text": "項目 12 件"}
        assert record_matches_selector(rec, {"text_re": r"\d+ 件"}) is True
        assert record_matches_selector(rec, {"text_re": r"^項目"}) is True
        assert record_matches_selector(rec, {"text_re": r"^件"}) is False

    def test_search_semantics_not_fullmatch(self) -> None:
        """``re.search`` 動作なので部分マッチで True。"""
        rec = {"class": "X", "text": "abc 123 def"}
        assert record_matches_selector(rec, {"text_re": r"\d+"}) is True

    def test_invalid_regex_does_not_raise(self) -> None:
        """不正な regex は silently skip して False を返す。"""
        rec = {"class": "X", "text": "any"}
        # ``[`` は壊れた pattern
        assert record_matches_selector(rec, {"text_re": "["}) is False

    def test_no_text_field_is_false(self) -> None:
        rec = {"class": "QFrame"}
        assert record_matches_selector(rec, {"text_re": "."}) is False


class TestAttr:
    def test_matches_record_attribute(self) -> None:
        rec = {"class": "X", "object_name": "btn", "is_visible": True, "value": 42}
        assert record_matches_selector(rec, {"attr": {"is_visible": True}}) is True
        assert record_matches_selector(rec, {"attr": {"value": 42}}) is True
        assert record_matches_selector(rec, {"attr": {"value": 99}}) is False

    def test_multiple_attrs_anded(self) -> None:
        rec = {"class": "X", "is_visible": True, "value": 42}
        assert record_matches_selector(rec, {"attr": {"is_visible": True, "value": 42}}) is True
        assert record_matches_selector(rec, {"attr": {"is_visible": True, "value": 99}}) is False

    def test_missing_field_is_false(self) -> None:
        """attr で要求された key が record に無ければ False。"""
        rec = {"class": "X"}
        assert record_matches_selector(rec, {"attr": {"is_visible": True}}) is False

    def test_non_dict_attr_is_false(self) -> None:
        rec = {"class": "X"}
        # 不正な値（dict 以外）は False
        assert record_matches_selector(rec, {"attr": "not a dict"}) is False


# ============================================================
# ADR-0002 §8.4: getByRole (Qt accessible role)
# ============================================================


class TestRole:
    def test_role_match(self) -> None:
        rec = {"class": "QPushButton", "role": "Button"}
        assert record_matches_selector(rec, {"role": "Button"}) is True
        assert record_matches_selector(rec, {"role": "ComboBox"}) is False

    def test_no_role_field_is_false(self) -> None:
        """record に role が無い → role 指定の selector は不一致。"""
        rec = {"class": "QPushButton"}
        assert record_matches_selector(rec, {"role": "Button"}) is False

    def test_role_anded_with_other_keys(self) -> None:
        rec = {"class": "QPushButton", "role": "Button", "text": "OK"}
        # AND 評価（class + role + text すべて一致）
        assert record_matches_selector(rec, {"role": "Button", "text": "OK"}) is True
        assert record_matches_selector(rec, {"role": "Button", "text": "Cancel"}) is False

    def test_role_none_does_not_filter(self) -> None:
        """selector に role を指定しなければ素通し。"""
        rec = {"class": "QPushButton"}  # role 無し
        # role キーを selector に含めない場合は他キーだけで判定
        assert record_matches_selector(rec, {"class": "QPushButton"}) is True
