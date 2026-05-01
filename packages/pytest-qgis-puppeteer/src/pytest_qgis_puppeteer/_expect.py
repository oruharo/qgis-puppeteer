"""Web-first assertions（Playwright 流の retry 内蔵 assertion）。

ADR-0002 §12 で計画されていた `expect(locator).to_*` を提供する。
通常の `assert locator.is_visible()` は 1-shot で flaky だが、
``expect(locator).to_be_visible()`` は内部で poll して条件を満たすまで待つ
（既定 5s × 250ms 間隔、`Locator.auto-wait` と同じ周期）。

## 提供メソッド

| メソッド | 用途 |
|---|---|
| `to_be_visible()` / `to_be_hidden()` | 表示状態 |
| `to_be_enabled()` / `to_be_disabled()` | 操作可能状態 |
| `to_have_text(expected)` | `get_text()` が完全一致 |
| `to_contain_text(substring)` | `get_text()` に部分一致 |
| `to_have_value(expected)` | `get_value()` が一致 |
| `to_be_checked()` / `not_.to_be_checked()` | check 状態 |

## not_ プロパティ

`expect(loc).not_.to_be_visible()` で否定形（=「非表示になるまで待つ」）。
内部で polling 条件を反転する。

## strict mode との関係

selector が複数マッチする場合、`Locator._wait_actionable` 同様に
:class:`SelectorAmbiguousError` を上げて即時 abort する（poll を続けても
候補数は減らないため）。

## タイムアウト

各メソッドは ``timeout_s`` で個別指定可能。未指定なら ``expect()`` 生成時の
既定値（5.0 秒）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pytest_qgis_puppeteer.locator import (
    SelectorAmbiguousError,
    _diagnostics_reason,
)

if TYPE_CHECKING:
    from pytest_qgis_puppeteer.locator import Locator

logger = logging.getLogger("pytest_qgis_puppeteer.expect")

# Locator と揃える（ADR-0002 §8.3 推奨値）
_DEFAULT_TIMEOUT_S = 5.0
_DEFAULT_POLL_INTERVAL_S = 0.25


class _LocatorAssertions:
    """`expect(locator)` から返される assertion ハンドル。

    各 `to_*` メソッドは内部で poll を回し、条件を満たすまで待つ。timeout 時は
    ``AssertionError``、selector 多重マッチ時は :class:`SelectorAmbiguousError`。
    """

    def __init__(
        self,
        locator: Locator,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        negated: bool = False,
    ) -> None:
        self._locator = locator
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s
        self._negated = negated

    @property
    def not_(self) -> _LocatorAssertions:
        """否定形の assertion ハンドルを返す。

        例: ``expect(loc).not_.to_be_visible()`` → 非表示になるまで poll。
        """
        return _LocatorAssertions(
            self._locator,
            timeout_s=self._timeout_s,
            poll_interval_s=self._poll_interval_s,
            negated=not self._negated,
        )

    # ------------------------------------------------------------
    # 状態系（actionability check 経由）
    # ------------------------------------------------------------

    def to_be_visible(self, *, timeout_s: float | None = None) -> None:
        """widget が visible になるまで poll。"""
        self._poll_actionability("visible", True, "to_be_visible", timeout_s)

    def to_be_hidden(self, *, timeout_s: float | None = None) -> None:
        """widget が visible でなくなるまで poll（消えてもよい）。"""
        self._poll_actionability("visible", False, "to_be_hidden", timeout_s)

    def to_be_enabled(self, *, timeout_s: float | None = None) -> None:
        """widget が enabled になるまで poll。"""
        self._poll_actionability("enabled", True, "to_be_enabled", timeout_s)

    def to_be_disabled(self, *, timeout_s: float | None = None) -> None:
        """widget が disabled になるまで poll。"""
        self._poll_actionability("enabled", False, "to_be_disabled", timeout_s)

    # ------------------------------------------------------------
    # 値系（snapshot 経由）
    # ------------------------------------------------------------

    def to_have_text(self, expected: str, *, timeout_s: float | None = None) -> None:
        """``locator.get_text()`` が ``expected`` と完全一致するまで poll。"""
        self._poll_value(
            self._locator.get_text,
            lambda v: v == expected,
            "to_have_text",
            expected,
            timeout_s,
        )

    def to_contain_text(self, substring: str, *, timeout_s: float | None = None) -> None:
        """``locator.get_text()`` が ``substring`` を含むまで poll。"""
        self._poll_value(
            self._locator.get_text,
            lambda v: isinstance(v, str) and substring in v,
            "to_contain_text",
            substring,
            timeout_s,
        )

    def to_have_value(self, expected: Any, *, timeout_s: float | None = None) -> None:
        """``locator.get_value()`` が ``expected`` と一致するまで poll。"""
        self._poll_value(
            self._locator.get_value,
            lambda v: v == expected,
            "to_have_value",
            expected,
            timeout_s,
        )

    def to_be_checked(self, *, timeout_s: float | None = None) -> None:
        """checkable widget が True 状態になるまで poll。"""
        self._poll_value(
            self._locator.get_value,
            lambda v: bool(v) is True,
            "to_be_checked",
            True,
            timeout_s,
        )

    # ------------------------------------------------------------
    # 内部: polling
    # ------------------------------------------------------------

    def _poll_actionability(
        self,
        field: str,
        expected: bool,
        name: str,
        timeout_s: float | None,
    ) -> None:
        """`check_actionability` の特定 bool フィールド (visible / enabled) を poll。"""
        effective_timeout = self._timeout_s if timeout_s is None else float(timeout_s)
        deadline = time.monotonic() + effective_timeout
        last_check: dict[str, Any] = {}

        while True:
            last_check = self._locator._check_actionability()
            self._abort_if_ambiguous(last_check)
            value = bool(last_check.get(field, False))
            matched = value == expected
            # XOR: 通常は matched なら成功、negated なら not matched が成功
            if matched != self._negated:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(self._poll_interval_s)

        self._raise_assertion(name, expected, last_check, effective_timeout)

    def _poll_value(
        self,
        getter: Callable[[], Any],
        condition: Callable[[Any], bool],
        name: str,
        expected: Any,
        timeout_s: float | None,
    ) -> None:
        """任意の getter + condition を poll（snapshot 経由 get_text/get_value 用）。

        ambiguous の即時 abort のために `check_actionability` も毎回呼ぶ — ただし
        snapshot 経由の getter とは別経路なので、snapshot 側が ambiguous で None
        を返してもまだ「未存在」と区別できないため、actionability check で
        ambiguous を検出して early-fail する設計にする。
        """
        effective_timeout = self._timeout_s if timeout_s is None else float(timeout_s)
        deadline = time.monotonic() + effective_timeout
        last_value: Any = None
        last_check: dict[str, Any] = {}

        while True:
            # ambiguous の早期検出
            last_check = self._locator._check_actionability()
            self._abort_if_ambiguous(last_check)

            try:
                last_value = getter()
            except Exception as exc:  # noqa: BLE001 — getter は client.snapshot_ui を通る
                logger.debug("getter raised in expect poll: %s", exc)
                last_value = None

            matched = condition(last_value)
            if matched != self._negated:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(self._poll_interval_s)

        # AssertionError に値の最終結果を埋めておく
        self._raise_assertion(
            name,
            expected,
            {"last_value": last_value, "actionability": last_check},
            effective_timeout,
        )

    def _abort_if_ambiguous(self, check_result: dict[str, Any]) -> None:
        if _diagnostics_reason(check_result) != "selector_ambiguous":
            return
        diag = check_result.get("diagnostics", {})
        match_count = diag.get("match_count")
        candidates = diag.get("candidates", [])
        preview = ", ".join(
            f"{c.get('class')}(objectName={c.get('object_name')!r})" for c in candidates[:3]
        )
        raise SelectorAmbiguousError(
            f"Selector {self._locator._selector!r} matched {match_count} widgets. "
            f"Top candidates: [{preview}]. expect() requires a unique match.",
            last_check=check_result,
        )

    def _raise_assertion(
        self,
        name: str,
        expected: Any,
        last: dict[str, Any],
        timeout_s: float,
    ) -> None:
        not_str = "not_." if self._negated else ""
        raise AssertionError(
            f"expect({self._locator._selector!r}).{not_str}{name}("
            f"{expected!r}): timed out after {timeout_s}s. last={last!r}"
        )


def expect(
    locator: Locator,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> _LocatorAssertions:
    """Playwright 流の web-first assertion handle を生成する。

    Args:
        locator: 対象 :class:`pytest_qgis_puppeteer.locator.Locator`。
        timeout_s: assertion の既定 timeout（秒）。各 ``to_*`` で個別 override 可。
        poll_interval_s: poll 間隔（秒）。

    Returns:
        :class:`_LocatorAssertions` — ``to_be_visible`` 等のメソッドを生やす。

    Example:
        >>> expect(qgis.locator({"object_name": "status"})).to_have_text("OK")
        >>> expect(qgis.locator({"object_name": "spinner"})).not_.to_be_visible()
    """
    return _LocatorAssertions(
        locator,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    )
