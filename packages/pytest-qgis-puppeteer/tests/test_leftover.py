"""テストのあとに残った状態を見つける（#28）。

比べ方（``_leftover``）は純粋な関数として、pytest の流れ（call の直前に読み、
後片付けのあとにもう一度読んで、teardown のエラーか警告にする）は偽のクライアントで
pytester を回して確かめる。実 QGIS での確認は user-guide / CHANGELOG を参照。
"""

from __future__ import annotations

import textwrap
from typing import Any

import pytest
from pytest_qgis_puppeteer import _leftover

pytest_plugins = ["pytester"]


class _Config:
    def __init__(self, **ini: list[str]) -> None:
        self.ini = ini

    def getini(self, name: str) -> Any:
        return self.ini.get(name, [])


class TestConfiguredItems:
    def test_items_are_read_from_both_lists(self) -> None:
        config = _Config(
            qgis_leftover_fail=["dialogs", "editing_modified"],
            qgis_leftover_warn=["editing,map_tool", "dialogs"],
        )
        fail, warn = _leftover.configured_items(config)  # type: ignore[arg-type]
        assert fail == ["dialogs", "editing_modified"]
        assert warn == ["editing", "map_tool"]  # 両方にあれば fail が勝つ

    def test_nothing_by_default(self) -> None:
        assert _leftover.configured_items(_Config()) == ([], [])  # type: ignore[arg-type]

    def test_an_unknown_item_is_a_usage_error(self) -> None:
        with pytest.raises(pytest.UsageError, match="unknown item 'dialog'"):
            _leftover.configured_items(_Config(qgis_leftover_fail=["dialog"]))  # type: ignore[arg-type]


def _win(title: str, *, modal: bool = False, cls: str = "QDialog") -> dict[str, Any]:
    return {"class": cls, "object_name": "", "title": title, "modal": modal}


def _layer(name: str, *, modified: bool = False, undo: int = 0) -> dict[str, Any]:
    return {"id": f"id-{name}", "name": name, "modified": modified, "undo": undo}


class TestLeftovers:
    def test_dialogs_left_open_but_not_those_open_from_the_start(self) -> None:
        before = [_win("Panel")]
        after = [_win("Panel"), _win("Save?", modal=True), _win("Search")]
        assert _leftover.leftovers("dialogs", before, after) == [
            "modal left open: QDialog 'Save?'",
            "dialog left open: QDialog 'Search'",
        ]
        assert _leftover.leftovers("dialogs", after, before) == []  # 閉じたものは出ない

    def test_popup(self) -> None:
        menu = {"class": "QMenu", "title": "Layer"}
        assert _leftover.leftovers("popup", None, menu) == ["popup left open: QMenu 'Layer'"]
        assert _leftover.leftovers("popup", None, None) == []

    def test_editing_is_split_by_whether_there_are_changes(self) -> None:
        after = [_layer("roads", modified=True), _layer("rivers"), _layer("pts", undo=2)]
        assert _leftover.leftovers("editing_modified", [], after) == [
            "layer 'roads' left with unsaved changes (modified=True, undo=0)",
            "layer 'pts' left with unsaved changes (modified=False, undo=2)",
        ]
        assert _leftover.leftovers("editing", [], after) == [
            "layer 'rivers' left in editing mode (no changes)"
        ]

    def test_editing_from_the_start_is_not_reported_unless_it_changed(self) -> None:
        before = [_layer("roads")]
        assert _leftover.leftovers("editing", before, [_layer("roads")]) == []
        changed = [_layer("roads", modified=True)]
        assert _leftover.leftovers("editing_modified", before, changed) == [
            "layer 'roads' left with unsaved changes (modified=True, undo=0)"
        ]

    def test_map_tool_and_active_layer(self) -> None:
        assert _leftover.leftovers("map_tool", "QgsMapToolPan", "QgsMapToolPan") == []
        assert _leftover.leftovers("map_tool", "QgsMapToolPan", "QgsMapToolIdentify") == [
            "map tool changed: 'QgsMapToolPan' -> 'QgsMapToolIdentify'"
        ]
        roads, rivers = {"id": "1", "name": "roads"}, {"id": "2", "name": "rivers"}
        assert _leftover.leftovers("active_layer", roads, rivers) == [
            "active layer: 'roads' -> 'rivers'"
        ]
        assert _leftover.leftovers("active_layer", roads, None) == ["active layer: 'roads' -> none"]

    def test_the_same_count_of_other_features_is_reported(self) -> None:
        before = {"1": {"name": "roads", "count": 3, "ids": "aaa"}}
        after = {"1": {"name": "roads", "count": 3, "ids": "bbb"}}
        assert _leftover.leftovers("selection", before, after) == [
            "selection on 'roads': 3 features, but different ones"
        ]
        assert _leftover.leftovers("selection", before, before) == []

    def test_selection_by_layer_name(self) -> None:
        before = {"1": {"name": "roads", "count": 2}}
        after = {"1": {"name": "roads", "count": 5}, "2": {"name": "pts", "count": 1}}
        assert _leftover.leftovers("selection", before, after) == [
            "selection on 'roads': 2 -> 5 features",
            "selection on 'pts': 0 -> 1 features",
        ]
        assert _leftover.leftovers("selection", after, {}) == [
            "selection on 'roads': 5 -> 0 features",
            "selection on 'pts': 1 -> 0 features",
        ]

    def test_layer_tree_says_what_changed_from_what(self) -> None:
        before = {
            "nodes": {"layer:1": {"label": "建物", "checked": True}},
            "legend": {"1/0": {"label": "建物 › 滅失済", "checked": True}},
        }
        after = {
            "nodes": {"layer:1": {"label": "建物", "checked": False}},
            "legend": {"1/0": {"label": "建物 › 滅失済", "checked": False}},
        }
        assert _leftover.leftovers("layer_tree", before, after) == [
            "建物: checked True -> False",
            "建物 › 滅失済: checked True -> False",
        ]
        assert _leftover.leftovers("layer_tree", before, before) == []

    def test_layers_and_groups_added_or_removed(self) -> None:
        before = {"layers": {"1": "roads"}, "groups": ["base"]}
        after = {"layers": {"1": "roads", "2": "scratch"}, "groups": ["base", "work"]}
        assert _leftover.leftovers("layers", before, after) == [
            "layer added and left: 'scratch'",
            "group added and left: 'work'",
        ]
        assert _leftover.leftovers("layers", after, before) == [
            "layer removed: 'scratch'",
            "group removed: 'work'",
        ]

    def test_project_dirty_only_when_it_became_dirty(self) -> None:
        assert _leftover.leftovers("project_dirty", False, True) == [
            "project left with unsaved changes"
        ]
        assert _leftover.leftovers("project_dirty", True, True) == []


