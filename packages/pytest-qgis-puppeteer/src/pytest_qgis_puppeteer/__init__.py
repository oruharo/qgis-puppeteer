"""pytest-qgis-puppeteer: pytest plugin for E2E testing QGIS via qgis-puppeteer.

Public API:
    - ``expect(locator)`` — Playwright 流の web-first assertion
    - ``Locator`` — Playwright 流の widget locator（実体は ``locator`` モジュール）
    - ``WidgetNotActionableError`` / ``SelectorAmbiguousError`` — Locator が
      上げる例外
    - ``LiveOnlySelectorError`` — snapshot で探す API に、snapshot では判定
      できない selector のキー（``window`` / ``class_inherits``）を渡した
    - ``WidgetOperationError`` — click / 値の設定が失敗した（入った値が違う
      ことも含む）ときに上がる例外
    - ``QgisLeftoverWarning`` — ``qgis_leftover_warn`` の項目で、テストのあとに
      状態が残っていたときの警告
    - ``QgisUnusedDialogHandlerWarning`` — ``once`` の dialog handler が、一度も
      確認に答えずに外された
    - ``E2EAutomationClient`` — 同期版 AutomationClient ラッパ
    - ``ExecResult`` — ``execute_python_detailed`` の typed 戻り値
    - ``WorkerCodeError`` / ``ConfirmationRequiredError`` /
      ``NonSerializableResultError`` — ``execute_python`` が Worker 側コード例外 /
      confirm ゲート / 非直列化 result で上げる例外

pytest plugin は ``entry_points`` 経由で auto-load されるため、テストコードから
``import pytest_qgis_puppeteer.plugin`` する必要はない。fixture は自動で利用可能。
"""

from __future__ import annotations

from qgis_puppeteer.client import (
    ConfirmationRequiredError,
    NonSerializableResultError,
    WorkerCodeError,
)
from qgis_puppeteer.selector_match import LiveOnlySelectorError

from pytest_qgis_puppeteer._expect import expect
from pytest_qgis_puppeteer._leftover import QgisLeftoverWarning
from pytest_qgis_puppeteer._message_check import unexpected_messages
from pytest_qgis_puppeteer.automation_client import (
    E2EAutomationClient,
    ExecResult,
    QgisUnusedDialogHandlerWarning,
    WidgetOperationError,
)
from pytest_qgis_puppeteer.locator import (
    Locator,
    SelectorAmbiguousError,
    WidgetNotActionableError,
)
from pytest_qgis_puppeteer.plugin import QgisPluginMismatchWarning
from pytest_qgis_puppeteer.spawn import (
    SpawnedWorker,
    WorkerLaunchFailed,
    WorkerRegisterRejected,
    WorkerRegisterTimeout,
    spawn_qgis,
    spawn_qgis_fixture,
)

__all__ = [
    "ConfirmationRequiredError",
    "E2EAutomationClient",
    "ExecResult",
    "LiveOnlySelectorError",
    "Locator",
    "NonSerializableResultError",
    "SelectorAmbiguousError",
    "SpawnedWorker",
    "WidgetNotActionableError",
    "WidgetOperationError",
    "WorkerCodeError",
    "WorkerLaunchFailed",
    "WorkerRegisterRejected",
    "WorkerRegisterTimeout",
    "expect",
    "spawn_qgis",
    "unexpected_messages",
    "QgisLeftoverWarning",
    "QgisPluginMismatchWarning",
    "QgisUnusedDialogHandlerWarning",
    "spawn_qgis_fixture",
]
