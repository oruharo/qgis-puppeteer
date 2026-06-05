"""E2EAutomationClient の便利メソッド単体テスト（ADR-0002 §2）。

`AutomationClient.call()` をモックに差し替えて、各便利メソッドが
- 期待した command 名
- 期待した params
- 正しい instance（kwarg）
で `call` を呼ぶことを検証する。

`wait_for_*` は `snapshot_ui` を poll するので、`call` を side_effect で
スクリプト的に振る舞わせて条件成立／タイムアウトの双方を検証する。

外部プロセス（Hub / QGIS）には一切依存しない pure-Python テスト。
pytest.ini の `-p no:qgis` / `-p no:qt` 設定下で動く想定。
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_qgis_puppeteer.automation_client import (
    ConfirmationRequiredError,
    E2EAutomationClient,
    ModalBlockedError,
    WorkerCodeError,
    _find_in_snapshot,
    _raise_for_execute_result,
)

# ============================================================
# fixtures
# ============================================================


def _make_client(
    call_results: list[Any] | None = None,
) -> tuple[E2EAutomationClient, MagicMock]:
    """connect() 済みを装った E2EAutomationClient を返す。

    内部 `_client.call` はそのまま `AsyncMock` に置換し、`_loop` は
    `run_until_complete` がコルーチンを同期実行する MagicMock に差し替える。
    こうすると `call(...)` -> `loop.run_until_complete(client.call(...))` の
    呼び出しチェーンが、テスト側で受け取れる側に流れる。
    """
    e2e = E2EAutomationClient(url="ws://test")

    inner = MagicMock(name="AutomationClient")
    if call_results is None:
        inner.call = AsyncMock(return_value={"ok": True})
    else:
        inner.call = AsyncMock(side_effect=call_results)
    inner.list_instances = AsyncMock(return_value=[])
    inner.close = AsyncMock(return_value=None)

    loop = MagicMock(name="EventLoop")

    def _run_until_complete(coro: Any) -> Any:
        # AsyncMock を await した結果を同期的に取り出す。
        # `coro.send(None)` は AsyncMock のコルーチンに対して `StopIteration`
        # を上げ、その value に AsyncMock の return_value（or side_effect）が
        # 乗ってくる。pytest 側で例外を期待するテストでは `StopIteration` の
        # value 経由ではなく side_effect の例外がそのまま送出される。
        try:
            coro.send(None)
        except StopIteration as e:
            return e.value
        # 通常の AsyncMock は 1 step で終わるので、ここに来るのは設計エラー
        raise RuntimeError("AsyncMock coroutine did not complete in one step")

    loop.run_until_complete = MagicMock(side_effect=_run_until_complete)

    # private 属性に直接ねじ込む（テスト用の許容差し替え）
    e2e._client = inner
    e2e._loop = loop
    return e2e, inner


# ============================================================
# use_instance（sticky な default instance）
# ============================================================


class TestUseInstance:
    """``use_instance`` で設定した default が ``call(...)`` に伝播するか。"""

    def test_default_is_none(self) -> None:
        e2e = E2EAutomationClient(url="ws://test")
        assert e2e.get_default_instance() is None

    def test_use_instance_sets_default(self) -> None:
        e2e = E2EAutomationClient(url="ws://test")
        e2e.use_instance("worker-A")
        assert e2e.get_default_instance() == "worker-A"

    def test_use_instance_clears_default(self) -> None:
        e2e = E2EAutomationClient(url="ws://test")
        e2e.use_instance("worker-A")
        e2e.use_instance(None)
        assert e2e.get_default_instance() is None

    def test_default_instance_is_used_when_no_explicit_instance(self) -> None:
        """``use_instance`` 後、``call`` に instance を渡さなければ sticky が効く。"""
        e2e, inner = _make_client()
        e2e.use_instance("worker-A")
        e2e.execute_python("x = 1")
        inner.call.assert_awaited_once_with(
            "qgis_execute_python", {"code": "x = 1"}, instance="worker-A"
        )

    def test_explicit_instance_overrides_sticky(self) -> None:
        """per-call ``instance=`` が sticky default より優先される。"""
        e2e, inner = _make_client()
        e2e.use_instance("worker-A")
        e2e.execute_python("x = 1", instance="worker-B")
        inner.call.assert_awaited_once_with(
            "qgis_execute_python", {"code": "x = 1"}, instance="worker-B"
        )

    def test_no_routing_when_neither_set(self) -> None:
        """sticky 未設定 + per-call 未指定 → instance=None で Hub default routing。"""
        e2e, inner = _make_client()
        e2e.execute_python("x = 1")
        inner.call.assert_awaited_once_with("qgis_execute_python", {"code": "x = 1"}, instance=None)


# ============================================================
# 各メソッドが正しい command/params で call() を呼ぶ
# ============================================================


class TestCommandWiring:
    """便利メソッド -> AutomationClient.call(command, params, instance) の写像確認。"""

    def test_execute_python(self) -> None:
        e2e, inner = _make_client()
        e2e.execute_python("print('x')", instance="i1")
        inner.call.assert_awaited_once_with(
            "qgis_execute_python", {"code": "print('x')"}, instance="i1"
        )

    def test_snapshot_ui_defaults(self) -> None:
        e2e, inner = _make_client()
        e2e.snapshot_ui()
        inner.call.assert_awaited_once_with(
            "qgis_snapshot_ui",
            {
                "max_depth": 8,
                "include_invisible": False,
                "include_main_window": False,
            },
            instance=None,
        )

    def test_snapshot_ui_overrides(self) -> None:
        e2e, inner = _make_client()
        e2e.snapshot_ui(
            max_depth=3,
            include_invisible=True,
            include_main_window=True,
            instance="main",
        )
        inner.call.assert_awaited_once_with(
            "qgis_snapshot_ui",
            {
                "max_depth": 3,
                "include_invisible": True,
                "include_main_window": True,
            },
            instance="main",
        )

    def test_click_widget(self) -> None:
        e2e, inner = _make_client()
        sel = {"class": "QPushButton", "text": "OK"}
        e2e.click_widget(sel)
        inner.call.assert_awaited_once_with("qgis_click_widget", {"selector": sel}, instance=None)

    def test_set_widget_value(self) -> None:
        e2e, inner = _make_client()
        sel = {"object_name": "lineEditFoo"}
        e2e.set_widget_value(sel, "hello", instance="x")
        inner.call.assert_awaited_once_with(
            "qgis_set_widget_value",
            {"selector": sel, "value": "hello"},
            instance="x",
        )

    def test_screenshot_no_args(self) -> None:
        e2e, inner = _make_client()
        e2e.screenshot()
        inner.call.assert_awaited_once_with("qgis_screenshot", {}, instance=None)

    def test_screenshot_full_args(self) -> None:
        e2e, inner = _make_client()
        e2e.screenshot(output_path="C:/tmp/x.png", width=800, height=600)
        inner.call.assert_awaited_once_with(
            "qgis_screenshot",
            {"output_path": "C:/tmp/x.png", "width": 800, "height": 600},
            instance=None,
        )

    def test_get_canvas_extent(self) -> None:
        e2e, inner = _make_client()
        e2e.get_canvas_extent()
        inner.call.assert_awaited_once_with("qgis_get_canvas_extent", {}, instance=None)

    def test_set_canvas_extent(self) -> None:
        e2e, inner = _make_client()
        e2e.set_canvas_extent(1.0, 2.0, 3.0, 4.0, instance="i")
        inner.call.assert_awaited_once_with(
            "qgis_set_canvas_extent",
            {"xmin": 1.0, "ymin": 2.0, "xmax": 3.0, "ymax": 4.0},
            instance="i",
        )

    def test_get_selected_features_default_limit(self) -> None:
        e2e, inner = _make_client()
        e2e.get_selected_features("layerA")
        inner.call.assert_awaited_once_with(
            "qgis_get_selected_features",
            {"layer_name": "layerA", "limit": 100},
            instance=None,
        )

    def test_get_selected_features_explicit_limit(self) -> None:
        e2e, inner = _make_client()
        e2e.get_selected_features("layerA", limit=10)
        inner.call.assert_awaited_once_with(
            "qgis_get_selected_features",
            {"layer_name": "layerA", "limit": 10},
            instance=None,
        )

    def test_select_features(self) -> None:
        e2e, inner = _make_client()
        e2e.select_features("layerA", "id = 1")
        inner.call.assert_awaited_once_with(
            "qgis_select_features",
            {"layer_name": "layerA", "expression": "id = 1"},
            instance=None,
        )

    def test_get_layer_info(self) -> None:
        e2e, inner = _make_client()
        e2e.get_layer_info("layerA")
        inner.call.assert_awaited_once_with(
            "qgis_get_layer_info", {"layer_name": "layerA"}, instance=None
        )


# ============================================================
# wait_for_modal / wait_for_modal_closed
# ============================================================


class TestWaitForModal:
    def test_returns_when_modal_appears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 1 回目: modal なし、2 回目: タイトル合致 modal が出現
        snapshots = [
            {"active_modal": None, "visible_dialogs": [], "active_window": None},
            {
                "active_modal": {
                    "class": "QDialog",
                    "object_name": "",
                    "title": "Confirm Save",
                    "visible": True,
                },
                "visible_dialogs": [],
                "active_window": None,
            },
        ]
        e2e, _ = _make_client(call_results=snapshots)
        # sleep をスキップしてテスト高速化
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        result = e2e.wait_for_modal("Confirm", timeout_s=1.0, poll_interval_s=0.01)
        assert result["title"] == "Confirm Save"

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 常に modal なし
        e2e, inner = _make_client()
        inner.call = AsyncMock(
            return_value={
                "active_modal": None,
                "visible_dialogs": [],
                "active_window": None,
            }
        )
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        with pytest.raises(TimeoutError):
            e2e.wait_for_modal("Nope", timeout_s=0.05, poll_interval_s=0.01)

    def test_title_substring_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 部分一致で hit すること
        e2e, inner = _make_client()
        inner.call = AsyncMock(
            return_value={
                "active_modal": {
                    "title": "Save Project As...",
                    "class": "QDialog",
                    "object_name": "",
                    "visible": True,
                },
                "visible_dialogs": [],
                "active_window": None,
            }
        )
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        result = e2e.wait_for_modal("Save Project", timeout_s=1.0)
        assert "Save Project" in result["title"]


class TestWaitForModalClosed:
    def test_returns_when_modal_disappears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshots = [
            {
                "active_modal": {"title": "X", "class": "QDialog", "object_name": ""},
                "visible_dialogs": [],
                "active_window": None,
            },
            {"active_modal": None, "visible_dialogs": [], "active_window": None},
        ]
        e2e, _ = _make_client(call_results=snapshots)
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        # 例外なく返ること
        e2e.wait_for_modal_closed(timeout_s=1.0, poll_interval_s=0.01)

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        e2e, inner = _make_client()
        inner.call = AsyncMock(
            return_value={
                "active_modal": {
                    "title": "stuck",
                    "class": "QDialog",
                    "object_name": "",
                },
                "visible_dialogs": [],
                "active_window": None,
            }
        )
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        with pytest.raises(TimeoutError):
            e2e.wait_for_modal_closed(timeout_s=0.05, poll_interval_s=0.01)


# ============================================================
# wait_for_widget
# ============================================================


class TestWaitForWidget:
    def _snapshot_with_button(self, *, visible: bool) -> dict[str, Any]:
        return {
            "active_modal": {
                "class": "QDialog",
                "object_name": "dlg",
                "title": "Test",
                "visible": True,
                "children": [
                    {
                        "class": "QPushButton",
                        "object_name": "btnOk",
                        "text": "OK",
                        "visible": visible,
                    }
                ],
            },
            "visible_dialogs": [],
            "active_window": None,
        }

    def test_returns_visible_widget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshots = [
            # 1 回目: button 不在
            {
                "active_modal": {
                    "class": "QDialog",
                    "object_name": "dlg",
                    "visible": True,
                    "children": [],
                },
                "visible_dialogs": [],
                "active_window": None,
            },
            # 2 回目: button 出現
            self._snapshot_with_button(visible=True),
        ]
        e2e, _ = _make_client(call_results=snapshots)
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        result = e2e.wait_for_widget(
            {"class": "QPushButton", "object_name": "btnOk"},
            timeout_s=1.0,
            poll_interval_s=0.01,
        )
        assert result["object_name"] == "btnOk"

    def test_returns_when_widget_hidden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshots = [
            self._snapshot_with_button(visible=True),
            self._snapshot_with_button(visible=False),
        ]
        e2e, _ = _make_client(call_results=snapshots)
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        result = e2e.wait_for_widget(
            {"class": "QPushButton", "object_name": "btnOk"},
            visible=False,
            timeout_s=1.0,
            poll_interval_s=0.01,
        )
        # visible=False 時は dict が返る（空 or 該当 node）
        assert isinstance(result, dict)

    def test_timeout_when_never_appears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        e2e, inner = _make_client()
        inner.call = AsyncMock(
            return_value={
                "active_modal": None,
                "visible_dialogs": [],
                "active_window": None,
            }
        )
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        with pytest.raises(TimeoutError):
            e2e.wait_for_widget(
                {"class": "QPushButton"},
                timeout_s=0.05,
                poll_interval_s=0.01,
            )


# ============================================================
# _find_in_snapshot helper
# ============================================================


class TestFindInSnapshot:
    def test_finds_in_active_modal_children(self) -> None:
        snap = {
            "active_modal": {
                "class": "QDialog",
                "object_name": "dlg",
                "children": [
                    {"class": "QLabel", "object_name": "lbl", "text": "Hi"},
                    {"class": "QPushButton", "object_name": "ok", "text": "OK"},
                ],
            },
            "visible_dialogs": [],
            "active_window": None,
        }
        node = _find_in_snapshot(snap, {"class": "QPushButton"})
        assert node is not None
        assert node["object_name"] == "ok"

    def test_returns_none_when_no_match(self) -> None:
        snap = {
            "active_modal": None,
            "visible_dialogs": [],
            "active_window": None,
        }
        assert _find_in_snapshot(snap, {"class": "QPushButton"}) is None

    def test_skips_truncated_subtree(self) -> None:
        snap = {
            "active_modal": {
                "class": "QDialog",
                "object_name": "dlg",
                "children": [{"_truncated": True, "remaining": "?"}],
            },
            "visible_dialogs": [],
            "active_window": None,
        }
        # _truncated ノードは selector keys を持たないので素のマッチでは
        # 当たらない（class None != "QPushButton"）。クラッシュしないことの確認。
        assert _find_in_snapshot(snap, {"class": "QPushButton"}) is None

    def test_searches_main_window(self) -> None:
        snap = {
            "active_modal": None,
            "visible_dialogs": [],
            "active_window": None,
            "main_window": {
                "class": "QgisApp",
                "object_name": "QgisApp",
                "children": [
                    {"class": "QToolButton", "object_name": "tb", "text": "Tool"},
                ],
            },
        }
        node = _find_in_snapshot(snap, {"object_name": "tb"})
        assert node is not None
        assert node["class"] == "QToolButton"


class TestFindInSnapshotLabelPlaceholder:
    """ADR-0002 Roadmap "getByLabel / getByPlaceholder" の snapshot 側パリティ。

    live tree 側 `_find_widget` でも同じ key を扱えるが、こちらは snapshot ノード
    に `label` / `placeholder` フィールドが populate されていることが前提
    （Worker 側 ``_describe_widget`` が ``QLabel.buddy()`` 経由で付与する）。
    """

    def test_match_by_label(self) -> None:
        # snapshot に `label` が populate されているケース（Worker が buddy 経由で
        # 付与した想定）
        snap = {
            "active_modal": {
                "class": "QDialog",
                "children": [
                    {
                        "class": "QLineEdit",
                        "object_name": "",
                        "label": "Username",
                    },
                    {
                        "class": "QLineEdit",
                        "object_name": "",
                        "label": "Password",
                    },
                ],
            },
            "visible_dialogs": [],
            "active_window": None,
        }
        node = _find_in_snapshot(snap, {"label": "Password"})
        assert node is not None
        assert node["label"] == "Password"

    def test_match_by_placeholder(self) -> None:
        snap = {
            "active_modal": {
                "class": "QDialog",
                "children": [
                    {
                        "class": "QLineEdit",
                        "object_name": "name",
                        "placeholder": "Enter your name",
                    },
                    {
                        "class": "QLineEdit",
                        "object_name": "email",
                        "placeholder": "user@example.com",
                    },
                ],
            },
            "visible_dialogs": [],
            "active_window": None,
        }
        node = _find_in_snapshot(snap, {"placeholder": "user@example.com"})
        assert node is not None
        assert node["object_name"] == "email"

    def test_label_combines_with_class(self) -> None:
        snap = {
            "active_modal": {
                "class": "QDialog",
                "children": [
                    {"class": "QLabel", "label": "Username", "text": "Username"},
                    {
                        "class": "QLineEdit",
                        "object_name": "",
                        "label": "Username",
                    },
                ],
            },
            "visible_dialogs": [],
            "active_window": None,
        }
        # AND セマンティクスで input だけ取れる
        node = _find_in_snapshot(snap, {"label": "Username", "class": "QLineEdit"})
        assert node is not None
        assert node["class"] == "QLineEdit"


class TestFindInSnapshotChain:
    """ADR-0002 Roadmap "Locator chain": ``_scope_chain`` の snapshot 側パリティ。

    `_find_in_snapshot` が selector の ``_scope_chain`` キーを順に解決し、
    最深 node の subtree に leaf を適用することを検証する。
    """

    def _nested_snapshot(self) -> dict[str, Any]:
        return {
            "active_modal": {
                "class": "QDialog",
                "object_name": "wizard",
                "children": [
                    {
                        "class": "QGroupBox",
                        "object_name": "step1",
                        "children": [
                            {
                                "class": "QLineEdit",
                                "object_name": "name",
                                "text": "alice",
                            },
                        ],
                    },
                    {
                        "class": "QGroupBox",
                        "object_name": "step2",
                        "children": [
                            {
                                "class": "QLineEdit",
                                "object_name": "name",
                                "text": "bob",
                            },
                        ],
                    },
                ],
            },
            "visible_dialogs": [],
            "active_window": None,
        }

    def test_chain_resolves_step_then_leaf(self) -> None:
        # step1 配下の name input を取る
        result = _find_in_snapshot(
            self._nested_snapshot(),
            {
                "object_name": "name",
                "_scope_chain": [{"object_name": "step1"}],
            },
        )
        assert result is not None
        assert result["text"] == "alice"

        # step2 配下の name input を取る
        result = _find_in_snapshot(
            self._nested_snapshot(),
            {
                "object_name": "name",
                "_scope_chain": [{"object_name": "step2"}],
            },
        )
        assert result is not None
        assert result["text"] == "bob"

    def test_chain_step_not_found_returns_none(self) -> None:
        result = _find_in_snapshot(
            self._nested_snapshot(),
            {
                "object_name": "name",
                "_scope_chain": [{"object_name": "nonexistent"}],
            },
        )
        assert result is None

    def test_chain_leaf_not_found_returns_none(self) -> None:
        result = _find_in_snapshot(
            self._nested_snapshot(),
            {
                "object_name": "absent_field",
                "_scope_chain": [{"object_name": "step1"}],
            },
        )
        assert result is None

    def test_deep_chain(self) -> None:
        # wizard > step1 > name を 2 段ネストで取る
        result = _find_in_snapshot(
            self._nested_snapshot(),
            {
                "object_name": "name",
                "_scope_chain": [
                    {"object_name": "wizard"},
                    {"object_name": "step1"},
                ],
            },
        )
        assert result is not None
        assert result["text"] == "alice"

    def test_chain_isolates_strict_mode(self) -> None:
        """chain 経由なら同名 widget が複数 subtree にあっても OK
        （各 subtree で 1 件ずつなので ambiguous にならない）。"""
        # snapshot には name input が 2 つあるが、step1 配下では 1 つだけ
        result = _find_in_snapshot(
            self._nested_snapshot(),
            {
                "object_name": "name",
                "_scope_chain": [{"object_name": "step1"}],
            },
        )
        assert result is not None
        assert result["text"] == "alice"


class TestFindInSnapshotStrictMode:
    """ADR-0002 §8.1 strict mode の snapshot 側パリティ。

    live tree 側 `_find_widget` と同じ semantics で `index` 未指定 + 複数マッチ
    を扱う（snapshot 側は None を返す = 「見つからなかった」扱い、live tree 側は
    diagnostics 付きで selector_ambiguous）。共通ロジックは
    `qgis_puppeteer.selector_match.resolve_with_index`。
    """

    def _multi_match_snapshot(self) -> dict[str, Any]:
        return {
            "active_modal": {
                "class": "QDialog",
                "object_name": "dlg",
                "children": [
                    {"class": "QPushButton", "object_name": "ok", "text": "OK"},
                    {
                        "class": "QPushButton",
                        "object_name": "cancel",
                        "text": "Cancel",
                    },
                    {"class": "QPushButton", "object_name": "apply", "text": "Apply"},
                ],
            },
            "visible_dialogs": [],
            "active_window": None,
        }

    def test_multi_match_without_index_returns_none(self) -> None:
        # strict mode: 複数マッチ + index 未指定 → None
        result = _find_in_snapshot(self._multi_match_snapshot(), {"class": "QPushButton"})
        assert result is None

    def test_multi_match_with_explicit_index_picks_nth(self) -> None:
        # index=1 → 2 番目の QPushButton (cancel) が返る
        result = _find_in_snapshot(
            self._multi_match_snapshot(),
            {"class": "QPushButton", "index": 1},
        )
        assert result is not None
        assert result["object_name"] == "cancel"

    def test_multi_match_with_index_zero_opts_out_of_strict(self) -> None:
        # index=0 を明示すれば旧挙動互換（先頭採用）
        result = _find_in_snapshot(
            self._multi_match_snapshot(),
            {"class": "QPushButton", "index": 0},
        )
        assert result is not None
        assert result["object_name"] == "ok"

    def test_single_match_works_without_index(self) -> None:
        # 1 件しかなければ index 未指定でも返る
        snap = {
            "active_modal": {
                "class": "QDialog",
                "children": [
                    {"class": "QPushButton", "object_name": "ok", "text": "OK"},
                    {"class": "QLabel", "text": "hello"},
                ],
            },
            "visible_dialogs": [],
            "active_window": None,
        }
        result = _find_in_snapshot(snap, {"class": "QPushButton"})
        assert result is not None
        assert result["object_name"] == "ok"

    def test_index_out_of_range_returns_none(self) -> None:
        result = _find_in_snapshot(
            self._multi_match_snapshot(),
            {"class": "QPushButton", "index": 99},
        )
        assert result is None


# ============================================================
# wait_for_project_loaded（モーダル検知付き）
# ============================================================


class TestWaitForProjectLoaded:
    """`wait_for_project_loaded` は `snapshot_ui` → `list_layers` を順に poll。

    - 非空 layers が返れば成功
    - `active_modal` が見つかれば `ModalBlockedError`
    - どちらも満たさず timeout なら `TimeoutError`
    """

    def test_returns_when_layers_appear(self) -> None:
        # 1 周目: snapshot(modal なし) → list_layers(空)、2 周目で layers 非空
        e2e, inner = _make_client(
            call_results=[
                {"active_modal": None, "main_window": {}},
                {"layers": [], "count": 0},
                {"active_modal": None, "main_window": {}},
                {"layers": [{"name": "buildings"}], "count": 1},
            ]
        )
        result = e2e.wait_for_project_loaded(timeout_s=2.0, poll_interval_s=0.01)
        assert result["count"] == 1
        # snapshot と list_layers それぞれ 2 回ずつ = 計 4 回 call
        assert inner.call.call_count == 4

    def test_raises_modal_blocked_when_modal_detected(self) -> None:
        e2e, _inner = _make_client(
            call_results=[
                {
                    "active_modal": {
                        "class": "LoginDialog",
                        "title": "ログイン",
                        "object_name": "login_dialog",
                    },
                }
            ]
        )
        with pytest.raises(ModalBlockedError) as excinfo:
            e2e.wait_for_project_loaded(timeout_s=2.0, poll_interval_s=0.01)
        assert excinfo.value.modal["title"] == "ログイン"
        assert "ログイン" in str(excinfo.value)

    def test_timeout_when_layers_stay_empty(self) -> None:
        # snapshot(modal なし) → list_layers(空) を繰り返し返す。
        # 実時間で timeout_s に達するまで poll が続く想定なので call 結果を多めに用意。
        call_results: list[Any] = [
            {"active_modal": None, "main_window": {}},
            {"layers": [], "count": 0},
        ] * 100
        e2e, _inner = _make_client(call_results=call_results)
        with pytest.raises(TimeoutError) as excinfo:
            e2e.wait_for_project_loaded(timeout_s=0.2, poll_interval_s=0.05)
        assert "Project did not finish loading" in str(excinfo.value)


# ============================================================
# B5a: action recorder の wire-up（call() が record する）
# ============================================================


class TestActionRecorderWireup:
    """``set_recorder()`` 後の ``call(...)`` で recorder が timeline を蓄積するか。"""

    def _attach(self, e2e: E2EAutomationClient) -> Any:
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        rec = ActionRecorder()
        e2e.set_recorder(rec)
        return rec

    def test_get_recorder_initially_none(self) -> None:
        e2e = E2EAutomationClient(url="ws://test")
        assert e2e.get_recorder() is None

    def test_set_and_get_recorder(self) -> None:
        e2e = E2EAutomationClient(url="ws://test")
        rec = self._attach(e2e)
        assert e2e.get_recorder() is rec

    def test_call_records_success(self) -> None:
        e2e, inner = _make_client(call_results=[{"layers": ["a"], "count": 1}])
        rec = self._attach(e2e)
        result = e2e.list_layers()
        assert result == {"layers": ["a"], "count": 1}
        actions = rec.actions()
        assert len(actions) == 1
        assert actions[0].command == "qgis_list_layers"
        assert actions[0].error is None
        assert actions[0].result == {"layers": ["a"], "count": 1}
        assert actions[0].duration_ms >= 0.0

    def test_call_records_error(self) -> None:
        e2e, inner = _make_client(call_results=[RuntimeError("boom")])
        rec = self._attach(e2e)
        with pytest.raises(RuntimeError, match="boom"):
            e2e.list_layers()
        actions = rec.actions()
        assert len(actions) == 1
        assert actions[0].error == {"type": "RuntimeError", "message": "boom"}
        # error 時は result は None
        assert actions[0].result is None

    def test_step_path_attached_to_record(self) -> None:
        e2e, _inner = _make_client(call_results=[{"ok": True}, {"ok": True}])
        rec = self._attach(e2e)
        with e2e.step("ログイン"):
            e2e.click_widget({"object_name": "ok"})
        e2e.click_widget({"object_name": "outside"})
        actions = rec.actions()
        assert actions[0].step_path == ("ログイン",)
        assert actions[1].step_path == ()

    def test_step_without_recorder_is_noop(self) -> None:
        """recorder 未 attach でも ``step()`` 自体は失敗しない（test 側の書きやすさ）。"""
        e2e, _inner = _make_client(call_results=[{"ok": True}])
        # recorder を attach しない
        with e2e.step("anything"):
            e2e.click_widget({"object_name": "x"})
        # 例外が出ないこと、recorder は None のままを確認
        assert e2e.get_recorder() is None

    def test_set_recorder_none_disables_recording(self) -> None:
        e2e, _inner = _make_client(call_results=[{"ok": True}, {"ok": True}])
        rec = self._attach(e2e)
        e2e.list_layers()
        e2e.set_recorder(None)
        e2e.list_layers()
        # 1 件だけ記録されている（detach 後の call は記録されない）
        assert len(rec.actions()) == 1

    def test_instance_recorded_from_use_instance(self) -> None:
        e2e, _inner = _make_client(call_results=[{"ok": True}])
        rec = self._attach(e2e)
        e2e.use_instance("worker-A")
        e2e.list_layers()
        assert rec.actions()[0].instance == "worker-A"

    def test_per_call_instance_overrides_default(self) -> None:
        e2e, _inner = _make_client(call_results=[{"ok": True}])
        rec = self._attach(e2e)
        e2e.use_instance("default-A")
        e2e.list_layers(instance="override-B")
        assert rec.actions()[0].instance == "override-B"


# ============================================================
# B5b: per-action screenshot capture
# ============================================================


class TestPerActionScreenshotCapture:
    """``recorder.screenshot_dir`` 設定下で各 ``call(...)`` 後に screenshot を撮り、
    ``ActionRecord.screenshot_path`` を bundle 相対 path で記録する（ADR-0002 §13.1）。

    実 screenshot ファイルの作成は Worker 側 handler の責務なので、ここでは
    ``screenshot_path`` の値と ``inner.call`` の呼び出し回数だけ検証する。
    """

    def test_screenshot_captured_per_action(self, tmp_path: Any) -> None:
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        # 2 件: list_layers の結果 + 内部 screenshot の結果
        e2e, inner = _make_client(call_results=[{"layers": [], "count": 0}, {"saved": True}])
        rec = ActionRecorder()
        rec.set_screenshot_dir(tmp_path / "_pending")
        e2e.set_recorder(rec)

        e2e.list_layers()
        actions = rec.actions()
        # 1 件だけ記録（screenshot capture は再帰防止 flag で recorder.record() しない）
        assert len(actions) == 1
        assert actions[0].command == "qgis_list_layers"
        # screenshot_path は "screenshots/0000.png" の bundle 相対
        assert actions[0].screenshot_path == "screenshots/0000.png"
        # inner.call は 2 回呼ばれた（list_layers + 内部 screenshot）
        assert inner.call.call_count == 2

    def test_no_capture_when_screenshot_dir_unset(self) -> None:
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        e2e, inner = _make_client(call_results=[{"layers": [], "count": 0}])
        rec = ActionRecorder()
        # screenshot_dir 未設定
        e2e.set_recorder(rec)
        e2e.list_layers()
        actions = rec.actions()
        assert len(actions) == 1
        assert actions[0].screenshot_path is None
        # inner.call は 1 回だけ（capture が起きないので）
        assert inner.call.call_count == 1

    def test_explicit_screenshot_call_skips_per_action_capture(self, tmp_path: Any) -> None:
        """``qgis_screenshot`` の call そのものでは per-action capture をスキップ。"""
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        e2e, inner = _make_client(call_results=[{"saved": True}])
        rec = ActionRecorder()
        rec.set_screenshot_dir(tmp_path / "_pending")
        e2e.set_recorder(rec)

        e2e.screenshot(output_path=str(tmp_path / "user.png"))
        actions = rec.actions()
        # 1 件のみ（per-action は skip）
        assert len(actions) == 1
        assert actions[0].command == "qgis_screenshot"
        # screenshot_path は None（user 自身の screenshot は per-action 対象外）
        assert actions[0].screenshot_path is None
        # inner.call も 1 回（再帰なし）
        assert inner.call.call_count == 1

    def test_capture_failure_does_not_break_call(self, tmp_path: Any) -> None:
        """screenshot 内部 call が失敗しても test 本体の結果は通常通り通る。"""
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        # 1 件目: list_layers OK / 2 件目: 内部 screenshot で例外
        e2e, _inner = _make_client(
            call_results=[{"layers": [], "count": 0}, RuntimeError("ss fail")]
        )
        rec = ActionRecorder()
        rec.set_screenshot_dir(tmp_path / "_pending")
        e2e.set_recorder(rec)

        # screenshot 失敗はマスクされる（test 本体は通る）
        result = e2e.list_layers()
        assert result == {"layers": [], "count": 0}
        actions = rec.actions()
        assert len(actions) == 1
        # capture 失敗で screenshot_path=None
        assert actions[0].screenshot_path is None

    def test_error_path_still_records_screenshot_path(self, tmp_path: Any) -> None:
        """call 自体が例外を投げた時も screenshot capture は試行され、
        record の screenshot_path に反映される（失敗時の絵も残る）。"""
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        e2e, _inner = _make_client(call_results=[RuntimeError("call fail"), {"saved": True}])
        rec = ActionRecorder()
        rec.set_screenshot_dir(tmp_path / "_pending")
        e2e.set_recorder(rec)

        with pytest.raises(RuntimeError, match="call fail"):
            e2e.list_layers()
        actions = rec.actions()
        assert len(actions) == 1
        assert actions[0].error == {"type": "RuntimeError", "message": "call fail"}
        # error path でも screenshot_path は記録される
        assert actions[0].screenshot_path == "screenshots/0000.png"

    def test_set_screenshot_dir_none_disables_mid_session(self, tmp_path: Any) -> None:
        """途中で ``set_screenshot_dir(None)`` すると以降の record は path=None。"""
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        # 1 回目: list_layers OK + screenshot OK / 2 回目: list_layers OK のみ
        e2e, _inner = _make_client(call_results=[{"ok": True}, {"saved": True}, {"ok": True}])
        rec = ActionRecorder()
        rec.set_screenshot_dir(tmp_path / "_pending")
        e2e.set_recorder(rec)

        e2e.list_layers()
        rec.set_screenshot_dir(None)
        e2e.list_layers()
        actions = rec.actions()
        assert len(actions) == 2
        assert actions[0].screenshot_path == "screenshots/0000.png"
        assert actions[1].screenshot_path is None

    def test_pending_dir_is_created(self, tmp_path: Any) -> None:
        """capture 時に pending dir が無ければ作る（plugin 側で作っているはずだが保険）。"""
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        e2e, _inner = _make_client(call_results=[{"ok": True}, {"saved": True}])
        rec = ActionRecorder()
        pending = tmp_path / "_pending" / "deeply" / "nested"
        rec.set_screenshot_dir(pending)
        e2e.set_recorder(rec)
        e2e.list_layers()
        # 親ディレクトリは作られる（screenshot 自体はファイル作成は Worker 責務）
        assert pending.exists()


# ============================================================
# execute_python の fail-fast（OSS 提案 / WorkerCodeError）
# ============================================================


class TestExecutePythonRaises:
    """``execute_python`` が Worker 側コード例外を握りつぶさず raise すること。"""

    _ERR_RESULT = {
        "success": False,
        "error": 'relation "foo" does not exist',
        "traceback": "Traceback ...\npsycopg2.errors.UndefinedTable: ...",
        "stdout": "before\n",
        "stderr": "",
        "permission_level": "whitelist",
        "risk_level": "low",
    }

    def test_raises_worker_code_error_on_failure(self) -> None:
        e2e, _inner = _make_client(call_results=[dict(self._ERR_RESULT)])
        with pytest.raises(WorkerCodeError) as ei:
            e2e.execute_python("open_dialog()")
        # 真因（DB エラー）とトレースバックが例外に載る
        assert "does not exist" in str(ei.value)
        assert ei.value.worker_traceback is not None
        assert "UndefinedTable" in ei.value.worker_traceback
        assert ei.value.stdout == "before\n"

    def test_success_returns_dict_without_raising(self) -> None:
        ok = {"success": True, "result": "0", "stdout": "", "stderr": ""}
        e2e, _inner = _make_client(call_results=[ok])
        assert e2e.execute_python("_result = 0") == ok

    def test_requires_confirmation_raises_confirmation_error(self) -> None:
        confirm = {
            "success": False,
            "requires_confirmation": True,
            "risk_level": "high",
        }
        e2e, _inner = _make_client(call_results=[confirm])
        with pytest.raises(ConfirmationRequiredError) as ei:
            e2e.execute_python("import os; os.system('rm -rf /')")
        assert ei.value.risk_level == "high"

    def test_raise_on_error_false_returns_raw_dict(self) -> None:
        e2e, _inner = _make_client(call_results=[dict(self._ERR_RESULT)])
        res = e2e.execute_python("open_dialog()", raise_on_error=False)
        assert res["success"] is False
        assert res["error"] == 'relation "foo" does not exist'

    def test_default_mock_result_does_not_raise(self) -> None:
        # 既存テスト互換：success キーが無い dict（{"ok": True}）は素通し。
        e2e, _inner = _make_client()  # 既定 return は {"ok": True}
        assert e2e.execute_python("x = 1") == {"ok": True}


class TestRaiseForExecuteResultHelper:
    """``_raise_for_execute_result`` の純粋ロジック（dict 判定）。"""

    def test_non_dict_is_noop(self) -> None:
        _raise_for_execute_result(None)
        _raise_for_execute_result("ok")
        _raise_for_execute_result(42)

    def test_success_true_is_noop(self) -> None:
        _raise_for_execute_result({"success": True})

    def test_missing_success_key_is_noop(self) -> None:
        # success キー欠落は True 扱い（後方互換）。
        _raise_for_execute_result({"ok": True})

    def test_confirmation_precedence_over_worker_error(self) -> None:
        # requires_confirmation が最優先（traceback が混在しても confirm を上げる）。
        with pytest.raises(ConfirmationRequiredError):
            _raise_for_execute_result(
                {"success": False, "requires_confirmation": True, "traceback": "x"}
            )

    def test_success_false_without_traceback_still_raises(self) -> None:
        # traceback 無しの success=False も silent にせず WorkerCodeError。
        with pytest.raises(WorkerCodeError):
            _raise_for_execute_result({"success": False, "message": "cancelled"})
