"""ADR-0002 §11 Locator の単体テスト。

Worker / Hub / QGIS は使わず、`E2EAutomationClient` をフェイクに差し替えて
Locator の auto-wait と各操作メソッドのコマンド配線だけを検証する。

`time.sleep` は monkeypatch で no-op 化し、`time.monotonic` はステップ式に
進めて poll loop の deadline 判定だけテストする。
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_qgis_puppeteer.locator import (
    Locator,
    SelectorAmbiguousError,
    WidgetNotActionableError,
)

# ============================================================
# フェイク E2EAutomationClient
# ============================================================


class _FakeClient:
    """`E2EAutomationClient` の最小フェイク。

    `_responses` に「コマンド名 → 戻り値の list（1 呼び出しごとに pop(0)）」を
    指定しておき、テストごとに必要な振る舞いを差し込む。
    """

    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def _take(self, command: str) -> Any:
        queue = self._responses.get(command)
        if not queue:
            raise AssertionError(
                f"unexpected call: {command} (no response queued)",
            )
        return queue.pop(0)

    def check_actionability(
        self, selector: dict[str, Any], *, instance: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("qgis_check_actionability", {"selector": selector}, instance))
        return self._take("qgis_check_actionability")

    def click_widget(
        self, selector: dict[str, Any], *, instance: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("qgis_click_widget", {"selector": selector}, instance))
        return self._take("qgis_click_widget")

    def set_widget_value(
        self,
        selector: dict[str, Any],
        value: Any,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "qgis_set_widget_value",
                {"selector": selector, "value": value},
                instance,
            )
        )
        return self._take("qgis_set_widget_value")

    def snapshot_ui(
        self,
        *,
        max_depth: int = 8,
        include_invisible: bool = False,
        include_main_window: bool = False,
        instance: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "qgis_snapshot_ui",
                {
                    "max_depth": max_depth,
                    "include_invisible": include_invisible,
                    "include_main_window": include_main_window,
                },
                instance,
            )
        )
        return self._take("qgis_snapshot_ui")


# ============================================================
# 即時状態チェック（wait なし）
# ============================================================


class TestImmediateChecks:
    def test_exists_returns_true_when_widget_found(self) -> None:
        client = _FakeClient({"qgis_check_actionability": [{"exists": True, "actionable": False}]})
        loc = Locator(client, {"object_name": "btn"})
        assert loc.exists() is True

    def test_exists_returns_false_when_not_found(self) -> None:
        client = _FakeClient({"qgis_check_actionability": [{"exists": False, "actionable": False}]})
        loc = Locator(client, {"object_name": "missing"})
        assert loc.exists() is False

    def test_is_visible_reflects_response(self) -> None:
        client = _FakeClient(
            {"qgis_check_actionability": [{"exists": True, "visible": True, "actionable": True}]}
        )
        loc = Locator(client, {"object_name": "btn"})
        assert loc.is_visible() is True

    def test_is_enabled_reflects_response(self) -> None:
        client = _FakeClient(
            {"qgis_check_actionability": [{"exists": True, "enabled": False, "actionable": False}]}
        )
        loc = Locator(client, {"object_name": "btn"})
        assert loc.is_enabled() is False


# ============================================================
# auto-wait（操作系）
# ============================================================


class TestAutoWaitClick:
    def test_click_succeeds_when_immediately_actionable(self) -> None:
        """初回 check で actionable なら sleep 不要で即 click。"""
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    {
                        "exists": True,
                        "visible": True,
                        "enabled": True,
                        "not_covered": True,
                        "actionable": True,
                    }
                ],
                "qgis_click_widget": [{"success": True}],
            }
        )
        loc = Locator(client, {"object_name": "ok"})
        result = loc.click()
        assert result == {"success": True}
        commands = [c[0] for c in client.calls]
        assert commands == ["qgis_check_actionability", "qgis_click_widget"]

    def test_click_polls_until_actionable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """初回 disabled → 2 回目 actionable で click が走る。

        sleep を no-op 化しないと test が遅くなるので monkeypatch する。
        """
        sleeps: list[float] = []
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda s: sleeps.append(s))
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    {
                        "exists": True,
                        "visible": True,
                        "enabled": False,
                        "not_covered": True,
                        "actionable": False,
                    },
                    {
                        "exists": True,
                        "visible": True,
                        "enabled": True,
                        "not_covered": True,
                        "actionable": True,
                    },
                ],
                "qgis_click_widget": [{"success": True}],
            }
        )
        loc = Locator(client, {"object_name": "ok"}, default_timeout_s=5.0)
        loc.click()
        # 初回 + 2 回目で 1 sleep 入る
        assert len(sleeps) == 1
        commands = [c[0] for c in client.calls]
        assert commands == [
            "qgis_check_actionability",
            "qgis_check_actionability",
            "qgis_click_widget",
        ]

    def test_click_raises_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """timeout までずっと non-actionable なら WidgetNotActionableError。

        time.monotonic を制御して deadline を確実に超えさせる。
        """
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        # 0, 0.5, 1.0, ... と進ませる。timeout=1s なら 3 回目以降 deadline 超過
        ticks = iter([0.0, 0.5, 1.5, 2.0, 2.5])
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.monotonic", lambda: next(ticks))

        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    {
                        "exists": True,
                        "visible": True,
                        "enabled": False,
                        "not_covered": True,
                        "actionable": False,
                    }
                ]
                * 5
            }
        )
        loc = Locator(client, {"object_name": "ok"}, default_timeout_s=1.0, poll_interval_s=0.1)
        with pytest.raises(WidgetNotActionableError) as exc_info:
            loc.click()
        # 最後の check 結果を保持しているか
        assert exc_info.value.last_check["enabled"] is False
        # click_widget は呼ばれていない
        assert all(c[0] == "qgis_check_actionability" for c in client.calls)


class TestSelectorAmbiguous:
    """ADR-0002 §8.2 strict mode: 複数マッチで selector_ambiguous → 即時 abort。

    poll を続けても候補は減らないので、auto-wait が早期に
    :class:`SelectorAmbiguousError` を上げて timeout を待たないこと。
    """

    def _ambiguous_result(self) -> dict[str, Any]:
        """Worker 側 strict mode が返す形を模擬した check_actionability 結果。"""
        return {
            "exists": False,
            "visible": False,
            "enabled": False,
            "not_covered": False,
            "actionable": False,
            "widget": None,
            "diagnostics": {
                "reason": "selector_ambiguous",
                "selector": {"class": "QPushButton"},
                "match_count": 3,
                "candidates": [
                    {"class": "QPushButton", "object_name": "ok_btn", "text": "OK"},
                    {"class": "QPushButton", "object_name": "cancel_btn", "text": "Cancel"},
                    {"class": "QPushButton", "object_name": "", "text": "Apply"},
                ],
                "hint": "Add 'index' to disambiguate",
            },
        }

    def test_click_aborts_immediately_without_polling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """初回 check で ambiguous → poll loop に入らず即例外。"""
        # sleep / monotonic は呼ばれないはず（早期 abort）。呼ばれたら test fail。
        sleep_calls = []
        monkeypatch.setattr(
            "pytest_qgis_puppeteer.locator.time.sleep",
            lambda s: sleep_calls.append(s),
        )

        client = _FakeClient({"qgis_check_actionability": [self._ambiguous_result()]})
        loc = Locator(
            client,
            {"class": "QPushButton"},
            default_timeout_s=5.0,
            poll_interval_s=0.25,
        )

        with pytest.raises(SelectorAmbiguousError):
            loc.click()

        # check_actionability は 1 回だけ
        assert len(client.calls) == 1
        assert client.calls[0][0] == "qgis_check_actionability"
        # poll の sleep は走っていない
        assert sleep_calls == []
        # click_widget は呼ばれていない（auto-wait で abort したため）
        assert "qgis_click_widget" not in [c[0] for c in client.calls]

    def test_exception_carries_diagnostics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        client = _FakeClient({"qgis_check_actionability": [self._ambiguous_result()]})
        loc = Locator(client, {"class": "QPushButton"})

        with pytest.raises(SelectorAmbiguousError) as exc_info:
            loc.click()

        last = exc_info.value.last_check
        assert last["diagnostics"]["reason"] == "selector_ambiguous"
        assert last["diagnostics"]["match_count"] == 3
        # 例外メッセージに candidates のプレビューが入る
        assert "match_count" not in str(exc_info.value)  # raw key 名は出さない
        assert "ok_btn" in str(exc_info.value)

    def test_fill_also_aborts_on_ambiguous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """fill も auto-wait 経由なので同じく早期 abort されるべき。"""
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        client = _FakeClient({"qgis_check_actionability": [self._ambiguous_result()]})
        loc = Locator(client, {"class": "QLineEdit"})
        with pytest.raises(SelectorAmbiguousError):
            loc.fill("value")
        assert "qgis_set_widget_value" not in [c[0] for c in client.calls]

    def test_resolves_after_ambiguous_disappears_does_not_apply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ambiguous は「時間で解決しない」前提なので、最初に ambiguous が
        出たら poll を続けない（次の check で OK になっても気にしない）。

        この挙動を保証するため、queue に [ambiguous, ok] を入れても 2 個目は
        消費されない（abort で 1 回だけ呼ばれる）ことを確認。
        """
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        ok = {
            "exists": True,
            "visible": True,
            "enabled": True,
            "not_covered": True,
            "actionable": True,
            "widget": {"class": "QPushButton", "object_name": "ok_btn"},
            "diagnostics": {"match_count": 1},
        }
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._ambiguous_result(), ok],
                "qgis_click_widget": [{"success": True}],
            }
        )
        loc = Locator(client, {"class": "QPushButton"})
        with pytest.raises(SelectorAmbiguousError):
            loc.click()
        # 2 個目の check は消費されないので queue に残るはず
        assert len(client._responses["qgis_check_actionability"]) == 1


