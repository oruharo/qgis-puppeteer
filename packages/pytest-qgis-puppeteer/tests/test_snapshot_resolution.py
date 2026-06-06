"""snapshot resolver (`_find_in_snapshot`) の回帰テスト（ADR-0002 §8.1）。

parent=mainWindow の modeless dialog が ``visible_dialogs`` と ``main_window``
subtree の両方に現れると、同一 widget が 2 件マッチして strict mode が
ambiguous 化し、``wait_for_widget`` / ``Locator.snapshot()`` が「見つからない」と
判断して timeout する回帰を、resolver の契約レベルで固定する。

Qt 非依存（snapshot は dict）なので常に実行される。Worker 側
(`qgis_tools.ui_tools.snapshot_ui`) が root を互いに素な森として出すのが契約で、
ここではその契約を前提に resolver が一意解決することと、契約が壊れた
（重複した）snapshot がなぜ ambiguous になるかの両方を記録する。
"""

from __future__ import annotations

from typing import Any

from pytest_qgis_puppeteer.automation_client import _find_in_snapshot


def _dialog_node() -> dict[str, Any]:
    """top-level dialog 1 件分の snapshot node（子に button を 1 つ持つ）。"""
    return {
        "class": "DemoDialog",
        "object_name": "demo_dialog",
        "title": "Demo",
        "visible": True,
        "children": [
            {
                "class": "QPushButton",
                "object_name": "demo_ok",
                "text": "OK",
                "visible": True,
            },
        ],
    }


def _main_window_node(children: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "class": "QgisApp",
        "object_name": "QgisApp",
        "visible": True,
        "children": children,
    }


class TestModelessDialogResolution:
    """parent=mainWindow modeless dialog の解決（二重カウント回帰）。"""

    def test_disjoint_snapshot_resolves_dialog(self) -> None:
        # Worker 修正後の形: dialog は visible_dialogs にだけ出る（main_window
        # subtree からは除外済み）。一意に解決できる。
        snap = {
            "active_modal": None,
            "visible_dialogs": [_dialog_node()],
            "active_window": None,
            "main_window": _main_window_node(
                [{"class": "QMenuBar", "object_name": "mb", "visible": True}]
            ),
        }
        found = _find_in_snapshot(snap, {"class": "DemoDialog"})
        assert found is not None
        assert found["object_name"] == "demo_dialog"

    def test_disjoint_snapshot_resolves_child_widget(self) -> None:
        # dialog 配下の widget も一意に取れる。
        snap = {
            "active_modal": None,
            "visible_dialogs": [_dialog_node()],
            "active_window": None,
            "main_window": _main_window_node([]),
        }
        found = _find_in_snapshot(snap, {"object_name": "demo_ok"})
        assert found is not None
        assert found["text"] == "OK"

    def test_duplicated_snapshot_is_ambiguous(self) -> None:
        # 回帰の核: 同じ dialog が visible_dialogs と main_window subtree の両方に
        # 出ると 2 件マッチし、strict mode（index 未指定）は ambiguous=None を返す。
        # これが「Worker が root を互いに素にしなければならない」理由。
        dup = _dialog_node()
        snap = {
            "active_modal": None,
            "visible_dialogs": [dup],
            "active_window": None,
            "main_window": _main_window_node([dup]),
        }
        assert _find_in_snapshot(snap, {"class": "DemoDialog"}) is None

    def test_duplicated_snapshot_resolvable_with_explicit_index(self) -> None:
        # ambiguous は index 未指定時のみ。index を明示すれば strict mode を抜ける。
        dup = _dialog_node()
        snap = {
            "active_modal": None,
            "visible_dialogs": [dup],
            "active_window": None,
            "main_window": _main_window_node([dup]),
        }
        found = _find_in_snapshot(snap, {"class": "DemoDialog", "index": 0})
        assert found is not None
        assert found["object_name"] == "demo_dialog"
