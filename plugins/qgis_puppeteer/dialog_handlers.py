"""Dialog handler registry — Qt 非依存のグローバル auto-respond 機構。

ADR-0002 Roadmap "Dialog handler（グローバル auto-respond）" の core 部分。
QGIS 環境で「想定外のダイアログ（Bad Layers / 認証失敗 / 古いプロジェクト警告
等）」が出たときに、事前に登録した handler が自動で `accept` / `reject` /
`close` できるようにする。

## 構造

- :class:`DialogHandlerRegistry` — pure Python の state container
- 各 handler は ``(name, predicate, action, once)`` の組
- ``predicate`` は selector dict（`class` / `object_name` / `title` で modal を絞る）
- ``action`` は ``"accept"`` / ``"reject"`` / ``"close"`` のいずれか
- ``once=True`` で一度発火したら自動 unregister

## tick の意味

Worker が QTimer で定期的に :meth:`select_for_modal` を呼び、現在の
``activeModalWidget()`` に対する応答を 1 件返す。Qt 操作（``accept()``
等）は Qt-tied 側（``qgis_tools.dialog_handler_tools``）が行う。

## 状態管理

「同じ modal を 2 回処理しない」を達成するため最後に処理した modal の
``id()`` を保持する。modal が閉じる（None になる）か、別の modal に切り替わる
と "ハンドル済み" 状態をリセットする。
"""

from __future__ import annotations

from dataclasses import dataclass

from qgis_puppeteer.selector_match import record_matches_selector

_VALID_ACTIONS = frozenset({"accept", "reject", "close"})


class DialogHandlerError(ValueError):
    """register() の引数バリデーション失敗。"""


@dataclass(frozen=True)
class DialogHandlerSpec:
    """1 つの dialog handler 定義。

    ``predicate`` は :func:`qgis_puppeteer.selector_match.record_matches_selector`
    に渡される selector dict（``class`` / ``object_name`` / ``title`` を主に
    想定）。``action`` は ``"accept"`` / ``"reject"`` / ``"close"``。
    """

    name: str
    predicate: dict
    action: str
    once: bool


class DialogHandlerRegistry:
    """Dialog handler の状態管理（Qt 非依存）。

    Worker 側 QTimer から :meth:`select_for_modal` を呼び、戻り値の
    :class:`DialogHandlerSpec` に従って Qt 操作を行うフロー。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, DialogHandlerSpec] = {}
        # 最後に観測した modal の id() — 「同じ modal を 2 回処理しない」用
        self._last_modal_id: int | None = None

    # ------------------------------------------------------------
    # 公開 API（handler 登録・解除・列挙）
    # ------------------------------------------------------------

    def register(
        self,
        name: str,
        predicate: dict,
        action: str,
        once: bool = False,
    ) -> None:
        """Handler を登録する（既存 name は上書き）。

        Raises:
            DialogHandlerError: ``name`` が空、``action`` が未対応値、または
                ``predicate`` が dict でない場合。
        """
        if not name:
            raise DialogHandlerError("name must not be empty")
        if action not in _VALID_ACTIONS:
            raise DialogHandlerError(
                f"unsupported action: {action!r}; must be one of {sorted(_VALID_ACTIONS)}"
            )
        if not isinstance(predicate, dict):
            raise DialogHandlerError("predicate must be a dict")
        self._handlers[name] = DialogHandlerSpec(
            name=name, predicate=dict(predicate), action=action, once=once
        )

    def unregister(self, name: str) -> bool:
        """``name`` の handler を削除。存在すれば True、無ければ False。"""
        return self._handlers.pop(name, None) is not None

    def clear(self) -> None:
        """全 handler と modal-id 状態をリセット（テスト / セッション境界用）。"""
        self._handlers.clear()
        self._last_modal_id = None

    def list_specs(self) -> list[dict]:
        """登録済み handler の一覧を dict のリストで返す。"""
        return [
            {
                "name": h.name,
                "predicate": dict(h.predicate),
                "action": h.action,
                "once": h.once,
            }
            for h in self._handlers.values()
        ]

    def __len__(self) -> int:
        return len(self._handlers)

    # ------------------------------------------------------------
    # tick 用: modal の評価
    # ------------------------------------------------------------

    def select_for_modal(
        self,
        modal_id: int | None,
        modal_record: dict | None,
    ) -> DialogHandlerSpec | None:
        """現在の modal に対して発火すべき handler を返す（無ければ None）。

        Args:
            modal_id: 現在の ``activeModalWidget()`` の ``id()``、または None。
            modal_record: ``{"class", "object_name", "title", ...}`` の dict、
                または None。

        Returns:
            適用すべき :class:`DialogHandlerSpec`、または None。

        副作用:
            - 同じ ``modal_id`` の連続呼び出しでは 2 回目以降 None を返す
              （冪等性: 1 度処理した modal を再度処理しない）
            - ``once=True`` の handler が発火した場合、内部から remove する
        """
        # modal が閉じた / 別の modal に切り替わったら状態リセット
        if modal_id is None:
            self._last_modal_id = None
            return None
        if modal_id == self._last_modal_id:
            # 既に評価済み（一致したら処理済、一致しなくても再評価不要）
            return None

        self._last_modal_id = modal_id

        if modal_record is None:
            return None

        for handler in list(self._handlers.values()):
            if record_matches_selector(modal_record, handler.predicate):
                if handler.once:
                    self._handlers.pop(handler.name, None)
                return handler
        return None
