"""Hub プロセス本体（Qt 配線 + エントリポイント）。

ADR-0001 §4, §6, §9 に基づき、`QCoreApplication` 上で
`QWebSocketServer` を listen させ、Worker / Client 接続を
`HubState` に委譲しながらルーティングする。

## 責務

- 127.0.0.1 の OS が選んだポート（port 0）で WebSocket listen（Origin 完全一致検証付き）
- Hub ファイル（`qgis_puppeteer.hub_file`）に接続情報（アドレス・token・pid・版）を書く。
  つなぐ側はこのファイルでアドレスと token を知る
- ``--listen`` を付けたときだけ、そのアドレスでもネットワークから受け付ける（TLS 必須）。
  token・鍵・証明書はリモートのフォルダに残したものを使い（`qgis_puppeteer.hub_remote`）、
  別の機械へ写す接続情報ファイル（``remote/hub.json``）を書く。そちらはクライアント
  だけを受け、Worker の登録は断る（QGIS はいつも Hub と同じ機械）。この Hub は人が
  常駐させるものなので、接続が無くなっても止まらない
- 接続ごとに挨拶（`qgis_puppeteer.handshake`）：hello を送り、proof の通った
  register が来るまでほかのメッセージを受け付けない
- 接続イベントを `HubState` に委譲
- `request` / `response` の双方向ルーティング（`id` で対応付け）
- grace 期限切れの定期 sweep
- 全接続 0 + 30 秒経過で自己終了

## 実行方法

```
python -m qgis_puppeteer.hub [--hub-file PATH] [--port N]
python -m qgis_puppeteer.hub --listen 0.0.0.0:9876 [--rotate] [--remote-dir DIR]
```

Worker プラグインから `subprocess.Popen` で DETACHED 起動される想定
（``qgis_puppeteer.hub_spawn``）。

## 終了コード

- 0：ふつうに止まった（idle 自動終了・シグナル）
- 1：bind に失敗した（``--listen`` の TLS を使えないときも）
- 2：Hub ファイル（または、ネットワーク用の token・鍵・証明書・接続情報ファイル）を
  書けなかった
- 3：同じ Hub ファイルで生きた Hub がもういる（上書きせずに降りた）
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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt5.QtCore import (  # type: ignore[import-not-found]
    QCoreApplication,
    QObject,
    QTimer,
)
from PyQt5.QtNetwork import (  # type: ignore[import-not-found]
    QAbstractSocket,
    QHostAddress,
    QNetworkInterface,
    QSsl,
    QSslCertificate,
    QSslConfiguration,
    QSslKey,
    QSslSocket,
)
from PyQt5.QtWebSockets import (  # type: ignore[import-not-found]
    QWebSocketCorsAuthenticator,
    QWebSocketServer,
)

from qgis_puppeteer._version import __version__
from qgis_puppeteer.handshake import (
    check_register,
    generate_nonce,
    generate_token,
    hub_proof,
)
from qgis_puppeteer.hub_file import (
    HubFileInfo,
    now_iso,
    remove_hub_file,
    resolve_hub_file_path,
    write_hub_file,
)
from qgis_puppeteer.hub_probe import find_live_hub, split_address
from qgis_puppeteer.hub_remote import (
    FINGERPRINT_KEY,
    REMOTE_HUB_FILE,
    RemoteIdentity,
    default_remote_dir,
    load_or_create_remote_identity,
)
from qgis_puppeteer.hub_spawn import (
    HUB_EXIT_ALREADY_RUNNING,
    HUB_EXIT_BIND_FAILED,
    HUB_EXIT_HUB_FILE_FAILED,
    hub_log_path,
)
from qgis_puppeteer.hub_state import (
    IDLE_SHUTDOWN_DELAY_SECONDS,
    LIVENESS_PROBE_SECONDS,
    HubState,
    validate_origin,
)
from qgis_puppeteer.protocol import (
    SUPPORTED_PROTOCOL,
    AbandonLaunch,
    Bye,
    Error,
    ErrorCode,
    Hello,
    ListInstancesRequest,
    ListInstancesResponse,
    Message,
    ProtocolDecodeError,
    RegisterAck,
    RegisterRequest,
    Request,
    Response,
    Role,
    UpdateInfo,
    decode_message,
    effective_timeout_ms,
    encode_message,
    error_code_value,
    reply_to_undecodable,
)

if TYPE_CHECKING:
    from PyQt5.QtWebSockets import QWebSocket  # noqa: F401

# ============================================================
# 定数
# ============================================================

# 0 は OS に選ばせる（ユーザーごと・pytest の session ごとに別のポート）。
# つなぐ側は Hub ファイルでポートを知る
HUB_PORT: int = 0
SWEEP_INTERVAL_MS: int = 5_000  # grace 期限切れ sweep の周期
# 接続してから proof の通った register が来るまで待つ時間。過ぎたら切る
REGISTER_TIMEOUT_MS: int = 10_000

logger = logging.getLogger("qgis_puppeteer.hub")


# ============================================================
# ConnectionKind：Worker か Client かの区別
# ============================================================


class _ConnectionKind:
    WORKER = "worker"
    CLIENT = "client"  # mcp_gateway / automation_client をまとめて扱う


@dataclass(frozen=True)
class _Session:
    """挨拶の通った接続。register_ack の hub_proof と版に使う。"""

    hub_nonce: str
    client_nonce: str
    protocol_version: int


class HubFileWriteError(Exception):
    """Hub ファイルを書けなかった。つなぐ側が token を読めないので Hub は動けない。"""


@dataclass(frozen=True)
class RemoteListen:
    """ネットワークから受け付ける待ち受け（``--listen``）。"""

    # IP アドレス（"0.0.0.0" / "::" ならすべて）
    host: str
    # 1〜65535（写したファイルが古くならないよう、0 は使わない）
    port: int
    identity: RemoteIdentity
    # 別の機械へ写す接続情報ファイル。None なら書かない（テスト用）
    hub_file: Path | None = None


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
        hub_file: Path | None = None,
        token: str | None = None,
        register_timeout_ms: int = REGISTER_TIMEOUT_MS,
        remote: RemoteListen | None = None,
        parent: QObject | None = None,
    ) -> None:
        """
        Args:
            port: 待ち受けるポート。0 なら OS が選ぶ
            hub_file: 接続情報を書く Hub ファイル。None なら書かない（token を
                ``token`` で渡して直接つなぐテスト用）
            token: 挨拶の token。None なら作る（ふだんはこちら。起きるたびに変わる）
            register_timeout_ms: 接続してから register を待つ時間
            remote: ネットワークからも受け付けるときの待ち受け（TLS）
        """
        super().__init__(parent)
        self._port = port
        self._hub_file = hub_file
        self._token = token if token is not None else generate_token()
        self._register_timeout_ms = register_timeout_ms
        self._remote = remote
        self._wrote_hub_file = False
        self._state = HubState()
        # ネットワーク側（TLS）から来た接続。token が違い、Worker は受けない
        self._remote_conns: set[str] = set()

        # 挨拶：hello を送ったが、まだ proof の通った register が来ていない接続 → hub の nonce
        self._hello_nonces: dict[str, str] = {}
        # 挨拶の通った接続
        self._sessions: dict[str, _Session] = {}

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
        self._server.newConnection.connect(lambda: self._on_new_connection(self._server, False))
        self._server.originAuthenticationRequired.connect(self._on_origin_auth)
        self._remote_server: QWebSocketServer | None = None
        if remote is not None:
            self._remote_server = QWebSocketServer(
                "qgis-puppeteer-hub", QWebSocketServer.SecureMode, self
            )
            self._remote_server.newConnection.connect(
                lambda: self._on_new_connection(self._remote_server, True)
            )
            self._remote_server.originAuthenticationRequired.connect(self._on_origin_auth)
            self._remote_server.sslErrors.connect(
                lambda errors: logger.warning(
                    "TLS errors on a network connection: %s",
                    "; ".join(e.errorString() for e in errors),
                )
            )
            self._remote_server.acceptError.connect(
                lambda err: logger.warning("Network accept error: %s", err)
            )

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

    @property
    def token(self) -> str:
        return self._token

    @property
    def hub_file(self) -> Path | None:
        return self._hub_file

    @property
    def remote_port(self) -> int | None:
        """ネットワーク側で待ち受けているポート（待ち受けていなければ None）。"""
        if self._remote_server is None or not self._remote_server.isListening():
            return None
        return int(self._remote_server.serverPort())

    def _token_for(self, conn_id: str) -> str:
        """接続の側（ローカル / ネットワーク）の token。"""
        if conn_id in self._remote_conns and self._remote is not None:
            return self._remote.identity.token
        return self._token

    def start(self) -> None:
        """listen を始め、Hub ファイルを書く。

        Raises:
            OSError: bind に失敗した（敗者 Hub は呼び出し側で exit）
            HubFileWriteError: Hub ファイルを書けなかった（listen はやめている）
        """
        ok = self._server.listen(QHostAddress.LocalHost, self._port)
        if not ok:
            raise OSError(
                f"Hub failed to bind to 127.0.0.1:{self._port}: {self._server.errorString()}"
            )
        port = self._server.serverPort()
        logger.info("Hub listening on 127.0.0.1:%d", port)
        if self._remote is not None:
            try:
                self._listen_remote(self._remote)
            except OSError:
                self._server.close()
                raise

        # listen できた Hub だけが書く（bind に負けた Hub は勝った Hub のファイルを
        # 上書きしない）。書いてからイベントループに戻るので、ファイルを読んだ相手が
        # つないでくるのは書いた後
        if self._hub_file is not None:
            try:
                write_hub_file(
                    self._hub_file,
                    HubFileInfo(
                        addresses=(f"127.0.0.1:{port}",),
                        token=self._token,
                        pid=os.getpid(),
                        package_version=__version__,
                        protocol=SUPPORTED_PROTOCOL,
                        started_at=now_iso(),
                    ),
                )
            except OSError as e:
                self._server.close()
                if self._remote_server is not None:
                    self._remote_server.close()
                raise HubFileWriteError(
                    f"Hub could not write its Hub file {self._hub_file}: {e}"
                ) from e
            self._wrote_hub_file = True
            logger.info("Hub file: %s", self._hub_file)
        if self._remote is not None and self._remote.hub_file is not None:
            self._write_remote_hub_file(self._remote)

        # 起動直後は全接続 0 なので idle 自動終了タイマーを起動
        self._reassess_idle_shutdown()

    def stop(self) -> None:
        """listen 停止、Hub ファイル削除、idle 自動終了タイマー停止。"""
        self._sweep_timer.stop()
        self._idle_shutdown_timer.stop()
        self._server.close()
        # ネットワーク用の接続情報ファイルは残す（写した先と同じ中身で、Hub を
        # 起こし直しても変わらない）
        if self._remote_server is not None:
            self._remote_server.close()
        if self._wrote_hub_file and self._hub_file is not None:
            # 自分の token のときだけ消す（あとから起きた別の Hub のファイルは残す）
            try:
                remove_hub_file(self._hub_file, token=self._token)
            except OSError as e:
                logger.warning("Failed to remove the Hub file: %s", e)
            self._wrote_hub_file = False
        logger.info("Hub stopped")

    def _listen_remote(self, remote: RemoteListen) -> None:
        """ネットワーク側の TLS の待ち受けを始める。

        Raises:
            OSError: TLS を使えない・鍵か証明書を読めない・bind に失敗した
        """
        assert self._remote_server is not None
        if not QSslSocket.supportsSsl():
            raise OSError(
                "This Qt has no TLS support (OpenSSL not found), so the Hub cannot listen "
                f"on {_format_address(remote.host, remote.port)}"
            )
        cert = QSslCertificate(remote.identity.cert_pem.encode("ascii"), QSsl.Pem)
        key = QSslKey(remote.identity.key_pem.encode("ascii"), QSsl.Rsa, QSsl.Pem, QSsl.PrivateKey)
        if cert.isNull() or key.isNull():
            raise OSError("Qt could not load the Hub's TLS certificate or key")
        config = QSslConfiguration.defaultConfiguration()
        config.setLocalCertificate(cert)
        config.setPrivateKey(key)
        config.setPeerVerifyMode(QSslSocket.VerifyNone)
        config.setProtocol(QSsl.TlsV1_2OrLater)
        self._remote_server.setSslConfiguration(config)
        if not self._remote_server.listen(_listen_host_address(remote.host), remote.port):
            raise OSError(
                f"Hub failed to bind to {_format_address(remote.host, remote.port)}: "
                f"{self._remote_server.errorString()}"
            )
        logger.info(
            "Hub listening for network clients on %s (TLS, certificate %s)",
            _format_address(remote.host, remote.port),
            remote.identity.fingerprint_sha256,
        )

    def _write_remote_hub_file(self, remote: RemoteListen) -> None:
        """別の機械へ写す接続情報ファイルを書く（pid は無い）。

        Raises:
            HubFileWriteError: 書けなかった（待ち受けはやめている）
        """
        assert remote.hub_file is not None
        addresses = network_addresses(remote.host, remote.port)
        if not addresses:
            logger.warning(
                "No network address found for %s; edit the addresses in %s by hand",
                remote.host,
                remote.hub_file,
            )
        try:
            write_hub_file(
                remote.hub_file,
                HubFileInfo(
                    addresses=addresses,
                    token=remote.identity.token,
                    tls={FINGERPRINT_KEY: remote.identity.fingerprint_sha256},
                    package_version=__version__,
                    protocol=SUPPORTED_PROTOCOL,
                ),
            )
        except OSError as e:
            self.stop()
            raise HubFileWriteError(
                f"Hub could not write the network Hub file {remote.hub_file}: {e}"
            ) from e
        logger.info("Network Hub file (copy it to the client machine): %s", remote.hub_file)

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

    def _on_new_connection(self, server: QWebSocketServer, remote: bool) -> None:
        # TCP accept された時点で idle shutdown タイマーを即キャンセルする
        # （ADR-0001 §4 "タイマーキャンセル条件"）。register 受信を待つと、
        # 受信前にタイマー満了して Hub が自殺する窓が残るため。
        if self._idle_shutdown_timer.isActive():
            logger.info("Connection accepted; idle shutdown timer canceled before register")
            self._idle_shutdown_timer.stop()

        while server.hasPendingConnections():
            ws = server.nextPendingConnection()
            # 受け取った接続は Python 側が持ち主になっている。そのままだと
            # `_sockets` から外した瞬間に C++ のオブジェクトが消え、それが
            # その接続の disconnected の中だと落ちる。Hub の子にして C++ 側に
            # 持たせ、消すのは `_on_disconnected` の deleteLater に任せる
            ws.setParent(self)
            conn_id = str(id(ws))
            self._sockets[conn_id] = ws
            # conn_id は使い回されうるので、毎回入れ直す
            if remote:
                self._remote_conns.add(conn_id)
            else:
                self._remote_conns.discard(conn_id)
            # kind は register メッセージ受信時に確定するため、ここでは未定
            ws.textMessageReceived.connect(
                lambda text, cid=conn_id: self._on_text_message(cid, text)
            )
            # ADR-0005 D5: pong 受信を liveness 更新に使う。
            ws.pong.connect(lambda _e=0, _p=b"", cid=conn_id: self._on_pong(cid))
            ws.disconnected.connect(lambda cid=conn_id: self._on_disconnected(cid))
            logger.info(
                "New connection: %s%s",
                conn_id,
                f" (network, from {ws.peerAddress().toString()})" if remote else "",
            )
            self._greet(conn_id)

        # register 到達後に正規の再判定。このパスでは既にタイマー止まってるので no-op
        self._reassess_idle_shutdown()

    # ------------------------------------------------------------
    # メッセージ受信
    # ------------------------------------------------------------

    def _greet(self, conn_id: str) -> None:
        """挨拶の 1 通目（hello）を送り、register を待つ時間を計り始める。"""
        nonce = generate_nonce()
        self._hello_nonces[conn_id] = nonce
        self._send(
            conn_id, Hello(hub_version=__version__, protocol=SUPPORTED_PROTOCOL, nonce=nonce)
        )
        # conn_id は id(ws) なので、切れた接続の id が使い回されうる。nonce で照合する
        QTimer.singleShot(
            self._register_timeout_ms,
            lambda cid=conn_id, n=nonce: self._on_register_timeout(cid, n),
        )

    def _on_register_timeout(self, conn_id: str, nonce: str) -> None:
        if self._hello_nonces.get(conn_id) != nonce:
            return
        logger.warning(
            "No register from %s within %d ms; closing", conn_id, self._register_timeout_ms
        )
        ws = self._sockets.get(conn_id)
        if ws is not None:
            ws.abort()

    def _on_unauthenticated_message(self, conn_id: str, text: str) -> None:
        """挨拶の済んでいない接続から来たメッセージ。register だけを受け付ける。"""
        hub_nonce = self._hello_nonces.get(conn_id)
        try:
            msg = decode_message(text)
        except ProtocolDecodeError as e:
            logger.warning("Undecodable message from %s before register: %s", conn_id, e)
            self._refuse_before_register(conn_id, e.message_id)
            return
        if hub_nonce is None or not isinstance(msg, RegisterRequest):
            logger.warning("Message %s from %s before register; closing", msg.type, conn_id)
            self._refuse_before_register(conn_id, getattr(msg, "id", None))
            return

        error = check_register(msg, token=self._token_for(conn_id), hub_nonce=hub_nonce)
        if error is None and msg.role is Role.WORKER and conn_id in self._remote_conns:
            error = Error(
                code=ErrorCode.UNAUTHORIZED,
                message=(
                    "QGIS registers with the Hub on 127.0.0.1 of its own machine; the "
                    "network address accepts clients only"
                ),
            )
        if error is not None:
            logger.warning("Rejected register from %s: %s", conn_id, error_code_value(error.code))
            self._send(
                conn_id,
                RegisterAck(id=msg.id, ok=False, error=error, package_version=__version__),
            )
            self._close(conn_id)
            return

        del self._hello_nonces[conn_id]
        assert msg.nonce is not None  # check_register が確かめた
        self._sessions[conn_id] = _Session(
            hub_nonce=hub_nonce, client_nonce=msg.nonce, protocol_version=msg.protocol_version
        )
        try:
            self._handle_register(conn_id, msg)
        except Exception:
            logger.exception("Register error for conn=%s", conn_id)

    def _refuse_before_register(self, conn_id: str, message_id: object) -> None:
        """register の前に来たほかのメッセージを断って切る（id が分かれば答えてから）。"""
        if isinstance(message_id, str):
            self._send_error_response(
                conn_id,
                request_id=message_id,
                code=ErrorCode.UNAUTHORIZED,
                message="Send register (with a valid proof) before any other message",
            )
        self._close(conn_id)

    def _close(self, conn_id: str) -> None:
        ws = self._sockets.get(conn_id)
        if ws is not None:
            ws.close()

    def _ack(self, conn_id: str, req_id: str, **fields: object) -> RegisterAck:
        """register_ack を作る。挨拶の通った接続なら版と hub_proof を載せる。"""
        session = self._sessions.get(conn_id)
        return RegisterAck(
            id=req_id,
            **fields,  # type: ignore[arg-type]
            protocol_version=None if session is None else session.protocol_version,
            package_version=__version__,
            hub_proof=(
                None
                if session is None
                else hub_proof(self._token_for(conn_id), session.client_nonce, session.hub_nonce)
            ),
        )

    def _on_text_message(self, conn_id: str, text: str) -> None:
        if conn_id not in self._sessions:
            self._on_unauthenticated_message(conn_id, text)
            return
        try:
            msg = decode_message(text)
        except ProtocolDecodeError as e:
            self._reply_to_undecodable(conn_id, e)
            return

        try:
            self._dispatch(conn_id, msg)
        except Exception:
            logger.exception("Dispatch error for conn=%s type=%s", conn_id, msg.type)

    def _reply_to_undecodable(self, conn_id: str, e: ProtocolDecodeError) -> None:
        """読めなかったメッセージに、送り主が待ち続けずに済むよう答える（id が分かれば）。"""
        logger.warning("Decode error from %s: %s", conn_id, e)
        reply = reply_to_undecodable(e, receiver="Hub")
        if reply is not None:
            self._send(conn_id, reply)

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
        elif isinstance(msg, UpdateInfo):
            self._handle_update_info(conn_id, msg)
        elif isinstance(msg, AbandonLaunch):
            self._handle_abandon_launch(conn_id, msg)
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
        self._send(conn_id, self._ack(conn_id, req.id, ok=True))
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
            ack = self._ack(
                conn_id,
                req_id,
                ok=True,
                instance_id=outcome.instance_id,  # type: ignore[attr-defined]
                resumed=outcome.resumed,  # type: ignore[attr-defined]
                superseded=outcome.superseded,  # type: ignore[attr-defined]
            )
        else:
            ack = self._ack(
                conn_id,
                req_id,
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
        entry = (conn_id, target_conn)
        self._inflight[req.id] = entry
        # 待つ時間を決め、Worker が実行してよい期限（deadline）を付けて転送する。
        # 期限を過ぎて Worker に読まれた request は実行されない（WorkerState.on_request）。
        timeout_ms = effective_timeout_ms(req.timeout_ms)
        # ADR-0001 §12.7：Hub が register 時の role で caller_role を上書きして
        # Worker に転送する。Client 側からの申告（req.caller_role が既に
        # セットされていても）は信用しない。
        caller_role = self._client_roles.get(conn_id)
        forwarded = Request(
            id=req.id,
            command=req.command,
            params=req.params,
            instance=req.instance,
            timeout_ms=timeout_ms,
            deadline=time.time() + timeout_ms / 1000.0,
            caller_role=caller_role,
        )
        self._send(target_conn, forwarded)
        QTimer.singleShot(
            timeout_ms,
            lambda rid=req.id, e=entry, cmd=req.command, ms=timeout_ms: self._expire_request(
                rid, e, cmd, ms
            ),
        )

    def _expire_request(
        self, request_id: str, entry: tuple[str, str], command: str, timeout_ms: int
    ) -> None:
        """timeout_ms が過ぎても応答の無い request を送り主に instance_timeout で返す。"""
        # 応答済み・切断で片づいた・同じ id の別 request に置き換わった、なら何もしない
        if self._inflight.get(request_id) is not entry:
            return
        del self._inflight[request_id]
        origin, _target = entry
        logger.info("Request %s (%s) timed out after %d ms", request_id, command, timeout_ms)
        if origin not in self._sockets:
            return
        self._send(
            origin,
            Response(
                id=request_id,
                ok=False,
                error=Error(
                    code=ErrorCode.INSTANCE_TIMEOUT,
                    message=(
                        f"QGIS did not reply to {command!r} within {timeout_ms} ms. "
                        "If QGIS had not started it yet, QGIS drops it without running it; "
                        "if it had, it may still finish. Check the state before sending "
                        "it again."
                    ),
                    details={"timeout_ms": timeout_ms, "command": command},
                ),
            ),
        )

    def _handle_response(self, conn_id: str, resp: Response) -> None:
        """Worker → Hub → 発信元 Client への返送。

        受け付けるのは、その request を転送した Worker の接続から来た応答だけ。
        ほかの接続が同じ id で答えても、本物の応答の代わりに届けない。
        """
        entry = self._inflight.get(resp.id)
        if entry is None:
            # Hub が instance_timeout を返したあとに届いた応答もここに来る
            logger.info("Response for unknown request id=%s (timed out or abandoned)", resp.id)
            return
        origin, target = entry
        if target != conn_id:
            logger.warning(
                "Ignoring response id=%s from %s: the request went to %s",
                resp.id,
                conn_id,
                target,
            )
            return
        del self._inflight[resp.id]
        if origin not in self._sockets:
            logger.info("Originating client %s gone; dropping response", origin)
            return
        self._send(origin, resp)

    # ------------------------------------------------------------
    # list_instances
    # ------------------------------------------------------------

    def _handle_list_instances(self, conn_id: str, req: ListInstancesRequest) -> None:
        instances = self._state.list_instances(include_disconnected=req.include_disconnected)
        rejected = self._state.list_rejected() if req.include_rejected else []
        self._send(
            conn_id, ListInstancesResponse(id=req.id, instances=instances, rejected=rejected)
        )

    # ------------------------------------------------------------
    # update_info
    # ------------------------------------------------------------

    def _handle_abandon_launch(self, conn_id: str, msg: AbandonLaunch) -> None:
        """起こした側がもう待っていない launch_token を覚える（#16）。応答は無い。"""
        if self._kinds.get(conn_id) is not _ConnectionKind.CLIENT:
            logger.warning("abandon_launch from non-client connection %s ignored", conn_id)
            return
        logger.info("Launch abandoned by its starter: %s", msg.launch_token)
        self._state.abandon_launch(msg.launch_token)

    def _handle_update_info(self, conn_id: str, msg: UpdateInfo) -> None:
        """Worker の自己申告情報（project）を差し替える。応答は無い。"""
        if self._kinds.get(conn_id) is not _ConnectionKind.WORKER:
            logger.warning("update_info from non-worker connection %s ignored", conn_id)
            return
        if self._state.update_worker_project(conn_id, msg.project):
            logger.info("Worker %s project -> %r", msg.instance_id, msg.project)

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
        ws = self._sockets.pop(conn_id, None)
        if ws is not None:
            # いまはこの接続の disconnected の中なので、すぐには消さない
            ws.deleteLater()
        self._hello_nonces.pop(conn_id, None)
        self._sessions.pop(conn_id, None)
        self._remote_conns.discard(conn_id)
        logger.info("Disconnected: %s (kind=%s)", conn_id, kind)
        self._reassess_idle_shutdown()

    # ------------------------------------------------------------
    # 定期 sweep
    # ------------------------------------------------------------

    def _on_pong(self, conn_id: str) -> None:
        if self._state.mark_seen(conn_id):
            logger.info("Worker %s is responding again (heartbeat resumed)", conn_id)

    def _on_sweep(self) -> None:
        # ADR-0005 D5: まず生存確認 ping を撒く。pong が途絶えた active は
        # unresponsive の印を付けるだけで、entry もルーティングも残す — QGIS の
        # GUI スレッドが塞がっているだけなら pong が戻り次第 active に復帰する。
        for ws in self._sockets.values():
            try:
                ws.ping()
            except Exception:  # noqa: BLE001 - ping は best-effort
                logger.debug("ws.ping() failed", exc_info=True)
        flagged = self._state.sweep_unresponsive()
        if flagged:
            # 普通の起動でも project 読み込みで 17 秒台まで伸びる（実測）ので、
            # ここは異常ではない。warning にすると毎回鳴って無視される。
            logger.info(
                "%d worker(s) stopped answering heartbeat (socket still open; "
                "marked unresponsive, will recover on next pong — normal while "
                "QGIS loads a project): %s",
                len(flagged),
                flagged,
            )

        # unresponsive が UNRESPONSIVE_CLOSE_SECONDS 続いたら half-open とみなして
        # Hub 側から閉じる。閉じると `disconnected` → grace の通常経路に乗り、
        # 実は生きていた Worker はループが空いた時点で自動再接続してくる。
        for conn_id in self._state.sweep_unresponsive_expired():
            ws = self._sockets.get(conn_id)
            logger.warning(
                "Worker %s unresponsive for too long; closing its socket "
                "(half-open cleanup / forces reconnect if it is alive)",
                conn_id,
            )
            if ws is not None:
                ws.abort()

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
        """全接続 0 なら idle 自動終了タイマー起動、そうでなければキャンセル。

        ネットワークから受け付ける Hub は人が常駐させるので、自分では止まらない。
        """
        if self._remote is None and self._state.should_idle_shutdown():
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
# ネットワークのアドレス
# ============================================================