# ============================================================
# pytest の流れ（偽のクライアント）
# ============================================================

_CONFTEST = textwrap.dedent(
    """
    import contextlib
    import copy

    import pytest

    STATE = {
        "dialogs": [],
        "editing": [],
        "editing_modified": [],
        "map_tool": "QgsMapToolPan",
        "selection": {},
        "layer_tree": {"nodes": {}, "legend": {}},
        "layers": {"layers": {}, "groups": []},
        "active_layer": None,
        "popup": None,
        "project_dirty": False,
    }
    CALLS = []
    BROKEN = []  # 中身があれば、状態を読む呼び出しが失敗する


    def win(title, modal=False, cls="QDialog"):
        return {"class": cls, "object_name": "", "title": title, "modal": modal}


    class FakeClient:
        def unrecorded(self):
            return contextlib.nullcontext()

        def call(self, command, params, **_):
            CALLS.append(command)
            assert command == "qgis_get_leftover_state"
            if BROKEN:
                raise RuntimeError("invalid_command: qgis_get_leftover_state")
            return {"success": True, "state": {i: copy.deepcopy(STATE[i]) for i in params["items"]}}


    @pytest.fixture(scope="session")
    def automation_client():
        return FakeClient()


    @pytest.fixture
    def search_window():
        STATE["dialogs"].append(win("Search"))
        yield
        STATE["dialogs"].pop()
    """
)


def _run(pytester: pytest.Pytester, ini: str, tests: str) -> Any:
    pytester.makeconftest(_CONFTEST)
    pytester.makeini("[pytest]\nqgis_fail_on_uncaught_exception = false\n" + ini)
    pytester.makepyfile(textwrap.dedent(tests))
    return pytester.runpytest("-p", "no:cacheprovider", "-rA")


def test_a_leftover_in_a_fail_item_errors_the_test_at_teardown(pytester: pytest.Pytester) -> None:
    result = _run(
        pytester,
        "qgis_leftover_fail = dialogs\n",
        """
        from conftest import STATE, win

        def test_leaves_a_dialog(automation_client):
            STATE["dialogs"].append(win("Delete?", modal=True, cls="QMessageBox"))

        def test_cleans_up(automation_client):
            STATE["dialogs"].clear()
        """,
    )
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*test_leaves_a_dialog left QGIS in a different state than it found it:*",
            "*[[]dialogs]*",
            "*modal left open: QMessageBox 'Delete?'*",
        ]
    )


