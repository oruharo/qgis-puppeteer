"""Hub プロセス本体（Qt 配線 + エントリポイント）。

ADR-0001 §4, §6, §9 に基づき、`QCoreApplication` 上で
`QWebSocketServer` を listen させ、Worker / Client 接続を
`HubState` に委譲しながらルーティングする。

## 責務

- 127.0.0.1:9876 で WebSocket listen（Origin 完全一致検証付き）
- 接続イベントを `HubState` に委譲
- `request` / `response` の双方向ルーティング（`id` で対応付け）
- grace 期限切れの定期 sweep
- 全接続 0 + 30 秒経過で自己終了
- PID ファイル（`%APPDATA%\\qgis_puppeteer\\hub.pid`）管理

## 実行方法

```
python -m qgis_puppeteer.hub
```

Worker プラグインから `subprocess.Popen` で DETACHED 起動される想定。
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt5.QtCore import (  # type: ignore[import-not-found]
    QCoreApplication,
    QObject,
    QTimer,
)
from PyQt5.QtNetwork import QHostAddress  # type: ignore[import-not-found]
from PyQt5.QtWebSockets import (  # type: ignore[import-not-found]
    QWebSocketCorsAuthenticator,
    QWebSocketServer,
)

from qgis_puppeteer.hub_state import (
    IDLE_SHUTDOWN_DELAY_SECONDS,
    LIVENESS_PROBE_SECONDS,
    HubState,
    validate_origin,
)
from qgis_puppeteer.protocol import (
    Bye,
    Error,
    ErrorCode,
    ListInstancesRequest,
    ListInstancesResponse,
    Message,
    ProtocolDecodeError,
    RegisterAck,
    RegisterRequest,
    Request,
    Response,
    Role,
    decode_message,
    encode_message,
)

if TYPE_CHECKING:
    from PyQt5.QtWebSockets import QWebSocket  # noqa: F401

# ============================================================
# 定数
# ============================================================

HUB_PORT: int = 9876
SWEEP_INTERVAL_MS: int = 5_000  # grace 期限切れ sweep の周期
PID_DIR_ENV: str = "APPDATA"  # Windows 想定。%APPDATA%\qgis_puppeteer\
PID_DIR_NAME: str = "qgis_puppeteer"  # %APPDATA% 配下のサブディレクトリ名
LOG_FILE_NAME: str = "hub.log"
PID_FILE_TEMPLATE: str = "hub-{port}.pid"  # port ごとに分離（ADR-0001 §4）

logger = logging.getLogger("qgis_puppeteer.hub")


# ============================================================
# ConnectionKind：Worker か Client かの区別
# ============================================================


class _ConnectionKind:
    WORKER = "worker"
    CLIENT = "client"  # mcp_gateway / automation_client をまとめて扱う


# ============================================================
# Hub 本体
# ============================================================


class Hub(QObject):
    """QGIS Puppeteer Hub。Qt イベントループ上で 1 スレッドで動作する。

    全ての状態更新は同一スレッドの逐次イベント処理で行うため、
    `HubState` にロックは不要（ADR-0001 §4 スレッドモデル参照）。
    """

    def __init__(
        self,
        *,
        port: int = HUB_PORT,
        pid_file: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._port = port
        self._pid_file = pid_file
        self._state = HubState()

        # 接続ごとの管理情報
        # conn_id（id(ws) の文字列化）→ (QWebSocket, kind)
        self._sockets: dict[str, QWebSocket] = {}
        self._kinds: dict[str, str] = {}
        # Client 接続の role（MCP_GATEWAY or AUTOMATION_CLIENT）。
        # Worker 転送時に caller_role として付与する（ADR-0001 §12.7）。
        self._client_roles: dict[str, Role] = {}

        # in-flight request 追跡：request.id → (発信 Client conn_id, 宛先 Worker conn_id)。
        # 発信元は Worker → Client の response ルーティングに使う。宛先は Worker 切断時に
        # 「その Worker に転送した未完了 request」を特定して Client 側にエラーで返す
        # ため必要（そうしないと Client が timeout まで待ち続けてしまう）。
        self._inflight: dict[str, tuple[str, str]] = {}

        # Qt オブジェクト
        self._server = QWebSocketServer("qgis-puppeteer-hub", QWebSocketServer.NonSecureMode, self)
        self._server.newConnection.connect(self._on_new_connection)
        self._server.originAuthenticationRequired.connect(self._on_origin_auth)

        # grace 期限切れ定期 sweep
        self._sweep_timer = QTimer(self)
        self._sweep_timer.setInterval(SWEEP_INTERVAL_MS)
        self._sweep_timer.timeout.connect(self._on_sweep)
        self._sweep_timer.start()

        # idle 自動終了タイマー（全接続 0 の時だけ作動）
        self._idle_shutdown_timer = QTimer(self)
        self._idle_shutdown_timer.setSingleShot(True)
        self._idle_shutdown_timer.setInterval(int(IDLE_SHUTDOWN_DELAY_SECONDS * 1000))
        self._idle_shutdown_timer.timeout.connect(self._on_idle_shutdown_fire)

    # ------------------------------------------------------------
    # 起動・終了
    # ------------------------------------------------------------

    def start(self) -> None:
        """listen 開始。bind 失敗時は例外を投げる（敗者 Hub は呼び出し側で exit）。"""
        ok = self._server.listen(QHostAddress.LocalHost, self._port)
        if not ok:
            raise OSError(
                f"Hub failed to bind to 127.0.0.1:{self._port}: {self._server.errorString()}"
            )
        logger.info("Hub listening on 127.0.0.1:%d", self._port)

        if self._pid_file is not None:
            self._pid_file.parent.mkdir(parents=True, exist_ok=True)
            self._pid_file.write_text(f"{os.getpid()}\n{time.time()}\n")

        # 起動直後は全接続 0 なので idle 自動終了タイマーを起動
        self._reassess_idle_shutdown()

    def stop(self) -> None:
        """listen 停止、PID ファイル削除、idle 自動終了タイマー停止。"""
        self._sweep_timer.stop()
        self._idle_shutdown_timer.stop()
        self._server.close()
        if self._pid_file is not None:
            try:
                self._pid_file.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Failed to remove PID file: %s", e)
        logger.info("Hub stopped")

    # ------------------------------------------------------------
    # Origin 検証（ハンドシェイク時）
    # ------------------------------------------------------------

    def _on_origin_auth(self, authenticator: QWebSocketCorsAuthenticator) -> None:
        """Origin ヘッダの hostname 完全一致検証（ADR-0001 §9.2）。"""
        origin = authenticator.origin()
        allowed = validate_origin(origin)
        authenticator.setAllowed(allowed)
        if not allowed:
            logger.warning("Rejected connection from origin %r", origin)

    # ------------------------------------------------------------
    # 新規接続
    # ------------------------------------------------------------

    def _on_new_connection(self) -> None:
        # TCP accept された時点で idle shutdown タイマーを即キャンセルする
        # （ADR-0001 §4 "タイマーキャンセル条件"）。register 受信を待つと、
        # 受信前にタイマー満了して Hub が自殺する窓が残るため。
        if self._idle_shutdown_timer.isActive():
            logger.info("Connection accepted; idle shutdown timer canceled before register")
            self._idle_shutdown_timer.stop()

        while self._server.hasPendingConnections():
            ws = self._server.nextPendingConnection()
            conn_id = str(id(ws))
            self._sockets[conn_id] = ws
            # kind は register メッセージ受信時に確定するため、ここでは未定
            ws.textMessageReceived.connect(
                lambda text, cid=conn_id: self._on_text_message(cid, text)
            )
            # ADR-0005 D5: pong 受信を liveness 更新に使う。
            ws.pong.connect(lambda _e=0, _p=b"", cid=conn_id: self._state.mark_seen(cid))
            ws.disconnected.connect(lambda cid=conn_id: self._on_disconnected(cid))
            logger.info("New connection: %s", conn_id)

        # register 到達後に正規の再判定。このパスでは既にタイマー止まってるので no-op
        self._reassess_idle_shutdown()

    # ------------------------------------------------------------
    # メッセージ受信
    # ------------------------------------------------------------

    def _on_text_message(self, conn_id: str, text: str) -> None:
        try:
            msg = decode_message(text)
        except ProtocolDecodeError as e:
            logger.warning("Decode error from %s: %s", conn_id, e)
            self._send_error_response(
                conn_id,
                request_id="<unknown>",
                code=ErrorCode.INVALID_COMMAND,
                message=f"Protocol decode error: {e}",
            )
            return

        try:
            self._dispatch(conn_id, msg)
        except Exception:
            logger.exception("Dispatch error for conn=%s type=%s", conn_id, msg.type)

    def _dispatch(self, conn_id: str, msg: Message) -> None:
        # isinstance narrowing で `Message` Union を具象クラスへ絞り、各 handler
        # の引数型（RegisterRequest / Request / ...）と一致させる。これで
        # `type: ignore[arg-type]` を使わずに型チェッカを通せる。
        if isinstance(msg, RegisterRequest):
            self._handle_register(conn_id, msg)
        elif isinstance(msg, Request):
            self._handle_request(conn_id, msg)
        elif isinstance(msg, Response):
            self._handle_response(conn_id, msg)
        elif isinstance(msg, ListInstancesRequest):
            self._handle_list_instances(conn_id, msg)
        elif isinstance(msg, Bye):
            self._handle_bye(conn_id, msg)
        else:
            # RegisterAck / ListInstancesResponse は Hub からの送信専用なので
            # inbound で来たら仕様違反。落とさず warning に留めて握り潰す。
            logger.warning("Unexpected inbound type %s from %s", msg.type, conn_id)

    # ------------------------------------------------------------
    # register
    # ------------------------------------------------------------

    def _handle_register(self, conn_id: str, req: RegisterRequest) -> None:
        if req.role is Role.WORKER:
            outcome = self._state.register_worker(conn_id, req)
            if outcome.pending:
                # ADR-0005 C1=(b): incumbent の生死を ping 確認する間 ack を
                # 保留。pong は QWebSocket.pong → _state.mark_seen で記録され、
                # LIVENESS_PROBE_SECONDS 後に finalize で勝敗を判定する。
                for inc_conn in outcome.liveness_probe_conn_ids:
                    ws = self._sockets.get(inc_conn)
                    if ws is not None:
                        try:
                            ws.ping()
                        except Exception:  # noqa: BLE001 - ping は best-effort
                            logger.debug("incumbent ping failed", exc_info=True)
                logger.info(
                    "Register for label conflict deferred; probing %d incumbent(s)",
                    len(outcome.liveness_probe_conn_ids),
                )
                QTimer.singleShot(
                    int(LIVENESS_PROBE_SECONDS * 1000),
                    lambda cid=conn_id, rid=req.id: self._finalize_pending(cid, rid),
                )
                return
            self._apply_worker_outcome(conn_id, req.id, outcome)
            self._reassess_idle_shutdown()
            return

        # mcp_gateway / automation_client
        self._state.register_client(conn_id)
        self._kinds[conn_id] = _ConnectionKind.CLIENT
        # 発信元 role を保持して、Worker への request 転送時に注入する
        # （ADR-0001 §12.7 caller_role 伝達）。
        self._client_roles[conn_id] = req.role
        self._send(conn_id, RegisterAck(id=req.id, ok=True))
        self._reassess_idle_shutdown()

    def _apply_worker_outcome(self, conn_id: str, req_id: str, outcome: object) -> None:
        """Worker 登録 outcome を ack 化して送る（即時 / finalize 共通）。"""
        # outcome は hub_state.RegisterOutcome（循環 import 回避で object 注釈）。
        if outcome.ok:  # type: ignore[attr-defined]
            self._kinds[conn_id] = _ConnectionKind.WORKER
            if outcome.evicted_instance_ids:  # type: ignore[attr-defined]
                logger.info(
                    "Evicted instances on label takeover/supersede: %s",
                    list(outcome.evicted_instance_ids),  # type: ignore[attr-defined]
                )
            ack = RegisterAck(
                id=req_id,
                ok=True,
                instance_id=outcome.instance_id,  # type: ignore[attr-defined]
                resumed=outcome.resumed,  # type: ignore[attr-defined]
                superseded=outcome.superseded,  # type: ignore[attr-defined]
            )
        else:
            ack = RegisterAck(
                id=req_id,
                ok=False,
                error=outcome.error,  # type: ignore[attr-defined]
            )
        self._send(conn_id, ack)

    def _finalize_pending(self, conn_id: str, req_id: str) -> None:
        """C1=(b): probe 窓経過後に保留登録を確定して ack を送る。"""
        outcome = self._state.finalize_pending_registration(conn_id)
        if outcome is None:
            # newcomer が probe 中に切断した等 → 何もしない。
            return
        self._apply_worker_outcome(conn_id, req_id, outcome)
        self._reassess_idle_shutdown()

    # ------------------------------------------------------------
    # request / response
    # ------------------------------------------------------------

    def _handle_request(self, conn_id: str, req: Request) -> None:
        """Client → Hub → Worker のルーティング。"""
        # Client 以外から request は来ない想定だが保険
        if self._kinds.get(conn_id) != _ConnectionKind.CLIENT:
            self._send_error_response(
                conn_id,
                request_id=req.id,
                code=ErrorCode.INVALID_COMMAND,
                message="Only registered clients can send requests",
            )
            return

        resolve = self._state.resolve_instance(req.instance)
        if not resolve.ok:
            self._send(conn_id, Response(id=req.id, ok=False, error=resolve.error))
            return

        target_conn = self._state.find_worker_conn(resolve.instance_id or "")
        if target_conn is None or target_conn not in self._sockets:
            self._send(
                conn_id,
                Response(
                    id=req.id,
                    ok=False,
                    error=Error(
                        code=ErrorCode.INSTANCE_NOT_FOUND,
                        message="Target worker disappeared",
                    ),
                ),
            )
            return

        # ルーティング記録：origin（Client）と target（Worker）の双方を覚える。
        # target を持つことで Worker が途中で落ちたときに未完了 request を
        # 一括で失敗応答に切り替えられる（_on_disconnected 参照）。
        self._inflight[req.id] = (conn_id, target_conn)
        # ADR-0001 §12.7：Hub が register 時の role で caller_role を上書きして
        # Worker に転送する。Client 側からの申告（req.caller_role が既に
        # セットされていても）は信用しない。
        caller_role = self._client_roles.get(conn_id)
        forwarded = Request(
            id=req.id,
            command=req.command,
            params=req.params,
            instance=req.instance,
            timeout_ms=req.timeout_ms,
            caller_role=caller_role,
        )
        self._send(target_conn, forwarded)

    def _handle_response(self, conn_id: str, resp: Response) -> None:
        """Worker → Hub → 発信元 Client への返送。"""
        entry = self._inflight.pop(resp.id, None)
        if entry is None:
            logger.warning("Response for unknown request id=%s", resp.id)
            return
        origin, _target = entry
        if origin not in self._sockets:
            logger.info("Originating client %s gone; dropping response", origin)
            return
        self._send(origin, resp)

    # ------------------------------------------------------------
    # list_instances
    # ------------------------------------------------------------

    def _handle_list_instances(self, conn_id: str, req: ListInstancesRequest) -> None:
        instances = self._state.list_instances()
        self._send(conn_id, ListInstancesResponse(id=req.id, instances=instances))

    # ------------------------------------------------------------
    # bye
    # ------------------------------------------------------------

    def _handle_bye(self, conn_id: str, msg: Bye) -> None:
        """Worker からの明示的終了通知：grace を経ずに即削除（§4）。"""
        self._state.bye_worker(conn_id)
        # 接続自体も閉じる
        ws = self._sockets.get(conn_id)
        if ws is not None:
            ws.close()
        self._reassess_idle_shutdown()

    # ------------------------------------------------------------
    # 切断
    # ------------------------------------------------------------

    def _on_disconnected(self, conn_id: str) -> None:
        kind = self._kinds.pop(conn_id, None)
        if kind == _ConnectionKind.WORKER:
            self._state.disconnect_worker(conn_id)
            # この Worker に転送済みの未完了 request を洗い出し、それぞれの
            # 発信元 Client へ「Worker が落ちた」旨の失敗 response を返して
            # `_inflight` から除去する。こうしないと Client 側は timeout まで
            # 無駄に待ち続け、再試行も遅れる。
            lost = [
                (rid, origin)
                for rid, (origin, target) in self._inflight.items()
                if target == conn_id
            ]
            for rid, origin in lost:
                self._inflight.pop(rid, None)
                if origin not in self._sockets:
                    # origin Client もすでに切れているならエラー通知は不要
                    continue
                self._send(
                    origin,
                    Response(
                        id=rid,
                        ok=False,
                        error=Error(
                            code=ErrorCode.INSTANCE_NOT_FOUND,
                            message="Target worker disconnected before response",
                        ),
                    ),
                )
        elif kind == _ConnectionKind.CLIENT:
            self._state.disconnect_client(conn_id)
            self._client_roles.pop(conn_id, None)
            # この Client 発の in-flight を掃除（レスポンス不着でもリークしない）
            self._inflight = {
                rid: entry for rid, entry in self._inflight.items() if entry[0] != conn_id
            }
        self._sockets.pop(conn_id, None)
        logger.info("Disconnected: %s (kind=%s)", conn_id, kind)
        self._reassess_idle_shutdown()

    # ------------------------------------------------------------
    # 定期 sweep
    # ------------------------------------------------------------

    def _on_sweep(self) -> None:
        # ADR-0005 D5: まず生存確認 ping を撒き、ハートビート途絶 active を
        # grace へ落とす（kill -9 half-open の救済）。次に grace 期限切れを掃除。
        for ws in self._sockets.values():
            try:
                ws.ping()
            except Exception:  # noqa: BLE001 - ping は best-effort
                logger.debug("ws.ping() failed", exc_info=True)
        demoted = self._state.sweep_stale_active()
        if demoted:
            logger.info(
                "Demoted %d stale active workers to grace (heartbeat lost): %s",
                len(demoted),
                demoted,
            )
            self._reassess_idle_shutdown()

        removed = self._state.sweep_expired()
        if removed:
            logger.info(
                "Swept %d expired worker entries after grace: %s",
                len(removed),
                removed,
            )
            self._reassess_idle_shutdown()

    # ------------------------------------------------------------
    # idle 自動終了判定
    # ------------------------------------------------------------

    def _reassess_idle_shutdown(self) -> None:
        """全接続 0 なら idle 自動終了タイマー起動、そうでなければキャンセル。"""
        if self._state.should_idle_shutdown():
            if not self._idle_shutdown_timer.isActive():
                logger.info(
                    "All connections empty; idle shutdown timer armed (%.0fs)",
                    IDLE_SHUTDOWN_DELAY_SECONDS,
                )
                self._idle_shutdown_timer.start()
        else:
            if self._idle_shutdown_timer.isActive():
                logger.info("Connection restored; idle shutdown timer canceled")
                self._idle_shutdown_timer.stop()

    def _on_idle_shutdown_fire(self) -> None:
        """idle 自動終了タイマー満了：再度条件を確認してから終了。"""
        if not self._state.should_idle_shutdown():
            return
        logger.info("Idle shutdown timer fired; shutting down")
        self.stop()
        QCoreApplication.quit()

    # ------------------------------------------------------------
    # 送信ヘルパ
    # ------------------------------------------------------------

    def _send(self, conn_id: str, msg: Message) -> None:
        ws = self._sockets.get(conn_id)
        if ws is None:
            return
        try:
            ws.sendTextMessage(encode_message(msg))
        except Exception:
            logger.exception("Send failed for conn=%s", conn_id)

    def _send_error_response(
        self,
        conn_id: str,
        *,
        request_id: str,
        code: ErrorCode,
        message: str,
    ) -> None:
        self._send(
            conn_id,
            Response(
                id=request_id,
                ok=False,
                error=Error(code=code, message=message),
            ),
        )


# ============================================================
# エントリポイント
# ============================================================


def _default_pid_file(port: int) -> Path | None:
    """`%APPDATA%\\qgis_puppeteer\\hub-{port}.pid` を返す。APPDATA 未定義なら None。

    port サフィックスで分離することで、xdist 並列や外部所有者モードで
    複数 Hub が異なる port で同時起動しても pid ファイルが衝突しない
    （ADR-0001 §4）。
    """
    appdata = os.environ.get(PID_DIR_ENV)
    if not appdata:
        return None
    return Path(appdata) / PID_DIR_NAME / PID_FILE_TEMPLATE.format(port=port)


def _setup_logging() -> None:
    """ローテーション付きログ設定（§Open Questions #1）。"""
    appdata = os.environ.get(PID_DIR_ENV)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if appdata:
        log_path = Path(appdata) / PID_DIR_NAME / LOG_FILE_NAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handlers.append(rotating)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """CLI 引数をパースする。テスト時は `--port` で動的ポート指定可能。"""
    parser = argparse.ArgumentParser(
        prog="python -m qgis_puppeteer.hub",
        description="QGIS Puppeteer Hub.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=HUB_PORT,
        help=f"WebSocket listen port (default: {HUB_PORT})",
    )
    parser.add_argument(
        "--no-pid-file",
        action="store_true",
        help="Disable PID file creation (for tests).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """python -m qgis_puppeteer.hub のエントリポイント。

    bind 失敗（既に別の Hub が動作中）は exit code 0 で降りる（敗者 Hub）。
    """
    _setup_logging()
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    app = QCoreApplication(sys.argv)
    pid_file = None if args.no_pid_file else _default_pid_file(args.port)
    hub = Hub(port=args.port, pid_file=pid_file)

    try:
        hub.start()
    except OSError as e:
        logger.info("Hub bind failed (other Hub already running?): %s", e)
        return 0

    # SIGINT/SIGTERM で正常終了できるように
    def _on_signal(signum: int, _frame: object) -> None:
        logger.info("Signal %d received; shutting down", signum)
        hub.stop()
        app.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows スレッドコンテキストでは一部シグナルが使えない
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _on_signal)

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