class TestEditableCheck:
    """ADR-0002 Roadmap "editable check": ``fill()`` 系は ``editable=True`` を要求。

    readOnly な input への ``fill()`` は auto-wait が editable 待ちで poll し、
    timeout 後に ``WidgetNotActionableError``（last_check に editable=False）を
    上げる。``click()`` 系は editable を要求しないので、readOnly でも通る。
    """

    @staticmethod
    def _result(
        *,
        actionable: bool = True,
        editable: bool = True,
    ) -> dict[str, Any]:
        return {
            "exists": True,
            "visible": True,
            "enabled": True,
            "not_covered": True,
            "editable": editable,
            "actionable": actionable,
            "widget": {"class": "QLineEdit", "object_name": "name"},
            "diagnostics": {"match_count": 1},
        }

    def test_fill_passes_when_editable_true(self) -> None:
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._result(editable=True)],
                "qgis_set_widget_value": [{"success": True, "prior_value": ""}],
            }
        )
        loc = Locator(client, {"object_name": "name"})
        result = loc.fill("hello")
        assert result["success"] is True
        # check_actionability が呼ばれた後に set_widget_value
        cmds = [c[0] for c in client.calls]
        assert cmds == ["qgis_check_actionability", "qgis_set_widget_value"]

    def test_fill_polls_until_editable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """editable=False で始まり、poll の途中で editable=True になれば通る。"""
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    self._result(editable=False),
                    self._result(editable=False),
                    self._result(editable=True),
                ],
                "qgis_set_widget_value": [{"success": True, "prior_value": ""}],
            }
        )
        loc = Locator(client, {"object_name": "name"}, default_timeout_s=10.0, poll_interval_s=0.1)
        result = loc.fill("hello")
        assert result["success"] is True
        # 3 回 poll して set_widget_value
        assert [c[0] for c in client.calls] == [
            "qgis_check_actionability",
            "qgis_check_actionability",
            "qgis_check_actionability",
            "qgis_set_widget_value",
        ]

    def test_fill_times_out_when_stays_readonly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """readOnly のまま timeout → WidgetNotActionableError、editable=False が記録される。"""
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        ticks = iter([0.0, 0.5, 1.5, 2.0, 2.5])
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.monotonic", lambda: next(ticks))
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._result(editable=False)] * 5,
            }
        )
        loc = Locator(client, {"object_name": "name"}, default_timeout_s=1.0, poll_interval_s=0.1)
        with pytest.raises(WidgetNotActionableError) as exc_info:
            loc.fill("hello")
        assert exc_info.value.last_check["editable"] is False
        assert exc_info.value.last_check["actionable"] is True
        # set_widget_value は呼ばれない
        assert "qgis_set_widget_value" not in [c[0] for c in client.calls]

    def test_click_does_not_require_editable(self) -> None:
        """click は editable を要求しない（表示専用 widget でも click できる）。"""
        client = _FakeClient(
            {
                # editable=False だが actionable=True なら click 通る
                "qgis_check_actionability": [self._result(editable=False)],
                "qgis_click_widget": [{"success": True}],
            }
        )
        loc = Locator(client, {"object_name": "label"})
        result = loc.click()
        assert result["success"] is True

    def test_select_does_not_require_editable(self) -> None:
        """select (ComboBox/Tab) は editable を要求しない（isReadOnly を持たない widget）。"""
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._result(editable=True)],
                "qgis_set_widget_value": [{"success": True, "prior_value": 0}],
            }
        )
        loc = Locator(client, {"object_name": "combo"})
        result = loc.select("option1")
        assert result["success"] is True

    def test_default_editable_true_when_field_missing(self) -> None:
        """旧 Worker (editable フィールド未送信) に対する後方互換: 欠落なら True 扱い。

        ``last.get("editable", True)`` の挙動を担保する。
        """
        # editable キーを意図的に省く
        result_no_editable = {
            "exists": True,
            "visible": True,
            "enabled": True,
            "not_covered": True,
            "actionable": True,
            "widget": {"class": "QLineEdit"},
            "diagnostics": {"match_count": 1},
        }
        client = _FakeClient(
            {
                "qgis_check_actionability": [result_no_editable],
                "qgis_set_widget_value": [{"success": True, "prior_value": ""}],
            }
        )
        loc = Locator(client, {"object_name": "name"})
        # editable 欠落でも fill が通る（旧 Worker との後方互換）
        result = loc.fill("hello")
        assert result["success"] is True


