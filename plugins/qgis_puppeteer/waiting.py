"""待ち合わせの条件の判定（Qt 非依存の純粋関数）。

MCP の ``qgis_wait_for`` が、QGIS 側のコマンドの結果（``qgis_check_actionability`` /
``qgis_snapshot_ui``）を受け取って「条件を満たしたか」を決めるのに使う。判定は
pytest の ``expect(locator).to_*`` と ``wait_for_modal`` / ``wait_for_popup`` に
合わせてある。くり返して呼ぶ側（待つ間隔・時間切れ）はここに持たない。

## 条件（``until``）

読むものは ``qgis_check_actionability`` の結果（check）か ``qgis_snapshot_ui`` の
結果（snapshot）。右は同じ意味の pytest の書き方。

- visible / hidden（check の ``visible``）: ``to_be_visible`` / ``to_be_hidden``
- enabled / disabled（check の ``enabled``）: ``to_be_enabled`` / ``to_be_disabled``。
  disabled は widget があることも要る（見つからない結果も ``enabled`` が False なので、
  selector の書き間違いで満たさないように）。hidden は無くても満たす
- actionable（check の ``actionable``）: Locator の操作の自動の待ち
- editable（check の ``actionable`` と ``editable``）: ``fill()`` の自動の待ち
- count（見えている widget の数）: ``to_have_count``
- text / text_contains（snapshot の ``text``）: ``to_have_text`` / ``to_contain_text``
- value（snapshot の :func:`node_value`）: ``to_have_value``
- checked / unchecked（同じく :func:`node_value`）: ``to_be_checked`` /
  ``not_.to_be_checked``
- row_count（snapshot の ``row_count``）: ``to_have_row_count``
- text / text_contains / value / checked / unchecked / row_count は、node が見つからな
  ければ満たさない（無い widget を「値が違う」「チェックされていない」とみなさない）
- modal / modal_closed（snapshot の ``active_modal``）: ``wait_for_modal`` /
  ``wait_for_modal_closed``
- popup / popup_closed（snapshot の ``active_popup``）: ``wait_for_popup`` /
  ``wait_for_popup_closed``
"""

from __future__ import annotations

from typing import Any, Literal

from qgis_puppeteer.selector_match import find_in_snapshot, label_forms

WaitUntil = Literal[
    "visible",
    "hidden",
    "enabled",
    "disabled",
    "actionable",
    "editable",
    "count",
    "text",
    "text_contains",
    "value",
    "checked",
    "unchecked",
    "row_count",
    "modal",
    "modal_closed",
    "popup",
    "popup_closed",
]

CHECK_CONDITIONS: frozenset[str] = frozenset(
    {"visible", "hidden", "enabled", "disabled", "actionable", "editable"}
)
"""``qgis_check_actionability`` の結果 1 つで決まる条件。"""

COUNT_CONDITIONS: frozenset[str] = frozenset({"count"})
"""selector に当たる数で決まる条件。複数に当たってよい。"""

NODE_CONDITIONS: frozenset[str] = frozenset(
    {"text", "text_contains", "value", "checked", "unchecked", "row_count"}
)
"""snapshot の中の 1 つの node の値で決まる条件。"""

WINDOW_CONDITIONS: frozenset[str] = frozenset({"modal", "modal_closed", "popup", "popup_closed"})
"""snapshot の ``active_modal`` / ``active_popup`` で決まる条件。selector は使わない。"""

NEEDS_EXPECTED: frozenset[str] = frozenset({"count", "text", "text_contains", "value", "row_count"})
"""``expected`` が要る条件。"""


def validate(
    until: str, selector: dict[str, Any] | None, expected: Any, title_contains: str | None
) -> str | None:
    """引数の組み合わせが通らなければ、その理由を返す（通れば None）。"""
    if until in WINDOW_CONDITIONS:
        if selector is not None:
            return f"until={until!r} waits for the window itself and takes no selector"
        if expected is not None:
            return f"until={until!r} takes no expected; use title_contains to pick the window"
        if title_contains is not None and until in ("modal_closed", "popup_closed"):
            return f"until={until!r} waits for whatever is open to close; drop title_contains"
        return None
    if selector is None:
        return f"until={until!r} needs a selector"
    if title_contains is not None:
        return "title_contains is only for until='modal' / 'popup'"
    if until in NEEDS_EXPECTED and expected is None:
        return f"until={until!r} needs expected"
    if until not in NEEDS_EXPECTED and expected is not None:
        return f"until={until!r} takes no expected"
    if until in ("count", "row_count") and (
        isinstance(expected, bool) or not isinstance(expected, int) or expected < 0
    ):
        return f"until={until!r} needs a whole number of 0 or more as expected"
    if until in ("text", "text_contains") and not isinstance(expected, str):
        return f"until={until!r} needs a string as expected"
    return None


