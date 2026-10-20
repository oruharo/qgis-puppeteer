"""dialog_handlers.py の単体テスト。

純粋ロジック (Qt 非依存) なので Qt 不要。`DialogHandlerRegistry` の登録 /
評価 / once / clear と、`select_for_modal` の冪等性 (同一 modal を 2 回処理
しない) を網羅する。
"""

from __future__ import annotations

import pytest
from qgis_puppeteer.dialog_handlers import (
    DialogHandlerError,
    DialogHandlerRegistry,
)

# ============================================================
# register / unregister / list / clear
# ============================================================


class TestRegistryCrud:
    def test_register_and_list(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("dismiss-bad-layers", {"title": "Bad Layers"}, "accept")
        specs = reg.list_specs()
        assert len(specs) == 1
        assert specs[0]["name"] == "dismiss-bad-layers"
        assert specs[0]["predicate"] == {"title": "Bad Layers"}
        assert specs[0]["action"] == "accept"
        assert specs[0]["once"] is False

    def test_register_overwrites_existing_name(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "A"}, "accept")
        reg.register("h", {"title": "B"}, "reject")
        specs = reg.list_specs()
        assert len(specs) == 1
        assert specs[0]["predicate"] == {"title": "B"}
        assert specs[0]["action"] == "reject"

    def test_unregister_returns_true_when_present(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("h", {}, "accept")
        assert reg.unregister("h") is True
        assert len(reg) == 0

    def test_unregister_returns_false_when_absent(self) -> None:
        reg = DialogHandlerRegistry()
        assert reg.unregister("nope") is False

    def test_clear_removes_all(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("a", {}, "accept")
        reg.register("b", {}, "reject")
        reg.clear()
        assert len(reg) == 0


# ============================================================
# Validation
# ============================================================


class TestRegisterValidation:
    def test_empty_name_rejected(self) -> None:
        reg = DialogHandlerRegistry()
        with pytest.raises(DialogHandlerError, match="name must not be empty"):
            reg.register("", {}, "accept")

    def test_invalid_action_rejected(self) -> None:
        reg = DialogHandlerRegistry()
        with pytest.raises(DialogHandlerError, match="unsupported action"):
            reg.register("h", {}, "explode")

    def test_non_dict_predicate_rejected(self) -> None:
        reg = DialogHandlerRegistry()
        with pytest.raises(DialogHandlerError, match="predicate must be a dict"):
            reg.register("h", "not-a-dict", "accept")  # type: ignore[arg-type]

    def test_all_valid_actions_accepted(self) -> None:
        reg = DialogHandlerRegistry()
        for action in ("accept", "reject", "close"):
            reg.register(action, {}, action)
        assert len(reg) == 3

    def test_button_action_keeps_the_button(self) -> None:
        """#21: action="button" は押すボタンを持ち、一覧にも出る。"""
        reg = DialogHandlerRegistry()
        reg.register("yes", {"class": "QMessageBox"}, "button", button="Yes")
        reg.register("acc", {}, "accept")
        specs = {s["name"]: s for s in reg.list_specs()}
        assert specs["yes"]["action"] == "button"
        assert specs["yes"]["button"] == "Yes"
        assert specs["acc"]["button"] is None
        spec = reg.select_for_modal(1, {"class": "QMessageBox"})
        assert spec is not None
        assert spec.button == "Yes"

    def test_register_reevaluates_the_open_modal(self) -> None:
        """#21: 登録より先に開いていた modal にも、新しい handler が効く。"""
        reg = DialogHandlerRegistry()
        record = {"class": "QMessageBox", "title": "Confirm"}
        assert reg.select_for_modal(7, record) is None  # まだ handler が無い
        reg.register("h", {"title": "Confirm"}, "button", button="Yes")
        spec = reg.select_for_modal(7, record)
        assert spec is not None
        assert spec.name == "h"
        assert reg.select_for_modal(7, record) is None  # 2 度目は発火しない

    def test_popups_go_only_to_handlers_that_opt_in(self) -> None:
        """#22: ポップアップは predicate に "popup": true を書いた handler だけ。

        何にでも当たる ``{}`` が、コンボのドロップダウンなどを閉じないように。
        """
        reg = DialogHandlerRegistry()
        reg.register("any-modal", {}, "close")
        reg.register("menu", {"popup": True, "title": "Layer"}, "button", button="Copy")
        popup = {"class": "QMenu", "title": "Layer", "popup": True}
        spec = reg.select_for_modal(1, popup)
        assert spec is not None
        assert spec.name == "menu"
        dropdown = {"class": "QComboBoxPrivateContainer", "popup": True}
        assert reg.select_for_modal(2, dropdown) is None
        spec = reg.select_for_modal(3, {"class": "QMessageBox", "title": "Layer"})
        assert spec is not None
        assert spec.name == "any-modal"

    def test_button_action_needs_a_button(self) -> None:
        reg = DialogHandlerRegistry()
        with pytest.raises(DialogHandlerError, match="needs button"):
            reg.register("h", {}, "button")
        with pytest.raises(DialogHandlerError, match="needs button"):
            reg.register("h", {}, "button", button="")

    def test_button_on_another_action_is_rejected(self) -> None:
        """accept に button を付けても押されないので、黙って受けずに断る。"""
        reg = DialogHandlerRegistry()
        with pytest.raises(DialogHandlerError, match="only for action"):
            reg.register("h", {}, "accept", button="Ok")


# ============================================================
# select_for_modal の判定 + 冪等性
# ============================================================


class TestSelectForModal:
    def test_no_modal_returns_none(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "Bad Layers"}, "accept")
        assert reg.select_for_modal(None, None) is None

    def test_no_handlers_returns_none(self) -> None:
        reg = DialogHandlerRegistry()
        assert reg.select_for_modal(123, {"class": "QDialog", "title": "X"}) is None

    def test_returns_matching_handler(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "Bad Layers"}, "accept")
        spec = reg.select_for_modal(123, {"class": "QDialog", "title": "Bad Layers"})
        assert spec is not None
        assert spec.name == "h"
        assert spec.action == "accept"

    def test_no_match_returns_none(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "Bad Layers"}, "accept")
        spec = reg.select_for_modal(123, {"class": "QDialog", "title": "Other"})
        assert spec is None

    def test_same_modal_id_processed_only_once(self) -> None:
        """冪等性: 同じ modal を 2 回連続で評価しても 2 回目以降は None。"""
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "Bad Layers"}, "accept")
        record = {"class": "QDialog", "title": "Bad Layers"}

        first = reg.select_for_modal(100, record)
        assert first is not None  # 1 回目は発火
        # 2 回目は同じ modal_id → None
        assert reg.select_for_modal(100, record) is None
        # 3 回目も None
        assert reg.select_for_modal(100, record) is None

    def test_different_modal_id_re_evaluates(self) -> None:
        """異なる modal id (新しい modal が出てきた) なら再評価される。"""
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "Bad Layers"}, "accept")
        record = {"class": "QDialog", "title": "Bad Layers"}

        assert reg.select_for_modal(100, record) is not None
        # 一度 None で modal が閉じる
        assert reg.select_for_modal(None, None) is None
        # 別の modal id (= 別 instance) で再度開く → 再評価
        assert reg.select_for_modal(200, record) is not None

    def test_modal_close_resets_processed_state(self) -> None:
        """modal=None を観測したら、再度同じ id で開いても処理対象になる。"""
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "X"}, "accept")
        record = {"class": "QDialog", "title": "X"}

        assert reg.select_for_modal(100, record) is not None
        assert reg.select_for_modal(None, None) is None
        # 同じ id=100 がまた来た（実プロセスでは別 instance だが id は再利用され得る）
        assert reg.select_for_modal(100, record) is not None

    def test_first_match_wins_when_multiple_handlers(self) -> None:
        """複数 handler が match する場合、登録順で最初のものが返る。"""
        reg = DialogHandlerRegistry()
        reg.register("a", {"class": "QDialog"}, "accept")
        reg.register("b", {"class": "QDialog", "title": "X"}, "reject")
        record = {"class": "QDialog", "title": "X"}
        spec = reg.select_for_modal(100, record)
        assert spec is not None
        assert spec.name == "a"  # 登録順で先勝ち


