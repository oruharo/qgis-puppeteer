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
from pytest_qgis_puppeteer import expect
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