def test_what_a_fixture_makes_and_cleans_up_is_not_reported(pytester: pytest.Pytester) -> None:
    result = _run(
        pytester,
        "qgis_leftover_fail = dialogs\n",
        """
        def test_uses_the_window(automation_client, search_window):
            pass
        """,
    )
    result.assert_outcomes(passed=1)


def test_a_warn_item_only_warns(pytester: pytest.Pytester) -> None:
    result = _run(
        pytester,
        "qgis_leftover_fail = editing_modified\nqgis_leftover_warn = editing map_tool\n",
        """
        from conftest import STATE

        def test_leaves_a_layer_editing(automation_client):
            STATE["editing"].append({"id": "1", "name": "roads", "modified": False, "undo": 0})
            STATE["editing_modified"] = STATE["editing"]
            STATE["map_tool"] = "QgsMapToolIdentify"
        """,
    )
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "*QgisLeftoverWarning*",
            "*layer 'roads' left in editing mode (no changes)*",
            "*map tool changed: 'QgsMapToolPan' -> 'QgsMapToolIdentify'*",
        ]
    )


def test_a_leftover_failure_asks_for_a_restart(pytester: pytest.Pytester) -> None:
    """qgis_auto_fresh_after_failure = session なら、teardown のエラーでも起こし直す。"""
    result = _run(
        pytester,
        "qgis_leftover_fail = dialogs\nqgis_auto_fresh_after_failure = session\n",
        """
        from conftest import STATE, win

        def test_leaves_a_dialog(automation_client):
            STATE["dialogs"].append(win("X"))

        def test_next(request):
            assert getattr(request.config, "_qgis_puppeteer_restart_session_qgis", False)
        """,
    )
    result.assert_outcomes(passed=2, errors=1)


def test_tests_without_the_client_or_without_items_are_not_checked(
    pytester: pytest.Pytester,
) -> None:
    result = _run(
        pytester,
        "",
        """
        from conftest import CALLS, STATE, win

        def test_no_items(automation_client):
            STATE["dialogs"].append(win("X"))

        def test_no_calls_were_made():
            assert CALLS == []
        """,
    )
    result.assert_outcomes(passed=2)


def test_a_test_that_failed_gets_a_warning_not_a_second_error(pytester: pytest.Pytester) -> None:
    """call で落ちたテストに、残った状態の teardown エラーを重ねない（警告にする）。"""
    result = _run(
        pytester,
        "qgis_leftover_fail = dialogs\n",
        """
        from conftest import STATE, win

        def test_fails_and_leaves_a_dialog(automation_client):
            STATE["dialogs"].append(win("Delete?", modal=True))
            assert False, "the test itself failed"
        """,
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*QgisLeftoverWarning*", "*modal left open*Delete?*"])


def test_an_unreadable_state_warns_once(pytester: pytest.Pytester) -> None:
    """読めないのに黙って緑にしない。ただし毎テストは出さない。"""
    result = _run(
        pytester,
        "qgis_leftover_fail = dialogs\n",
        """
        from conftest import BROKEN

        BROKEN.append(True)

        def test_a(automation_client):
            pass

        def test_b(automation_client):
            pass
        """,
    )
    result.assert_outcomes(passed=2, warnings=1)  # 2 本目はログだけ
    assert "leftover checks are not running" in result.stdout.str()


def test_a_teardown_failure_without_qgis_does_not_ask_for_a_restart(
    pytester: pytest.Pytester,
) -> None:
    """QGIS を使っていないテストの後片付けの失敗では、QGIS を起こし直さない。"""
    result = _run(
        pytester,
        "qgis_auto_fresh_after_failure = session\n",
        """
        import pytest

        @pytest.fixture
        def broken_cleanup():
            yield
            raise RuntimeError("tmp dir is busy")

        def test_unrelated(broken_cleanup):
            pass

        def test_next(request):
            assert not getattr(request.config, "_qgis_puppeteer_restart_session_qgis", False)
        """,
    )
    result.assert_outcomes(passed=2, errors=1)


def test_an_unknown_item_stops_the_run(pytester: pytest.Pytester) -> None:
    result = _run(pytester, "qgis_leftover_warn = dialog\n", "def test_x():\n    pass\n")
    result.stderr.fnmatch_lines(["*qgis_leftover_warn: unknown item 'dialog'*"])
    assert result.ret != 0
