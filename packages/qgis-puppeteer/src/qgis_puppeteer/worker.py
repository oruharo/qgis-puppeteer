"""Worker プロセス本体（Qt 配線 + ライフサイクル管理）。

ADR-0001 §4 "Worker 側" に基づき、`QWebSocket` で Hub に逆向き接続し、
`WorkerState` に委譲しながら request を handler に dispatch する。

## 責務

- Hub に dial out（`QWebSocket.open`）し register を送る
- register_ack で instance_id を受理（`registered` シグナル発火）
- request を handler に dispatch して response を返送
- 切断時に auto-reconnect（`QTimer` で遅延再試行）
- 明示的終了時は `bye` を送ってクリーンに閉じる

## スレッドモデル

QGIS プラグインから呼ばれる想定で、単一 Qt メインスレッド上で動作。
handler は同期関数で、呼び出し中は Qt イベントループがブロックされる点に注意
（長時間処理は Worker 側で QThread 等を使うべき）。
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence
from pathlib import Path

from PyQt5.QtCore import (  # type: ignore[import-not-found]
    QObject,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt5.QtNetwork import QAbstractSocket  # type: ignore[import-not-found]
from PyQt5.QtWebSockets import (  # type: ignore[import-not-found]
    QWebSocket,
    QWebSocketProtocol,
)

from qgis_puppeteer.hub_spawn import (
    HubStartupError,
    ensure_hub_reachable,
)
from qgis_puppeteer.protocol import (
    Error,
    ErrorCode,
    ProtocolDecodeError,
    RegisterAck,
    Request,
    Response,
    decode_message,
    encode_message,
)
from qgis_puppeteer.worker_state import (
    Handler,
    WorkerConfig,
    WorkerRegisterError,
    WorkerState,
)

logger = logging.getLogger("qgis_puppeteer.worker")

DEFAULT_HUB_URL: str = "ws://127.0.0.1:9876"
DEFAULT_ORIGIN: str = "http://localhost"
DEFAULT_RECONNECT_DELAY_MS: int = 1_000


# ============================================================
# Worker 本体
# ============================================================


class Worker(QObject):
    """Hub に接続して Request を handler に dispatch する Qt クライアント。

    Signals:
        registered(str): register_ack 成功時に instance_id を載せて発火
        unregistered(): 切断時（grace 期間への移行）
        register_failed(str): register_ack が ok=False の場合（非復帰）
        hub_startup_failed(str): auto_spawn 有効時に Hub 起動が失敗した場合
    """

    registered = pyqtSignal(str)
    unregistered = pyqtSignal()
    register_failed = pyqtSignal(str)
    hub_startup_failed = pyqtSignal(str)

    def __init__(
        self,
        *,
        hub_url: str = DEFAULT_HUB_URL,
        origin: str = DEFAULT_ORIGIN,
        pid: int | None = None,
        label: str | None = None,
        project: str | None = None,
        launch_token: str | None = None,
        conflict_policy: str | None = None,
        auto_reconnect: bool = True,
        reconnect_delay_ms: int = DEFAULT_RECONNECT_DELAY_MS,
        hub_lock_path: Path | None = None,
        hub_spawn_command: Sequence[str] | None = None,
        hub_spawn_env: dict[str, str] | None = None,
        hub_log_file: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._hub_url = QUrl(hub_url)
        self._origin = origin
        self._auto_reconnect = auto_reconnect
        self._reconnect_delay_ms = reconnect_delay_ms
        self._hub_lock_path = hub_lock_path
        self._hub_spawn_command = (
            tuple(hub_spawn_command) if hub_spawn_command is not None else None
        )
        # subprocess に渡す環境変数（PYTHONPATH など）。None なら親環境を継承。
        self._hub_spawn_env = dict(hub_spawn_env) if hub_spawn_env is not None else None
        self._hub_log_file = hub_log_file

        self._state = WorkerState(
            config=WorkerConfig(
                pid=pid if pid is not None else os.getpid(),
                label=label,
                project=project,
                launch_token=launch_token,
                conflict_policy=conflict_policy,
            )
        )

        # QWebSocket：Origin をコンストラクタ引数で指定（ハンドシェイクで使われる）
        self._ws = QWebSocket(origin, QWebSocketProtocol.Version13, self)
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_text_message)
        # error シグナルは Qt 5.15 で廃止、errorOccurred に移行
        # PyQt5 は両方サポート。ここでは errorOccurred を使う
        self._ws.error.connect(self._on_socket_error)

        # 再接続タイマー
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._open_socket)

        # 明示的 disconnect 中なら auto-reconnect を抑止
        self._closing: bool = False

    # ------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------

    def register_core_handler(
        self,
        command: str,
        handler: Handler,
        *,
        allow_overwrite: bool = False,
    ) -> None:
        """Core（Tier 1 `qgis_*`）ハンドラを登録する。

        本パッケージ内部（`qgis_tools.*` → `plugins/qgis_puppet/handlers.py`
        経由）専用。外部 Plugin / Test からは `register_handler` を使う。
        """
        self._state.register_core_handler(command, handler, allow_overwrite=allow_overwrite)

    def register_handler(
        self,
        command: str,
        handler: Handler,
        *,
        allow_overwrite: bool = False,
    ) -> None:
        """外部（Tier 2 Plugin / Tier 3 Test）からのハンドラ登録。

        ADR-0001 §12.4 に従った namespace 検証を通す。詳細は
        `WorkerState.register_handler` を参照。
        """
        self._state.register_handler(command, handler, allow_overwrite=allow_overwrite)

    def unregister_handler(self, command: str) -> None:
        """command 名のハンドラを削除する。"""
        self._state.unregister_handler(command)

    @property
    def instance_id(self) -> str | None:
        """現在の instance_id（未登録なら None）。"""
        return self._state.instance_id

    @property
    def is_registered(self) -> bool:
        return self._state.is_registered

    def connect_to_hub(self) -> None:
        """Hub への接続を開始する（非同期、`registered` シグナルで完了通知）。

        `hub_lock_path` が指定されている場合、WebSocket 接続前に
        `ensure_hub_reachable` を同期実行して Hub の listen を確認する
        （必要なら Hub プロセスを spawn）。Hub 起動に失敗した場合は
        `hub_startup_failed` シグナルを発火して WebSocket 接続はしない。
        """
        self._closing = False
        if self._hub_lock_path is not None and not self._try_ensure_hub_ready():
            return
        self._open_socket()

    def _try_ensure_hub_ready(self) -> bool:
        """ensure_hub_reachable を呼び出し、失敗時は signal を発火して False を返す。"""
        assert self._hub_lock_path is not None
        host = self._hub_url.host() or "127.0.0.1"
        port = self._hub_url.port() if self._hub_url.port() != -1 else 9876
        try:
            ensure_hub_reachable(
                lock_path=self._hub_lock_path,
                host=host,
                port=port,
                command=(
                    list(self._hub_spawn_command) if self._hub_spawn_command is not None else None
                ),
                log_file=self._hub_log_file,
                env=(dict(self._hub_spawn_env) if self._hub_spawn_env is not None else None),
            )
            return True
        except HubStartupError as e:
            logger.warning("Hub startup failed: %s", e)
            self._closing = True
            self._reconnect_timer.stop()
            self.hub_startup_failed.emit(str(e))
            return False

    def disconnect_from_hub(self, *, send_bye: bool = True) -> None:
        """Hub から明示的に切断する。

        send_bye=True なら登録済みの場合に bye を送る（Hub は grace を経ずに
        即エントリ削除）。auto-reconnect はキャンセルされる。
        """
        self._closing = True
        self._reconnect_timer.stop()

        if send_bye and self._state.is_registered:
            bye = self._state.build_bye(self._new_msg_id())
            if bye is not None:
                try:
                    self._ws.sendTextMessage(encode_message(bye))
                except Exception:
                    logger.debug("Failed to send bye", exc_info=True)

        # 閉じる（disconnected シグナルが来るが closing=True で reconnect 抑止）
        self._ws.close()

    # ------------------------------------------------------------
    # 内部：接続制御
    # ------------------------------------------------------------

    def _open_socket(self) -> None:
        """WebSocket ハンドシェイクを開始する。"""
        state = self._ws.state()
        if state not in (
            QAbstractSocket.UnconnectedState,
            QAbstractSocket.ClosingState,
        ):
            logger.debug("Skipping open(): socket already in state %s", state)
            return
        logger.info("Worker connecting to %s", self._hub_url.toString())
        self._ws.open(self._hub_url)

    def _schedule_reconnect(self) -> None:
        if not self._auto_reconnect or self._closing:
            return
        if self._reconnect_timer.isActive():
            return
        logger.info("Worker reconnecting in %d ms", self._reconnect_delay_ms)
        self._reconnect_timer.start(self._reconnect_delay_ms)

    # ------------------------------------------------------------
    # 内部：Qt シグナルハンドラ
    # ------------------------------------------------------------

    def _on_connected(self) -> None:
        """ハンドシェイク成功：register メッセージを送信。"""
        msg = self._state.build_register(self._new_msg_id())
        self._ws.sendTextMessage(encode_message(msg))

    def _on_disconnected(self) -> None:
        """切断：状態を更新し、必要なら再接続を予約。"""
        was_registered = self._state.is_registered
        self._state.on_disconnect()
        if was_registered:
            self.unregistered.emit()
        self._schedule_reconnect()

    def _on_text_message(self, text: str) -> None:
        try:
            msg = decode_message(text)
        except ProtocolDecodeError as e:
            logger.warning("Worker decode error: %s", e)
            return

        if isinstance(msg, RegisterAck):
            self._handle_register_ack(msg)
        elif isinstance(msg, Request):
            self._handle_request(msg)
        else:
            logger.warning("Worker received unexpected message type %s", msg.type.value)

    def _handle_register_ack(self, ack: RegisterAck) -> None:
        try:
            self._state.on_register_ack(ack)
        except WorkerRegisterError as e:
            reason = e.error.code.value if e.error is not None else "unknown"
            logger.warning("Register failed: %s", e)
            # 復帰不可能なエラー（label 衝突など）は close して再接続を抑止
            self._closing = True
            self._reconnect_timer.stop()
            self.register_failed.emit(reason)
            self._ws.close()
            return
        assert self._state.instance_id is not None
        logger.info("Worker registered as %s", self._state.instance_id)
        self.registered.emit(self._state.instance_id)

    def _handle_request(self, req: Request) -> None:
        """Request を dispatch → Response を JSON 化して送信する。

        Response 合成時のハンドラ例外は `WorkerState.on_request` 側で
        WORKER_EXECUTION_ERROR に変換される。ここで個別にケアするのは
        **エンコード段の失敗**（e.g. QVariant のような JSON 不可能型が
        result に混入）と送信段の失敗のみ。

        無応答で client を timeout させないため、エンコード失敗時は元の
        Response を捨てて最小構成のエラー Response を送り直す。
        """
        resp = self._state.on_request(req)
        try:
            text = encode_message(resp)
        except Exception as e:  # noqa: BLE001 - 何が来ても無応答だけは避ける
            logger.exception("Failed to encode response for %s", req.command)
            fallback = Response(
                id=req.id,
                ok=False,
                error=Error(
                    code=ErrorCode.WORKER_EXECUTION_ERROR,
                    message=f"response encode failed: {e}",
                    details={"type": type(e).__name__, "command": req.command},
                ),
            )
            try:
                text = encode_message(fallback)
            except Exception:
                logger.exception(
                    "Failed to encode fallback error response for %s",
                    req.command,
                )
                return
        try:
            self._ws.sendTextMessage(text)
        except Exception:
            logger.exception("Failed to send response for %s", req.command)

    def _on_socket_error(self, _error: QAbstractSocket.SocketError) -> None:
        # error シグナルでは自動的に disconnected も飛んでくるため、ここでは log のみ
        logger.warning("Worker socket error: %s", self._ws.errorString())

    # ------------------------------------------------------------
    # 補助
    # ------------------------------------------------------------

    def _new_msg_id(self) -> str:
        return uuid.uuid4().hex
