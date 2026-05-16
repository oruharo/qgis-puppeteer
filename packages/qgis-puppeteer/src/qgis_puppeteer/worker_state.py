"""Worker の純ロジック（Qt 非依存）。

ADR-0001 §4 "Worker 側" に基づき、register メッセージ構築・
Request → Response 変換・Disconnect 時の instance_id 引き継ぎ判断を
Qt 配線から分離し、ユニットテスト可能な形で提供する。

ADR-0001 §12 "ハンドラ拡張機構" に従い、ハンドラ登録は Tier（Core / Plugin /
Test）ごとに名前空間プレフィックスで分離し、外部からの登録は検証を通す:

- `qgis_*`     → Core（本パッケージ内の `qgis_tools.*` のみ登録可、外部禁止）
- `<plugin>.*` → Tier 2（外部 QGIS プラグイン固有機能、例: `extensions.*`）
- `test.*`     → Tier 3（`QPUPPETEER_ALLOW_TEST_HANDLERS=1` が必要）

## スレッドモデル

`WorkerState` 自体はロックを持たない。Qt 側（`worker.py`）が
textMessageReceived シグナルの逐次処理で順序を保証する。

## 設計方針

- ハンドラは **同期関数** のみ（QGIS API は Qt メインスレッド affine のため）
- ハンドラ例外は `WORKER_EXECUTION_ERROR` の Response として握りつぶす
  （Worker プロセス自体はクラッシュさせない）
- 不明なコマンドは `INVALID_COMMAND` で返す
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from qgis_puppeteer.protocol import (
    Bye,
    Error,
    ErrorCode,
    RegisterAck,
    RegisterRequest,
    Request,
    Response,
    Role,
)

logger = logging.getLogger("qgis_puppeteer.worker_state")

# Worker が受理するハンドラの型：params dict を受け取り任意の JSON 値を返す。
# 例外を投げた場合は自動的にエラー Response に変換される。
Handler = Callable[[dict[str, Any]], Any]

# ADR-0001 §12.4 env 分離（§9 の TRUSTED_MODE とは別系統）。
# このフラグは `test.*` ハンドラ登録の許可のみを制御する。
ENV_ALLOW_TEST_HANDLERS = "QPUPPETEER_ALLOW_TEST_HANDLERS"

# Core 予約 namespace：外部から register_handler で登録できない。
# 本パッケージ内部は `register_core_handler` 経由で登録する。
_RESERVED_NAMESPACE_PREFIX = "qgis_"
# Test namespace：env チェックが必要。
_TEST_NAMESPACE_PREFIX = "test."


def _is_test_namespace(command: str) -> bool:
    return command.startswith(_TEST_NAMESPACE_PREFIX)


def _is_reserved_namespace(command: str) -> bool:
    return command.startswith(_RESERVED_NAMESPACE_PREFIX)


def _allow_test_handlers() -> bool:
    """`QPUPPETEER_ALLOW_TEST_HANDLERS=1` が設定されているか。"""
    return os.environ.get(ENV_ALLOW_TEST_HANDLERS) == "1"


# ============================================================
# 例外
# ============================================================


class WorkerRegisterError(Exception):
    """register_ack が ok=False を返した。"""

    def __init__(self, error: Error | None) -> None:
        self.error = error
        msg = f"worker register failed: {error.code.value if error else 'unknown'}"
        if error:
            msg += f" ({error.message})"
        super().__init__(msg)


class HandlerRegistrationError(Exception):
    """ハンドラ登録失敗の基底。`code` に対応 ErrorCode を持つ（ADR-0001 §12.6）。"""

    code: ErrorCode = ErrorCode.REGISTRATION_DENIED

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ReservedNamespaceError(HandlerRegistrationError):
    """`qgis_*` など予約 namespace への外部登録試行。"""

    code = ErrorCode.RESERVED_NAMESPACE


class RegistrationDeniedError(HandlerRegistrationError):
    """権限不足で登録拒否（`test.*` への env 未設定時など）。"""

    code = ErrorCode.REGISTRATION_DENIED


class HandlerAlreadyRegisteredError(HandlerRegistrationError):
    """上書き禁止時に同名ハンドラの再登録を試行した。"""

    code = ErrorCode.HANDLER_ALREADY_REGISTERED


# ============================================================
# データ型
# ============================================================


@dataclass
class WorkerConfig:
    """Worker の自己申告情報。"""

    pid: int
    label: str | None = None
    project: str | None = None
    # ADR-0005 D6: 公式 launch helper が QPUPPETEER_LAUNCH_TOKEN 経由で
    # 注入する相関トークン。helper 非経由起動なら None。
    launch_token: str | None = None
    # ADR-0005 D4: active 同 label 衝突ポリシー（"takeover"/"suffix"/None=reject）。
    conflict_policy: str | None = None


# ============================================================
# WorkerState
# ============================================================


@dataclass
class WorkerState:
    """Worker の純ロジック状態。

    Qt 配線側（`worker.py`）はこのクラスの以下メソッドを呼び出して
    WebSocket 送信メッセージを得る：

    - `build_register(msg_id, ...)` → 接続直後の登録メッセージ
    - `on_register_ack(ack)` → instance_id を受理
    - `on_request(req)` → Response を合成して返却
    - `on_disconnect()` → 切断時に previous_instance_id を保全
    - `build_bye(msg_id)` → 明示的終了時の Bye メッセージ（active 時のみ）
    """

    config: WorkerConfig
    handlers: dict[str, Handler] = field(default_factory=dict)

    # register_ack で確定する。未登録なら None
    instance_id: str | None = None
    # 切断後、次回接続で resume を試みるために保持する
    previous_instance_id: str | None = None

    # ------------------------------------------------------------
    # ハンドラ管理（ADR-0001 §12）
    # ------------------------------------------------------------

    def register_core_handler(
        self,
        command: str,
        handler: Handler,
        *,
        allow_overwrite: bool = False,
    ) -> None:
        """Core（Tier 1）ハンドラを登録する。`qgis_*` のみ許可。

        本パッケージ内部（`qgis_tools.*`）からの登録専用で、namespace 予約
        チェックをバイパスする。外部 Plugin / Test からは呼ばない。
        """
        if not _is_reserved_namespace(command):
            raise ReservedNamespaceError(
                f"register_core_handler requires a '{_RESERVED_NAMESPACE_PREFIX}' prefix: "
                f"got {command!r}"
            )
        if command in self.handlers and not allow_overwrite:
            raise HandlerAlreadyRegisteredError(
                f"handler {command!r} is already registered; pass allow_overwrite=True to replace"
            )
        self.handlers[command] = handler

    def register_handler(
        self,
        command: str,
        handler: Handler,
        *,
        allow_overwrite: bool = False,
    ) -> None:
        """外部（Tier 2 Plugin / Tier 3 Test）からのハンドラ登録。

        ADR-0001 §12.4 に従い、以下を検証する:

        - `qgis_*` は予約 namespace → `ReservedNamespaceError`
        - `test.*` は `QPUPPETEER_ALLOW_TEST_HANDLERS=1` 必須 → なければ
          `RegistrationDeniedError`
        - 同名既存ハンドラは既定で拒否（`HandlerAlreadyRegisteredError`）、
          `allow_overwrite=True` で上書き可能

        Raises:
            ReservedNamespaceError: `qgis_*` への登録試行
            RegistrationDeniedError: `test.*` への env 未設定での登録試行
            HandlerAlreadyRegisteredError: 上書き禁止時の再登録
        """
        if _is_reserved_namespace(command):
            raise ReservedNamespaceError(
                f"namespace '{_RESERVED_NAMESPACE_PREFIX}*' is reserved for core handlers"
            )
        if _is_test_namespace(command) and not _allow_test_handlers():
            raise RegistrationDeniedError(
                f"registering '{_TEST_NAMESPACE_PREFIX}*' handlers requires "
                f"{ENV_ALLOW_TEST_HANDLERS}=1"
            )
        if command in self.handlers and not allow_overwrite:
            raise HandlerAlreadyRegisteredError(
                f"handler {command!r} is already registered; pass allow_overwrite=True to replace"
            )
        self.handlers[command] = handler

    def unregister_handler(self, command: str) -> None:
        """command 文字列のハンドラを削除する（存在しなければ何もしない）。"""
        self.handlers.pop(command, None)

    # ------------------------------------------------------------
    # メッセージ構築・処理
    # ------------------------------------------------------------

    def build_register(self, msg_id: str) -> RegisterRequest:
        """Hub に送る register メッセージを組み立てる。

        `previous_instance_id` がセットされていれば resume 要求として送られる。
        """
        return RegisterRequest(
            id=msg_id,
            role=Role.WORKER,
            pid=self.config.pid,
            label=self.config.label,
            project=self.config.project,
            previous_instance_id=self.previous_instance_id,
            launch_token=self.config.launch_token,
            conflict_policy=self.config.conflict_policy,
        )

    def on_register_ack(self, ack: RegisterAck) -> None:
        """Hub からの register_ack を処理する。

        成功時は instance_id を記録、失敗時は例外を投げる。
        成功したら previous_instance_id はクリア（もう使わない）。
        """
        if not ack.ok:
            raise WorkerRegisterError(ack.error)
        self.instance_id = ack.instance_id
        self.previous_instance_id = None

    def on_request(self, req: Request) -> Response:
        """Request を適切なハンドラに dispatch して Response を合成する。

        - 未知コマンド → INVALID_COMMAND
        - ハンドラ例外 → WORKER_EXECUTION_ERROR（例外型・メッセージを保持）
        - `test.*` を `AUTOMATION_CLIENT` 以外から呼んだ場合 → HANDLER_NOT_VISIBLE
          （ADR-0001 §12.7 caller_role による Tier 3 隔離）
        """
        # caller_role による Tier 3 可視性チェック（ADR-0001 §12.9）。
        # プロトコル version 1 では Hub が必ず caller_role を注入するので、
        # `caller_role=None` は正常系では発生しない（Hub が介さない直接接続等の
        # 異常系のみ）。fail-closed で `test.*` は明示的に AUTOMATION_CLIENT
        # の場合のみ許可し、None も MCP_GATEWAY も実行不可。
        if _is_test_namespace(req.command) and req.caller_role is not Role.AUTOMATION_CLIENT:
            return Response(
                id=req.id,
                ok=False,
                error=Error(
                    code=ErrorCode.HANDLER_NOT_VISIBLE,
                    message=(
                        f"handler {req.command!r} is only visible to automation_client callers"
                    ),
                    details={
                        "command": req.command,
                        "caller_role": (req.caller_role.value if req.caller_role else None),
                    },
                ),
            )

        handler = self.handlers.get(req.command)
        if handler is None:
            return Response(
                id=req.id,
                ok=False,
                error=Error(
                    code=ErrorCode.INVALID_COMMAND,
                    message=f"No handler for command {req.command!r}",
                    details={"command": req.command},
                ),
            )

        try:
            result = handler(req.params)
        except Exception as e:
            return Response(
                id=req.id,
                ok=False,
                error=Error(
                    code=ErrorCode.WORKER_EXECUTION_ERROR,
                    message=str(e),
                    details={"type": type(e).__name__},
                ),
            )

        return Response(id=req.id, ok=True, result=result)

    # ------------------------------------------------------------
    # 切断・終了
    # ------------------------------------------------------------

    def on_disconnect(self) -> None:
        """切断時に呼ばれる。次回 register で resume 要求するため
        現在の instance_id を previous_instance_id に移し替える。

        未登録（instance_id is None）の切断では何もしない。
        """
        if self.instance_id is not None:
            self.previous_instance_id = self.instance_id
            self.instance_id = None

    def build_bye(self, msg_id: str) -> Bye | None:
        """明示的終了時の Bye メッセージを組み立てる。

        未登録状態（instance_id is None）では Bye を送る必要がないため
        None を返す（呼び出し側は送信をスキップ）。
        """
        if self.instance_id is None:
            return None
        return Bye(id=msg_id, instance_id=self.instance_id)

    # ------------------------------------------------------------
    # 状態クエリ
    # ------------------------------------------------------------

    @property
    def is_registered(self) -> bool:
        return self.instance_id is not None
