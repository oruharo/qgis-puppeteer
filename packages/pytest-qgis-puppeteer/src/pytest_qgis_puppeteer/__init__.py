"""pytest-qgis-puppeteer: pytest plugin for E2E testing QGIS via qgis-puppeteer.

Public API:
    - ``expect(locator)`` — Playwright 流の web-first assertion
    - ``Locator`` — Playwright 流の widget locator（実体は ``locator`` モジュール）
    - ``WidgetNotActionableError`` / ``SelectorAmbiguousError`` — Locator が
      上げる例外
    - ``E2EAutomationClient`` — 同期版 AutomationClient ラッパ

pytest plugin は ``entry_points`` 経由で auto-load されるため、テストコードから
``import pytest_qgis_puppeteer.plugin`` する必要はない。fixture は自動で利用可能。
"""

from __future__ import annotations

from pytest_qgis_puppeteer._expect import expect
from pytest_qgis_puppeteer.automation_client import E2EAutomationClient
from pytest_qgis_puppeteer.locator import (
    Locator,
    SelectorAmbiguousError,
    WidgetNotActionableError,
)
from pytest_qgis_puppeteer.spawn import (
    SpawnedWorker,
    WorkerRegisterTimeout,
    spawn_qgis,
    spawn_qgis_fixture,
)

__all__ = [
    "E2EAutomationClient",
    "Locator",
    "SelectorAmbiguousError",
    "SpawnedWorker",
    "WidgetNotActionableError",
    "WorkerRegisterTimeout",
    "expect",
    "spawn_qgis",
    "spawn_qgis_fixture",
]
