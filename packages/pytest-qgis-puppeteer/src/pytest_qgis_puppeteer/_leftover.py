"""テストのあとに残った状態を見つける（#28）。

テストの call の直前と、fixture の後片付けが終わったあとに、Worker の
``qgis_get_leftover_state`` で状態を読み、**始めに無かったのに残ったもの**を知らせる。
fixture が作って fixture が片付けたもの、ホストが始めから開いている窓などは出ない。

見る項目は ini で選ぶ（既定はどれも見ない）:

- ``qgis_leftover_fail``: 見つけたら teardown のエラーにする項目
- ``qgis_leftover_warn``: 見つけたら警告（:class:`QgisLeftoverWarning`）だけの項目

製品の既知の振る舞いで、決まったテストがいつも残す項目は、テストに
``@pytest.mark.qgis_leftover_expected("map_tool", reason=...)`` を付けて判定から
外せる（#31）。付けたのに残らなければ :class:`QgisLeftoverWarning` で知らせる。

片付け（編集の破棄、窓を閉じる）はしない。利用者のデータを壊しうるので、
利用側の conftest の役目。
"""

from __future__ import annotations

from typing import Any

import pytest

ITEMS = (
    "dialogs",
    "popup",
    "editing",
    "editing_modified",
    "map_tool",
    "selection",
    "layer_tree",
    "layers",
    "active_layer",
    "project_dirty",
)

INI_FAIL = "qgis_leftover_fail"
INI_WARN = "qgis_leftover_warn"
MARKER = "qgis_leftover_expected"


class QgisLeftoverWarning(UserWarning):
    """``qgis_leftover_warn`` の項目で、テストのあとに状態が残っていた（#28）。"""


def configured_items(config: pytest.Config) -> tuple[list[str], list[str]]:
    """ini の ``(失敗にする項目, 警告だけの項目)``。知らない項目は ``UsageError``。"""
    fail = _items(config, INI_FAIL)
    warn = [i for i in _items(config, INI_WARN) if i not in fail]
    return fail, warn


def expected_items(item: pytest.Item) -> dict[str, str | None]:
    """テストに付いた ``qgis_leftover_expected`` の ``{項目: reason}``（#31）。

    function・class・module の印を合わせる。同じ項目は近い印の ``reason`` を使う。
    項目が無い、知らない項目、``reason`` 以外の引数は ``UsageError``。
    """
    found: dict[str, str | None] = {}
    for mark in item.iter_markers(MARKER):
        where = f"{item.nodeid}: @pytest.mark.{MARKER}"
        if not mark.args:
            raise pytest.UsageError(f"{where} needs at least one item")
        extra = set(mark.kwargs) - {"reason"}
        if extra:
            raise pytest.UsageError(f"{where}: unknown argument(s) {', '.join(sorted(extra))}")
        for name in mark.args:
            if name not in ITEMS:
                raise pytest.UsageError(
                    f"{where}: unknown item {name!r}; choose from {', '.join(ITEMS)}"
                )
            found.setdefault(name, mark.kwargs.get("reason"))
    return found


def _items(config: pytest.Config, name: str) -> list[str]:
    raw = config.getini(name) or []
    items: list[str] = []
    for value in raw:
        for part in str(value).replace(",", " ").split():
            if part not in ITEMS:
                raise pytest.UsageError(
                    f"{name}: unknown item {part!r}; choose from {', '.join(ITEMS)}"
                )
            if part not in items:
                items.append(part)
    return items


def leftovers(item: str, before: Any, after: Any) -> list[str]:
    """``item`` で、始め（``before``）に無かったのに終わり（``after``）に残ったもの。

    人が読む 1 行ずつの説明の list。無ければ空。
    """
    return _DIFFS[item](before, after)


def _dialogs(before: list[dict], after: list[dict]) -> list[str]:
    def key(w: dict) -> tuple:
        return (w.get("class"), w.get("object_name"), w.get("title"))

    remaining = [key(w) for w in before]
    found = []
    for w in after:
        k = key(w)
        if k in remaining:
            remaining.remove(k)
            continue
        kind = "modal" if w.get("modal") else "dialog"
        found.append(f"{kind} left open: {w.get('class')} {w.get('title')!r}")
    return found


def _popup(before: Any, after: Any) -> list[str]:
    if after is None or after == before:
        return []
    return [f"popup left open: {after.get('class')} {after.get('title', '')!r}".rstrip()]