class TestAutoWaitVariants:
    """fill / select / check / uncheck のコマンド配線。"""

    def _ok_check(self) -> dict[str, Any]:
        return {
            "exists": True,
            "visible": True,
            "enabled": True,
            "not_covered": True,
            "actionable": True,
        }

    def test_fill_calls_set_widget_value(self) -> None:
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._ok_check()],
                "qgis_set_widget_value": [{"success": True}],
            }
        )
        loc = Locator(client, {"object_name": "name_input"})
        loc.fill("haruo")
        last = client.calls[-1]
        assert last[0] == "qgis_set_widget_value"
        assert last[1]["value"] == "haruo"

    def test_select_calls_set_widget_value(self) -> None:
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._ok_check()],
                "qgis_set_widget_value": [{"success": True}],
            }
        )
        loc = Locator(client, {"object_name": "option_combo"})
        loc.select("option_a")
        assert client.calls[-1][1]["value"] == "option_a"

    def test_check_sets_true(self) -> None:
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._ok_check()],
                "qgis_set_widget_value": [{"success": True}],
            }
        )
        loc = Locator(client, {"object_name": "agree"})
        loc.check()
        assert client.calls[-1][1]["value"] is True

    def test_uncheck_sets_false(self) -> None:
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._ok_check()],
                "qgis_set_widget_value": [{"success": True}],
            }
        )
        loc = Locator(client, {"object_name": "agree"})
        loc.uncheck()
        assert client.calls[-1][1]["value"] is False