def count_selector(selector: dict[str, Any]) -> dict[str, Any]:
    """数えるための selector。``index`` を外し、見えている widget だけにする。

    pytest の ``Locator.count()`` と同じ。見えていない widget を数えるなら
    ``attr={"visible": False}`` を明示する。
    """
    counted = {k: v for k, v in selector.items() if k != "index"}
    attr = dict(counted.get("attr") or {})
    attr.setdefault("visible", True)
    counted["attr"] = attr
    return counted


def count_from_check(check: dict[str, Any]) -> int:
    """``count_selector`` で取った ``qgis_check_actionability`` の結果から数を読む。"""
    diagnostics = check.get("diagnostics") or {}
    match_count = diagnostics.get("match_count")
    if isinstance(match_count, int):
        return match_count
    return 1 if check.get("exists") else 0


def is_ambiguous(check: dict[str, Any]) -> bool:
    """selector が複数に当たって 1 つに決まらなかった結果か。"""
    diagnostics = check.get("diagnostics") or {}
    return isinstance(diagnostics, dict) and diagnostics.get("reason") == "selector_ambiguous"


def evaluate_check(until: str, check: dict[str, Any]) -> bool:
    """:data:`CHECK_CONDITIONS` の条件を ``qgis_check_actionability`` の結果で決める。

    見つからない widget の結果は ``visible`` も ``enabled`` も False。hidden はそれで
    満たすが、disabled は ``exists`` も要る（Playwright の ``toBeDisabled`` と同じ）。
    """
    if until == "visible":
        return bool(check.get("visible"))
    if until == "hidden":
        return not check.get("visible")
    if until == "enabled":
        return bool(check.get("enabled"))
    if until == "disabled":
        return bool(check.get("exists")) and not check.get("enabled")
    if until == "actionable":
        return bool(check.get("actionable"))
    if until == "editable":
        return bool(check.get("actionable")) and bool(check.get("editable", True))
    raise ValueError(f"not a check condition: {until!r}")


def node_value(node: dict[str, Any] | None) -> Any:
    """snapshot の node から、widget の種類ごとの「今の値」を読む。

    pytest の ``Locator.get_value()`` と同じ順（``value`` / ``current_text`` /
    ``checked`` / ``current_index`` / ``text``）で、最初にあるものを返す。
    """
    if node is None:
        return None
    for key in ("value", "current_text", "checked", "current_index", "text"):
        if key in node:
            return node[key]
    return None


def read_node(until: str, snapshot: dict[str, Any], selector: dict[str, Any]) -> tuple[bool, Any]:
    """:data:`NODE_CONDITIONS` の条件が比べる値を snapshot から読む。

    Returns:
        (node が見つかったか, 値)。見つからなければ ``(False, None)``。見つかっても
        値が無ければ値は None（「無い」と「値が無い」を分けるため、見つかったかを返す）。

    Raises:
        LiveOnlySelectorError: snapshot に無いキー（``window`` など）を使っている。
    """
    node = find_in_snapshot(snapshot, selector)
    if node is None:
        return False, None
    if until in ("text", "text_contains"):
        text = node.get("text")
        return True, text if isinstance(text, str) else None
    if until == "row_count":
        return True, node.get("row_count")
    return True, node_value(node)


def evaluate_node(until: str, observed: Any, expected: Any, *, found: bool = True) -> bool:
    """:data:`NODE_CONDITIONS` の条件を、:func:`read_node` で読んだ値で決める。

    ``found`` が False（node が見つからない）なら、どの条件も満たさない。
    """
    if until not in NODE_CONDITIONS:
        raise ValueError(f"not a node condition: {until!r}")
    if not found:
        return False
    if until in ("text", "value", "row_count"):
        return observed is not None and observed == expected
    if until == "text_contains":
        return isinstance(observed, str) and expected in observed
    if until == "checked":
        return bool(observed) is True
    return bool(observed) is not True  # unchecked


def read_window(until: str, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """:data:`WINDOW_CONDITIONS` の条件が見る窓（``active_modal`` / ``active_popup``）。"""
    key = "active_modal" if until.startswith("modal") else "active_popup"
    window = snapshot.get(key)
    return window if isinstance(window, dict) else None


def evaluate_window(until: str, window: dict[str, Any] | None, title_contains: str | None) -> bool:
    """:data:`WINDOW_CONDITIONS` の条件を、:func:`read_window` で読んだ窓で決める。

    モーダルの題名は部分一致（pytest の ``wait_for_modal``）。ポップアップの題名は
    ``&`` のニーモニックを除いた形でも比べる（``wait_for_popup``）。
    """
    if until in ("modal_closed", "popup_closed"):
        return window is None
    if window is None:
        return False
    if title_contains is None:
        return True
    title = window.get("title") or ""
    if until == "modal":
        return title_contains in title
    return any(title_contains in form for form in label_forms(title))


def window_summary(window: dict[str, Any] | None) -> dict[str, Any] | None:
    """結果に添える窓の要約。子の widget の木は省く（要るなら qgis_snapshot_ui）。

    ポップアップのメニューの項目（``menu_items``）は、次に選ぶ項目を決める材料なので残す。
    """
    if window is None:
        return None
    return {k: v for k, v in window.items() if k != "children"}