def _editing_layers(before: list[dict], after: list[dict], *, modified: bool) -> list[str]:
    def changed(layer: dict) -> bool:
        return bool(layer.get("modified")) or int(layer.get("undo") or 0) > 0

    was = {layer["id"]: layer for layer in before}
    found = []
    for layer in after:
        if changed(layer) != modified:
            continue
        old = was.get(layer["id"])
        if old is not None and changed(old) == changed(layer):
            continue  # 始めから同じ状態だった
        if modified:
            found.append(
                f"layer {layer['name']!r} left with unsaved changes "
                f"(modified={layer.get('modified')}, undo={layer.get('undo')})"
            )
        else:
            found.append(f"layer {layer['name']!r} left in editing mode (no changes)")
    return found


def _changed(label: str) -> Any:
    def diff(before: Any, after: Any) -> list[str]:
        return [] if after == before else [f"{label} changed: {before!r} -> {after!r}"]

    return diff


def _selection(before: dict, after: dict) -> list[str]:
    found = []
    for lid in sorted(set(before) | set(after)):
        old, new = before.get(lid), after.get(lid)
        old_n = old["count"] if old else 0
        new_n = new["count"] if new else 0
        name = (new or old)["name"]
        if old_n != new_n:
            found.append(f"selection on {name!r}: {old_n} -> {new_n} features")
        elif old and new and old.get("ids") != new.get("ids"):
            found.append(f"selection on {name!r}: {new_n} features, but different ones")
    return found


def _layer_tree(before: dict, after: dict) -> list[str]:
    """表示と凡例のチェックで、始めと違うもの（``レイヤ › 項目: checked True -> False``）。"""
    found = []
    for part in ("nodes", "legend"):
        was = before.get(part, {})
        for key, now in after.get(part, {}).items():
            old = was.get(key)
            if old is not None and old["checked"] != now["checked"]:
                found.append(f"{now['label']}: checked {old['checked']} -> {now['checked']}")
    return found


def _layers(before: dict, after: dict) -> list[str]:
    found = []
    old_layers, new_layers = before.get("layers", {}), after.get("layers", {})
    found += [
        f"layer added and left: {name!r}"
        for lid, name in new_layers.items()
        if lid not in old_layers
    ]
    found += [
        f"layer removed: {name!r}" for lid, name in old_layers.items() if lid not in new_layers
    ]
    old_groups, new_groups = before.get("groups", []), after.get("groups", [])
    found += [f"group added and left: {g!r}" for g in new_groups if g not in old_groups]
    found += [f"group removed: {g!r}" for g in old_groups if g not in new_groups]
    return found


def _active_layer(before: Any, after: Any) -> list[str]:
    def name(layer: Any) -> str:
        return repr(layer["name"]) if layer else "none"

    old_id = before["id"] if before else None
    new_id = after["id"] if after else None
    return [] if old_id == new_id else [f"active layer: {name(before)} -> {name(after)}"]


def _project_dirty(before: Any, after: Any) -> list[str]:
    return ["project left with unsaved changes"] if after and not before else []


_DIFFS = {
    "dialogs": _dialogs,
    "popup": _popup,
    "editing": lambda b, a: _editing_layers(b, a, modified=False),
    "editing_modified": lambda b, a: _editing_layers(b, a, modified=True),
    "map_tool": _changed("map tool"),
    "selection": _selection,
    "layer_tree": _layer_tree,
    "layers": _layers,
    "active_layer": _active_layer,
    "project_dirty": _project_dirty,
}


def format_unmet(nodeid: str, unmet: dict[str, str | None]) -> str:
    """印を付けたのに残らなかった項目の知らせ（#31）。直ったなら印を外せるように。"""
    lines = [f"{nodeid} was expected to leave these behind, but did not:"]
    for item, reason in unmet.items():
        lines.append(f"  [{item}]" + (f" ({reason})" if reason else ""))
    names = ", ".join(repr(name) for name in unmet)
    lines.append(f"  if it was fixed, remove them from @pytest.mark.{MARKER}({names})")
    return "\n".join(lines)


def format_leftovers(nodeid: str, found: dict[str, list[str]]) -> str:
    lines = [f"{nodeid} left QGIS in a different state than it found it:"]
    for item, entries in found.items():
        lines.append(f"  [{item}]")
        lines.extend(f"    - {entry}" for entry in entries)
    return "\n".join(lines)
