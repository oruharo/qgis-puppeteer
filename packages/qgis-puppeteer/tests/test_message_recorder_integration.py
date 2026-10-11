"""`message_recorder` を本物の QgsMessageLog / メッセージバーで確かめる（#5）。

QGIS の Python でしか走らない（`scripts/qt_tests.py`）。venv と CI では skip。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("qgis.core")

from tests._qgis_child import run_in_qgis  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def logged() -> dict[str, Any]:
    return run_in_qgis(
        """
import threading
from qgis.core import Qgis, QgsMessageLog
from qgis.gui import QgsMessageBar
from qgis_puppeteer.qgis_tools import message_recorder as mr

first = mr.install()
second = mr.install()

QgsMessageLog.logMessage("boom", "MyPlugin", Qgis.Critical)
QgsMessageLog.logMessage("all good", "Processing", Qgis.Success)
QgsMessageLog.logMessage("fyi", "MyPlugin", Qgis.Info)

bar = QgsMessageBar()
bar.pushMessage("Title", "shown to the user", Qgis.Warning)

t = threading.Thread(target=lambda: QgsMessageLog.logMessage("from a thread", "Bg", Qgis.Warning))
t.start()
t.join()
for _ in range(20):
    _app.processEvents()

RESULT = {
    "first": first,
    "second": second,
    "all": mr.get_recent(),
    "critical": mr.get_recent(min_level="critical"),
    "bar": mr.get_recent(sources=["message_bar"]),
}
""",
        gui=True,
    )


def test_install_is_idempotent(logged: dict[str, Any]) -> None:
    assert (logged["first"], logged["second"]) == (True, False)


def test_messages_are_recorded_with_level_and_tag(logged: dict[str, Any]) -> None:
    by_message = {m["message"]: m for m in logged["all"]}
    assert by_message["boom"]["level"] == "critical"
    assert by_message["boom"]["tag"] == "MyPlugin"
    assert by_message["all good"]["level"] == "success"
    assert by_message["fyi"]["level"] == "info"


def test_success_does_not_count_as_critical(logged: dict[str, Any]) -> None:
    assert [m["message"] for m in logged["critical"]] == ["boom"]


def test_message_bar_pushes_are_recorded_as_such(logged: dict[str, Any]) -> None:
    """QgsMessageBar は表示したメッセージをタグ "Messages" でログにも書く。"""
    [bar] = logged["bar"]
    assert bar["message"] == "Title : shown to the user"
    assert bar["level"] == "warning"


def test_messages_from_other_threads_are_recorded(logged: dict[str, Any]) -> None:
    assert any(m["message"] == "from a thread" for m in logged["all"])