# ============================================================
# once フラグ
# ============================================================


class TestOnceFlag:
    def test_once_true_unregisters_after_match(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "X"}, "accept", once=True)
        record = {"class": "QDialog", "title": "X"}

        spec = reg.select_for_modal(100, record)
        assert spec is not None
        assert spec.once is True
        # 自動 unregister されている
        assert len(reg) == 0
        assert reg.list_specs() == []

    def test_once_false_keeps_handler_after_match(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "X"}, "accept", once=False)
        record = {"class": "QDialog", "title": "X"}
        reg.select_for_modal(100, record)
        # まだ登録されている
        assert len(reg) == 1

    def test_once_handler_can_re_fire_on_different_modal_after_re_register(
        self,
    ) -> None:
        # once=True で消えた後、もう一度 register すれば再度発火可能
        reg = DialogHandlerRegistry()
        reg.register("h", {"title": "X"}, "accept", once=True)
        reg.select_for_modal(100, {"class": "QDialog", "title": "X"})
        assert len(reg) == 0
        reg.register("h", {"title": "X"}, "accept", once=True)
        spec = reg.select_for_modal(200, {"class": "QDialog", "title": "X"})
        assert spec is not None


# ============================================================
# Predicate matching の幅
# ============================================================


class TestPredicateMatching:
    def test_matches_by_object_name(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("h", {"object_name": "bad_layers_dlg"}, "accept")
        spec = reg.select_for_modal(100, {"class": "QDialog", "object_name": "bad_layers_dlg"})
        assert spec is not None

    def test_matches_by_class(self) -> None:
        reg = DialogHandlerRegistry()
        reg.register("h", {"class": "QMessageBox"}, "reject")
        spec = reg.select_for_modal(100, {"class": "QMessageBox", "title": "Confirm"})
        assert spec is not None

    def test_empty_predicate_matches_any_modal(self) -> None:
        # 空 predicate: 全 modal にマッチ（catch-all）
        reg = DialogHandlerRegistry()
        reg.register("catch-all", {}, "close")
        assert reg.select_for_modal(100, {"class": "QDialog"}) is not None