def _listen_host_address(host: str) -> QHostAddress:
    if host == "0.0.0.0":
        return QHostAddress(QHostAddress.AnyIPv4)
    if host == "::":
        return QHostAddress(QHostAddress.Any)
    return QHostAddress(host)


def _format_address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def network_addresses(host: str, port: int) -> tuple[str, ...]:
    """接続情報ファイルに書くアドレス。

    ``0.0.0.0`` ならこの機械のループバック以外の IPv4 のアドレスを、``::`` なら
    IPv6（リンクローカルを除く）も並べる。それ以外は ``host`` そのもの。
    """
    if host not in ("0.0.0.0", "::"):
        return (_format_address(host, port),)
    found: list[str] = []
    for address in QNetworkInterface.allAddresses():
        if address.isLoopback():
            continue
        protocol = address.protocol()
        ipv4 = protocol == QAbstractSocket.IPv4Protocol
        ipv6 = (
            host == "::" and protocol == QAbstractSocket.IPv6Protocol and not address.isLinkLocal()
        )
        if ipv4 or ipv6:
            found.append(_format_address(address.toString(), port))
    return tuple(dict.fromkeys(found))


def parse_listen(value: str) -> tuple[str, int]:
    """``--listen`` の ``"<IP>:<port>"`` を (host, port) にする。

    Raises:
        ValueError: 形が違う・IP アドレスでない・ポートが 1〜65535 でない
    """
    host, port = split_address(value)
    if not 1 <= port <= 65535:
        raise ValueError(
            f"--listen needs a fixed port from 1 to 65535, not {port} (a port the OS "
            "picks would change on each start and break the copied Hub file)"
        )
    if host not in ("0.0.0.0", "::") and QHostAddress(host).isNull():
        raise ValueError(f"--listen needs an IP address, not {host!r}")
    return host, port