# ============================================================
# snapshot ベースの値取得
# ============================================================


class TestLocatorChain:
    """ADR-0002 Roadmap "Locator chain": ``parent.locator(child)`` で構築される
    `_scope_chain` の挙動を確認する。

    Locator 自体は state を持たないので chain も不変オブジェクトとして実装。
    操作時に Worker へ送る selector に ``_scope_chain`` キーを足す。
    """

    @staticmethod
    def _ok_check() -> dict[str, Any]:
        return {
            "exists": True,
            "visible": True,
            "enabled": True,
            "not_covered": True,
            "editable": True,
            "actionable": True,
            "widget": {"class": "QPushButton", "object_name": "submit"},
            "diagnostics": {"match_count": 1},
        }

    def test_locator_method_returns_new_locator(self) -> None:
        client = _FakeClient({})
        parent = Locator(client, {"object_name": "form"})
        child = parent.locator({"object_name": "submit"})
        # 別オブジェクト
        assert child is not parent
        # parent は無傷
        assert parent._parent_chain == []
        # child の chain には parent selector が入る
        assert child._parent_chain == [{"object_name": "form"}]
        assert child._selector == {"object_name": "submit"}

    def test_chain_serialized_to_scope_chain_key(self) -> None:
        client = _FakeClient({})
        parent = Locator(client, {"object_name": "form"})
        child = parent.locator({"object_name": "submit"})

        effective = child._effective_selector()
        assert effective == {
            "object_name": "submit",
            "_scope_chain": [{"object_name": "form"}],
        }

    def test_no_chain_serializes_without_scope_chain_key(self) -> None:
        # chain が空なら _scope_chain キーは付かない（旧バージョンの Worker 互換）
        client = _FakeClient({})
        loc = Locator(client, {"object_name": "btn"})
        assert loc._effective_selector() == {"object_name": "btn"}
        assert "_scope_chain" not in loc._effective_selector()

    def test_deep_chain(self) -> None:
        # 3 段ネスト
        client = _FakeClient({})
        outer = Locator(client, {"object_name": "panel"})
        middle = outer.locator({"object_name": "form"})
        inner = middle.locator({"object_name": "submit"})

        assert inner._parent_chain == [
            {"object_name": "panel"},
            {"object_name": "form"},
        ]
        eff = inner._effective_selector()
        assert eff["object_name"] == "submit"
        assert eff["_scope_chain"] == [
            {"object_name": "panel"},
            {"object_name": "form"},
        ]

    def test_click_via_chain_sends_scope_chain_to_worker(self) -> None:
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._ok_check()],
                "qgis_click_widget": [{"success": True}],
            }
        )
        parent = Locator(client, {"object_name": "form"})
        child = parent.locator({"object_name": "submit"})
        child.click()

        # check_actionability + click_widget の両方に _scope_chain が乗る
        assert len(client.calls) == 2
        check_call = client.calls[0]
        click_call = client.calls[1]
        assert check_call[0] == "qgis_check_actionability"
        assert check_call[1]["selector"] == {
            "object_name": "submit",
            "_scope_chain": [{"object_name": "form"}],
        }
        assert click_call[0] == "qgis_click_widget"
        assert click_call[1]["selector"] == {
            "object_name": "submit",
            "_scope_chain": [{"object_name": "form"}],
        }

    def test_fill_via_chain(self) -> None:
        client = _FakeClient(
            {
                "qgis_check_actionability": [self._ok_check()],
                "qgis_set_widget_value": [{"success": True, "prior_value": ""}],
            }
        )
        form = Locator(client, {"object_name": "form"})
        username = form.locator({"object_name": "username"})
        username.fill("alice")

        set_call = client.calls[1]
        assert set_call[1]["selector"] == {
            "object_name": "username",
            "_scope_chain": [{"object_name": "form"}],
        }
        assert set_call[1]["value"] == "alice"

    def test_snapshot_via_chain_passes_scope_chain(self) -> None:
        # snapshot() 経由でも _find_in_snapshot に chain 込み selector が渡る
        snap = {
            "active_modal": {
                "class": "QDialog",
                "object_name": "form",
                "children": [
                    {
                        "class": "QLineEdit",
                        "object_name": "username",
                        "text": "alice",
                    },
                ],
            },
            "visible_dialogs": [],
            "active_window": None,
        }
        client = _FakeClient({"qgis_snapshot_ui": [snap]})
        # form の中の username を chain 経由で取る
        form = Locator(client, {"object_name": "form"})
        username = form.locator({"object_name": "username"})
        node = username.snapshot()

        assert node is not None
        assert node["object_name"] == "username"
        assert node["text"] == "alice"

    def test_chain_does_not_mutate_parent(self) -> None:
        # 親 Locator から複数の子を作っても親は無傷
        client = _FakeClient({})
        parent = Locator(client, {"object_name": "form"})
        child1 = parent.locator({"object_name": "username"})
        child2 = parent.locator({"object_name": "password"})

        # parent の chain は空のまま、子は同じ parent_chain を持つ
        assert parent._parent_chain == []
        assert child1._parent_chain == [{"object_name": "form"}]
        assert child2._parent_chain == [{"object_name": "form"}]
        # 子同士の selector は独立
        assert child1._selector == {"object_name": "username"}
        assert child2._selector == {"object_name": "password"}


