"""waiting.py（MCP の ``qgis_wait_for`` が使う条件の判定）の単体テスト。

純粋関数なので Qt も Hub も要らない。くり返して待つ側は ``test_mcp_gateway.py`` の
``TestMcpGatewayWaitFor``。
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from qgis_puppeteer import waiting
from qgis_puppeteer.selector_match import LiveOnlySelectorError


def _check(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "exists": True,
        "visible": True,
        "enabled": True,
        "not_covered": True,
        "editable": True,
        "actionable": True,
        "diagnostics": {"match_count": 1},
    }
    base.update(overrides)
    return base


class TestConditionSets:
    def test_every_condition_belongs_to_exactly_one_set(self) -> None:
        sets = (
            waiting.CHECK_CONDITIONS,
            waiting.COUNT_CONDITIONS,
            waiting.NODE_CONDITIONS,
            waiting.WINDOW_CONDITIONS,
        )
        for until in get_args(waiting.WaitUntil):
            assert sum(until in s for s in sets) == 1, until
        assert set().union(*sets) == set(get_args(waiting.WaitUntil))


class TestValidate:
    @pytest.mark.parametrize(
        ("until", "selector", "expected", "title_contains"),
        [
            ("visible", {"object_name": "ok"}, None, None),
            ("count", {"class": "QDialog"}, 0, None),
            ("text_contains", {"object_name": "status"}, "done", None),
            ("value", {"object_name": "spin"}, 3, None),
            ("value", {"object_name": "check"}, False, None),
            ("checked", {"object_name": "check"}, None, None),
            ("row_count", {"object_name": "table"}, 0, None),
            ("modal", None, None, None),
            ("modal", None, None, "Save"),
            ("popup_closed", None, None, None),
        ],
    )
    def test_accepted(
        self, until: str, selector: Any, expected: Any, title_contains: str | None
    ) -> None:
        assert waiting.validate(until, selector, expected, title_contains) is None

    @pytest.mark.parametrize(
        ("until", "selector", "expected", "title_contains", "fragment"),
        [
            ("visible", None, None, None, "needs a selector"),
            ("visible", {"object_name": "ok"}, True, None, "takes no expected"),
            ("visible", {"object_name": "ok"}, None, "Save", "title_contains is only"),
            ("text", {"object_name": "s"}, None, None, "needs expected"),
            ("text", {"object_name": "s"}, 3, None, "needs a string"),
            ("count", {"class": "QDialog"}, -1, None, "whole number"),
            ("count", {"class": "QDialog"}, True, None, "whole number"),
            ("row_count", {"object_name": "t"}, 1.5, None, "whole number"),
            ("modal", {"class": "QDialog"}, None, None, "takes no selector"),
            ("popup", None, "File", None, "use title_contains"),
            ("modal_closed", None, None, "Save", "drop title_contains"),
        ],
    )
    def test_rejected(
        self,
        until: str,
        selector: Any,
        expected: Any,
        title_contains: str | None,
        fragment: str,
    ) -> None:
        problem = waiting.validate(until, selector, expected, title_contains)
        assert problem is not None
        assert fragment in problem


class TestCount:
    def test_selector_drops_index_and_counts_visible_widgets(self) -> None:
        selector = {"class": "QDialog", "index": 2}
        assert waiting.count_selector(selector) == {"class": "QDialog", "attr": {"visible": True}}
        assert selector == {"class": "QDialog", "index": 2}  # 元は変えない

    def test_explicit_visibility_is_kept(self) -> None:
        counted = waiting.count_selector({"class": "QDialog", "attr": {"visible": False}})
        assert counted["attr"] == {"visible": False}

    def test_count_reads_match_count(self) -> None:
        assert waiting.count_from_check(_check(diagnostics={"match_count": 3})) == 3
        assert waiting.count_from_check(_check(exists=False, diagnostics={"match_count": 0})) == 0

    def test_count_falls_back_to_exists(self) -> None:
        assert waiting.count_from_check(_check(diagnostics={})) == 1
        assert waiting.count_from_check(_check(exists=False, diagnostics=None)) == 0


class TestCheckConditions:
    def test_ambiguous(self) -> None:
        assert waiting.is_ambiguous(
            _check(exists=False, diagnostics={"reason": "selector_ambiguous", "match_count": 2})
        )
        assert not waiting.is_ambiguous(_check())
        assert not waiting.is_ambiguous(_check(diagnostics=None))

    @pytest.mark.parametrize(
        ("until", "check", "met"),
        [
            ("visible", _check(), True),
            ("visible", _check(visible=False), False),
            ("hidden", _check(visible=False), True),
            ("hidden", _check(exists=False, visible=False), True),
            ("hidden", _check(), False),
            ("enabled", _check(), True),
            ("disabled", _check(enabled=False), True),
            ("disabled", _check(), False),
            # 見つからない結果も enabled=False。selector の書き間違いで満たさない
            ("disabled", _check(exists=False, visible=False, enabled=False), False),
            ("actionable", _check(), True),
            ("actionable", _check(not_covered=False, actionable=False), False),
            ("editable", _check(), True),
            ("editable", _check(editable=False), False),
            ("editable", _check(actionable=False), False),
        ],
    )
    def test_evaluate(self, until: str, check: dict[str, Any], met: bool) -> None:
        assert waiting.evaluate_check(until, check) is met

    def test_other_conditions_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            waiting.evaluate_check("text", _check())


_SNAPSHOT: dict[str, Any] = {
    "active_modal": None,
    "visible_dialogs": [],
    "main_window": {
        "class": "QMainWindow",
        "children": [
            {"class": "QLabel", "object_name": "status", "text": "Loading 3 layers"},
            {"class": "QSpinBox", "object_name": "spin", "value": 4, "text": "4"},
            {"class": "QComboBox", "object_name": "crs", "current_text": "EPSG:4326"},
            {"class": "QCheckBox", "object_name": "check", "checked": True, "text": "Snap"},
            {"class": "QTableWidget", "object_name": "table", "row_count": 2},
            {"class": "QPushButton", "text": "OK"},
            {"class": "QPushButton", "text": "OK"},
        ],
    },
}


class TestNodeConditions:
    @pytest.mark.parametrize(
        ("until", "selector", "value"),
        [
            ("text", {"object_name": "status"}, "Loading 3 layers"),
            ("text_contains", {"object_name": "status"}, "Loading 3 layers"),
            ("value", {"object_name": "spin"}, 4),
            ("value", {"object_name": "crs"}, "EPSG:4326"),
            ("checked", {"object_name": "check"}, True),
            ("row_count", {"object_name": "table"}, 2),
            ("text", {"object_name": "table"}, None),  # 文字を持たない
            ("value", {"object_name": "missing"}, None),
            ("text", {"text": "OK", "class": "QPushButton"}, None),  # 2 つに当たる
        ],
    )
    def test_read_node(self, until: str, selector: dict[str, Any], value: Any) -> None:
        assert waiting.read_node(until, _SNAPSHOT, selector) == value

    def test_read_node_rejects_live_only_keys(self) -> None:
        with pytest.raises(LiveOnlySelectorError):
            waiting.read_node("text", _SNAPSHOT, {"object_name": "status", "window": "Main"})

    @pytest.mark.parametrize(
        ("until", "observed", "expected", "met"),
        [
            ("text", "done", "done", True),
            ("text", "done!", "done", False),
            ("text", None, "", False),
            ("text_contains", "Loading 3 layers", "3 layers", True),
            ("text_contains", None, "3", False),
            ("value", 4, 4, True),
            ("value", None, None, False),
            ("row_count", 0, 0, True),
            ("row_count", None, 0, False),
            ("checked", True, None, True),
            ("checked", None, None, False),
            ("unchecked", False, None, True),
            ("unchecked", None, None, True),
            ("unchecked", True, None, False),
        ],
    )
    def test_evaluate(self, until: str, observed: Any, expected: Any, met: bool) -> None:
        assert waiting.evaluate_node(until, observed, expected) is met

    def test_node_value_order(self) -> None:
        assert waiting.node_value(None) is None
        assert waiting.node_value({"text": "4", "value": 4}) == 4
        assert waiting.node_value({"text": "Snap", "checked": False}) is False
        assert waiting.node_value({"class": "QWidget"}) is None


class TestWindowConditions:
    _MODAL: dict[str, Any] = {
        "class": "QMessageBox",
        "title": "Save changes?",
        "children": [{"class": "QPushButton", "text": "&Save"}],
    }
    _POPUP: dict[str, Any] = {
        "class": "QMenu",
        "title": "&File",
        "menu_items": [{"text": "&Open", "menu_item": True}],
    }

    def test_read_window(self) -> None:
        snapshot = {"active_modal": self._MODAL, "active_popup": None}
        assert waiting.read_window("modal", snapshot) is self._MODAL
        assert waiting.read_window("modal_closed", snapshot) is self._MODAL
        assert waiting.read_window("popup", snapshot) is None

    @pytest.mark.parametrize(
        ("until", "window", "title_contains", "met"),
        [
            ("modal", _MODAL, None, True),
            ("modal", _MODAL, "Save", True),
            ("modal", _MODAL, "Open", False),
            ("modal", None, None, False),
            ("modal_closed", None, None, True),
            ("modal_closed", _MODAL, None, False),
            ("popup", _POPUP, "File", True),  # & を除いた形でも比べる
            ("popup", _POPUP, "Edit", False),
            ("popup_closed", None, None, True),
        ],
    )
    def test_evaluate(
        self, until: str, window: dict[str, Any] | None, title_contains: str | None, met: bool
    ) -> None:
        assert waiting.evaluate_window(until, window, title_contains) is met

    def test_summary_drops_children_but_keeps_menu_items(self) -> None:
        assert waiting.window_summary(None) is None
        assert waiting.window_summary(self._MODAL) == {
            "class": "QMessageBox",
            "title": "Save changes?",
        }
        assert waiting.window_summary(self._POPUP) == self._POPUP
