"""ADR-0002 §12 web-first assertions の単体テスト。

`expect(locator).to_*` が:
- 条件を満たすまで poll し、満たせば return
- timeout で AssertionError
- ambiguous で即 SelectorAmbiguousError
- not_ で否定形が逆の挙動
を検証する。Worker / QGIS は使わず、フェイク client で振る舞いを差し込む。
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_qgis_puppeteer import LiveOnlySelectorError, expect
from pytest_qgis_puppeteer.automation_client import WidgetOperationError
from pytest_qgis_puppeteer.locator import Locator, SelectorAmbiguousError

# ============================================================
# フェイク client (test_locator.py と同形式)
# ============================================================


class _FakeClient:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def _take(self, command: str) -> Any:
        queue = self._responses.get(command)
        if not queue:
            raise AssertionError(f"unexpected call: {command} (no response queued)")
        return queue.pop(0)

    def check_actionability(
        self, selector: dict[str, Any], *, instance: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("qgis_check_actionability", {"selector": selector}, instance))
        return self._take("qgis_check_actionability")

    def snapshot_ui(
        self,
        *,
        max_depth: int = 8,
        include_invisible: bool = False,
        include_main_window: bool = False,
        instance: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("qgis_snapshot_ui", {"include_main_window": include_main_window}, instance)
        )
        return self._take("qgis_snapshot_ui")

    def read_cell(
        self,
        selector: dict[str, Any],
        cell: Any,
        *,
        role: str | int = "display",
        instance: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("qgis_read_rows", {"selector": selector, "cell": cell, "role": role}, instance)
        )
        result = self._take("qgis_read_rows")
        if not result.get("success"):
            raise WidgetOperationError("qgis_read_rows", result)
        return result


# ============================================================
# helpers
# ============================================================


def _act_result(
    *,
    visible: bool = True,
    enabled: bool = True,
    actionable: bool | None = None,
    editable: bool = True,
    exists: bool = True,
) -> dict[str, Any]:
    if actionable is None:
        actionable = visible and enabled
    return {
        "exists": exists,
        "visible": visible,
        "enabled": enabled,
        "not_covered": True,
        "editable": editable,
        "actionable": actionable,
        "widget": {"class": "QPushButton", "object_name": "btn"} if exists else None,
        "diagnostics": {"match_count": 1} if exists else {"reason": "no_match"},
    }


def _ambiguous_result() -> dict[str, Any]:
    return {
        "exists": False,
        "visible": False,
        "enabled": False,
        "not_covered": False,
        "editable": False,
        "actionable": False,
        "widget": None,
        "diagnostics": {
            "reason": "selector_ambiguous",
            "match_count": 3,
            "candidates": [
                {"class": "QPushButton", "object_name": "ok"},
                {"class": "QPushButton", "object_name": "cancel"},
            ],
        },
    }


def _snapshot_with_text(text: str | None) -> dict[str, Any]:
    if text is None:
        return {"active_modal": None, "visible_dialogs": [], "active_window": None}
    return {
        "active_modal": {
            "class": "QDialog",
            "children": [
                {"class": "QLabel", "object_name": "status", "text": text},
            ],
        },
        "visible_dialogs": [],
        "active_window": None,
    }


# ============================================================
# to_be_visible / to_be_hidden
# ============================================================


class TestToBeVisible:
    def test_passes_immediately_when_visible(self) -> None:
        client = _FakeClient({"qgis_check_actionability": [_act_result(visible=True)]})
        loc = Locator(client, {"object_name": "btn"})
        # 例外を上げずに return する
        expect(loc).to_be_visible()

    def test_polls_until_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    _act_result(visible=False),
                    _act_result(visible=False),
                    _act_result(visible=True),
                ]
            }
        )
        loc = Locator(client, {"object_name": "btn"})
        expect(loc).to_be_visible()
        # 3 回 poll した
        assert len(client.calls) == 3

    def test_times_out_when_never_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        # deadline を確実に超えさせる
        ticks = iter([0.0, 0.1, 0.5, 1.5, 2.0])
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.monotonic", lambda: next(ticks))
        client = _FakeClient({"qgis_check_actionability": [_act_result(visible=False)] * 10})
        loc = Locator(client, {"object_name": "btn"})
        with pytest.raises(AssertionError, match="to_be_visible"):
            expect(loc, timeout_s=1.0).to_be_visible()


class TestToBeHidden:
    def test_passes_when_widget_disappears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    _act_result(visible=True),
                    _act_result(visible=False),
                ]
            }
        )
        loc = Locator(client, {"object_name": "spinner"})
        expect(loc).to_be_hidden()


# ============================================================
# to_be_enabled / to_be_disabled
# ============================================================


class TestEnabledDisabled:
    def test_to_be_enabled_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    _act_result(enabled=False),
                    _act_result(enabled=True),
                ]
            }
        )
        loc = Locator(client, {"object_name": "submit"})
        expect(loc).to_be_enabled()

    def test_to_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    _act_result(enabled=True),
                    _act_result(enabled=False),
                ]
            }
        )
        loc = Locator(client, {"object_name": "submit"})
        expect(loc).to_be_disabled()

    @staticmethod
    def _missing() -> dict[str, Any]:
        return _act_result(exists=False, visible=False, enabled=False)

    @pytest.mark.parametrize("negated", [False, True], ids=["to_be_disabled", "not_enabled"])
    def test_disabled_fails_when_no_widget_matches(
        self, monkeypatch: pytest.MonkeyPatch, negated: bool
    ) -> None:
        """見つからない結果も enabled=False。selector の書き間違いで通らないこと。"""
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        ticks = iter([0.0, 0.1, 0.5, 1.5, 2.0])
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.monotonic", lambda: next(ticks))
        client = _FakeClient({"qgis_check_actionability": [self._missing()] * 10})
        loc = Locator(client, {"object_name": "sumbit"})
        assertion = expect(loc, timeout_s=1.0)
        with pytest.raises(AssertionError, match="no widget matched") as excinfo:
            if negated:
                assertion.not_.to_be_enabled()
            else:
                assertion.to_be_disabled()
        assert "'exists': False" in str(excinfo.value)  # last_check が載る
        assert len(client.calls) > 1  # すぐには諦めず、現れるのを待った

    def test_to_be_disabled_waits_for_the_widget_to_appear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {"qgis_check_actionability": [self._missing(), _act_result(enabled=False)]}
        )
        loc = Locator(client, {"object_name": "submit"})
        expect(loc).to_be_disabled()
        assert len(client.calls) == 2

    def test_hidden_still_passes_when_no_widget_matches(self) -> None:
        """「見えない」は無くても満たす（to_be_hidden / not_.to_be_visible は今のまま）。"""
        client = _FakeClient({"qgis_check_actionability": [self._missing()] * 2})
        loc = Locator(client, {"object_name": "spinner"})
        expect(loc).to_be_hidden()
        expect(loc).not_.to_be_visible()

    def test_not_to_be_disabled_waits_for_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {"qgis_check_actionability": [self._missing(), _act_result(enabled=True)]}
        )
        loc = Locator(client, {"object_name": "submit"})
        expect(loc).not_.to_be_disabled()
        assert len(client.calls) == 2


# ============================================================
# to_have_text / to_contain_text
# ============================================================


class TestToHaveText:
    def test_passes_when_text_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result()],
                "qgis_snapshot_ui": [_snapshot_with_text("Logged in")],
            }
        )
        loc = Locator(client, {"object_name": "status"})
        expect(loc).to_have_text("Logged in")

    def test_polls_until_text_changes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result()] * 4,
                "qgis_snapshot_ui": [
                    _snapshot_with_text("Loading..."),
                    _snapshot_with_text("Loading..."),
                    _snapshot_with_text("Loading..."),
                    _snapshot_with_text("Done"),
                ],
            }
        )
        loc = Locator(client, {"object_name": "status"})
        expect(loc).to_have_text("Done")

    def test_times_out_with_last_value_in_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        ticks = iter([0.0, 0.1, 0.5, 1.5, 2.0])
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.monotonic", lambda: next(ticks))
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result()] * 5,
                "qgis_snapshot_ui": [_snapshot_with_text("Loading...")] * 5,
            }
        )
        loc = Locator(client, {"object_name": "status"})
        with pytest.raises(AssertionError) as exc_info:
            expect(loc, timeout_s=1.0).to_have_text("Done")
        assert "Loading..." in str(exc_info.value)
        assert "to_have_text" in str(exc_info.value)


class TestToContainText:
    def test_substring_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result()],
                "qgis_snapshot_ui": [_snapshot_with_text("12 件 表示中")],
            }
        )
        # _snapshot_with_text は object_name="status" の QLabel を作る
        loc = Locator(client, {"object_name": "status"})
        expect(loc).to_contain_text("件")

    def test_none_text_does_not_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # snapshot に該当 widget 無しで get_text() = None。"X" を含まないので fail
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        ticks = iter([0.0, 0.1, 0.5, 1.5, 2.0])
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.monotonic", lambda: next(ticks))
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result()] * 5,
                "qgis_snapshot_ui": [_snapshot_with_text(None)] * 5,
            }
        )
        loc = Locator(client, {"object_name": "missing"})
        with pytest.raises(AssertionError):
            expect(loc, timeout_s=1.0).to_contain_text("X")


# ============================================================
# not_ プロパティ
# ============================================================


class TestNegation:
    def test_not_to_be_visible_passes_when_hidden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient({"qgis_check_actionability": [_act_result(visible=False)]})
        loc = Locator(client, {"object_name": "spinner"})
        expect(loc).not_.to_be_visible()

    def test_not_to_have_text_passes_when_text_differs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result()],
                "qgis_snapshot_ui": [_snapshot_with_text("Done")],
            }
        )
        loc = Locator(client, {"object_name": "status"})
        expect(loc).not_.to_have_text("Loading...")

    @pytest.mark.parametrize(
        "assertion",
        [
            lambda e: e.not_.to_have_text("Loading..."),
            lambda e: e.not_.to_contain_text("Loading"),
            lambda e: e.not_.to_have_value(3),
            lambda e: e.not_.to_be_checked(),
            lambda e: e.not_.to_have_row_count(5),
        ],
        ids=["text", "contain_text", "value", "checked", "row_count"],
    )
    def test_value_negations_fail_when_no_widget_matches(
        self, monkeypatch: pytest.MonkeyPatch, assertion: Any
    ) -> None:
        """見つからないと値は None。「値が違う」とみなすと、selector の書き間違いで通る。"""
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        ticks = iter([0.0, 0.1, 0.5, 1.5, 2.0])
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.monotonic", lambda: next(ticks))
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result(exists=False)] * 10,
                "qgis_snapshot_ui": [_snapshot_with_text(None)] * 10,
            }
        )
        loc = Locator(client, {"object_name": "stauts"})
        with pytest.raises(AssertionError, match="no widget matched") as excinfo:
            assertion(expect(loc, timeout_s=1.0))
        assert "'found': False" in str(excinfo.value)
        snapshots = [c for c in client.calls if c[0] == "qgis_snapshot_ui"]
        assert len(snapshots) > 1  # すぐには諦めず、現れるのを待った

    def test_value_negation_waits_for_the_widget_to_appear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result(exists=False), _act_result()],
                "qgis_snapshot_ui": [_snapshot_with_text(None), _snapshot_with_text("Done")],
            }
        )
        loc = Locator(client, {"object_name": "status"})
        expect(loc).not_.to_have_text("Loading...")
        assert len([c for c in client.calls if c[0] == "qgis_snapshot_ui"]) == 2

    def test_double_not_returns_normal(self) -> None:
        # not_.not_ は元に戻る（純粋プロパティの動作確認）
        client = _FakeClient({"qgis_check_actionability": [_act_result(visible=True)]})
        loc = Locator(client, {"object_name": "btn"})
        expect_handle = expect(loc).not_.not_
        # not_.not_.to_be_visible() は通常の to_be_visible() と同じ
        expect_handle.to_be_visible()


# ============================================================
# Strict mode 連動 (ambiguous で early-fail)
# ============================================================


class TestAmbiguousAbort:
    def test_to_be_visible_raises_immediately_on_ambiguous(self) -> None:
        client = _FakeClient({"qgis_check_actionability": [_ambiguous_result()]})
        loc = Locator(client, {"class": "QPushButton"})
        with pytest.raises(SelectorAmbiguousError):
            expect(loc).to_be_visible()

    def test_to_have_text_raises_immediately_on_ambiguous(self) -> None:
        client = _FakeClient({"qgis_check_actionability": [_ambiguous_result()]})
        loc = Locator(client, {"class": "QLabel"})
        with pytest.raises(SelectorAmbiguousError):
            expect(loc).to_have_text("X")


# ============================================================
# 個別 timeout
# ============================================================


class TestPerCallTimeout:
    def test_method_timeout_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)
        # default 5.0、method 上書き 0.1
        ticks = iter([0.0, 0.05, 0.2, 0.3])
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.monotonic", lambda: next(ticks))
        client = _FakeClient({"qgis_check_actionability": [_act_result(visible=False)] * 5})
        loc = Locator(client, {"object_name": "btn"})
        with pytest.raises(AssertionError):
            expect(loc).to_be_visible(timeout_s=0.1)


# ============================================================
# to_be_checked(cell=...)：表・ツリーの行のチェック
# ============================================================


def _cell(state: str | None) -> dict[str, Any]:
    return {"success": True, "cell": {"row": 0, "column": 0, "path": [1, 0]}, "value": state}


class TestToBeCheckedCell:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.sleep", lambda _s: None)

    def test_waits_until_the_row_is_checked(self) -> None:
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result()] * 3,
                "qgis_read_rows": [
                    {"success": False, "error": "row_not_found"},
                    _cell("unchecked"),
                    _cell("checked"),
                ],
            }
        )
        loc = Locator(client, {"object_name": "layers"})
        expect(loc).to_be_checked(cell={"path": ["roads", "primary"]})
        reads = [c[1] for c in client.calls if c[0] == "qgis_read_rows"]
        assert reads[-1] == {
            "selector": {"object_name": "layers"},
            "cell": {"path": ["roads", "primary"]},
            "role": "check",
        }

    @pytest.mark.parametrize("state", ["unchecked", "partial"])
    def test_not_is_met_by_unchecked_and_partial(self, state: str) -> None:
        client = _FakeClient(
            {"qgis_check_actionability": [_act_result()], "qgis_read_rows": [_cell(state)]}
        )
        expect(Locator(client, {"object_name": "layers"})).not_.to_be_checked(cell=0)

    @pytest.mark.parametrize("negated", [False, True])
    def test_a_cell_without_a_check_box_meets_neither(
        self, monkeypatch: pytest.MonkeyPatch, negated: bool
    ) -> None:
        ticks = iter([0.0, 0.0, 10.0])
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.monotonic", lambda: next(ticks))
        client = _FakeClient(
            {"qgis_check_actionability": [_act_result()] * 2, "qgis_read_rows": [_cell(None)] * 2}
        )
        assertions = expect(Locator(client, {"object_name": "layers"}))
        if negated:
            assertions = assertions.not_
        with pytest.raises(AssertionError, match="the cell has no check box"):
            assertions.to_be_checked(cell=0, timeout_s=1.0)

    def test_a_missing_widget_is_not_read_and_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ticks = iter([0.0, 0.0, 10.0])
        monkeypatch.setattr("pytest_qgis_puppeteer._expect.time.monotonic", lambda: next(ticks))
        client = _FakeClient({"qgis_check_actionability": [_act_result(exists=False)] * 2})
        with pytest.raises(AssertionError, match="widget_not_found"):
            expect(Locator(client, {"object_name": "layers"})).not_.to_be_checked(
                cell=0, timeout_s=1.0
            )


# ============================================================
# to_have_count（#35）
# ============================================================


def _count_result(n: int) -> dict[str, Any]:
    if n == 0:
        return _act_result(exists=False)
    if n == 1:
        return _act_result()
    return {
        **_ambiguous_result(),
        "diagnostics": {"reason": "selector_ambiguous", "match_count": n},
    }


class TestToHaveCount:
    def test_waits_until_the_count_and_does_not_abort_on_several(self) -> None:
        client = _FakeClient(
            {"qgis_check_actionability": [_count_result(3), _count_result(1), _count_result(0)]}
        )
        loc = Locator(client, {"scope": "any", "class": "QDialog", "index": 1})
        expect(loc, poll_interval_s=0.0).to_have_count(0, timeout_s=1.0)
        sent = client.calls[0][1]["selector"]
        assert "index" not in sent  # index は数えるときは使わない
        assert sent["attr"] == {"visible": True}  # 見えているものだけ

    def test_times_out_with_the_last_count(self) -> None:
        client = _FakeClient({"qgis_check_actionability": [_count_result(2)] * 50})
        loc = Locator(client, {"class": "QDialog"})
        with pytest.raises(AssertionError, match="last_count.*2"):
            expect(loc, poll_interval_s=0.01).to_have_count(1, timeout_s=0.05)

    def test_negated(self) -> None:
        client = _FakeClient({"qgis_check_actionability": [_count_result(2)]})
        loc = Locator(client, {"class": "QDialog"})
        expect(loc, poll_interval_s=0.0).not_.to_have_count(0, timeout_s=1.0)


class TestToHaveRowCount:
    """#36"""

    def test_waits_for_the_row_count_of_the_snapshot(self) -> None:
        def snap(n: int) -> dict[str, Any]:
            table = {"class": "QTableView", "object_name": "t", "row_count": n}
            return {"active_modal": {"class": "QDialog", "children": [table]}}

        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result()] * 3,
                "qgis_snapshot_ui": [snap(100), snap(2500)],
            }
        )
        loc = Locator(client, {"object_name": "t"})
        expect(loc, poll_interval_s=0.0).to_have_row_count(2500, timeout_s=1.0)


