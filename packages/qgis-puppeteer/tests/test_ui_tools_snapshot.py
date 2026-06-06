"""snapshot_ui / _collect_candidates の二重カウント回帰テスト（ADR-0002 §8.1）。

parent=mainWindow で show() した modeless dialog は
  - visible_dialogs（top-level QDialog なので ``topLevelWidgets`` に出る）
  - main_window subtree（QObject 親子で ``main.children()`` に出る）
の両方に現れる。selector resolver の strict mode が「同じ widget の二重カウント」を
ambiguous と誤判定し、``wait_for_widget`` が timeout する回帰を防ぐ。

実 Qt widget を生成するため ``qgis.PyQt``（QGIS バンドルの Qt shim）が import
できる環境でのみ実行し、無い環境（最小 CI）では全件 skip する。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest

# QApplication 生成前に offscreen を指定（実画面を要求させない）。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# qgis.PyQt が無ければ（= ui_tools を import できない）全件 skip。
QtWidgets = pytest.importorskip("qgis.PyQt.QtWidgets")

from qgis_puppeteer.qgis_tools.ui_tools import (  # noqa: E402
    _collect_candidates,
    snapshot_ui,
)

QApplication = QtWidgets.QApplication
QDialog = QtWidgets.QDialog
QMainWindow = QtWidgets.QMainWindow


class DemoDialog(QDialog):
    """class 名でマッチさせるためのテスト専用 QDialog サブクラス。"""


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


def _count_nodes(node: Any, predicate: Callable[[dict], bool]) -> int:
    """snapshot node tree から predicate に合致する node を再帰的に数える。"""
    if not isinstance(node, dict):
        return 0
    total = 1 if predicate(node) else 0
    for child in node.get("children") or ():
        total += _count_nodes(child, predicate)
    return total


def _is_demo(node: dict) -> bool:
    return node.get("class") == "DemoDialog"


def test_parent_mainwindow_dialog_not_double_counted(app: Any) -> None:
    main = QMainWindow()
    main.setObjectName("QgisApp")
    dlg = DemoDialog(main)  # parent=mainWindow の modeless dialog
    dlg.setObjectName("demo_dialog")
    dlg.setWindowTitle("Demo")
    main.show()
    dlg.show()
    try:
        snap = snapshot_ui(include_main_window=True)

        # visible_dialogs に DemoDialog がちょうど 1 件。
        dialog_hits = [d for d in snap["visible_dialogs"] if _is_demo(d)]
        assert len(dialog_hits) == 1

        # main_window subtree には現れない（= 二重カウントしない）。
        assert snap.get("main_window") is not None
        assert _count_nodes(snap["main_window"], _is_demo) == 0

        # 全 root を合算しても DemoDialog はちょうど 1 件。
        total = (
            _count_nodes(snap.get("active_modal"), _is_demo)
            + sum(_count_nodes(d, _is_demo) for d in snap["visible_dialogs"])
            + _count_nodes(snap.get("main_window"), _is_demo)
        )
        assert total == 1
    finally:
        dlg.close()
        main.close()
        dlg.deleteLater()
        main.deleteLater()
        app.processEvents()


def test_nested_dialog_not_double_counted(app: Any) -> None:
    # dialog-on-dialog（inner を outer に parent 付け）でも二重カウントしない。
    outer = QDialog()
    outer.setObjectName("outer_dialog")
    inner = QDialog(outer)
    inner.setObjectName("inner_dialog")
    outer.show()
    inner.show()
    try:
        snap = snapshot_ui(include_main_window=False)

        def is_inner(n: dict) -> bool:
            return n.get("object_name") == "inner_dialog"

        outer_entries = [
            d for d in snap["visible_dialogs"] if d.get("object_name") == "outer_dialog"
        ]
        assert len(outer_entries) == 1
        # inner は outer の subtree（children）からは除外される。
        assert _count_nodes(outer_entries[0], is_inner) == 0
        # inner は独立 root として visible_dialogs にちょうど 1 件だけ出る。
        inner_total = sum(_count_nodes(d, is_inner) for d in snap["visible_dialogs"])
        assert inner_total == 1
    finally:
        inner.close()
        outer.close()
        inner.deleteLater()
        outer.deleteLater()
        app.processEvents()


def test_collect_candidates_dedupes_overlapping_roots(app: Any) -> None:
    # scope="any" 相当: main と、その子である top-level dialog が両方 root に入る。
    # dialog は両 root の walk で 2 回拾われるが id() で 1 件に dedup される。
    main = QMainWindow()
    main.setObjectName("QgisApp")
    dlg = DemoDialog(main)
    dlg.setObjectName("demo_dialog")
    main.show()
    dlg.show()
    try:
        cands = _collect_candidates([main, dlg], {"class": "DemoDialog"})
        assert len(cands) == 1
        assert cands[0] is dlg
    finally:
        dlg.close()
        main.close()
        dlg.deleteLater()
        main.deleteLater()
        app.processEvents()
