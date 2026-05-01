"""ADR-0002 §11 Locator 抽象（Playwright 流）。

selector dict を毎回 API に渡すスタイルから、`Locator` オブジェクトに
カプセル化するスタイルへ移行するための薄い層。Locator 自体は state を持たず、
操作のたびに内部で selector を Worker 側に再投入するので、QGIS 側の再描画で
widget pointer が無効化されても影響を受けない（§11.1 stale widget pointer
問題の構造的回避）。

## 提供メソッド（ADR-0002 §11.2）

| メソッド | 用途 |
|---|---|
| `click()` | auto-wait + click |
| `fill(value)` | 入力系（LineEdit / TextEdit / SpinBox 等） |
| `select(value)` | ComboBox / List / Tab |
| `check() / uncheck()` | CheckBox / RadioButton |
| `get_text() / get_value()` | snapshot ベースの現在値取得 |
| `is_visible() / is_enabled() / exists()` | 即時状態チェック（wait なし） |
| `snapshot()` | 配下のウィジェットツリー取得 |

## auto-wait（ADR-0002 §8.3）

`click` / `fill` / `select` / `check` / `uncheck` は実行前に
`qgis_check_actionability` を poll し、4 項目（exists / visible / enabled /
not_covered）が揃うまで待つ。timeout は既定 5 秒、poll 間隔は 250ms
（RTT 中央値より十分長く、Hub を詰まらせない）。

timeout 時は `WidgetNotActionableError` を上げる。例外には最後の
actionability 結果が `last_check` 属性で残るので、テスト失敗時に
「どの項目で落ちたか」を診断バンドルに出せる。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pytest_qgis_puppeteer.automation_client import E2EAutomationClient

logger = logging.getLogger("pytest_qgis_puppeteer.locator")


# poll 間隔は ADR-0002 §8.3 の推奨値 250ms（RTT 中央値より十分長く CPU 負荷も
# 抑えられる）。default_timeout は §8.3 表の 5s に揃える。
_DEFAULT_TIMEOUT_S = 5.0
_DEFAULT_POLL_INTERVAL_S = 0.25


class WidgetNotActionableError(RuntimeError):
    """auto-wait timeout 時に上がる例外。

    `last_check` には Worker 側 `qgis_check_actionability` が最後に返した辞書を
    そのまま持つ。`exists` / `visible` / `enabled` / `not_covered` の各 bool と
    `diagnostics`（match 候補 etc.）が入っているので、テスト失敗時の診断材料
    になる。
    """

    def __init__(self, message: str, *, last_check: dict[str, Any]) -> None:
        super().__init__(message)
        self.last_check = last_check


class SelectorAmbiguousError(RuntimeError):
    """selector が複数 widget にマッチして disambiguate 不能なときに上がる例外。

    Worker 側 strict mode（ADR-0002 §8.2 / Roadmap）で複数候補が見つかったが
    `index` が明示されていない場合に検出する。auto-wait の poll を続けても
    結果は変わらない（時間が経っても 1 件に減らない）ので、`_wait_actionable`
    は早期 abort してこの例外を上げる。

    `last_check` には Worker 側の最終結果（`diagnostics.candidates` に
    上位候補の class / object_name / text を含む）が入っているので、
    selector を直すヒントになる。
    """

    def __init__(self, message: str, *, last_check: dict[str, Any]) -> None:
        super().__init__(message)
        self.last_check = last_check


def _diagnostics_reason(check_result: dict[str, Any]) -> str | None:
    """`check_actionability` 結果から `diagnostics.reason` を取り出す薄いアクセサ。

    結果の形は ``{... "diagnostics": {"reason": ..., ...}}``。diagnostics 自体が
    None や非 dict のケースもあり得るので防御的に取り出す。
    """
    diag = check_result.get("diagnostics")
    if not isinstance(diag, dict):
        return None
    reason = diag.get("reason")
    return reason if isinstance(reason, str) else None


class Locator:
    """ADR-0002 §11 Locator。selector を保持して操作のたびに再解決する。

    `E2EAutomationClient.locator(selector)` から生成する。直接 `__init__`
    を呼んでもよいが、通常はクライアント経由が推奨。

    state を持たないので同じ Locator を複数テストで使い回しても安全。

    Locator chain (Playwright 流):
        ``form = qgis.locator({"object_name": "login_form"})`` のような親 Locator
        を作った後、``form.locator({"object_name": "username"})`` で子 Locator を
        作ると、操作時に ``_scope_chain`` 経由で「親 widget 内で子 selector を
        探す」セマンティクスになる。Worker 側でも同 selector 仕様を解釈する
        （ADR-0002 §8 / Roadmap "Locator chain"）。
    """

    def __init__(
        self,
        client: E2EAutomationClient,
        selector: dict[str, Any],
        *,
        instance: str | None = None,
        default_timeout_s: float = _DEFAULT_TIMEOUT_S,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        stable: bool = False,
        _parent_chain: list[dict[str, Any]] | None = None,
    ) -> None:
        self._client = client
        self._selector = dict(selector)  # 防御的コピー
        self._instance = instance
        self._default_timeout_s = default_timeout_s
        self._poll_interval_s = poll_interval_s
        # ADR-0002 §8.3 stable check: 直前 2 回 ``check_actionability`` の geometry
        # が一致するまで待つ（アニメ中操作の吸収）。opt-in なのは普通の widget では
        # 1 回目で stable な前提で、無駄に 1 RTT 増やしたくないため。
        self._stable = bool(stable)
        # Locator chain: 外側 → 内側 の親 selector list（leaf を除く）。
        # `locator(child)` で 1 段ずつ伸びる。
        self._parent_chain: list[dict[str, Any]] = (
            [dict(s) for s in _parent_chain] if _parent_chain else []
        )

    # ------------------------------------------------------------
    # Chain 構築
    # ------------------------------------------------------------

    def locator(self, selector: dict[str, Any]) -> Locator:
        """子 Locator を返す（Playwright 流の chain）。

        親 Locator の selector + _parent_chain を引き継いで、与えられた
        ``selector`` を新しい leaf にする。state を持たない不変オブジェクトとして
        運用するため、self は変更しない。

        Example:
            >>> form = qgis.locator({"object_name": "login_form"})
            >>> input = form.locator({"object_name": "username"})
            >>> input.fill("alice")
            # 内部で {"object_name": "username", "_scope_chain": [{"object_name": "login_form"}]}
            # を Worker に送る。
        """
        new_chain = self._parent_chain + [self._selector]
        return Locator(
            self._client,
            selector,
            instance=self._instance,
            default_timeout_s=self._default_timeout_s,
            poll_interval_s=self._poll_interval_s,
            stable=self._stable,
            _parent_chain=new_chain,
        )

    def _effective_selector(self) -> dict[str, Any]:
        """Worker に渡す selector を組み立てる。chain があれば ``_scope_chain`` 付き。"""
        if not self._parent_chain:
            return dict(self._selector)
        return {**self._selector, "_scope_chain": [dict(s) for s in self._parent_chain]}

    # ------------------------------------------------------------
    # 操作系（auto-wait 内蔵）
    # ------------------------------------------------------------

    def click(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """actionable になるまで待ってから click。

        ``editable`` は要求しない。表示専用 (readOnly な) widget でも click は
        可能なため。
        """
        self._wait_actionable(timeout_s=timeout_s)
        return self._client.click_widget(self._effective_selector(), instance=self._instance)

    def fill(self, value: Any, *, timeout_s: float | None = None) -> dict[str, Any]:
        """actionable + editable になるまで待ってから値を流し込む。

        ``editable`` を要求する: readOnly な input への ``setText`` は silent に
        no-op になるため、auto-wait で「書き込み可能になるまで待つ」のが
        Playwright 流。

        `select` / `check` / `uncheck` との違いは API 呼び出し先ではなく、
        テストの「意図」を読みやすくするためのエイリアス。実体は同じ
        `qgis_set_widget_value`。
        """
        self._wait_actionable(timeout_s=timeout_s, require_editable=True)
        return self._client.set_widget_value(
            self._effective_selector(), value, instance=self._instance
        )

    def select(self, value: Any, *, timeout_s: float | None = None) -> dict[str, Any]:
        """actionable になるまで待ってから select（ComboBox / Tab / List）。

        ComboBox / Tab / List は ``isReadOnly`` を持たないため ``editable`` は
        要求しない（`set_widget_value` の guard も pass する）。
        """
        self._wait_actionable(timeout_s=timeout_s)
        return self._client.set_widget_value(
            self._effective_selector(), value, instance=self._instance
        )

    def check(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """actionable になるまで待ってから checked=True を設定。"""
        self._wait_actionable(timeout_s=timeout_s)
        return self._client.set_widget_value(
            self._effective_selector(), True, instance=self._instance
        )

    def uncheck(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """actionable になるまで待ってから checked=False を設定。"""
        self._wait_actionable(timeout_s=timeout_s)
        return self._client.set_widget_value(
            self._effective_selector(), False, instance=self._instance
        )

    # ------------------------------------------------------------
    # 即時状態チェック（wait なし）
    # ------------------------------------------------------------

    def exists(self) -> bool:
        """selector に該当する widget が現在存在するか（wait なし）。"""
        return bool(self._check_actionability().get("exists", False))

    def is_visible(self) -> bool:
        """selector が指す widget が現在 visible か（wait なし）。"""
        return bool(self._check_actionability().get("visible", False))

    def is_enabled(self) -> bool:
        """selector が指す widget が現在 enabled か（wait なし）。"""
        return bool(self._check_actionability().get("enabled", False))

    # ------------------------------------------------------------
    # snapshot / 値取得
    # ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any] | None:
        """`snapshot_ui(include_main_window=True)` から selector に最初に
        マッチする node 辞書を返す。見つからなければ None。
        """
        from pytest_qgis_puppeteer.automation_client import _find_in_snapshot

        snap = self._client.snapshot_ui(include_main_window=True, instance=self._instance)
        return _find_in_snapshot(snap, self._effective_selector())

    def get_text(self) -> str | None:
        """snapshot から `text` フィールドを取得（QPushButton / QLabel / QLineEdit
        等で `_fill_*` が埋めるフィールド）。
        """
        node = self.snapshot()
        if node is None:
            return None
        text = node.get("text")
        return text if isinstance(text, str) else None

    def get_value(self) -> Any:
        """widget 種別ごとの「現在値」を snapshot から推定して返す。

        - QLineEdit / QTextEdit / QPlainTextEdit / QLabel: `text`
        - QSpinBox / QDoubleSpinBox: `value`
        - QCheckBox / QRadioButton / 押下式ボタン: `checked`
        - QComboBox: `current_text`
        - QTabWidget: `current_index`

        該当フィールドが無ければ None。
        """
        node = self.snapshot()
        if node is None:
            return None
        for key in ("value", "current_text", "checked", "current_index", "text"):
            if key in node:
                return node[key]
        return None

    # ------------------------------------------------------------
    # internal: auto-wait
    # ------------------------------------------------------------

    def _check_actionability(self) -> dict[str, Any]:
        """Worker 側 `qgis_check_actionability` を 1 回呼んで結果をそのまま返す。

        chain がある場合は ``_scope_chain`` 付きの effective selector を送る。
        Worker 側の `_find_widget` が chain を解決した後の widget に対する
        actionability を返す。
        """
        return self._client.check_actionability(self._effective_selector(), instance=self._instance)

    def _wait_actionable(
        self,
        *,
        timeout_s: float | None,
        require_editable: bool = False,
    ) -> dict[str, Any]:
        """`actionable=True` になるまで poll。timeout 時は例外。

        最初の 1 回は即時評価（既に actionable なら 0 RTT で返す、§8.3）。

        Selector が複数 widget にマッチした場合（`reason="selector_ambiguous"`）
        は poll を続けても結果が改善しないので、即座に
        :class:`SelectorAmbiguousError` を上げて abort する（ADR-0002 §8.2
        strict mode）。

        ``require_editable=True`` の場合は ``actionable`` に加えて
        ``editable=True`` も満たすまで待つ（fill 等の書き込み操作で使う、
        ADR-0002 Roadmap "editable check"）。``isReadOnly()`` を持たない widget
        は常に ``editable=True`` を返すので影響を受けない。
        """
        effective_timeout = self._default_timeout_s if timeout_s is None else float(timeout_s)
        deadline = time.monotonic() + effective_timeout
        last: dict[str, Any] = {}
        # stable check 用の前回 geometry。actionable が崩れたら reset。
        prev_geometry: dict[str, Any] | None = None

        def _ready(result: dict[str, Any]) -> bool:
            nonlocal prev_geometry
            if not result.get("actionable"):
                # actionable でなくなったら stable の積み立てもリセット。
                prev_geometry = None
                return False
            if require_editable and not result.get("editable", True):
                return False
            if not self._stable:
                return True
            # ADR-0002 §8.3 stable check: 直前 1 回と geometry が一致したら ready。
            # 初回は prev_geometry=None なので必ず False を返し、最低 2 RTT で
            # アニメ中の widget を吸収する。
            cur_geometry = result.get("geometry")
            if prev_geometry is not None and cur_geometry == prev_geometry:
                return True
            prev_geometry = cur_geometry
            return False

        # 即時 1 回目
        last = self._check_actionability()
        if _ready(last):
            return last
        self._abort_if_ambiguous(last)

        while time.monotonic() < deadline:
            time.sleep(self._poll_interval_s)
            last = self._check_actionability()
            if _ready(last):
                return last
            self._abort_if_ambiguous(last)

        logger.warning(
            "Widget not actionable within %.2fs: selector=%s last=%s",
            effective_timeout,
            self._selector,
            last,
        )
        raise WidgetNotActionableError(
            f"Widget {self._selector!r} did not become actionable within "
            f"{effective_timeout}s (last check: "
            f"exists={last.get('exists')} visible={last.get('visible')} "
            f"enabled={last.get('enabled')} not_covered={last.get('not_covered')} "
            f"editable={last.get('editable')})",
            last_check=last,
        )

    def _abort_if_ambiguous(self, check_result: dict[str, Any]) -> None:
        """selector_ambiguous なら poll を継続せず即例外を上げる。

        timeout を待つ意味がない（時間が経っても候補数は減らない、selector を
        直すしかない）ため早期 abort。診断バンドルに `last_check` をそのまま
        載せられるよう、Worker 側の上位候補 (`diagnostics.candidates`) を保持。
        """
        if _diagnostics_reason(check_result) != "selector_ambiguous":
            return
        diag = check_result.get("diagnostics", {})
        match_count = diag.get("match_count")
        candidates = diag.get("candidates", [])
        # 例外メッセージには上位 3 件の (class, object_name) を埋めて即読み取れるようにする
        preview = ", ".join(
            f"{c.get('class')}(objectName={c.get('object_name')!r})" for c in candidates[:3]
        )
        logger.warning(
            "Selector ambiguous: selector=%s match_count=%s preview=%s",
            self._selector,
            match_count,
            preview,
        )
        raise SelectorAmbiguousError(
            f"Selector {self._selector!r} matched {match_count} widgets. "
            f"Top candidates: [{preview}]. "
            f"Add 'index' to disambiguate, or refine the selector "
            f"(e.g. add 'object_name' / 'root_object_name' / 'scope').",
            last_check=check_result,
        )