class TestLocatorReadRows:
    """#36: 見えるまで待ち（無効でもよい）、client の read_rows に渡す。"""

    def test_waits_until_visible_then_reads(self) -> None:
        class Client(_FakeClient):
            def read_rows(self, selector: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
                self.calls.append(("read_rows", {"selector": selector, **kwargs}, None))
                return {"rows": [["a"]], "row_count": 1, "start": 0}

        disabled = _act_result(enabled=False)
        client = Client({"qgis_check_actionability": [_act_result(exists=False), disabled]})
        loc = Locator(client, {"object_name": "t"}, poll_interval_s=0.0)

        assert loc.read_rows(role="user")["rows"] == [["a"]]
        name, params, _ = client.calls[-1]
        assert name == "read_rows"
        assert params["role"] == "user"
        assert params["selector"] == {"object_name": "t"}


class TestLiveOnlySelectorKeys:
    """#38: window / class_inherits は snapshot に無いので、snapshot で探す確認は断る"""

    _SELECTOR = {"scope": "any", "window": True, "class_inherits": "QDialog"}

    def test_to_have_count_sends_them_to_the_live_lookup(self) -> None:
        client = _FakeClient({"qgis_check_actionability": [_count_result(0)]})
        expect(Locator(client, dict(self._SELECTOR)), poll_interval_s=0.0).to_have_count(0)
        sent = client.calls[0][1]["selector"]
        assert sent["window"] is True
        assert sent["class_inherits"] == "QDialog"

    def test_a_value_check_raises_at_once_instead_of_timing_out(self) -> None:
        client = _FakeClient(
            {
                "qgis_check_actionability": [_act_result()],
                "qgis_snapshot_ui": [{"active_modal": None}],
            }
        )
        loc = Locator(client, {"scope": "any", "class_inherits": "QgsAttributeDialog"})
        with pytest.raises(LiveOnlySelectorError, match="class_inherits"):
            expect(loc, poll_interval_s=0.0).to_have_text("x", timeout_s=5.0)

    def test_get_value_raises(self) -> None:
        client = _FakeClient({"qgis_snapshot_ui": [{"active_modal": None}]})
        outer = Locator(client, {"scope": "any", "class_inherits": "QgsAttributeDialog"})
        with pytest.raises(LiveOnlySelectorError):
            outer.locator({"object_name": "name"}).get_value()