class TestSnapshotAccessors:
    def _snap_with_node(self, node: dict[str, Any]) -> dict[str, Any]:
        # active_modal 配下に対象 node を 1 つ置いた snapshot
        return {
            "active_modal": {
                "class": "QDialog",
                "object_name": "main",
                "visible": True,
                "children": [node],
            },
            "visible_dialogs": [],
            "active_window": None,
        }

    def test_get_text_reads_text_field(self) -> None:
        snap = self._snap_with_node(
            {
                "class": "QPushButton",
                "object_name": "ok",
                "text": "OK",
                "visible": True,
            }
        )
        client = _FakeClient({"qgis_snapshot_ui": [snap]})
        loc = Locator(client, {"object_name": "ok"})
        assert loc.get_text() == "OK"

    def test_get_text_returns_none_when_not_found(self) -> None:
        snap = self._snap_with_node(
            {"class": "QPushButton", "object_name": "other", "visible": True}
        )
        client = _FakeClient({"qgis_snapshot_ui": [snap]})
        loc = Locator(client, {"object_name": "missing"})
        assert loc.get_text() is None

    def test_get_value_prefers_value_over_text(self) -> None:
        """SpinBox は `value` フィールドを優先して返す。"""
        snap = self._snap_with_node(
            {
                "class": "QSpinBox",
                "object_name": "count",
                "value": 42,
                "text": "42",
                "visible": True,
            }
        )
        client = _FakeClient({"qgis_snapshot_ui": [snap]})
        loc = Locator(client, {"object_name": "count"})
        assert loc.get_value() == 42

    def test_get_value_returns_combo_text(self) -> None:
        snap = self._snap_with_node(
            {
                "class": "QComboBox",
                "object_name": "option_combo",
                "current_text": "option_a",
                "current_index": 1,
                "visible": True,
            }
        )
        client = _FakeClient({"qgis_snapshot_ui": [snap]})
        loc = Locator(client, {"object_name": "option_combo"})
        assert loc.get_value() == "option_a"

    def test_get_value_returns_checked_for_checkbox(self) -> None:
        snap = self._snap_with_node(
            {
                "class": "QCheckBox",
                "object_name": "agree",
                "checked": True,
                "text": "同意する",
                "visible": True,
            }
        )
        client = _FakeClient({"qgis_snapshot_ui": [snap]})
        loc = Locator(client, {"object_name": "agree"})
        # value < current_text < checked の優先順なので checked が返る
        assert loc.get_value() is True

    def test_snapshot_returns_node_or_none(self) -> None:
        snap = self._snap_with_node(
            {"class": "QLabel", "object_name": "title", "text": "Hi", "visible": True}
        )
        client = _FakeClient({"qgis_snapshot_ui": [snap, snap]})
        loc_hit = Locator(client, {"object_name": "title"})
        node = loc_hit.snapshot()
        assert node is not None
        assert node["text"] == "Hi"

        loc_miss = Locator(client, {"object_name": "nope"})
        assert loc_miss.snapshot() is None


