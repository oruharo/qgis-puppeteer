"""信頼モードで走ったコードが QgsMessageLog に残ることを、本物の QGIS で確かめる。

QGIS の Python でしか走らない（`scripts/qt_tests.py`）。venv と CI では skip。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("qgis.core")

from tests._qgis_child import run_in_qgis  # noqa: E402

pytestmark = pytest.mark.integration

LONG_CODE = "x = 1\n# " + "a" * 300


@pytest.fixture(scope="module")
def executed() -> dict[str, Any]:
    return run_in_qgis(
        f"""
import os, tempfile
from qgis_puppeteer.qgis_tools import message_recorder as mr
from qgis_puppeteer.qgis_tools import python_executor as pe
from qgis_puppeteer.qgis_tools.permission_manager import PermissionManager

mr.install()
whitelist = os.path.join(tempfile.mkdtemp(), "whitelist.json")

os.environ["QPUPPETEER_TRUSTED_MODE"] = "1"
pe._permission_manager = PermissionManager(whitelist_path=whitelist)
trusted = pe.execute_python({LONG_CODE!r})

del os.environ["QPUPPETEER_TRUSTED_MODE"]
pe._permission_manager = PermissionManager(whitelist_path=whitelist)
confirmed = pe.execute_with_permission("y = 2", "once")

RESULT = {{
    "tag": pe.AUDIT_LOG_TAG,
    "trusted_ok": trusted["success"],
    "confirmed_ok": confirmed["success"],
    "all": mr.get_recent(),
    "info_or_above": mr.get_recent(min_level="info"),
}}
""",
    )


def test_both_runs_succeed(executed: dict[str, Any]) -> None:
    assert (executed["trusted_ok"], executed["confirmed_ok"]) == (True, True)


def test_trusted_run_is_recorded_with_the_first_200_characters(executed: dict[str, Any]) -> None:
    records = [m for m in executed["all"] if m["tag"] == executed["tag"]]
    assert [m["message"] for m in records] == ["[trusted mode] exec: " + LONG_CODE[:200]]
    assert records[0]["level"] == "none"


def test_record_does_not_count_for_min_level(executed: dict[str, Any]) -> None:
    """``qgis_fail_on_message_level = info`` のテストを execute_python で落とさない。"""
    assert not [m for m in executed["info_or_above"] if m["tag"] == executed["tag"]]


def test_confirmed_run_is_not_recorded(executed: dict[str, Any]) -> None:
    assert not [m for m in executed["all"] if "y = 2" in m["message"]]
