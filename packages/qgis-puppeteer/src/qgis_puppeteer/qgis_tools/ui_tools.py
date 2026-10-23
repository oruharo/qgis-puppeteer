"""QGIS UI 検査 / 操作ツール群。

Qt ウィジェットツリー（active modal / 可視ダイアログ / メインウィンドウ）の
構造化スナップショットと、セレクタ指定でのターゲット操作（click / set value）
を提供する。生の ``execute_python`` を使わずにエージェントが QGIS UI を
観察・操作できるようにすることが目的。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from typing import Any

from qgis.PyQt.QtCore import QEvent, QItemSelectionModel, QPersistentModelIndex, QPointF, Qt
from qgis.PyQt.QtGui import QContextMenuEvent, QKeyEvent, QMouseEvent
from qgis.PyQt.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDoubleSpinBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableView,
    QTabWidget,
    QTextEdit,
    QTimeEdit,
    QToolButton,
    QTreeView,
    QWidget,
)

from qgis_puppeteer.qgis_tools import queued
from qgis_puppeteer.selector_match import (
    label_forms,
    resolve_with_index,
)

# ADR-0002 §8.4 入れ子 modal: scope="modal_stack[N]" を parse する。N は負も可。
_MODAL_STACK_RE = re.compile(r"^modal_stack\[(-?\d+)\]$")

# Cap recursion to keep the payload bounded.
_DEFAULT_MAX_DEPTH = 8
_DEFAULT_MAX_CHILDREN = 200
# リスト・表の中身の上限（#12）。属性テーブルのように大きな表で応答が膨らまないように
_MAX_ITEM_ROWS = 100
_MAX_ITEM_COLUMNS = 50


def snapshot_ui(
    max_depth: int = _DEFAULT_MAX_DEPTH,
    include_invisible: bool = False,
    include_main_window: bool = False,
) -> dict:
    """Return a structured snapshot of the current UI state.

    Args:
        max_depth: Maximum recursion depth when describing widget trees.
        include_invisible: If True, include widgets where ``isVisible()`` is False.
        include_main_window: If True, include the QGIS main window tree
            (can be very large; off by default).

    Returns:
        Dictionary with:
            - active_modal: Description of ``QApplication.activeModalWidget()``.
            - active_popup: Description of ``QApplication.activePopupWidget()``
              (a ``QMenu`` shown with ``exec()`` / ``popup()``, a combo box's
              drop-down, ...). A ``QMenu`` also carries ``menu_items``: one node
              per visible action (``text`` / ``enabled`` / ``checkable`` /
              ``checked`` / ``separator`` / ``submenu``), selectable with
              ``scope="popup"`` (#22).
            - visible_dialogs: Other visible top-level QDialog instances.
            - active_window: The currently focused top-level widget class/title.
            - main_window: QGIS main window tree (only if include_main_window).

    The returned roots form a **disjoint forest**: a widget reported as its own
    root (``active_modal`` / a ``visible_dialogs`` entry / ``main_window``) is
    excluded from every other root's subtree. A modeless ``QDialog`` shown with
    ``parent=mainWindow`` is a top-level window *and* a ``QObject`` child of the
    main window, so without this it would appear in both ``visible_dialogs`` and
    the ``main_window`` subtree — the duplicate then trips the selector resolver's
    strict mode (``selector_ambiguous``) and ``wait_for_widget`` times out
    (ADR-0002 §8.1). Consequence: such a dialog is reported under
    ``visible_dialogs`` only, *not* under ``main_window`` (the live-tree
    ``_find_widget`` with ``root_object_name="QgisApp"`` still reaches it via
    ``findChildren``, which crosses window boundaries — see that selector's note).
    """
    app = QApplication.instance()
    result: dict[str, Any] = {
        "active_modal": None,
        "active_popup": None,
        "visible_dialogs": [],
        "active_window": None,
    }

    if app is None:
        return result

    active_modal = app.activeModalWidget()
    active_popup = app.activePopupWidget()

    # snapshot を「互いに素な root の森」にするため、個別 root として報告する
    # live widget を先に確定する。各 root の subtree からは「他の root」を除外し、
    # 同一 widget が 2 か所に出る二重カウントを防ぐ。
    #
    # 典型例: parent=mainWindow で show() した modeless dialog は
    #   - visible_dialogs（top-level QDialog なので topLevelWidgets に出る）
    #   - main_window subtree（QObject 親子で main の children に出る）
    # の両方に現れる。selector resolver の strict mode は「同じ widget の二重
    # カウント」を ambiguous と誤判定するため、wait_for_widget / Locator.snapshot()
    # が「見つからない」と判断して timeout する（ADR-0002 §8.1 の回帰）。
    # dialog-on-dialog（dialog を別 dialog に parent 付け）でも同じ重複が起きる
    # ので、active_modal / visible_dialogs / main_window すべてを互いに除外する。
    dialog_widgets = [
        top
        for top in app.topLevelWidgets()
        if top is not active_modal
        and isinstance(top, QDialog)
        and (include_invisible or top.isVisible())
    ]
    main_widget: QWidget | None = None
    if include_main_window:
        for top in app.topLevelWidgets():
            if top.objectName() == "QgisApp":
                main_widget = top
                break

    exclude_ids: set[int] = {id(w) for w in dialog_widgets}
    if active_modal is not None:
        exclude_ids.add(id(active_modal))
    if active_popup is not None:
        exclude_ids.add(id(active_popup))
    if main_widget is not None:
        exclude_ids.add(id(main_widget))

    if active_modal is not None:
        buddy_map = _build_buddy_label_map([active_modal])
        result["active_modal"] = _describe_widget(
            active_modal,
            max_depth,
            include_invisible,
            buddy_map=buddy_map,
            exclude_ids=exclude_ids,
        )

    if active_popup is not None:
        popup_node = _describe_widget(
            active_popup, max_depth, include_invisible, exclude_ids=exclude_ids
        )
        if isinstance(active_popup, QMenu):
            popup_node["menu_items"] = _menu_items(active_popup)
            # メニューの windowTitle はふつう空。題名（dialog handler の predicate の
            # title と同じもの）を出す（#27）
            if not popup_node.get("title") and active_popup.title():
                popup_node["title"] = active_popup.title()
        result["active_popup"] = popup_node

    active_window = app.activeWindow()
    if active_window is not None:
        result["active_window"] = {
            "class": type(active_window).__name__,
            "object_name": active_window.objectName(),
            "title": active_window.windowTitle(),
        }

    for top in dialog_widgets:
        buddy_map = _build_buddy_label_map([top])
        result["visible_dialogs"].append(
            _describe_widget(
                top,
                max_depth,
                include_invisible,
                buddy_map=buddy_map,
                exclude_ids=exclude_ids,
            )
        )

    if main_widget is not None:
        buddy_map = _build_buddy_label_map([main_widget])
        result["main_window"] = _describe_widget(
            main_widget,
            max_depth,
            include_invisible,
            buddy_map=buddy_map,
            exclude_ids=exclude_ids,
        )

    return result


def _resolve_actionable_widget(selector: dict) -> tuple[QWidget | None, dict | None]:
    """Resolve selector and apply common preconditions for click/set 系 RPC。

    `_find_widget` 失敗時のエラーコード変換（``selector_ambiguous`` /
    ``widget_not_found``）と ``isEnabled()`` チェックをまとめる。

    Returns:
        ``(widget, None)`` 成功時、または ``(None, error_dict)`` 失敗時。
        ``error_dict`` は ``{"success": False, "error": ..., ...}`` 形式で
        そのまま RPC 応答として返せる。
    """
    widget, diagnostics = _find_widget(selector)
    if widget is None:
        # Strict mode の ambiguous は別エラーコードで返す（caller / Locator が
        # 「selector を直せ」と即判断できるよう）
        error_code = (
            "selector_ambiguous"
            if diagnostics.get("reason") == "selector_ambiguous"
            else "widget_not_found"
        )
        return None, {
            "success": False,
            "error": error_code,
            "diagnostics": diagnostics,
        }

    if not widget.isEnabled():
        return None, {
            "success": False,
            "error": "widget_disabled",
            "widget": _widget_summary(widget),
        }

    return widget, None


_CLICK_KINDS = ("left", "double", "right")


def click_widget(
    selector: dict,
    iface=None,
    *,
    wait: bool = True,
    kind: str = "left",
    cell: Any = None,
) -> dict:
    """Click a widget located by selector.

    Args:
        selector: See ``_find_widget`` for selector syntax.
        iface: Unused; kept for handler signature consistency.
        wait: False なら、click を Qt のイベントループに積んですぐ返す（#21）。
            押すと ``exec()`` の確認を出すボタン用。widget の解決と「押せるか」は
            その場で確かめ、click の中で出た失敗は recent_exceptions に残る。
        kind: ``"left"``（既定）/ ``"double"`` / ``"right"``（#24）。既定の左クリックは
            ``click()`` / ``trigger()`` で押す。それ以外（と ``cell`` 付き）は、Qt の
            マウスのイベントを widget（表・ツリーなら viewport）に送る。``"right"`` は
            ``QContextMenuEvent`` も送るので、``customContextMenuRequested`` が出る。
            右クリックで開くメニューは ``exec()`` で止まるので、``wait=False`` で積み、
            ``scope="popup"`` で項目を選ぶ。
        cell: 表・ツリー・リストの行の指定（``{"row": N}`` か
            ``{"text": セルの文字, "column": 見出しの名前か番号}``、
            :func:`_resolve_row`）。そのセルの位置にクリックを送る。

    Returns:
        Dictionary with success flag and widget metadata. ``cell`` 付きなら
        ``cell: {"row", "column"}`` も。``wait=False`` では ``queued: True`` が付く。
    """
    del iface  # unused
    if kind not in _CLICK_KINDS:
        return {
            "success": False,
            "error": "invalid_argument",
            "message": f"kind must be one of {_CLICK_KINDS}, not {kind!r}",
        }
    widget, error = _resolve_actionable_widget(selector)
    if error is not None:
        return error

    result: dict[str, Any] = {"success": True, "widget": _widget_summary(widget)}
    if kind != "left" or cell is not None:
        press, error = _mouse_press(widget, kind, cell, result)
        if error is not None:
            return error
    elif isinstance(widget, QAbstractButton):
        press = widget.click
    elif isinstance(widget, QAction):
        menu = _menu_showing(widget)
        if menu is not None:
            # 開いているポップアップの項目は、メニューから選ぶ（#22）。trigger() では
            # `QMenu.exec()` がその項目を返さないので、選んだ項目で分岐する処理が進まない
            def press() -> None:
                activate_menu_action(menu, widget)

        else:
            press = widget.trigger
    elif hasattr(widget, "click") and callable(widget.click):
        press = widget.click
    else:
        return {
            "success": False,
            "error": "widget_not_clickable",
            "widget": _widget_summary(widget),
        }

    if not wait:
        queued.run_later(press, what="qgis_click_widget", target=widget)
        result["queued"] = True
        return result
    outcome = press()
    if isinstance(outcome, dict) and outcome.get("success") is False:
        return outcome  # セルに当たらなかった（_mouse_press）
    return result


def _mouse_press(
    widget: Any, kind: str, cell: Any, result: dict
) -> tuple[Callable[[], dict | None] | None, dict | None]:
    """Qt のマウスのイベントで押す処理を作る（#24）。

    ``cell`` があれば、その行のセルの中心に送る（表示する位置まで scroll してから）。
    無ければ widget の中心。表・ツリー・リストは ``viewport()`` が受け手。
    ``QAction`` は位置が無いので押せない。

    修飾キー（Ctrl / Shift の複数行の選択）は、ここに ``modifiers`` を足せば通る形。
    """
    if not isinstance(widget, QWidget):
        return None, {
            "success": False,
            "error": "widget_not_clickable",
            "widget": _widget_summary(widget),
        }
    index = None
    if cell is not None:
        if not isinstance(widget, QAbstractItemView):
            return None, {
                "success": False,
                "error": "cell_needs_a_view",
                "widget": _widget_summary(widget),
            }
        index, error = _resolve_row(widget, cell)
        if error is not None:
            return None, error
        # 隠した列（ID の列など、行の目印にだけ使う）のセルは人には押せないので、
        # 同じ行の見えている最初の列に送る
        index = _visible_cell(widget, index)
        if index is None:
            return None, {
                "success": False,
                "error": "no_visible_column",
                "widget": _widget_summary(widget),
            }
        if not index.flags() & Qt.ItemIsEnabled:
            # 無効な項目は、押しても Qt が黙って無視する
            return None, {
                "success": False,
                "error": "cell_disabled",
                "cell": {"row": index.row(), "column": index.column()},
                "widget": _widget_summary(widget),
            }
        result["cell"] = {"row": index.row(), "column": index.column()}
    target = widget.viewport() if isinstance(widget, QAbstractItemView) else widget
    # 積んでから押すまでにモデルが変わっても、同じ項目を追えるように
    persistent = QPersistentModelIndex(index) if index is not None else None

    def press() -> dict | None:
        pos = target.rect().center()
        if persistent is not None:
            if not persistent.isValid():
                return _missed(widget, "row_gone", "the row was removed before the click")
            current = widget.model().index(
                persistent.row(), persistent.column(), persistent.parent()
            )
            widget.scrollTo(current)
            # セルが viewport より広いと、中心が画面の外になる。見えている部分の中心へ
            rect = widget.visualRect(current).intersected(target.rect())
            if rect.isEmpty():
                return _missed(widget, "click_missed", "the cell is not on the screen")
            pos = rect.center()
            hit = widget.indexAt(pos)
            if hit.row() != current.row() or hit.parent() != current.parent():
                return _missed(widget, "click_missed", f"the point hits row {hit.row()}")
        _send_mouse(target, pos, kind)
        return None

    return press, None


def _missed(widget: Any, error: str, message: str) -> dict:
    return {
        "success": False,
        "error": error,
        "message": message,
        "widget": _widget_summary(widget),
    }


def _visible_cell(view: QAbstractItemView, index: Any) -> Any:
    """``index`` の列が隠れていれば、同じ行の見えている最初の列（画面の並び順）。

    隠れているかは ``_column_shown`` で見る（幅 0 の列も隠れている）。見えている列が
    1 つも無ければ None。リストは列を隠せないのでそのまま。
    """
    if not isinstance(view, (QTableView, QTreeView)):
        return index
    header = _column_header(view)
    if _column_shown(view, header, index.column()):
        return index
    model = view.model()
    for visual in range(header.count()):
        column = header.logicalIndex(visual)
        if _column_shown(view, header, column):
            return model.index(index.row(), column, index.parent())
    return None


def _column_header(view: QAbstractItemView) -> Any:
    """表・ツリーの横の見出し（列の幅と画面の並びを持つ）。"""
    return view.horizontalHeader() if isinstance(view, QTableView) else view.header()


def _column_shown(view: QAbstractItemView, header: Any, column: int) -> bool:
    """列が画面に出ているか。``setColumnHidden`` の列も、幅を 0 にした列
    （``setColumnWidth(c, 0)``。ID の列をこうして隠す表がある）も、人には押せない。"""
    return not view.isColumnHidden(column) and header.sectionSize(column) > 0


def _send_mouse(target: QWidget, pos: Any, kind: str, modifiers: Any = Qt.NoModifier) -> None:
    """``target`` の ``pos`` にクリックを送る。

    ``"double"`` は press → release → doubleClick → release（``QTest.mouseDClick`` と
    同じ並び。最初の press で行が選ばれ、doubleClick で ``doubleClicked`` が出る）。
    ``"right"`` は右ボタンの press / release のあと ``QContextMenuEvent``（``sendEvent``
    では Qt が contextMenu を合成しないため）。
    """
    button = Qt.RightButton if kind == "right" else Qt.LeftButton
    local = QPointF(pos)
    global_ = QPointF(target.mapToGlobal(pos))

    def event(kind_: Any, buttons: Any) -> QMouseEvent:
        return QMouseEvent(kind_, local, global_, button, buttons, modifiers)

    QApplication.sendEvent(target, event(QEvent.MouseButtonPress, button))
    QApplication.sendEvent(target, event(QEvent.MouseButtonRelease, Qt.NoButton))
    if kind == "double":
        QApplication.sendEvent(target, event(QEvent.MouseButtonDblClick, button))
        QApplication.sendEvent(target, event(QEvent.MouseButtonRelease, Qt.NoButton))
    elif kind == "right":
        QApplication.sendEvent(
            target,
            QContextMenuEvent(QContextMenuEvent.Mouse, pos, target.mapToGlobal(pos), modifiers),
        )


def _resolve_row(view: QAbstractItemView, spec: Any) -> tuple[Any, dict | None]:
    """表・ツリー・リストの行の指定を ``QModelIndex`` にする（#24）。

    ``spec`` は行の番号か、``{"row": N}`` / ``{"text": セルの文字, "column": 見出しの
    名前か番号}``。``column`` を省くと全列から探す。文字は画面に出ている文字との
    完全一致（``_cell_matches``）。

    - ``row`` は今、画面に並んでいる順の番号（``QTableWidget`` は並べ替え後の行、
      ``QTableView`` に代理のモデルがあれば代理のモデルの行）
    - ``setRowHidden`` で隠した行は、文字の候補から外す。``row`` で指せば
      ``row_hidden`` で断る（人には見えない行を選ばないため）
    - 複数の行に当たれば ``row_ambiguous``（当たった行の番号付き）
    - ツリーは一番上の階層だけ（snapshot と同じ）

    Returns:
        ``(index, None)`` か ``(None, error_dict)``。``index`` の列は、``column``
        指定ならその列、文字で当てたならその列、どちらでもなければ画面で最初に
        見えている列（``_first_column``）。
    """
    model = view.model()
    if model is None:
        return None, {"success": False, "error": "no_model", "widget": _widget_summary(view)}
    root = view.rootIndex()
    if _is_index(spec):
        spec = {"row": spec}
    if not isinstance(spec, dict) or len({"row", "text"} & set(spec)) != 1:
        return None, _invalid(view, spec, '{"row": N} or {"text": ..., "column": ...}')
    count = model.rowCount(root)
    column = spec.get("column")
    col: int | None = None
    if column is not None:
        col, error = _resolve_column(view, model, root, column)
        if error is not None:
            return None, error
    if "row" in spec:
        row = spec["row"]
        if not _is_index(row):
            return None, _invalid(view, row, "row number")
        if not 0 <= row < count:
            return None, _out_of_range(view, row, count)
        if _row_hidden(view, row, root):
            return None, {
                "success": False,
                "error": "row_hidden",
                "row": row,
                "widget": _widget_summary(view),
            }
        return model.index(row, col if col is not None else _first_column(view), root), None

    text = str(spec["text"])
    columns = [col] if col is not None else _columns(view, model, root)
    hits: list[tuple[int, int]] = []
    for row in range(count):
        if _row_hidden(view, row, root):
            continue
        for c in columns:
            if _cell_matches(view, model.index(row, c, root), text):
                hits.append((row, c))
                break
        if len(hits) > _MAX_AMBIGUOUS_ROWS:
            break  # 曖昧なのはもう分かった。大きな表を最後まで読まない
    if not hits:
        return None, {
            "success": False,
            "error": "row_not_found",
            "text": text,
            "column": column,
            "widget": _widget_summary(view),
        }
    if len(hits) > 1:
        return None, {
            "success": False,
            "error": "row_ambiguous",
            "text": text,
            "column": column,
            "rows": [row for row, _ in hits],
            "hint": "narrow with column, or use row",
            "widget": _widget_summary(view),
        }
    row, c = hits[0]
    return model.index(row, c, root), None


# row_ambiguous で返す行の番号の上限（それ以上は探さない）
_MAX_AMBIGUOUS_ROWS = 10


def _columns(view: QAbstractItemView, model: Any, root: Any) -> list[int]:
    """指せる列の番号（すべて。snapshot の出力の上限とは別）。

    リストは表示している 1 列（``QAbstractListModel.columnCount()`` は呼べない）。
    """
    if isinstance(view, QListView):
        return [view.modelColumn()]
    return list(range(model.columnCount(root)))


def _first_column(view: QAbstractItemView) -> int:
    """列を指さないときの列：リストは表示している列、表・ツリーは画面の並びで最初に
    見えている列（隠した列や並べ替えた列を押さないため）。"""
    if isinstance(view, QListView):
        return view.modelColumn()
    if isinstance(view, (QTableView, QTreeView)):
        header = _column_header(view)
        for visual in range(header.count()):
            column = header.logicalIndex(visual)
            if _column_shown(view, header, column):
                return column
    return 0


def _cell_matches(view: QAbstractItemView, index: Any, text: str) -> bool:
    """セルの文字が ``text`` と同じか。画面に出ている文字（delegate の表示）でも比べる。

    ``None`` は空の文字、真偽値は ``true`` / ``false``、数や日付は delegate が出す
    形（ロケール込み）でも当たる。
    """
    try:
        value = index.data(Qt.ItemDataRole.DisplayRole)
    except Exception:  # noqa: BLE001
        return False
    forms = {""} if value is None else {str(value), str(_jsonable(value))}
    if isinstance(value, bool):
        forms.add("true" if value else "false")
    try:
        delegate = view.itemDelegate(index)
        if delegate is not None and hasattr(delegate, "displayText"):
            forms.add(delegate.displayText(value, view.locale()))
    except Exception:  # noqa: BLE001 - delegate が無い・合わない型は生の値で比べる
        pass
    return text in forms


def _resolve_column(
    view: QAbstractItemView, model: Any, root: Any, column: Any
) -> tuple[int | None, dict | None]:
    """``column``（見出しの名前か番号）を列の番号にする。"""
    columns = _columns(view, model, root)
    names: list[Any] = [_display_header(model, c) for c in columns]
    if _is_index(column):
        if column in columns:
            return column, None
    else:
        same = [c for c, name in zip(columns, names, strict=True) if str(name) == str(column)]
        if len(same) == 1:
            return same[0], None
        if len(same) > 1:
            return None, {
                "success": False,
                "error": "column_ambiguous",
                "column": column,
                "columns": same,
                "hint": "use the column number",
                "widget": _widget_summary(view),
            }
    return None, {
        "success": False,
        "error": "column_not_found",
        "column": column,
        "available": names,
        "widget": _widget_summary(view),
    }


def _row_hidden(view: QAbstractItemView, row: int, root: Any) -> bool:
    try:
        if isinstance(view, QTreeView):
            return bool(view.isRowHidden(row, root))
        if isinstance(view, (QTableView, QListView)):
            return bool(view.isRowHidden(row))
    except Exception:  # noqa: BLE001 - 隠しの判定に失敗しても選べる方に倒す
        return False
    return False


def set_widget_value(
    selector: dict, value: Any, iface=None, *, wait: bool = True, verify: bool = True
) -> dict:
    """Set a value on a widget located by selector.

    Handles common input widgets: QLineEdit, QTextEdit/QPlainTextEdit,
    QComboBox, QCheckBox/QRadioButton, QSpinBox/QDoubleSpinBox, date/time
    editors, QTabWidget (selects tab), list/tree/table views (selects a row:
    by number, or ``{"text": セルの文字, "column": 見出し}``; the whole row is
    selected and ``currentChanged`` / ``selectionChanged`` fire as for a
    person, #24), and QGIS's QgsFileWidget (a path, through ``setFilePath()``;
    #25).

    入れる前に確かめられることは確かめて断る（範囲外の番号は
    ``index_out_of_range``、形の違う値は ``invalid_value``、候補に無い文字は
    ``*_not_found``）。入れたあとは値を読み直して ``value_after`` に返し、頼んだ値と
    違えば ``value_not_applied`` で失敗にする（Qt が黙って丸めた・切り詰めた・
    無視した場合、#23）。

    Args:
        selector: See ``_find_widget``.
        value: Value to set. Type interpretation depends on widget class.
        iface: Unused.
        wait: False なら、値を入れる処理を Qt のイベントループに積んですぐ返す
            （#21）。値の変更に結ばれた処理が ``exec()`` の確認を出す場合用。
            ``prior_value`` は積む前に読み、入れる前の確認の失敗はその場で返す。
            入れたあとの失敗（``value_not_applied`` など）は、戻り値ではなく
            recent_exceptions に残る。
        verify: False なら、入れたあとに頼んだ値と比べない（``value_after`` は返す）。
            値を自分で直す欄（表示用に日付を整形する ``QLineEdit`` の派生など）に
            使う。比べるかどうかをクラスでは分けない：部品ごとの一覧を持つと、
            部品の増減や版ごとの違いに追われるので、ホストの部品の知識は利用側に置く。

    Returns:
        Dictionary with success flag, prior value, value after, and widget metadata.
        For a password field (`_is_secret_widget`) ``prior_value`` and
        ``value_after`` are None and ``masked`` is True, so that callers that log
        calls can drop ``value``. ``wait=False`` では ``queued: True`` が付き、
        ``value_after`` は無い。
    """
    del iface
    widget, error = _resolve_actionable_widget(selector)
    if error is not None:
        return error
    if wait:
        return _apply_widget_value(widget, value, verify=verify)
    return _queue_widget_value(widget, value, verify=verify)


_DATE_FORMAT = "yyyy-MM-dd"
_TIME_FORMAT = "HH:mm:ss"
_DATETIME_FORMAT = "yyyy-MM-ddTHH:mm:ss"

# チェックボックスに渡せる文字（#23。bool("false") は True なので、文字は決めた
# ものだけ受ける）
_BOOL_STRINGS = {"true": True, "false": False}


def _apply_widget_value(widget: Any, value: Any, *, verify: bool = True) -> dict:
    """`set_widget_value` の本体：解決済みの widget に値を入れ、読み直して確かめる。"""
    prepared = _prepare_widget_value(widget, value)
    if "error" in prepared:
        return _masked(widget, prepared)
    return _masked(widget, _set_and_verify(widget, value, prepared, verify=verify))


def _queue_widget_value(widget: Any, value: Any, *, verify: bool = True) -> dict:
    """``wait=False`` の値の設定：入れる前の確認だけその場で行い、残りを積む。"""
    prepared = _prepare_widget_value(widget, value)
    if "error" in prepared:
        return _masked(widget, prepared)
    prior = _read_value(widget)

    def run() -> dict:
        return _masked(widget, _set_and_verify(widget, value, prepared, verify=verify))

    queued.run_later(run, what="qgis_set_widget_value", target=widget)
    return _masked(
        widget,
        {
            "success": True,
            "queued": True,
            "widget": _widget_summary(widget),
            "prior_value": prior,
        },
    )


def _set_and_verify(widget: Any, value: Any, prepared: dict, *, verify: bool) -> dict:
    """``prepared["apply"]`` で値を入れ、読み直して ``prepared["expected"]`` と比べる。"""
    prior = _read_value(widget)
    try:
        prepared["apply"]()
    except Exception as exc:  # noqa: BLE001 - Qt / 利用側の slot の失敗を返す
        return {
            "success": False,
            "error": "set_value_failed",
            "message": str(exc),
            "widget": _widget_summary(widget),
        }
    after = _read_value(widget)
    result: dict[str, Any] = {
        "success": True,
        "widget": _widget_summary(widget),
        "prior_value": prior,
        "value_after": after,
    }
    if isinstance(widget, QLineEdit) and widget.validator() is not None:
        # setText は validator を通さない。入った文字が受け付けられる形かを添える
        result["acceptable_input"] = widget.hasAcceptableInput()
    expected = prepared["expected"]
    same = prepared.get("same", lambda a, b: a == b)
    if verify and not same(after, expected):
        return {
            "success": False,
            "error": "value_not_applied",
            "requested": value,
            "expected": expected,
            "value_after": after,
            "prior_value": prior,
            "widget": _widget_summary(widget),
        }
    return result


def _masked(widget: Any, result: dict) -> dict:
    """パスワード欄の結果から、値を外に出さないように伏せる。"""
    if not _is_secret_widget(widget):
        return result
    for key in ("prior_value", "value_after", "expected"):
        if key in result:
            result[key] = None
    result.pop("requested", None)
    result["masked"] = True
    return result


def _prepare_widget_value(widget: Any, value: Any) -> dict:  # noqa: C901 - widget 種別の列挙
    """入れる前の確認。``{"apply", "expected"}`` か、失敗の dict を返す。

    ``expected`` は、入ったあとに :func:`_read_value` が返すはずの値。その場で
    確かめられる失敗（読み取り専用・範囲外の番号・候補に無い文字・形の違う値）は
    ここで返すので、``wait=False`` でも戻り値で分かる。
    """
    # readOnly な input への setValue は Qt 側で no-op になる（例: QLineEdit の
    # setText が値を変えない）が、テスト側からは「成功した」ように見えて
    # silent な失敗になる。早期に明示エラーで返して原因を見える化する。
    # `_widget_is_writable` は isReadOnly() を持たない widget には True を返す
    # ので、QComboBox / QCheckBox 等は影響を受けない。
    if not _widget_is_writable(widget):
        return {"success": False, "error": "widget_readonly", "widget": _widget_summary(widget)}

    if _is_file_widget(widget):
        return _prepare_file_path(widget, value)
    if isinstance(widget, QLineEdit):
        text = str(value)
        return {"apply": lambda: widget.setText(text), "expected": text}
    if isinstance(widget, (QPlainTextEdit, QTextEdit)):
        text = str(value)
        return {"apply": lambda: widget.setPlainText(text), "expected": text}
    if isinstance(widget, QComboBox):
        texts = [widget.itemText(i) for i in range(widget.count())]
        if _is_index(value):
            if not 0 <= value < len(texts):
                return _out_of_range(widget, value, len(texts))
            idx = value
        else:
            # findText の既定は完全一致・大文字小文字を区別
            idx = widget.findText(str(value))
            if idx < 0:
                return _not_found("combo_item_not_found", widget, value, texts)
        return {"apply": lambda: widget.setCurrentIndex(idx), "expected": texts[idx]}
    if isinstance(widget, (QCheckBox, QRadioButton)):
        checked = _as_bool(value)
        if checked is None:
            return _invalid(widget, value, "true / false")
        return {"apply": lambda: widget.setChecked(checked), "expected": checked}
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        integer = isinstance(widget, QSpinBox)
        number = _as_number(value, integer=integer)
        if number is None:
            return _invalid(widget, value, "int" if integer else "float")
        return {"apply": lambda: widget.setValue(number), "expected": number}
    if isinstance(widget, QDateTimeEdit):
        return _prepare_date_time(widget, value)
    if isinstance(widget, QTabWidget):
        texts = [widget.tabText(i) for i in range(widget.count())]
        if _is_index(value):
            if not 0 <= value < len(texts):
                return _out_of_range(widget, value, len(texts))
            idx = value
        elif str(value) in texts:
            idx = texts.index(str(value))
        else:
            return _not_found("tab_not_found", widget, value, texts)
        return {"apply": lambda: widget.setCurrentIndex(idx), "expected": idx}
    if isinstance(widget, QListWidget) and isinstance(value, str):
        # 整数の 0 ではなく MatchFlag を渡す（PyQt は 0 を型で弾く、#23）
        items = [i for i in widget.findItems(value, Qt.MatchExactly) if not i.isHidden()]
        if not items:
            count = widget.count()
            texts = [widget.item(i).text() for i in range(min(count, _MAX_ITEM_ROWS))]
            return _not_found("list_item_not_found", widget, value, texts)
        if len(items) > 1:
            return {
                "success": False,
                "error": "row_ambiguous",
                "text": value,
                "rows": sorted(widget.row(i) for i in items),
                "hint": "use the row number",
                "widget": _widget_summary(widget),
            }
        row = widget.row(items[0])
        return {"apply": lambda: widget.setCurrentRow(row), "expected": row}
    if isinstance(widget, (QListView, QTreeView, QTableView)):
        index, error = _resolve_row(widget, value)
        if error is not None:
            return error
        row = index.row()
        # 行全体を選ぶ（QTableView.selectRow 相当）。setCurrentIndex だけだと
        # selectionBehavior によってはセル 1 つの選択になり、行の選択で動くホストの
        # 処理が進まない（#24）
        flags = QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows

        def apply() -> None:
            widget.selectionModel().setCurrentIndex(index, flags)

        return {"apply": apply, "expected": row}
    return {
        "success": False,
        "error": "unsupported_widget_type",
        "widget": _widget_summary(widget),
    }


def _prepare_date_time(widget: Any, value: Any) -> dict:
    """日付・時刻の欄。実際のクラスで形を決める（#23）。

    ``QDateEdit`` / ``QTimeEdit`` は ``QDateTimeEdit`` の下位クラスなので、先に
    見る（これまでは ``QDateTimeEdit`` の分岐に入り、日時の形で読んで失敗していた）。
    形の違う文字は、入れる前に ``invalid_value`` で断る（無効な値を入れると Qt は
    黙って無視する）。
    """
    from qgis.PyQt.QtCore import QDate, QDateTime, QTime

    text = str(value)
    if isinstance(widget, QDateEdit):
        date = QDate.fromString(text, _DATE_FORMAT)
        if not date.isValid():
            return _invalid(widget, value, _DATE_FORMAT)
        return {"apply": lambda: widget.setDate(date), "expected": date.toString(_DATE_FORMAT)}
    if isinstance(widget, QTimeEdit):
        time = QTime.fromString(text, _TIME_FORMAT)
        if not time.isValid():
            return _invalid(widget, value, _TIME_FORMAT)
        return {"apply": lambda: widget.setTime(time), "expected": time.toString(_TIME_FORMAT)}
    moment = QDateTime.fromString(text, _DATETIME_FORMAT)
    if not moment.isValid():
        return _invalid(widget, value, _DATETIME_FORMAT)
    return {
        "apply": lambda: widget.setDateTime(moment),
        "expected": moment.toString(_DATETIME_FORMAT),
    }


_file_widget_class: Any = None  # 未調査は None、無ければ False、あればクラス


def _is_file_widget(widget: Any) -> bool:
    """QGIS のファイル・フォルダ選択の部品 ``QgsFileWidget`` か（#25）。

    ``qgis.gui`` は QGIS の中でしか無いので、ここで初めて import する（Qt だけの
    環境では常に False）。
    """
    global _file_widget_class
    if _file_widget_class is None:
        try:
            from qgis.gui import QgsFileWidget

            _file_widget_class = QgsFileWidget
        except ImportError:
            _file_widget_class = False
    return bool(_file_widget_class) and isinstance(widget, _file_widget_class)


# QgsFileWidget.StorageMode の番号 → 名前（enum の .name が PyQt5 では無いことがある）
_STORAGE_MODES = {0: "GetFile", 1: "GetDirectory", 2: "GetMultipleFiles", 3: "SaveFile"}


def _file_widget_read_only(widget: Any) -> bool:
    """``QgsFileWidget`` が読み取り専用か。

    ``isReadOnly()`` を持たないので ``_widget_is_writable`` の一般の判定では見えない。
    ``setReadOnly()`` は（3.34 では）中の入力欄とボタンを無効にするだけで、部品自身は
    有効なまま。そこで中の入力欄（``lineEdit()`` は公開 API）が無効か読み取り専用かで
    見る。``readOnly()`` の getter がある版ならそれを使う。
    """
    try:
        getter = getattr(widget, "readOnly", None)
        if callable(getter):
            return bool(getter())
        line = widget.lineEdit()
        return not line.isEnabled() or bool(line.isReadOnly())
    except Exception:  # noqa: BLE001 - 判定できなければ入れてみる
        return False


def _storage_mode(widget: Any) -> str:
    try:
        mode = int(widget.storageMode())
    except Exception:  # noqa: BLE001 - 補助情報なので落とさない
        return "?"
    return _STORAGE_MODES.get(mode, str(mode))


def _prepare_file_path(widget: Any, value: Any) -> dict:
    """``QgsFileWidget`` にパスを入れる（#25）。

    ``setFilePath()`` で入れ、読み直しは ``filePath()``。どちらも QGIS の公開 API
    なので、中の入力欄の作りには依存しない。まだ無いパスも入れる（保存先や出力先の
    フォルダはこれから作る場所を指す）。複数ファイル（``GetMultipleFiles``）の
    list は受けない：まずは 1 つのパスだけ。
    """
    if _file_widget_read_only(widget):
        return {"success": False, "error": "widget_readonly", "widget": _widget_summary(widget)}
    if not isinstance(value, str):
        return _invalid(widget, value, "a single path (str)")
    path = value

    def same(after: Any, expected: Any) -> bool:
        # 区切り（\ と /）や末尾の区切りなど、同じ場所を指す表記の違いは失敗にしない
        if after == expected:
            return True
        if not isinstance(after, str):
            return False
        return os.path.normcase(os.path.normpath(after)) == os.path.normcase(
            os.path.normpath(expected)
        )

    return {"apply": lambda: widget.setFilePath(path), "expected": path, "same": same}


def _read_value(widget: Any) -> Any:
    """今の値を読む（``prior_value`` / ``value_after``）。対象外の widget は None。

    日付・時刻は、``set_widget_value`` に渡す形と同じ文字で返す。
    """
    if _is_file_widget(widget):
        return widget.filePath()
    if isinstance(widget, QLineEdit):
        return widget.text()
    if isinstance(widget, (QPlainTextEdit, QTextEdit)):
        return widget.toPlainText()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, (QCheckBox, QRadioButton)):
        return widget.isChecked()
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        return widget.value()
    if isinstance(widget, QDateEdit):
        return widget.date().toString(_DATE_FORMAT)
    if isinstance(widget, QTimeEdit):
        return widget.time().toString(_TIME_FORMAT)
    if isinstance(widget, QDateTimeEdit):
        return widget.dateTime().toString(_DATETIME_FORMAT)
    if isinstance(widget, QTabWidget):
        return widget.currentIndex()
    if isinstance(widget, QListWidget):
        return widget.currentRow()
    if isinstance(widget, (QListView, QTreeView, QTableView)):
        return widget.currentIndex().row()
    return None


def _is_index(value: Any) -> bool:
    """番号として扱う値か（``True`` / ``False`` は番号にしない）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _as_bool(value: Any) -> bool | None:
    """チェックの値。真偽値か ``"true"`` / ``"false"``（大文字小文字は問わない）だけ。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _BOOL_STRINGS.get(value.strip().lower())
    return None


def _as_number(value: Any, *, integer: bool) -> int | float | None:
    """数の欄の値。数か数の文字だけ受ける（真偽値は受けない）。

    整数の欄に整数でない数を渡すと、``int()`` で黙って切り捨てて、読み直しても
    一致してしまうので断る。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    if integer:
        if isinstance(value, float) and not value.is_integer():
            return None
        return int(value)
    return float(value)


def _out_of_range(widget: Any, value: int, count: int) -> dict:
    return {
        "success": False,
        "error": "index_out_of_range",
        "value": value,
        "count": count,
        "widget": _widget_summary(widget),
    }


def _not_found(error: str, widget: Any, value: Any, available: list[str]) -> dict:
    return {
        "success": False,
        "error": error,
        "value": value,
        "available": available,
        "widget": _widget_summary(widget),
    }


def _invalid(widget: Any, value: Any, expected_form: str) -> dict:
    return {
        "success": False,
        "error": "invalid_value",
        "value": value,
        "expected_form": expected_form,
        "widget": _widget_summary(widget),
    }


def _menu_showing(action: Any) -> QMenu | None:
    """``action`` を項目に持つ、いま開いているポップアップのメニュー（無ければ None）。"""
    app = QApplication.instance()
    popup = app.activePopupWidget() if app is not None else None
    if isinstance(popup, QMenu) and any(a is action for a in popup.actions()):
        return popup
    return None


def activate_menu_action(menu: QMenu, action: QAction) -> None:
    """開いているメニューで ``action`` を選ぶ（人が選んだのと同じ形で、#22）。

    ``action.trigger()`` では、メニューを ``exec()`` で出した側に、選ばれた項目が
    返らない。項目をメニューの今の項目にして Return を送ると、``exec()`` がその
    項目を返し、``triggered`` も出る。下位メニューを持つ項目なら、それが開く。
    """
    menu.setActiveAction(action)
    QApplication.sendEvent(menu, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))


def _menu_items(menu: QMenu) -> list[dict]:
    """snapshot 用：メニューの見えている項目を、selector で選べる node にする。"""
    items: list[dict] = []
    for action in menu.actions():
        if not action.isVisible():
            continue
        if len(items) >= _DEFAULT_MAX_CHILDREN:
            items.append({"_truncated": True, "remaining": "?"})
            break
        if action.isSeparator():
            items.append({"class": "QAction", "separator": True})
            continue
        node: dict[str, Any] = {
            "class": "QAction",
            "object_name": action.objectName(),
            "text": action.text(),
            "enabled": action.isEnabled(),
            # 文言を `&` や `(&N)` 抜きでも当てる印（実ツリー側と同じ比べ方）
            "menu_item": True,
        }
        if action.isCheckable():
            node["checkable"] = True
            node["checked"] = action.isChecked()
        if action.menu() is not None:
            node["submenu"] = True
        items.append(node)
    return items


def check_actionability(selector: dict, iface=None) -> dict:
    """Evaluate all actionability checks for a selector in a single round-trip.

    ADR-0002 §8.3 / §11.2 で要求される auto-wait 用の判定をまとめる。Client が
    各項目を個別 RTT で poll するのを避け、Worker 側で 1 コマンドに集約する。

    Args:
        selector: See ``_find_widget``.
        iface: Unused; kept for handler signature consistency.

    Returns:
        Dictionary:
            - exists: 候補が見つかったか。
            - visible: ``widget.isVisible()``（exists=False のときは False）。
            - enabled: ``widget.isEnabled()``（同上）。
            - not_covered: 上にモーダルが被っていないか
              （active_modal が無いか、見つかった widget が active_modal の
              子孫であれば True）。
            - editable: ``widget.isReadOnly()`` が False（または `isReadOnly`
              を持たない widget）。`fill()` / `select()` 系操作の前に確認する用途。
              `click()` 系は editable を要求しないので `actionable` には含めない。
            - actionable: ``visible`` / ``enabled`` / ``not_covered`` のすべてが
              True なら True。`editable` は含めない（用途別に Locator 側で組み合わせる）。
            - widget: 見つかった widget の summary（exists=True のとき）。
            - diagnostics: ``_find_widget`` の diagnostics dict。
    """
    del iface  # unused
    widget, diagnostics = _find_widget(selector)

    if widget is None:
        return {
            "exists": False,
            "visible": False,
            "enabled": False,
            "not_covered": False,
            "editable": False,
            "actionable": False,
            "geometry": None,
            "widget": None,
            "diagnostics": diagnostics,
        }

    # QAction は QWidget でないので isVisible/isEnabled が無いケースがある。
    # 共通のフォールバックとして getattr で吸収する。
    try:
        visible = bool(widget.isVisible()) if hasattr(widget, "isVisible") else True
    except Exception:  # noqa: BLE001 - Qt 側で例外は想定外だが防御的に
        visible = False
    try:
        enabled = bool(widget.isEnabled()) if hasattr(widget, "isEnabled") else True
    except Exception:  # noqa: BLE001
        enabled = False

    not_covered = _is_not_covered_by_modal(widget)
    editable = _widget_is_writable(widget)

    # `editable` は actionable に含めない: click 系は readOnly でも操作したい
    # （例: 表示専用の状態を click で確認する）。fill 系は Locator 側で
    # `editable` を別途確認する。
    actionable = visible and enabled and not_covered

    # ADR-0002 §8.3 stable check 用に widget の global geometry を返す。
    # アニメ中のフェードイン / スライド等を Client 側で 2 回比較で吸収する。
    # QAction には geometry が無いので None を入れる。
    geometry = None
    try:
        if hasattr(widget, "geometry"):
            rect = widget.geometry()
            # widget.geometry() は parent 座標系。stable 判定にはサイズ変化と
            # 親に対する移動を観測すれば十分（global 座標までは要らない）。
            geometry = {
                "x": int(rect.x()),
                "y": int(rect.y()),
                "width": int(rect.width()),
                "height": int(rect.height()),
            }
    except Exception:  # noqa: BLE001 - Qt 側の予期せぬ失敗で stable 判定を止めない
        geometry = None

    return {
        "exists": True,
        "visible": visible,
        "enabled": enabled,
        "not_covered": not_covered,
        "editable": editable,
        "actionable": actionable,
        "geometry": geometry,
        "widget": _widget_summary(widget),
        "diagnostics": diagnostics,
    }


def _widget_is_writable(widget: Any) -> bool:
    """`fill()` 系操作で値を書き込めるかを判定（ADR-0002 Roadmap "editable"）。

    ``isReadOnly()`` を持つ widget（QLineEdit / QTextEdit / QPlainTextEdit /
    QSpinBox / QDoubleSpinBox / QDateTimeEdit 等）は ``not isReadOnly()`` を
    返す。``isReadOnly`` を持たない widget（QComboBox / QCheckBox / QPushButton
    / QListWidget など）は「読み取り専用」の概念がない（または ``setCurrentXxx``
    系で操作する経路）ので ``True`` を返す。

    QAction も該当しないので True。

    True は「fill() 経路で OK」、False は「readOnly なので setText/setValue で
    弾かれる」を意味する。
    """
    if hasattr(widget, "isReadOnly"):
        try:
            return not bool(widget.isReadOnly())
        except Exception:  # noqa: BLE001
            return True
    if _is_file_widget(widget):
        return not _file_widget_read_only(widget)
    return True


def _list_modal_stack() -> list[QWidget]:
    """ADR-0002 §8.4: 現在の modal stack を「外側 → 内側」で best-effort 並べる。

    Qt API では modal の open 順を直接取れないので、parent chain depth で推定する
    （深いほど後から開かれた = より「内側」）。同じ depth なら順序は不定。
    結果の最後の要素が最前面（``activeModalWidget()`` と同じ）になる想定。

    使い道は ``scope="modal_stack[N]"``。N=0 で外側、N=-1 で最前面。
    入れ子 modal（QFileDialog の中で警告ダイアログが出る等）の外側に届きたい
    ケースを救う。
    """
    app = QApplication.instance()
    if app is None:
        return []
    modals: list[QWidget] = []
    for w in app.topLevelWidgets():
        try:
            if w.isVisible() and hasattr(w, "isModal") and w.isModal():
                modals.append(w)
        except Exception:  # noqa: BLE001 - visibility / modality check は防御的
            continue

    def _depth(widget: QWidget) -> int:
        d = 0
        try:
            p = widget.parent()
            while p is not None:
                d += 1
                p = p.parent()
        except Exception:  # noqa: BLE001
            return d
        return d

    return sorted(modals, key=_depth)


def _is_not_covered_by_modal(widget: Any) -> bool:
    """active modal が存在する場合、widget がその子孫であれば「覆われていない」。

    QAction は QWidget ツリーに乗らないので、ホストする QMenu/QToolBar の
    上位を辿る。`_find_widget` はそれらを root として返さないため、保守的に
    「QAction は modal 配下にない限り常に not_covered=True」を返す（誤検知より
    検出漏れを優先；操作実行時に Qt が disabled として弾く想定）。
    """
    app = QApplication.instance()
    if app is None:
        return True
    modal = app.activeModalWidget()
    if modal is None:
        return True

    # QAction の場合は parent() を辿って QWidget を探す。
    if isinstance(widget, QAction):
        host = widget.parent()
        if not isinstance(host, QWidget):
            return True
        widget_for_check: QWidget = host
    elif isinstance(widget, QWidget):
        widget_for_check = widget
    else:
        return True

    if widget_for_check is modal:
        return True
    parent = widget_for_check.parentWidget()
    while parent is not None:
        if parent is modal:
            return True
        parent = parent.parentWidget()
    return False


def _build_buddy_label_map(roots: list[QWidget]) -> dict[int, str]:
    """Walk roots and build ``{id(widget): label_text}`` for ``QLabel.buddy()`` relations.

    ``QLabel`` の ``buddy()`` は「このラベルがどの widget を指しているか」を返す
    Qt 標準の関連付け。`_find_widget` の ``label`` selector で使う:
    「buddy が 'Username' のラベルに紐付いている widget を取得する」。

    `id(widget)` を key にするのは QWidget が hashable でも equality がオブジェクト
    identity 依存のため。findChildren で重複した buddy が出るケースは後勝ちでよい
    （同じ widget に複数 label が付くのは稀）。
    """
    mapping: dict[int, str] = {}
    for root in roots:
        # QLabel 自体が root のケース、root 配下にあるケース両方を扱う
        candidates_iter: Iterator[QLabel]
        candidates_iter = iter([root]) if isinstance(root, QLabel) else iter(())
        for label in list(candidates_iter) + list(root.findChildren(QLabel)):
            try:
                buddy = label.buddy()
            except Exception:  # noqa: BLE001
                continue
            if buddy is not None:
                mapping[id(buddy)] = label.text()
    return mapping


def _find_widget(selector: dict) -> tuple[QWidget | None, dict]:
    """Resolve a widget by selector.

    Selector keys (all optional; combined with AND semantics):
        object_name: Qt ``objectName()``.
        text: Button text / label text / action text.
        class: Python class name (e.g. "QPushButton").
        title: Window title (for dialog selection).
        label: Match widgets associated with a ``QLabel`` whose ``text()`` equals
               this value via ``QLabel.buddy()`` (Playwright ``getByLabel`` 相当).
               ラベル文字列で対応 input を引きたい用途で objectName 未付与の
               widget を救う。
        placeholder: Match widgets whose ``placeholderText()`` equals this value
               (Playwright ``getByPlaceholder`` 相当). LineEdit / TextEdit 等で有効。
        index: 0-based index among matches. **If the selector matches multiple
               widgets and ``index`` is not given, ``selector_ambiguous`` is
               reported (strict mode).** Set ``index`` explicitly to opt out
               of strict mode and pick the N-th match.
        scope: "modal" (default) — search only the active modal;
               "active_window" — search within ``activeWindow()``;
               "popup" — search the open popup (``activePopupWidget()``, e.g. a
               ``QMenu`` shown with ``exec()``). A menu's items are ``QAction``s
               matched by ``text`` (also without ``&`` and a trailing ``(&N)``)
               or ``object_name``; clicking one chooses it in the menu (#22);
               "any" — search all top-level widgets.
        root_object_name: If set, constrain search under the top-level
               widget whose objectName equals this value (e.g. "QgisApp").
               If no top-level widget has that name, a visible widget with that
               objectName inside a window is used as the root (a dock or panel
               in the main window, #20); more than one is ``selector_ambiguous``.
               Note: this walks ``findChildren`` from that root, which follows
               the ``QObject`` parent-child tree across window boundaries, so a
               modeless dialog parented to the root is reachable here. The
               ``snapshot_ui`` path differs — it reports such a dialog as its own
               root rather than nested under ``main_window`` (see ``snapshot_ui``
               "disjoint forest" note).
        _scope_chain: 内部用。`Locator.locator(child)` でネスト時に
               外側 → 内側の selector list を渡す。各要素を順に解決して
               最深 widget を root に leaf selector を適用する
               （Playwright Locator chain 相当、ADR-0002 Roadmap）。

    Strict mode (ADR-0002 Roadmap):
        Multiple matches without an explicit ``index`` returns
        ``(None, {"reason": "selector_ambiguous", ...})``. This catches
        objectName collisions early instead of silently picking the first
        match. To preserve the legacy "first match" behaviour, set
        ``index=0`` explicitly.

    Returns:
        Tuple of (widget or None, diagnostics dict).
    """
    app = QApplication.instance()
    if app is None:
        return None, {"reason": "no_qapplication"}

    # ---- _scope_chain 解決（Locator chain）----
    # 内部は Pure な再帰で済む: 各 step を `_find_widget` で解決し、結果
    # widget を leaf selector の `_explicit_roots` として渡す。chain は連続
    # 適用するので深さ N でも O(N) 回の Worker 内呼び出しで済む。
    chain = selector.get("_scope_chain") or []
    if chain:
        parent_widget: QWidget | None = None
        for step_index, step in enumerate(chain):
            if parent_widget is None:
                step_widget, step_diag = _find_widget(step)
            else:
                step_widget, step_diag = _find_widget_within(parent_widget, step)
            if step_widget is None:
                return None, {
                    "reason": "chain_step_not_found",
                    "step_index": step_index,
                    "step_selector": step,
                    "step_diagnostics": step_diag,
                }
            parent_widget = step_widget
        # leaf 部分（chain を除いた残り）を innermost parent 内で解決
        leaf = {k: v for k, v in selector.items() if k != "_scope_chain"}
        assert parent_widget is not None
        return _find_widget_within(parent_widget, leaf)

    scope = selector.get("scope", "modal")
    roots: list[QWidget] = []
    modal_stack_match = _MODAL_STACK_RE.match(scope) if isinstance(scope, str) else None

    if "root_object_name" in selector:
        name = selector["root_object_name"]
        for top in app.topLevelWidgets():
            if top.objectName() == name:
                roots.append(top)
        if not roots:
            # 一番上の窓に無ければ、窓の中の widget（ドック、パネルなど）を根にする（#20）
            inner = _widgets_named(app, name)
            if len(inner) > 1:
                return None, {
                    "reason": "selector_ambiguous",
                    "root_object_name": name,
                    "match_count": len(inner),
                    "candidates": [_widget_summary(w) for w in inner[:10]],
                    "hint": "root_object_name matches several widgets; use _scope_chain",
                }
            roots.extend(inner)
    elif scope == "modal":
        modal = app.activeModalWidget()
        if modal is not None:
            roots.append(modal)
        else:
            win = app.activeWindow()
            if win is not None:
                roots.append(win)
    elif modal_stack_match:
        # ADR-0002 §8.4 入れ子 modal: modal_stack[N] で N 番目を取る。
        # N=0 は外側（最初に開いた）、N=-1 は内側（最前面）。``activeModalWidget`` は
        # 最前面 1 個しか返さないので、入れ子 modal の外側に届かないケースを救う。
        idx = int(modal_stack_match.group(1))
        stack = _list_modal_stack()
        if not stack:
            return None, {"reason": "no_modals_in_stack", "scope": scope}
        try:
            roots.append(stack[idx])
        except IndexError:
            return None, {
                "reason": "modal_stack_index_out_of_range",
                "scope": scope,
                "stack_size": len(stack),
            }
    elif scope == "active_window":
        win = app.activeWindow()
        if win is not None:
            roots.append(win)
    elif scope == "popup":
        # 開いているポップアップ（QMenu.exec() のメニューなど）。モーダルではないので
        # "modal" では届かない（#22）
        popup = app.activePopupWidget()
        if popup is None:
            return None, {"reason": "no_popup", "scope": scope}
        roots.append(popup)
    elif scope == "any":
        roots = [w for w in app.topLevelWidgets() if w.isVisible()]
    else:
        return None, {"reason": "invalid_scope", "scope": scope}

    if not roots:
        return None, {"reason": "no_root_widget", "scope": scope}

    candidates = _collect_candidates(roots, selector)
    return _finalize_match(candidates, selector, roots)


def _widgets_named(app: Any, name: str) -> list[QWidget]:
    """一番上の窓の中から、objectName が ``name`` の見えている widget を集める（重複なし）。

    見えていないものは入れない。閉じたあとまだ消えていない窓の中の widget まで
    拾うと、同じ名前が重なって ``selector_ambiguous`` になるため。
    """
    found: list[QWidget] = []
    seen: set[int] = set()
    for top in app.topLevelWidgets():
        for widget in top.findChildren(QWidget, name):
            if not widget.isVisible():
                continue
            if id(widget) not in seen:
                seen.add(id(widget))
                found.append(widget)
    return found


def _find_widget_within(root: QWidget, selector: dict) -> tuple[QWidget | None, dict]:
    """指定 root 配下に閉じて selector を解決する（Locator chain 内部用）。

    `_find_widget` の scope/root_object_name 解決を skip し、与えられた root
    （およびその子孫）だけを対象にマッチング + strict mode 解釈を行う。
    chain ステップで「親 widget の中で次の selector を引く」用途。

    `_scope_chain` を持つ selector を渡すと再帰的に解決される。
    """
    # 内部からさらに chain がネストしている場合は再帰的に解決する
    chain = selector.get("_scope_chain") or []
    if chain:
        parent_widget: QWidget | None = root
        for step_index, step in enumerate(chain):
            assert parent_widget is not None
            step_widget, step_diag = _find_widget_within(parent_widget, step)
            if step_widget is None:
                return None, {
                    "reason": "chain_step_not_found",
                    "step_index": step_index,
                    "step_selector": step,
                    "step_diagnostics": step_diag,
                }
            parent_widget = step_widget
        leaf = {k: v for k, v in selector.items() if k != "_scope_chain"}
        assert parent_widget is not None
        return _find_widget_within(parent_widget, leaf)

    roots = [root]
    candidates = _collect_candidates(roots, selector)
    return _finalize_match(candidates, selector, roots)


def _collect_candidates(roots: list[QWidget], selector: dict) -> list[QWidget]:
    """selector の match キーで roots 配下の QWidget / QAction を絞り込む。

    `_find_widget` と `_find_widget_within` で共有する純粋なフィルタ。scope /
    `root_object_name` / `index` / strict mode は呼び出し側の責務。

    `label` 指定があれば buddy map を 1 度だけ lazy 構築する（無指定なら
    余計なツリー走査を発生させない）。

    QAction は QWidget ツリーに乗らないので `findChildren(QAction)` で別途
    収集する（class 指定が None / "QAction" のときのみ、かつ object_name / text
    の少なくとも一方が指定されているとき：素のフィルタなしで全 QAction を
    返すのは候補爆発するため）。
    """
    object_name = selector.get("object_name")
    text = selector.get("text")
    class_name = selector.get("class")
    title = selector.get("title")
    placeholder = selector.get("placeholder")
    label_text = selector.get("label")

    buddy_map: dict[int, str] | None = None
    candidates: list[QWidget] = []
    for root in roots:
        for widget in _walk_widgets(root):
            if class_name is not None and type(widget).__name__ != class_name:
                continue
            if object_name is not None and widget.objectName() != object_name:
                continue
            if text is not None:
                widget_text = _widget_text(widget)
                if widget_text != text:
                    continue
            if title is not None and _title_of(widget) != title:
                continue
            if placeholder is not None:
                if not hasattr(widget, "placeholderText"):
                    continue
                try:
                    if widget.placeholderText() != placeholder:
                        continue
                except Exception:  # noqa: BLE001
                    continue
            if label_text is not None:
                if buddy_map is None:
                    buddy_map = _build_buddy_label_map(roots)
                if buddy_map.get(id(widget)) != label_text:
                    continue
            candidates.append(widget)

        if class_name in (None, "QAction") and (object_name is not None or text is not None):
            is_menu = isinstance(root, QMenu)
            # メニューが根なら、その項目だけ（#22）。項目はメニューの子とは限らない
            # （addAction(既存の action)）うえ、findChildren では下位メニューの項目
            # まで拾い、開いていない項目と重なる
            actions = (
                # 見えていない項目は人には選べない（snapshot の menu_items と同じ）
                [a for a in root.actions() if not a.isSeparator() and a.isVisible()]
                if is_menu
                else root.findChildren(QAction)
            )
            for action in actions:
                if object_name is not None and action.objectName() != object_name:
                    continue
                if text is not None:
                    # メニューの項目は、表示どおりの文言（`&` や `(&N)` 抜き）でも当てる
                    forms = label_forms(action.text()) if is_menu else (action.text(),)
                    if text not in forms:
                        continue
                if class_name is not None and class_name != "QAction":
                    continue
                candidates.append(action)

    # 同一 widget が複数 root 経由で重複収集される場合に id() で排除する。
    # 例: scope="any" は全可視 top-level widget を root にするため、parent 付け
    # された top-level dialog が「main window の子孫」としても「独立 root」とし
    # ても walk され、同じ widget が 2 回入る。strict mode がこれを ambiguous と
    # 誤判定して click/fill が selector_ambiguous で失敗するのを防ぐ
    # （snapshot 側 snapshot_ui の root 分離と対になる live tree 側の対策）。
    seen: set[int] = set()
    unique: list[QWidget] = []
    for cand in candidates:
        cid = id(cand)
        if cid in seen:
            continue
        seen.add(cid)
        unique.append(cand)
    return unique


def _finalize_match(
    candidates: list[QWidget], selector: dict, roots: list[QWidget]
) -> tuple[QWidget | None, dict]:
    """候補リスト → 確定 widget + diagnostics への変換（strict / index 解釈含む）。

    `selector_match.resolve_with_index` に委譲することで snapshot 側
    (`_find_in_snapshot`) と同一 semantics を保証する。``records`` は
    `_widget_summary` 形式（diagnostics の `candidates` に出す前提）。
    """
    if not candidates:
        return None, {
            "reason": "no_match",
            "selector": selector,
            "searched_roots": [_widget_summary(r) for r in roots],
        }

    records = [_widget_summary(c) for c in candidates]
    chosen_record, diagnostics = resolve_with_index(records, selector)
    if chosen_record is None:
        if diagnostics.get("reason") == "selector_ambiguous":
            diagnostics["selector"] = selector
        return None, diagnostics

    # records と candidates は同じ index 並びなので、確定 record の位置を取って
    # 元の widget を返す。`index` 未指定単一マッチも `index=N` 指定も対応できる。
    # 位置は同じオブジェクトかどうかで探す。`records.index()` は中身の等しい最初の
    # 要素を返すので、要約（クラス・object_name）が同じ候補だと、`index=1` でも
    # 1 つ目に戻っていた（#20）
    chosen_index = next(i for i, record in enumerate(records) if record is chosen_record)
    return candidates[chosen_index], diagnostics


def _title_of(widget: Any) -> str | None:
    """selector の ``title`` と比べる題名。メニューは ``QMenu.title()``。

    メニューの ``windowTitle`` はふつう空なので、snapshot の ``active_popup`` と
    dialog handler の predicate が使う題名（``QMenu.title()``）に揃える。
    """
    if isinstance(widget, QMenu) and widget.title():
        return widget.title()
    return widget.windowTitle() if hasattr(widget, "windowTitle") else None


def _walk_widgets(root: QWidget) -> Iterator[QWidget]:
    yield root
    yield from root.findChildren(QWidget)


def _is_secret_widget(widget: Any) -> bool:
    """パスワード欄か（文字を外に出してはいけない欄か）。

    ``echoMode`` が ``Normal`` 以外（``Password`` / ``PasswordEchoOnEdit`` /
    ``NoEcho``）の ``QLineEdit`` が対象。``QgsPasswordLineEdit`` は目のボタンで
    ``Normal`` に切り替えられるので、クラスでも判定する。
    """
    if not isinstance(widget, QLineEdit):
        return False
    if any(cls.__name__ == "QgsPasswordLineEdit" for cls in type(widget).__mro__):
        return True
    try:
        return widget.echoMode() != QLineEdit.EchoMode.Normal
    except Exception:  # noqa: BLE001 - 判定できなければ伏せる側に倒す
        return True


def _widget_text(widget: QWidget) -> str | None:
    """widget の表示文字列。パスワード欄は None（要約にも探索にも使わせない）。"""
    if _is_secret_widget(widget):
        return None
    if isinstance(widget, QAbstractButton):
        return widget.text()
    if isinstance(widget, QLabel):
        return widget.text()
    if isinstance(widget, QAction):
        return widget.text()
    if hasattr(widget, "text") and callable(widget.text):
        try:
            value = widget.text()
            if isinstance(value, str):
                return value
        except TypeError:
            return None
    return None


def _widget_summary(widget: Any) -> dict:
    info: dict[str, Any] = {
        "class": type(widget).__name__,
        "object_name": widget.objectName() if hasattr(widget, "objectName") else "",
    }
    text = _widget_text(widget) if isinstance(widget, QWidget) else None
    if isinstance(widget, QAction):
        text = widget.text()
    if text:
        info["text"] = text
    if _is_secret_widget(widget):
        info["masked"] = True
    role = _widget_role(widget)
    if role is not None:
        info["role"] = role
    return info


def _widget_role(widget: Any) -> str | None:
    """Qt accessible role 名を文字列で返す（ADR-0002 §8.4 getByRole）。

    `QAccessible.queryAccessibleInterface(widget)` で interface を取り、その
    `role()` を enum 名（例: "Button", "EditableText", "ComboBox"）に変換する。
    accessible interface を持たない widget（例: QAction）は None。

    Qt 5 / Qt 6 で enum 取得 API が違うので両対応する（``QAccessible.Role(int).name``
    は両方で動く前提）。失敗は best-effort で None。
    """
    try:
        from qgis.PyQt.QtGui import QAccessible
    except ImportError:
        return None
    try:
        iface = QAccessible.queryAccessibleInterface(widget)
        if iface is None:
            return None
        role = iface.role()
    except Exception:  # noqa: BLE001 - best-effort, role は補助情報
        return None
    # enum -> name
    try:
        return QAccessible.Role(role).name
    except (AttributeError, ValueError, TypeError):
        try:
            return str(role)
        except Exception:  # noqa: BLE001
            return None


def _describe_widget(
    widget: QWidget,
    max_depth: int,
    include_invisible: bool,
    child_cap: int = _DEFAULT_MAX_CHILDREN,
    buddy_map: dict[int, str] | None = None,
    exclude_ids: set[int] | frozenset[int] | None = None,
) -> dict:
    """Describe widget into dict for snapshot.

    Args:
        buddy_map: If provided, lookup ``id(widget)`` to populate the ``label``
            field (Playwright ``getByLabel`` selector parity). Built once at the
            top of ``snapshot_ui`` and passed down recursively to avoid
            re-scanning ``QLabel`` per node.
        exclude_ids: ``id(widget)`` set of widgets that are reported as their own
            snapshot root (``active_modal`` / ``visible_dialogs`` / ``main_window``).
            Any *descendant* whose id is in this set is skipped so the same widget
            never appears under two roots (avoids strict-mode false-ambiguity; see
            ``snapshot_ui``). The node passed as ``widget`` itself is always
            described — only its children are filtered — so a root can carry its
            own id in the set harmlessly.
    """
    info: dict[str, Any] = {
        "class": type(widget).__name__,
        "object_name": widget.objectName(),
        "enabled": widget.isEnabled(),
        "visible": widget.isVisible(),
    }
    if hasattr(widget, "windowTitle"):
        title = widget.windowTitle()
        if title:
            info["title"] = title

    _populate_widget_value(widget, info)

    # buddy 経由で対応するラベル文字列を持っていれば付ける（D: getByLabel）。
    # placeholder は _fill_line_edit が既に populate 済み。
    if buddy_map is not None:
        label_text = buddy_map.get(id(widget))
        if label_text:
            info["label"] = label_text

    # ADR-0002 §8.4 getByRole: Qt accessible role 名（例: "Button" / "ComboBox"）
    role = _widget_role(widget)
    if role is not None:
        info["role"] = role

    if max_depth <= 0:
        return info

    children: list[dict] = []
    emitted = 0
    for child in widget.children():
        if not isinstance(child, QWidget):
            continue
        # 別 root（active_modal / visible_dialogs / main_window）として個別に
        # 報告される top-level dialog 等は subtree から除外して二重カウントを防ぐ。
        if exclude_ids is not None and id(child) in exclude_ids:
            continue
        if not include_invisible and not child.isVisible():
            continue
        if emitted >= child_cap:
            children.append({"_truncated": True, "remaining": "?"})
            break
        children.append(
            _describe_widget(
                child, max_depth - 1, include_invisible, child_cap, buddy_map, exclude_ids
            )
        )
        emitted += 1
    if children:
        info["children"] = children
    return info


def _populate_widget_value(widget: QWidget, info: dict) -> None:
    """Attach value/state fields based on widget class."""
    if _is_file_widget(widget):
        info["file_path"] = widget.filePath()
        info["storage_mode"] = _storage_mode(widget)
        return
    handlers: list[tuple[type, Callable[[QWidget, dict], None]]] = [
        (QPushButton, _fill_button),
        (QToolButton, _fill_button),
        (QCheckBox, _fill_checkable),
        (QRadioButton, _fill_checkable),
        (QLineEdit, _fill_line_edit),
        (QComboBox, _fill_combo),
        (QSpinBox, _fill_spin),
        (QDoubleSpinBox, _fill_spin),
        (QLabel, _fill_label),
        (QPlainTextEdit, _fill_plain_text),
        (QTextEdit, _fill_text_edit),
        (QTabWidget, _fill_tab_widget),
        (QAbstractItemView, _fill_item_view),
    ]
    for cls, fn in handlers:
        if isinstance(widget, cls):
            fn(widget, info)
            return


def _fill_button(widget: QAbstractButton, info: dict) -> None:
    info["text"] = widget.text()
    if widget.isCheckable():
        info["checked"] = widget.isChecked()


def _fill_checkable(widget: QAbstractButton, info: dict) -> None:
    info["text"] = widget.text()
    info["checked"] = widget.isChecked()


def _fill_line_edit(widget: QLineEdit, info: dict) -> None:
    # パスワード欄は文字も長さも出さない（#10）。読む必要があるテストは
    # execute_python で明示的に読む
    if _is_secret_widget(widget):
        info["masked"] = True
    else:
        info["text"] = widget.text()
    placeholder = widget.placeholderText()
    if placeholder:
        info["placeholder"] = placeholder
    info["read_only"] = widget.isReadOnly()


def _fill_combo(widget: QComboBox, info: dict) -> None:
    info["current_text"] = widget.currentText()
    info["current_index"] = widget.currentIndex()
    info["items"] = [widget.itemText(i) for i in range(widget.count())]


def _fill_spin(widget: QSpinBox | QDoubleSpinBox, info: dict) -> None:
    info["value"] = widget.value()
    info["min"] = widget.minimum()
    info["max"] = widget.maximum()


def _fill_label(widget: QLabel, info: dict) -> None:
    info["text"] = widget.text()


def _fill_plain_text(widget: QPlainTextEdit, info: dict) -> None:
    info["text"] = widget.toPlainText()


def _fill_text_edit(widget: QTextEdit, info: dict) -> None:
    info["text"] = widget.toPlainText()


def _fill_item_view(widget: QAbstractItemView, info: dict) -> None:
    """リスト・表・ツリーの中身を、モデルの DisplayRole から読む（#12）。

    - リスト（``QListView`` / ``QListWidget``）：``items``（文字列の list）と ``current_row``
    - 表・ツリー（``QTableView`` / ``QTableWidget`` / ``QTreeView`` / ``QTreeWidget``）：
      ``columns``（見出し。見出しを隠していれば無し）、``rows``（セルの 2 次元 list）、
      ``current``（``{"row", "column"}``）。ツリーは一番上の階層だけ
    - どちらも ``row_count``（全体の行数）。``_MAX_ITEM_ROWS`` 行 / ``_MAX_ITEM_COLUMNS``
      列を超えた分は返さず、``truncated: true`` を付ける（表は ``column_count`` も）

    ``QHeaderView`` は表の子として同じモデルを持つので読まない（二重になる）。
    値は伏せない（モデルにパスワード欄の概念は無い）。
    """
    if isinstance(widget, QHeaderView):
        return
    model = widget.model()
    if model is None:
        return
    root = widget.rootIndex()
    is_list = isinstance(widget, QListView)
    try:
        total_rows = model.rowCount(root)
        # QAbstractListModel では columnCount() が private で呼べない（リストは 1 列）
        total_cols = 1 if is_list else model.columnCount(root)
    except Exception:  # noqa: BLE001 - 壊れたモデルでも snapshot は止めない
        return
    n_rows = min(total_rows, _MAX_ITEM_ROWS)
    current = widget.currentIndex()
    info["row_count"] = total_rows
    truncated = total_rows > n_rows
    if is_list:
        column = widget.modelColumn()
        info["items"] = [_display(model.index(r, column, root)) for r in range(n_rows)]
        info["current_row"] = current.row() if current.isValid() else None
    else:
        n_cols = min(total_cols, _MAX_ITEM_COLUMNS)
        # 見出しを隠したツリー（レイヤパネルなど）では、モデルの見出しは Qt の既定の
        # 連番になるだけなので出さない
        if _header_shown(widget):
            info["columns"] = [_display_header(model, c) for c in range(n_cols)]
        info["rows"] = [
            [_display(model.index(r, c, root)) for c in range(n_cols)] for r in range(n_rows)
        ]
        info["current"] = (
            {"row": current.row(), "column": current.column()} if current.isValid() else None
        )
        if total_cols > n_cols:
            info["column_count"] = total_cols
            truncated = True
    if truncated:
        info["truncated"] = True


def _header_shown(widget: Any) -> bool:
    """表・ツリーの横の見出しが表示される設定か（隠されていなければ True）。"""
    if isinstance(widget, QTreeView):
        return not widget.isHeaderHidden()
    header = getattr(widget, "horizontalHeader", None)
    if header is None:
        return True
    try:
        return not header().isHidden()
    except Exception:  # noqa: BLE001
        return True


def _display(index: Any) -> Any:
    """セルの表示値。JSON にできる値（str / 数値 / bool / None）はそのまま、ほかは str。"""
    try:
        value = index.data(Qt.ItemDataRole.DisplayRole)
    except Exception:  # noqa: BLE001
        return None
    return _jsonable(value)


def _display_header(model: Any, column: int) -> Any:
    try:
        value = model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
    except Exception:  # noqa: BLE001
        return None
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    try:
        if value.isNull():  # QVariant / QGIS の NULL、空の QDate など
            return None
    except Exception:  # noqa: BLE001 - isNull を持たない型
        pass
    # QDate / QTime / QDateTime は ISO 形式に。str() だと "PyQt5.QtCore.QDate(...)" になる
    try:
        return value.toString(Qt.DateFormat.ISODate)
    except Exception:  # noqa: BLE001 - toString(format) を持たない型
        return str(value)


def _fill_tab_widget(widget: QTabWidget, info: dict) -> None:
    info["current_index"] = widget.currentIndex()
    info["tabs"] = [widget.tabText(i) for i in range(widget.count())]