# ============================================================
# ADR-0002 §8.3: stable check (geometry stability)
# ============================================================


class TestStableCheck:
    """``stable=True`` で auto-wait に geometry stability check が入る。"""

    def test_stable_flag_propagates_via_chain(self) -> None:
        """``locator()`` chain で stable が継承される。"""
        client = _FakeClient({})
        parent = Locator(client, {"object_name": "form"}, stable=True)
        child = parent.locator({"object_name": "input"})
        # 内部 _stable は継承
        assert child._stable is True

    def test_default_is_not_stable(self) -> None:
        client = _FakeClient({})
        loc = Locator(client, {"object_name": "x"})
        assert loc._stable is False

    def test_stable_requires_two_consecutive_same_geometries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stable=True のとき、最初の actionable=True 1 回目では未確定で
        2 回目に同じ geometry を見て初めて click に進む。"""
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        # 1 回目: actionable=True, geometry={x:0,y:0,w:100,h:30} → 未確定（stable=True 初回）
        # 2 回目: 同じ geometry → stable! → click を呼ぶ
        same_geom = {"x": 0, "y": 0, "width": 100, "height": 30}
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    {"exists": True, "actionable": True, "geometry": same_geom},
                    {"exists": True, "actionable": True, "geometry": same_geom},
                ],
                "qgis_click_widget": [{"ok": True}],
            }
        )
        loc = Locator(client, {"object_name": "btn"}, stable=True)
        loc.click()
        # check_actionability が 2 回呼ばれて、その後 click が呼ばれている
        check_calls = [c for c in client.calls if c[0] == "qgis_check_actionability"]
        assert len(check_calls) == 2
        click_calls = [c for c in client.calls if c[0] == "qgis_click_widget"]
        assert len(click_calls) == 1

    def test_stable_keeps_polling_when_geometry_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """geometry が変わり続ける間は poll を続ける（アニメ中の widget）。"""
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        # 3 回 geometry が動いて 4 回目で 3 回目と同じ → stable で click
        g1 = {"x": 0, "y": 0, "width": 100, "height": 30}
        g2 = {"x": 0, "y": 5, "width": 100, "height": 30}
        g3 = {"x": 0, "y": 10, "width": 100, "height": 30}
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    {"exists": True, "actionable": True, "geometry": g1},
                    {"exists": True, "actionable": True, "geometry": g2},
                    {"exists": True, "actionable": True, "geometry": g3},
                    {"exists": True, "actionable": True, "geometry": g3},
                ],
                "qgis_click_widget": [{"ok": True}],
            }
        )
        loc = Locator(client, {"object_name": "btn"}, stable=True)
        loc.click()
        assert len([c for c in client.calls if c[0] == "qgis_check_actionability"]) == 4

    def test_non_stable_returns_immediately_on_actionable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``stable=False`` (既定) なら 1 回目 actionable=True で即 click。"""
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    {"exists": True, "actionable": True, "geometry": {"x": 0, "y": 0}},
                ],
                "qgis_click_widget": [{"ok": True}],
            }
        )
        loc = Locator(client, {"object_name": "btn"})  # stable 未指定 = False
        loc.click()
        # check_actionability は 1 回のみ
        assert len([c for c in client.calls if c[0] == "qgis_check_actionability"]) == 1

    def test_stable_resets_on_actionable_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """途中で actionable=False が来たら、それまでの stable 積み上げを reset。"""
        monkeypatch.setattr("pytest_qgis_puppeteer.locator.time.sleep", lambda _s: None)
        same_geom = {"x": 0, "y": 0, "width": 100, "height": 30}
        client = _FakeClient(
            {
                "qgis_check_actionability": [
                    {"exists": True, "actionable": True, "geometry": same_geom},
                    # actionable=False で reset → 次に同じ geometry を見ても直前 None なので未確定
                    {"exists": True, "actionable": False, "geometry": same_geom},
                    {"exists": True, "actionable": True, "geometry": same_geom},
                    {"exists": True, "actionable": True, "geometry": same_geom},
                ],
                "qgis_click_widget": [{"ok": True}],
            }
        )
        loc = Locator(client, {"object_name": "btn"}, stable=True)
        loc.click()
        # 4 回 check_actionability を経て 4 回目で stable 達成
        assert len([c for c in client.calls if c[0] == "qgis_check_actionability"]) == 4
