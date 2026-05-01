"""QGIS UI 検査 / 操作ツール群。

Qt ウィジェットツリー（active modal / 可視ダイアログ / メインウィンドウ）の
構造化スナップショットと、セレクタ指定でのターゲット操作（click / set value）
を提供する。生の ``execute_python`` を使わずにエージェントが QGIS UI を
観察・操作できるようにすることが目的。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

from qgis.PyQt.QtWidgets import (
    QAbstractButton,
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableView,
    QTableWidget,
    QTabWidget,
    QTextEdit,
    QTimeEdit,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QWidget,
)

from qgis_puppeteer.selector_match import (
    resolve_with_index,
)

# ADR-0002 §8.4 入れ子 modal: scope="modal_stack[N]" を parse する。N は負も可。
_MODAL_STACK_RE = re.compile(r"^modal_stack\[(-?\d+)\]$")

# Cap recursion to keep the payload bounded.
_DEFAULT_MAX_DEPTH = 8
_DEFAULT_MAX_CHILDREN = 200


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
            - visible_dialogs: Other visible top-level QDialog instances.
            - active_window: The currently focused top-level widget class/title.
            - main_window: QGIS main window tree (only if include_main_window).
    """
    app = QApplication.instance()
    result: dict[str, Any] = {
        "active_modal": None,
        "visible_dialogs": [],
        "active_window": None,
    }

    if app is None:
        return result

    active_modal = app.activeModalWidget()
    if active_modal is not None:
        buddy_map = _build_buddy_label_map([active_modal])
        result["active_modal"] = _describe_widget(
            active_modal, max_depth, include_invisible, buddy_map=buddy_map
        )

    active_window = app.activeWindow()
    if active_window is not None:
        result["active_window"] = {
            "class": type(active_window).__name__,
            "object_name": active_window.objectName(),
            "title": active_window.windowTitle(),
        }

    for top in app.topLevelWidgets():
        if top is active_modal:
            continue
        if not isinstance(top, QDialog):
            continue
        if not include_invisible and not top.isVisible():
            continue
        buddy_map = _build_buddy_label_map([top])
        result["visible_dialogs"].append(
            _describe_widget(top, max_depth, include_invisible, buddy_map=buddy_map)
        )

    if include_main_window:
        main = None
        for top in app.topLevelWidgets():
            if top.objectName() == "QgisApp":
                main = top
                break
        if main is not None:
            buddy_map = _build_buddy_label_map([main])
            result["main_window"] = _describe_widget(
                main, max_depth, include_invisible, buddy_map=buddy_map
            )

    return result


def click_widget(selector: dict, iface=None) -> dict:
    """Click a widget located by selector.

    Args:
        selector: See ``_find_widget`` for selector syntax.
        iface: Unused; kept for handler signature consistency.

    Returns:
        Dictionary with success flag and widget metadata.
    """
    del iface  # unused
    widget, diagnostics = _find_widget(selector)
    if widget is None:
        # Strict mode の ambiguous は別エラーコードで返す（caller / Locator が
        # 「selector を直せ」と即判断できるよう）
        error_code = (
            "selector_ambiguous"
            if diagnostics.get("reason") == "selector_ambiguous"
            else "widget_not_found"
        )
        return {
            "success": False,
            "error": error_code,
            "diagnostics": diagnostics,
        }

    if not widget.isEnabled():
        return {
            "success": False,
            "error": "widget_disabled",
            "widget": _widget_summary(widget),
        }

    if isinstance(widget, QAbstractButton):
        widget.click()
    elif isinstance(widget, QAction):
        widget.trigger()
    elif hasattr(widget, "click") and callable(widget.click):
        widget.click()
    else:
        return {
            "success": False,
            "error": "widget_not_clickable",
            "widget": _widget_summary(widget),
        }

    return {"success": True, "widget": _widget_summary(widget)}


