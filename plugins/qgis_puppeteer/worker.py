"""Worker プロセス本体（Qt 配線 + ライフサイクル管理）。

ADR-0001 §4 "Worker 側" に基づき、`QWebSocket` で Hub に逆向き接続し、
`WorkerState` に委譲しながら request を handler に dispatch する。

## 責務

- つなぐたびに Hub ファイルを読み（auto-spawn なら ``ensure_hub`` で生きた Hub を
  確かめ、いなければ起こす）、そのアドレスに dial out（`QWebSocket.open`）する
- Hub の hello を受けたら、同じ Hub ファイルの token で proof を作って register を
  送る（`qgis_puppeteer.handshake`）
- register_ack の hub_proof を確かめ、instance_id を受理（`registered` シグナル発火）
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

from qgis_puppeteer.handshake import (
    ack_is_trusted,
    choose_protocol_version,
    client_proof,
    generate_nonce,
)
from qgis_puppeteer.hub_file import (
    HubFileError,
    HubFileInfo,
    read_hub_file,
    resolve_hub_file_path,
)
from qgis_puppeteer.hub_spawn import HubStartupError, ensure_hub
from qgis_puppeteer.protocol import (
    Error,
    ErrorCode,
    Hello,
    ProtocolDecodeError,
    RegisterAck,
    Request,
    Response,
    decode_message,
    encode_message,
    reply_to_undecodable,
)
from qgis_puppeteer.worker_state import (
    Handler,
    WorkerConfig,
    WorkerRegisterError,
    WorkerState,
    next_reconnect_delay_ms,
    register_failure_reason,
)

logger = logging.getLogger("qgis_puppeteer.worker")

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
        hub_startup_failed(str): auto-spawn 有効時に、初回の接続で Hub を起こせなかった場合
    """

    registered = pyqtSignal(str)
    unregistered = pyqtSignal()
    register_failed = pyqtSignal(str)
    hub_startup_failed = pyqtSignal(str)

    def __init__(
        self,
        *,
        hub_file: str | os.PathLike[str] | None = None,
        origin: str = DEFAULT_ORIGIN,
        pid: int | None = None,
        label: str | None = None,
        project: str | None = None,
        launch_token: str | None = None,
        conflict_policy: str | None = None,
        auto_reconnect: bool = True,
        reconnect_delay_ms: int = DEFAULT_RECONNECT_DELAY_MS,
        hub_spawn_command: Sequence[str] | None = None,
        hub_spawn_env: dict[str, str] | None = None,
        hub_log_file: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        """
        Args:
            hub_file: アドレスと token を読む Hub ファイル。None なら
                ``QPUPPETEER_HUB_FILE``、それも無ければ既定の場所（``qgis_puppeteer.hub_file``）
            hub_spawn_command: Hub を起こすコマンド。指定すると auto-spawn：つなぐたびに
                生きた Hub を確かめ、いなければ起こす（``qgis_puppeteer.hub_spawn.ensure_hub``）。
                None なら Hub ファイルを読むだけ（Hub は誰かが起こしている）
            hub_spawn_env: 起こす Hub に渡す環境変数。None なら親の環境を継承
            hub_log_file: 起こす Hub の標準出力・標準エラーの受け皿
        """
        super().__init__(parent)
        self._hub_file = resolve_hub_file_path(hub_file)
        # 今つないでいる（つなごうとしている）Hub の token。つなぐたびに Hub ファイルから
        self._token: str | None = None
        # 挨拶の途中：(token, hub の nonce, 自分の nonce)。register_ack で使う
        self._handshake: tuple[str, str, str] | None = None
        self._origin = origin
        self._auto_reconnect = auto_reconnect
        self._reconnect_delay_ms = reconnect_delay_ms
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

        # 再接続タイマー。接続だけでなく Hub の立て直しも通す（_reconnect）。
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._reconnect)
        # 連続失敗回数（バックオフ用）。register できたら 0 に戻る。
        self._reconnect_failures: int = 0

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

        本パッケージ内部（`qgis_tools.*` → `qgis_plugin/handlers.py`
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

    def update_project(self, project: str | None) -> None:
        """開いているプロジェクトが変わったことを Hub に伝える。

        register は plugin ロード時の 1 回きりで、ランチャー起動では QGIS が
        プロジェクトを開く前なので永久に None のままだった。登録済みなら
        update_info を送り、未登録なら次の register に載せる。
        """
        if not self._state.set_project(project):
            return
        msg = self._state.build_update_info(self._new_msg_id())
        if msg is None:
            return
        try:
            self._ws.sendTextMessage(encode_message(msg))
            logger.info("Worker project -> %r", project)
        except Exception:  # pragma: no cover - 送信失敗は次の register で追いつく
            logger.debug("Failed to send update_info", exc_info=True)

    @property
    def hub_file(self) -> Path:
        """アドレスと token を読む Hub ファイル。"""
        return self._hub_file

    def connect_to_hub(self) -> None:
        """Hub への接続を開始する（非同期、`registered` シグナルで完了通知）。

        auto-spawn（``hub_spawn_command`` を指定）なら、WebSocket 接続前に
        ``ensure_hub`` を同期実行して生きた Hub を確かめる（いなければ起こす）。
        起こせなければ `hub_startup_failed` を発火して接続しない。
        auto-spawn でなければ Hub ファイルを読み、読めなければ再接続を予約する。
        """
        self._closing = False
        self._reconnect_failures = 0
        self._connect(give_up=True)

    def _connect(self, *, give_up: bool) -> None:
        """Hub ファイルからアドレスと token を取ってつなぐ。取れなければ再接続を予約する。"""
        info = self._find_hub(give_up=give_up)
        if info is None:
            if not self._closing:
                self._reconnect_failures += 1
                self._schedule_reconnect()
            return
        self._open_socket(info)

    def _find_hub(self, *, give_up: bool) -> HubFileInfo | None:
        """今の Hub の Hub ファイルの中身。取れなければ None。

        auto-spawn なら ``ensure_hub``（生きた Hub を確かめ、いなければ起こす）。
        ``give_up=True``（初回接続）で起こせなければ `hub_startup_failed` を発火して
        auto-reconnect も止める（プラグインがメッセージバーで知らせる）。
        ``give_up=False``（再接続）ならログだけ出して再試行に任せる。
        Hub が一時的に消えただけで Worker が永久に諦めるのは困る。

        auto-spawn でなければ Hub ファイルを読むだけ（Hub は誰かが起こしている）。
        """
        if self._hub_spawn_command is None:
            try:
                return read_hub_file(self._hub_file)
            except HubFileError as e:
                logger.warning("Cannot read the Hub file; will retry: %s", e)
                return None
        try:
            return ensure_hub(
                hub_file=self._hub_file,
                command=list(self._hub_spawn_command),
                log_file=self._hub_log_file,
                env=(dict(self._hub_spawn_env) if self._hub_spawn_env is not None else None),
            ).info
        except HubStartupError as e:
            if not give_up:
                logger.warning(
                    "No live Hub and could not (re)start one (attempt %d): %s",
                    self._reconnect_failures + 1,
                    e,
                )
                return None
            logger.warning("Hub startup failed: %s", e)
            self._closing = True
            self._reconnect_timer.stop()
            self.hub_startup_failed.emit(str(e))
            return None

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

    def _open_socket(self, info: HubFileInfo) -> None:
        """Hub ファイルの最初のアドレスへ WebSocket ハンドシェイクを開始する。

        Hub は同じ機械にいるので、最初のアドレス（127.0.0.1）でつなぐ。
        """
        state = self._ws.state()
        if state not in (
            QAbstractSocket.UnconnectedState,
            QAbstractSocket.ClosingState,
        ):
            logger.debug("Skipping open(): socket already in state %s", state)
            return
        if not info.addresses:
            logger.warning("The Hub file %s has no address; will retry", self._hub_file)
            self._reconnect_failures += 1
            self._schedule_reconnect()
            return
        self._token = info.token
        url = QUrl(f"ws://{info.addresses[0]}")
        logger.info("Worker connecting to %s", url.toString())
        self._ws.open(url)

    def _schedule_reconnect(self) -> None:
        if not self._auto_reconnect or self._closing:
            return
        if self._reconnect_timer.isActive():
            return
        delay = next_reconnect_delay_ms(self._reconnect_failures, self._reconnect_delay_ms)
        logger.info("Worker reconnecting in %d ms", delay)
        self._reconnect_timer.start(delay)

    def _reconnect(self) -> None:
        """再接続タイマーの着火先。Hub ファイルを読み直してから繋ぐ。

        Hub が起き直すとポートも token も変わるので、毎回 Hub ファイルを読む。
        auto-spawn なら Hub が消えていれば起こし直す（生きていれば挨拶 1 回で済む）。
        """
        if self._closing:
            return
        self._connect(give_up=False)

    # ------------------------------------------------------------
    # 内部：Qt シグナルハンドラ
    # ------------------------------------------------------------

    def _on_connected(self) -> None:
        """ハンドシェイク成功。register は Hub の hello を受けてから送る。"""
        self._handshake = None

    def _on_disconnected(self) -> None:
        """切断：状態を更新し、必要なら再接続を予約。"""
        self._handshake = None
        was_registered = self._state.is_registered
        self._state.on_disconnect()
        if was_registered:
            self.unregistered.emit()
        else:
            # 繋がらなかった（接続拒否など）= 1 回の失敗。登録済みからの切断は
            # 新しい障害なので数えず、まず素早く 1 回目を試す。
            self._reconnect_failures += 1
        self._schedule_reconnect()

    def _on_text_message(self, text: str) -> None:
        try:
            msg = decode_message(text)
        except ProtocolDecodeError as e:
            logger.warning("Worker decode error: %s", e)
            # 知らない type（新しい Hub から来た、など）でも、id があれば答えて
            # 送り主を待たせない
            reply = reply_to_undecodable(e, receiver="QGIS")
            if reply is not None:
                try:
                    self._ws.sendTextMessage(encode_message(reply))
                except Exception:
                    logger.exception("Failed to answer an undecodable message")
            return

        if isinstance(msg, Hello):
            self._handle_hello(msg)
        elif isinstance(msg, RegisterAck):
            self._handle_register_ack(msg)
        elif isinstance(msg, Request):
            self._handle_request(msg)
        else:
            logger.warning("Worker received unexpected message type %s", msg.type.value)

    def _handle_hello(self, hello: Hello) -> None:
        """Hub の挨拶に、Hub ファイルの token で作った proof 付きの register で答える。

        token は、つなぐときにアドレスと一緒に Hub ファイルから読んだもの。
        """
        token = self._token
        if token is None:  # pragma: no cover - _open_socket が必ず入れる
            self._ws.close()
            return
        nonce = generate_nonce()
        self._handshake = (token, hello.nonce, nonce)
        msg = self._state.build_register(
            self._new_msg_id(),
            nonce=nonce,
            proof=client_proof(token, hello.nonce, nonce),
            protocol_version=choose_protocol_version(hello.protocol),
        )
        self._ws.sendTextMessage(encode_message(msg))

    def _handle_register_ack(self, ack: RegisterAck) -> None:
        handshake, self._handshake = self._handshake, None
        if handshake is None or not ack_is_trusted(
            ack, token=handshake[0], client_nonce=handshake[2], hub_nonce=handshake[1]
        ):
            # token を知らない相手（Hub ではない、または別の Hub）。言うことを
            # 聞かない（launch_abandoned で QGIS を閉じたりしない）。切って再接続に任せる
            logger.warning(
                "Ignoring a register_ack without a valid Hub proof (not our Hub?); will reconnect"
            )
            self._ws.close()
            return
        try:
            self._state.on_register_ack(ack)
        except WorkerRegisterError as e:
            reason = register_failure_reason(e.error)
            logger.warning("Register failed: %s", e)
            # 復帰不可能なエラー（label 衝突など）は close して再接続を抑止
            self._closing = True
            self._reconnect_timer.stop()
            self.register_failed.emit(reason)
            self._ws.close()
            return
        assert self._state.instance_id is not None
        self._reconnect_failures = 0
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
