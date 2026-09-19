"""Permission Manager for Python Code Execution.

This module implements a 3-tier permission system:
- Level 0: Whitelist (always allowed)
- Level 1: Session permission (allowed for current session only)
- Level 2: Confirmation required (ask every time)

ADR-0002 §9: `QPUPPETEER_TRUSTED_MODE=1` が設定されている場合は
`check_permission` が常に `"whitelist"` を返す（E2E テスト用）。
`QPUPPETEER_ALLOW_TEST_HANDLERS` とは役割分離されており、
本 env は execute_python の permission bypass のみを制御する。
"""

import json
import logging
import os
from pathlib import Path
from typing import Literal

logger = logging.getLogger("qgis_puppeteer.permission_manager")

PermissionLevel = Literal["whitelist", "session", "confirm"]

# 信頼モード env（ADR-0002 §9）。`QPUPPETEER_ALLOW_TEST_HANDLERS`（§12.7）
# とは別系統で、execute_python の confirm フロー bypass のみを制御する。
ENV_TRUSTED_MODE = "QPUPPETEER_TRUSTED_MODE"


def _read_trusted_mode() -> bool:
    """`QPUPPETEER_TRUSTED_MODE=1` が設定されているか。"""
    return os.environ.get(ENV_TRUSTED_MODE) == "1"


class PermissionManager:
    """Manages code execution permissions with 3-tier system."""

    def __init__(self, whitelist_path: str | None = None) -> None:
        """Initialize the permission manager.

        Args:
            whitelist_path: Path to whitelist JSON file
                          (default: .claude/qgis_whitelist.json)
        """
        if whitelist_path is None:
            # Default path relative to project root
            project_root = self._find_project_root()
            whitelist_path = os.path.join(project_root, ".claude", "qgis_whitelist.json")

        self.whitelist_path = whitelist_path
        self.whitelist: set[str] = self._load_whitelist()
        self.session_allowed: set[str] = set()
        # 信頼モード（ADR-0002 §9）：初期化時点の env を見て固定。
        # 途中で env を書き換えても影響しない（プロセス起動時に決まる）。
        self._trusted_mode: bool = _read_trusted_mode()
        if self._trusted_mode:
            logger.warning(
                "%s=1 detected; all code execution bypasses confirmation flow",
                ENV_TRUSTED_MODE,
            )

    def _find_project_root(self) -> str:
        """Find the project root directory.

        Returns:
            Path to project root
        """
        # Start from current file and go up
        current = Path(__file__).parent
        while current != current.parent:
            if (current / ".git").exists() or (current / "pyproject.toml").exists():
                return str(current)
            current = current.parent

        # Fallback to current directory
        return os.getcwd()

    def _load_whitelist(self) -> set[str]:
        """Load whitelist from JSON file.

        Returns:
            Set of whitelisted code patterns
        """
        if not os.path.exists(self.whitelist_path):
            return set()

        try:
            with open(self.whitelist_path, encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("always_allowed", []))
        except (OSError, json.JSONDecodeError) as e:
            # OSError: 読み込み不可（権限エラーなど）、JSONDecodeError: ファイル破損
            # いずれも安全側倒しで空 whitelist にフォールバック
            logger.error("Failed to load whitelist from %s: %s", self.whitelist_path, e)
            return set()

    def _save_whitelist(self) -> None:
        """Save whitelist to JSON file."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.whitelist_path), exist_ok=True)

            # Save whitelist
            with open(self.whitelist_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "always_allowed": sorted(self.whitelist),
                        "_comment": "Code patterns that are always allowed without confirmation",
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except OSError as e:
            # 書き込み不可（権限・容量不足など）。呼び出し側はセッション中の
            # session_allowed で動作継続できるので、致命的にはしない。
            logger.error("Failed to save whitelist to %s: %s", self.whitelist_path, e)

    def check_permission(self, code: str) -> PermissionLevel:
        """Check if code is allowed to execute.

        Args:
            code: Python code to check

        Returns:
            Permission level: "whitelist", "session", or "confirm"
        """
        # 信頼モード（ADR-0002 §9）は全コードを whitelist 扱いで即実行。
        # pytest の E2E fixture で subprocess 限定で付与される想定。
        if self._trusted_mode:
            return "whitelist"

        # Normalize code (remove leading/trailing whitespace)
        normalized_code = code.strip()

        # Level 0: Check whitelist
        if normalized_code in self.whitelist:
            return "whitelist"

        # Level 1: Check session permission
        if normalized_code in self.session_allowed:
            return "session"

        # Level 2: Confirmation required
        return "confirm"

    @property
    def trusted_mode(self) -> bool:
        """`QPUPPETEER_TRUSTED_MODE=1` が初期化時に設定されていたか。"""
        return self._trusted_mode

    def add_to_whitelist(self, code: str) -> None:
        """Add code to permanent whitelist.

        Args:
            code: Python code to whitelist
        """
        normalized_code = code.strip()
        self.whitelist.add(normalized_code)
        self._save_whitelist()

    def add_to_session(self, code: str) -> None:
        """Add code to session-only permission.

        Args:
            code: Python code to allow for this session
        """
        normalized_code = code.strip()
        self.session_allowed.add(normalized_code)

    def remove_from_whitelist(self, code: str) -> None:
        """Remove code from permanent whitelist.

        Args:
            code: Python code to remove
        """
        normalized_code = code.strip()
        self.whitelist.discard(normalized_code)
        self._save_whitelist()

    def clear_session(self) -> None:
        """Clear all session permissions."""
        self.session_allowed.clear()

    def is_whitelisted(self, code: str) -> bool:
        """Check if code is in permanent whitelist.

        Args:
            code: Python code to check

        Returns:
            True if whitelisted, False otherwise
        """
        return code.strip() in self.whitelist

    def is_session_allowed(self, code: str) -> bool:
        """Check if code is allowed for current session.

        Args:
            code: Python code to check

        Returns:
            True if session-allowed, False otherwise
        """
        return code.strip() in self.session_allowed

    def get_whitelist(self) -> list[str]:
        """Get all whitelisted code patterns.

        Returns:
            List of whitelisted code patterns
        """
        return sorted(self.whitelist)

    def get_session_allowed(self) -> list[str]:
        """Get all session-allowed code patterns.

        Returns:
            List of session-allowed code patterns
        """
        return sorted(self.session_allowed)