def set_widget_value(selector: dict, value: Any, iface=None) -> dict:
    """Set a value on a widget located by selector.

    Handles common input widgets: QLineEdit, QTextEdit/QPlainTextEdit,
    QComboBox, QCheckBox/QRadioButton, QSpinBox/QDoubleSpinBox, date/time
    editors, QTabWidget (selects tab), list/tree/table views (selects row).

    Args:
        selector: See ``_find_widget``.
        value: Value to set. Type interpretation depends on widget class.
        iface: Unused.

    Returns:
        Dictionary with success flag, prior value, and widget metadata.
    """
    del iface
    widget, diagnostics = _find_widget(selector)
    if widget is None:
        error_code = (
            "selector_ambiguous"
            if diagnostics.get("reason") == "selector_ambiguous"
            else "widget_not_found"
        )
        return {
            "success": False,
            "error": error_code,
            "diagnostics": diagnostics,
        }

    if not widget.isEnabled():
        return {
            "success": False,
            "error": "widget_disabled",
            "widget": _widget_summary(widget),
        }

    # readOnly な input への setValue は Qt 側で no-op になる（例: QLineEdit の
    # setText が値を変えない）が、テスト側からは「成功した」ように見えて
    # silent な失敗になる。早期に明示エラーで返して原因を見える化する。
    # `_widget_is_writable` は isReadOnly() を持たない widget には True を返す
    # ので、QComboBox / QCheckBox 等は影響を受けない。
    if not _widget_is_writable(widget):
        return {
            "success": False,
            "error": "widget_readonly",
            "widget": _widget_summary(widget),
        }

    prior: Any = None
    try:
        if isinstance(widget, QLineEdit):
            prior = widget.text()
            widget.setText(str(value))
        elif isinstance(widget, (QPlainTextEdit, QTextEdit)):
            prior = widget.toPlainText()
            widget.setPlainText(str(value))
        elif isinstance(widget, QComboBox):
            prior = widget.currentText()
            if isinstance(value, int):
                widget.setCurrentIndex(value)
            else:
                idx = widget.findText(str(value))
                if idx < 0:
                    return {
                        "success": False,
                        "error": "combo_item_not_found",
                        "value": value,
                        "available": [widget.itemText(i) for i in range(widget.count())],
                    }
                widget.setCurrentIndex(idx)
        elif isinstance(widget, (QCheckBox, QRadioButton)):
            prior = widget.isChecked()
            widget.setChecked(bool(value))
        elif isinstance(widget, QSpinBox):
            prior = widget.value()
            widget.setValue(int(value))
        elif isinstance(widget, QDoubleSpinBox):
            prior = widget.value()
            widget.setValue(float(value))
        elif isinstance(widget, QDateTimeEdit):
            # Also covers QDateEdit / QTimeEdit subclasses.
            from qgis.PyQt.QtCore import QDate, QDateTime, QTime

            prior = widget.dateTime().toString("yyyy-MM-ddTHH:mm:ss")
            if isinstance(widget, QDateEdit) and not isinstance(widget, QDateTimeEdit):
                widget.setDate(QDate.fromString(str(value), "yyyy-MM-dd"))
            elif isinstance(widget, QTimeEdit) and not isinstance(widget, QDateTimeEdit):
                widget.setTime(QTime.fromString(str(value), "HH:mm:ss"))
            else:
                widget.setDateTime(QDateTime.fromString(str(value), "yyyy-MM-ddTHH:mm:ss"))
        elif isinstance(widget, QTabWidget):
            prior = widget.currentIndex()
            if isinstance(value, int):
                widget.setCurrentIndex(value)
            else:
                for i in range(widget.count()):
                    if widget.tabText(i) == str(value):
                        widget.setCurrentIndex(i)
                        break
                else:
                    return {
                        "success": False,
                        "error": "tab_not_found",
                        "value": value,
                        "available": [widget.tabText(i) for i in range(widget.count())],
                    }
        elif isinstance(widget, QListWidget):
            prior = widget.currentRow()
            if isinstance(value, int):
                widget.setCurrentRow(value)
            else:
                items = widget.findItems(str(value), 0)
                if not items:
                    return {
                        "success": False,
                        "error": "list_item_not_found",
                        "value": value,
                    }
                widget.setCurrentItem(items[0])
        elif isinstance(widget, (QListView, QTreeView, QTableView, QTreeWidget, QTableWidget)):
            model = widget.model()
            if model is None:
                return {"success": False, "error": "no_model"}
            if not isinstance(value, int):
                return {"success": False, "error": "row_index_required"}
            prior = widget.currentIndex().row()
            index = model.index(value, 0)
            widget.setCurrentIndex(index)
        else:
            return {
                "success": False,
                "error": "unsupported_widget_type",
                "widget": _widget_summary(widget),
            }
    except Exception as exc:
        return {
            "success": False,
            "error": "set_value_failed",
            "message": str(exc),
            "widget": _widget_summary(widget),
        }

    return {
        "success": True,
        "widget": _widget_summary(widget),
        "prior_value": prior,
    }


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
               "any" — search all top-level widgets.
        root_object_name: If set, constrain search under the top-level
               widget whose objectName equals this value (e.g. "QgisApp").
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
    elif scope == "any":
        roots = [w for w in app.topLevelWidgets() if w.isVisible()]
    else:
        return None, {"reason": "invalid_scope", "scope": scope}

    if not roots:
        return None, {"reason": "no_root_widget", "scope": scope}

    object_name = selector.get("object_name")
    text = selector.get("text")
    class_name = selector.get("class")
    title = selector.get("title")
    placeholder = selector.get("placeholder")
    label_text = selector.get("label")

    # `label` 指定があれば buddy map を 1 度だけ構築する。`label` 未指定なら
    # 不要なツリー走査を避けるため lazily に解決する。
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
            if title is not None and (
                not hasattr(widget, "windowTitle") or widget.windowTitle() != title
            ):
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

        # Also search QActions (not QWidgets) under this root.
        if class_name in (None, "QAction") and (object_name is not None or text is not None):
            for action in root.findChildren(QAction):
                if object_name is not None and action.objectName() != object_name:
                    continue
                if text is not None and action.text() != text:
                    continue
                if class_name is not None and class_name != "QAction":
                    continue
                candidates.append(action)  # QAction isn't a QWidget but we treat it similarly

    if not candidates:
        return None, {
            "reason": "no_match",
            "selector": selector,
            "searched_roots": [_widget_summary(r) for r in roots],
        }

    # Strict mode と index 解釈は共通ロジック (selector_match.resolve_with_index)
    # に委譲。snapshot 側 (_find_in_snapshot) と完全に同じ semantics を保つ。
    # records は widget_summary（後段の diagnostics に出すために必要）。
    records = [_widget_summary(c) for c in candidates]
    chosen_record, diagnostics = resolve_with_index(records, selector)
    if chosen_record is None:
        if diagnostics.get("reason") == "selector_ambiguous":
            diagnostics["selector"] = selector
        return None, diagnostics

    # records と candidates は同じ index 並びなので、確定 record の位置を取って
    # 元の widget を返す。`index` 未指定単一マッチも `index=N` 指定も対応できる。
    chosen_index = records.index(chosen_record)
    return candidates[chosen_index], diagnostics


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

    # match keys（_find_widget と同一）
    object_name = selector.get("object_name")
    text = selector.get("text")
    class_name = selector.get("class")
    title = selector.get("title")
    placeholder = selector.get("placeholder")
    label_text = selector.get("label")

    roots = [root]
    buddy_map: dict[int, str] | None = None
    candidates: list[QWidget] = []
    for widget in _walk_widgets(root):
        if class_name is not None and type(widget).__name__ != class_name:
            continue
        if object_name is not None and widget.objectName() != object_name:
            continue
        if text is not None:
            widget_text = _widget_text(widget)
            if widget_text != text:
                continue
        if title is not None and (
            not hasattr(widget, "windowTitle") or widget.windowTitle() != title
        ):
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

    # QAction も拾う（_find_widget と同様）
    if class_name in (None, "QAction") and (object_name is not None or text is not None):
        for action in root.findChildren(QAction):
            if object_name is not None and action.objectName() != object_name:
                continue
            if text is not None and action.text() != text:
                continue
            if class_name is not None and class_name != "QAction":
                continue
            candidates.append(action)

    if not candidates:
        return None, {
            "reason": "no_match",
            "selector": selector,
            "searched_roots": [_widget_summary(root)],
        }

    records = [_widget_summary(c) for c in candidates]
    chosen_record, diagnostics = resolve_with_index(records, selector)
    if chosen_record is None:
        if diagnostics.get("reason") == "selector_ambiguous":
            diagnostics["selector"] = selector
        return None, diagnostics

    chosen_index = records.index(chosen_record)
    return candidates[chosen_index], diagnostics


def _walk_widgets(root: QWidget) -> Iterator[QWidget]:
    yield root
    yield from root.findChildren(QWidget)


def _widget_text(widget: QWidget) -> str | None:
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
) -> dict:
    """Describe widget into dict for snapshot.

    Args:
        buddy_map: If provided, lookup ``id(widget)`` to populate the ``label``
            field (Playwright ``getByLabel`` selector parity). Built once at the
            top of ``snapshot_ui`` and passed down recursively to avoid
            re-scanning ``QLabel`` per node.
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
        if not include_invisible and not child.isVisible():
            continue
        if emitted >= child_cap:
            children.append({"_truncated": True, "remaining": "?"})
            break
        children.append(
            _describe_widget(child, max_depth - 1, include_invisible, child_cap, buddy_map)
        )
        emitted += 1
    if children:
        info["children"] = children
    return info


def _populate_widget_value(widget: QWidget, info: dict) -> None:
    """Attach value/state fields based on widget class."""
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


def _fill_tab_widget(widget: QTabWidget, info: dict) -> None:
    info["current_index"] = widget.currentIndex()
    info["tabs"] = [widget.tabText(i) for i in range(widget.count())]