# ============================================================
# エントリポイント
# ============================================================


def _setup_logging(hub_file: Path) -> None:
    """ローテーション付きのログを Hub ファイルの隣（``hub_log_path``）に書く。

    Hub ファイルごとに分かれるので、別の Hub（pytest の session など）と混ざらない。
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_path = hub_log_path(hub_file)
    try:
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
    except OSError as e:
        print(f"qgis-puppeteer Hub: cannot open the log {log_path}: {e}", file=sys.stderr)
    else:
        handlers.append(rotating)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """CLI 引数をパースする。"""
    parser = argparse.ArgumentParser(
        prog="python -m qgis_puppeteer.hub",
        description="QGIS Puppeteer Hub.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=HUB_PORT,
        help="WebSocket listen port on 127.0.0.1 (default: 0, chosen by the OS).",
    )
    parser.add_argument(
        "--hub-file",
        default=None,
        help=(
            "Where to write the Hub file (address and token for clients). "
            "Default: $QPUPPETEER_HUB_FILE, else the per-user default location."
        ),
    )
    parser.add_argument(
        "--listen",
        default=None,
        metavar="ADDR:PORT",
        help=(
            "Also accept clients from the network on ADDR:PORT, over TLS only "
            "(e.g. 0.0.0.0:9876). Writes remote/hub.json to copy to the client machine."
        ),
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="With --listen: make a new network token and TLS key (copy hub.json again).",
    )
    parser.add_argument(
        "--remote-dir",
        default=None,
        help=(
            "With --listen: where the network token, TLS key and remote hub.json live. "
            "Default: the per-user qgis_puppeteer/remote folder."
        ),
    )
    # テストで register 待ちの時間切れを短くするため（ふだんは使わない）
    parser.add_argument(
        "--register-timeout-ms",
        type=int,
        default=REGISTER_TIMEOUT_MS,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.listen is None and (args.rotate or args.remote_dir is not None):
        parser.error("--rotate and --remote-dir need --listen")
    if args.listen is not None:
        try:
            args.listen = parse_listen(args.listen)
        except ValueError as e:
            parser.error(str(e))
    return args


def main(argv: list[str] | None = None) -> int:
    """python -m qgis_puppeteer.hub のエントリポイント。

    終了コードはモジュールの docstring のとおり（``HUB_EXIT_*``）。
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    hub_file = resolve_hub_file_path(args.hub_file)
    _setup_logging(hub_file)

    # 同じ Hub ファイルの生きた Hub を上書きしない（つないでいる相手の token が変わる）
    live = find_live_hub(hub_file)
    if live is not None:
        logger.info(
            "A live Hub already uses %s (pid %s, %s); exiting",
            hub_file,
            live.pid,
            ", ".join(live.addresses),
        )
        return HUB_EXIT_ALREADY_RUNNING

    remote: RemoteListen | None = None
    if args.listen is not None:
        remote_dir = Path(args.remote_dir) if args.remote_dir else default_remote_dir()
        try:
            identity = load_or_create_remote_identity(remote_dir, rotate=args.rotate)
        except OSError as e:
            logger.error("Could not keep the network token and TLS key in %s: %s", remote_dir, e)
            return HUB_EXIT_HUB_FILE_FAILED
        host, port = args.listen
        remote = RemoteListen(
            host=host, port=port, identity=identity, hub_file=remote_dir / REMOTE_HUB_FILE
        )

    app = QCoreApplication(sys.argv)
    hub = Hub(
        port=args.port,
        hub_file=hub_file,
        register_timeout_ms=args.register_timeout_ms,
        remote=remote,
    )

    try:
        hub.start()
    except HubFileWriteError as e:
        logger.error("%s", e)
        return HUB_EXIT_HUB_FILE_FAILED
    except OSError as e:
        logger.error("%s", e)
        return HUB_EXIT_BIND_FAILED

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
