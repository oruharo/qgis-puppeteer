"""
Python Code Executor with 3-Tier Permission System

This module provides safe Python code execution with:
- Level 0: Whitelist (always allowed)
- Level 1: Session permission (allowed for current session)
- Level 2: Confirmation required (ask before execution)
"""

import io
import logging
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from qgis.gui import QgisInterface

from .code_analyzer import CodeAnalyzer
from .permission_manager import PermissionManager

logger = logging.getLogger("qgis_puppeteer.python_executor")

# Global permission manager instance
_permission_manager: PermissionManager | None = None


def get_permission_manager() -> PermissionManager:
    """Get the global permission manager instance.

    Returns:
        PermissionManager instance
    """
    global _permission_manager
    if _permission_manager is None:
        _permission_manager = PermissionManager()
    return _permission_manager


def execute_python(code: str, iface: QgisInterface | None = None) -> dict:
    """Execute Python code with 3-tier permission system.

    Args:
        code: Python code to execute
        iface: QGIS interface instance (optional, for GUI mode)

    Returns:
        Dictionary with execution results:
        - success: Whether execution was successful
        - requires_confirmation: Whether confirmation is required
        - risk_level: Risk level of the code
        - stdout: Standard output
        - stderr: Standard error
        - result: Return value (if any)
        - error: Error message (if failed)
        - permission_level: Permission level used
    """
    # Get permission manager
    pm = get_permission_manager()

    # Check permission
    permission = pm.check_permission(code)

    # Analyze code
    analysis = CodeAnalyzer.analyze(code)

    # If confirmation required, return analysis and wait for user decision
    if permission == "confirm":
        return {
            "success": False,
            "requires_confirmation": True,
            "risk_level": analysis["risk_level"],
            "analysis": analysis,
            "permission_level": "confirm",
            "message": "Code requires confirmation before execution",
            "code": code,
        }

    # Permission granted, execute code
    return _execute_code_internal(code, iface, permission, analysis)


def execute_with_permission(
    code: str,
    permission_choice: str,
    iface: QgisInterface | None = None,
) -> dict:
    """Execute code after user has made permission choice.

    Args:
        code: Python code to execute
        permission_choice: User's choice ("once", "session", "always", "cancel")
        iface: QGIS interface instance (optional)

    Returns:
        Dictionary with execution results
    """
    if permission_choice == "cancel":
        return {
            "success": False,
            "cancelled": True,
            "message": "Execution cancelled by user",
        }

    # Get permission manager
    pm = get_permission_manager()

    # Apply permission choice
    if permission_choice == "always":
        pm.add_to_whitelist(code)
        permission_level = "whitelist"
    elif permission_choice == "session":
        pm.add_to_session(code)
        permission_level = "session"
    elif permission_choice == "once":
        permission_level = "once"
    else:
        return {
            "success": False,
            "error": f"Invalid permission choice: {permission_choice}",
        }

    # Analyze and execute
    analysis = CodeAnalyzer.analyze(code)
    return _execute_code_internal(code, iface, permission_level, analysis)


def _execute_code_internal(
    code: str,
    iface: QgisInterface | None,
    permission_level: str,
    analysis: dict,
) -> dict:
    """Internal method to execute code.

    Args:
        code: Python code to execute
        iface: QGIS interface instance (optional)
        permission_level: Permission level used
        analysis: Code analysis results

    Returns:
        Dictionary with execution results
    """
    # Prepare execution context
    context = _prepare_context(iface)

    # 信頼モードで走る際は監査ログに残す（ADR-0002 §9.3）。
    # マスキング規則は将来の検討項目。現状は先頭 200 文字を素のまま出す。
    if get_permission_manager().trusted_mode:
        logger.info("[trusted mode] exec: %s", code[:200])

    # Capture stdout/stderr
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()

    try:
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            # Execute code
            exec(code, context)

        # Extract result if available
        result = context.get("_result", None)

        return {
            "success": True,
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
            "result": str(result) if result is not None else None,
            "permission_level": permission_level,
            "risk_level": analysis["risk_level"],
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
            "permission_level": permission_level,
            "risk_level": analysis["risk_level"],
        }


def _prepare_context(iface: QgisInterface | None) -> dict[str, Any]:
    """Prepare execution context with QGIS objects.

    Args:
        iface: QGIS interface instance (optional)

    Returns:
        Dictionary with execution context
    """
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsExpression,
        QgsFeature,
        QgsFeatureRequest,
        QgsGeometry,
        QgsPointXY,
        QgsProject,
        QgsVectorLayer,
    )

    context = {
        # QGIS Core
        "QgsProject": QgsProject,
        "QgsVectorLayer": QgsVectorLayer,
        "QgsFeature": QgsFeature,
        "QgsGeometry": QgsGeometry,
        "QgsPointXY": QgsPointXY,
        "QgsFeatureRequest": QgsFeatureRequest,
        "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem,
        "QgsExpression": QgsExpression,
        # Built-ins
        "print": print,
        "len": len,
        "str": str,
        "int": int,
        "float": float,
        "list": list,
        "dict": dict,
        "set": set,
        "tuple": tuple,
        "range": range,
        "enumerate": enumerate,
        "zip": zip,
        "map": map,
        "filter": filter,
        "sorted": sorted,
        "sum": sum,
        "min": min,
        "max": max,
        "abs": abs,
        "round": round,
    }

    # Add iface if available (GUI mode)
    if iface is not None:
        context["iface"] = iface

    return context
