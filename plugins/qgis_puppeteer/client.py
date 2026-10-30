"""AutomationClient：Hub への asyncio WebSocket クライアント。

ADR-0001 §2, §3 に基づき、McpGateway やテストハーネスから Worker を
呼び出すための汎用クライアントを提供する。

## 責務

- Hub に `role=automation_client` として接続・登録。Hub の hello に、Hub ファイル
  （`qgis_puppeteer.hub_file`）の token で作った proof で答え、Hub の
  hub_proof を確かめる（`qgis_puppeteer.handshake`）
- `request` を送って `id` で対応する `response` を待機
- `list_instances` で現在の active Worker 一覧を取得
- エラー応答を Python 例外として伝播

## 使い方

```python
async with AutomationClient() as client:
    instances = await client.list_instances()
    result = await client.call("qgis_list_layers", instance="main")
```

## スレッドモデル

単一の asyncio イベントループ内で動く。`connect()` 後に reader タスクが
走り、受信メッセージを `id` に紐づく Future に dispatch する。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from qgis_puppeteer._version import __version__
from qgis_puppeteer.handshake import (
    ack_is_trusted,
    choose_protocol_version,
    client_proof,
    generate_nonce,
)
from qgis_puppeteer.hub_file import read_hub_file, resolve_hub_file_path
from qgis_puppeteer.protocol import (
    SUPPORTED_PROTOCOL,
    AbandonLaunch,
    Error,
    ErrorCode,
    Hello,
    InstanceInfo,
    ListInstancesRequest,
    ListInstancesResponse,
    Message,
    MessageType,
    ProtocolDecodeError,
    RegisterAck,
    RegisterRequest,
    RejectedRegistration,
    Request,
    Response,
    Role,
    decode_message,
    encode_message,
    error_code_value,
)

logger = logging.getLogger("qgis_puppeteer.client")

DEFAULT_HUB_URL: str = "ws://127.0.0.1:9876"
# Origin ヘッダはデフォルトで "http://localhost"（ADR-0001 §9.2 で許可）
DEFAULT_ORIGIN: str = "http://localhost"
DEFAULT_REQUEST_TIMEOUT_SECONDS: float = 30.0
# call() は Hub に timeout_ms を渡し、Hub が時間切れを instance_timeout で返す。
# その返事が届くのを待つため、クライアントは timeout_ms よりこれだけ長く待つ
_HUB_REPLY_GRACE_SECONDS: float = 1.0
DEFAULT_REGISTER_TIMEOUT_SECONDS: float = 5.0
# connect() 全体のタイムアウト（TCP + WebSocket ハンドシェイク）。
# Hub 不在時に Windows の TCP SYN retry（〜20-40s）で Gateway がブロックするのを防ぐ。
# ローカル接続前提なので 2 秒あれば十分。
DEFAULT_CONNECT_TIMEOUT_SECONDS: float = 2.0


# ============================================================
# 例外型
# ============================================================


class AutomationClientError(Exception):
    """AutomationClient の基底例外。"""


class NotConnectedError(AutomationClientError):
    """Hub との接続が無い、または応答を待つ間に切れた。

    この型そのものは「送ったあとで切れた」ときに出る。要求は QGIS 側で実行
    されたかもしれないので、黙って送り直してはいけない。送る前に分かったときは
    子の :class:`RequestNotSentError` になる。
    """


class RequestNotSentError(NotConnectedError):
    """要求を送る前に、接続が無い・切れていると分かった。

    要求は Hub に届いていないので、接続し直して同じ要求を送り直してよい。
    """


class RegisterError(AutomationClientError):
    """register_ack が ok=False を返した。"""

    def __init__(self, error: Error | None) -> None:
        self.error = error
        msg = f"register failed: {error_code_value(error.code) if error else 'unknown'}"
        if error:
            msg += f" ({error.message})"
        super().__init__(msg)


class HubAuthError(AutomationClientError):
    """つないだ相手を Hub として信用できない。

    挨拶（hello）が来ない・hello でない（Hub ではない、または挨拶の無い古い Hub）、
    または register_ack の hub_proof が合わない（Hub ファイルの token を知らない
    相手）。相手には何も頼んでいない。
    """


class RequestError(AutomationClientError):
    """response.ok=False のエラー応答。"""

    def __init__(self, error: Error) -> None:
        self.error = error
        super().__init__(f"{error_code_value(error.code)}: {error.message}")

    @property
    def code(self) -> ErrorCode | str:
        """エラーコード。この版の知らないコードは文字列のまま（``Error.code``）。"""
        return self.error.code

    @property
    def details(self) -> dict[str, Any]:
        return self.error.details


class AttributionAmbiguousError(AutomationClientError):
    """ADR-0005 H4: best-effort attribution（registered_seq）で複数候補が
    出て「自分の起動分」を一意に決められない。silent 誤帰属を防ぐため送出。
    """


class RegistrationRejectedError(AutomationClientError):
    """待っていた Worker の登録を Hub が断った（#13）。

    断られた Worker はつなぎ直さないので、待ち続けても登録されない。
    ``rejection`` に Hub の記録（理由と、断られた QGIS の ``pid``）が入る。
    """

    def __init__(self, rejection: RejectedRegistration) -> None:
        self.rejection = rejection
        super().__init__(
            f"Hub rejected the registration of launch_token={rejection.launch_token} "
            f"(pid={rejection.pid}, label={rejection.label!r}): "
            f"{rejection.code}: {rejection.message}"
        )


def find_rejection(
    selector: str, rejected: list[RejectedRegistration]
) -> RejectedRegistration | None:
    """``selector`` が launch_token なら、その token の断られた登録を返す（#13）。"""
    for rec in reversed(rejected):
        if rec.launch_token == selector:
            return rec
    return None


class WorkerCodeError(AutomationClientError):
    """``execute_python`` 系で Worker 側のコード実行が例外で失敗した。

    Worker handler は ``exec(code)`` 内の例外を握って
    ``{"success": False, "error": ..., "traceback": ...}`` を返す（応答自体は
    プロトコル上 ``ok=True``）。pytest ラッパはこの ``success=False`` を検出して
    本例外へ変換し、Playwright ``page.evaluate`` と同様に fail-fast させる。

    ``worker_traceback`` に Worker 側の完全なトレースバックが入るので、真因
    （DB エラー等）が pytest の失敗メッセージへ直接現れる。
    """

    def __init__(
        self,
        error: str | None,
        traceback: str | None = None,
        *,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        self.worker_error = error
        self.worker_traceback = traceback
        self.stdout = stdout
        self.stderr = stderr
        msg = error or "worker code execution failed"
        if traceback:
            msg = f"{msg}\n--- worker traceback ---\n{traceback.rstrip()}"
        super().__init__(msg)


class ConfirmationRequiredError(AutomationClientError):
    """``execute_python`` が permission confirm ゲートで弾かれた。

    信頼モード未設定（``QPUPPETEER_TRUSTED_MODE`` 未設定）の Worker で
    whitelist 外コードを実行しようとすると、Worker は実行せずに
    ``requires_confirmation`` を返す。素の戻り値は ``success=False`` の dict なので
    見落とすと silent に誤る。これを明示例外にして原因を即わかるようにする。
    （確認 UI は存在しないので、ここで待たされることはない。）
    """

    def __init__(self, risk_level: str | None = None) -> None:
        self.risk_level = risk_level
        super().__init__(
            "execute_python requires confirmation: the worker is not in trusted "
            "mode. Set QPUPPETEER_TRUSTED_MODE=1 on the worker (or whitelist the "
            f"code). risk_level={risk_level}"
        )


class NonSerializableResultError(AutomationClientError):
    """``execute_python`` の ``_result`` が JSON 直列化できなかった。

    QGIS の layer / geometry など live オブジェクトを ``_result`` に入れると
    wire を越えられない。silent に repr 文字列へ化けると「本物の値」と誤認して
    テストが静かに誤るため、値モードの ``execute_python`` は本例外で loud に倒す。
    値が欲しい場合は Worker 側で素データへ変換すること
    （例: ``_result = layer.name()`` / ``layer.featureCount()``）。
    """

    def __init__(self, result_type: str | None = None, result_repr: str | None = None) -> None:
        self.result_type = result_type
        self.result_repr = result_repr
        super().__init__(
            f"execute_python result is not JSON-serializable (type={result_type}). "
            "Convert it to plain data in the worker before returning "
            "(e.g. layer.name(), layer.featureCount()). "
            f"repr: {result_repr or '?'}"
        )


# ============================================================
# AutomationClient
# ============================================================


class AutomationClient:
    """Hub に接続して Request / ListInstances を送る asyncio クライアント。

    典型的な使い方は `async with` ブロック：

        async with AutomationClient() as client:
            result = await client.call("cmd", {"k": 1}, instance="main")

    connect() / close() を明示的に呼ぶ使い方も可能。
    """

    def __init__(
        self,
        *,
        url: str = DEFAULT_HUB_URL,
        origin: str = DEFAULT_ORIGIN,
        role: Role = Role.AUTOMATION_CLIENT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        register_timeout: float = DEFAULT_REGISTER_TIMEOUT_SECONDS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        token: str | None = None,
        hub_file: str | os.PathLike[str] | None = None,
    ) -> None:
        """
        Args:
            role: Hub に自己申告する role。McpGateway は `Role.MCP_GATEWAY` を、
                pytest / その他の直接呼び出しは既定の `Role.AUTOMATION_CLIENT`
                を使う（ADR-0001 §12.7 caller_role 伝達）。
            token: 挨拶に使う token。省略時は Hub ファイルから読む
            hub_file: token を読む Hub ファイル。省略時は ``QPUPPETEER_HUB_FILE``、
                それも無ければ既定の場所（``qgis_puppeteer.hub_file``）。
                ``connect()`` のたびに読む（Hub が起き直すと token が変わる）
        """
        if role == Role.WORKER:
            raise ValueError("AutomationClient cannot register as WORKER role")
        self._url = url
        self._origin = origin
        self._role = role
        self._request_timeout = request_timeout
        self._register_timeout = register_timeout
        self._connect_timeout = connect_timeout
        self._token = token
        self._hub_file = hub_file
        # register_ack で分かる Hub の版
        self._hub_package_version: str | None = None

        self._ws: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        # 送信済みリクエスト id → 応答を待つ Future
        self._pending: dict[str, asyncio.Future[Message]] = {}
        # 切断フラグ（close 中の reader 例外を抑止するため）
        self._closing: bool = False
        # reader が終わった（Hub 側から切れた）。以後の送信は届かないので、
        # 送る前に RequestNotSentError にする
        self._lost: bool = False

    # ------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------

    @property
    def hub_package_version(self) -> str | None:
        """つないでいる Hub のライブラリの版（connect 前は None）。"""
        return self._hub_package_version

    async def __aenter__(self) -> AutomationClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Hub に接続し、挨拶して register_ack まで完了させる。

        接続全体に `connect_timeout` を課す。ローカル前提なので短時間で
        成否を確定させたい（特に Windows では TCP SYN の OS retry が長く、
        Hub 不在時に Gateway が数十秒ブロックするのを避ける）。

        タイムアウト時は `ConnectionRefusedError` を投げる（呼び出し側は
        接続不能を単一のエラー型で扱えるようにするため）。

        Raises:
            ConnectionRefusedError / OSError: つながらない
            HubFileError: token を引数で渡さず、Hub ファイルが無い・読めない
                （``OSError`` の子）
            HubAuthError: 相手を Hub として信用できない
            RegisterError: Hub が登録を断った（``unauthorized`` /
                ``protocol_version_mismatch`` など）
        """
        if self._ws is not None:
            if not self._lost:
                return
            # 切れた接続を抱えたまま。捨てて張り直す
            await self._hard_close()
        self._lost = False
        self._closing = False

        # Origin ヘッダを明示（Hub 側で完全一致検証されるため）
        try:
            self._ws = await connect(
                self._url,
                additional_headers={"Origin": self._origin},
                open_timeout=self._connect_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError) as e:
            # Python 3.11+ では両者は同一だが、古い環境でも安全に拾えるよう両方 catch
            raise ConnectionRefusedError(
                f"Hub connection timed out after {self._connect_timeout}s at {self._url}"
            ) from e

        try:
            # 挨拶の 1 通目は reader を起こす前に自分で受ける
            hello = await self._receive_hello()
            token = self._resolve_token()
            # reader タスクを起動してから register を送る
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name="AutomationClient.reader"
            )
            await self._register(hello, token)
        except BaseException:
            # 登録失敗時は掃除して再送できる状態にする
            await self._hard_close()
            raise

    async def close(self) -> None:
        """切断。reader タスクを止めて WebSocket を閉じる。"""
        if self._ws is None:
            return
        self._closing = True
        # 送信中の pending を解放（呼び出し側が await していたら例外を上げる）
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(NotConnectedError("client closed"))
        self._pending.clear()
        await self._hard_close()

    async def _hard_close(self) -> None:
        """WebSocket と reader タスクを確実に終了させる。"""
        ws = self._ws
        task = self._reader_task
        self._ws = None
        self._reader_task = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("ws.close() raised", exc_info=True)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------
    # register
    # ------------------------------------------------------------

    async def _receive_hello(self) -> Hello:
        """Hub の挨拶（hello）を待つ。来ない・hello でなければ HubAuthError。"""
        assert self._ws is not None
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self._register_timeout)
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise HubAuthError(
                f"{self._url} sent no hello within {self._register_timeout}s: it is not a "
                "qgis-puppeteer Hub, or a Hub too old to greet (protocol 1)"
            ) from e
        except websockets.exceptions.ConnectionClosed as e:
            raise NotConnectedError(f"the Hub closed the connection before its hello: {e}") from e
        msg: Message | None = None
        if isinstance(raw, str):
            with contextlib.suppress(ProtocolDecodeError):
                msg = decode_message(raw)
        if not isinstance(msg, Hello):
            raise HubAuthError(
                f"{self._url} did not greet with hello: it is not a qgis-puppeteer Hub, "
                "or a Hub too old to greet (protocol 1)"
            )
        return msg

    def _resolve_token(self) -> str:
        """挨拶に使う token：引数か、Hub ファイル（毎回読む）。"""
        if self._token is not None:
            return self._token
        return read_hub_file(resolve_hub_file_path(self._hub_file)).token

    async def _register(self, hello: Hello, token: str) -> None:
        req_id = self._new_id()
        nonce = generate_nonce()
        fut = self._make_future(req_id)
        await self._send(
            RegisterRequest(
                id=req_id,
                role=self._role,
                protocol_version=choose_protocol_version(hello.protocol),
                protocol=SUPPORTED_PROTOCOL,
                package_version=__version__,
                nonce=nonce,
                proof=client_proof(token, hello.nonce, nonce),
            )
        )
        try:
            msg = await asyncio.wait_for(fut, timeout=self._register_timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(req_id, None)
            raise RegisterError(None) from e

        if not isinstance(msg, RegisterAck):
            raise RegisterError(
                Error(
                    code=ErrorCode.INVALID_COMMAND,
                    message=f"unexpected reply type: {msg.type.value}",
                )
            )
        if not ack_is_trusted(msg, token=token, client_nonce=nonce, hub_nonce=hello.nonce):
            raise HubAuthError(
                f"{self._url} answered register without a valid Hub proof: it does not "
                "know the token in the Hub file"
            )
        if not msg.ok:
            raise RegisterError(msg.error)
        self._hub_package_version = msg.package_version

    # ------------------------------------------------------------
    # 外部 API
    # ------------------------------------------------------------

    async def call(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        *,
        instance: str | None = None,
        timeout_ms: int | None = None,
    ) -> Any:
        """Worker にコマンドを送って結果を受け取る。

        Args:
            command: Worker 側で解釈するコマンド名
            params: コマンド引数
            instance: selector（label / instance_id / project basename / pid）
            timeout_ms: 待つ時間（ミリ秒）。省略すると ``request_timeout``。Hub に
                渡し、Hub はこの時間が過ぎても QGIS が応答しなければ
                ``instance_timeout`` を返す。QGIS がまだ読んでいなかった要求は
                実行されずに捨てられる

        Returns:
            Response.result（任意の JSON 値）

        Raises:
            RequestNotSentError: 送る前に接続が無い・切れていると分かった。
                要求は届いていないので送り直してよい
            NotConnectedError: 送ったあと応答の前に接続が切れた。要求は実行
                されたかもしれない
            RequestError: response.ok=False（Hub / Worker のエラー応答）。時間内に
                QGIS が応答しなかったときは code が ``instance_timeout``（要求は
                実行されたかもしれない）
            asyncio.TimeoutError: Hub が時間内に返事をしなかった。要求は実行
                されたかもしれない
        """
        self._require_connected()

        if timeout_ms is None:
            timeout_ms = max(1, int(self._request_timeout * 1000))
        req_id = self._new_id()
        fut = self._make_future(req_id)
        await self._send(
            Request(
                id=req_id,
                command=command,
                params=params or {},
                instance=instance,
                timeout_ms=timeout_ms,
            )
        )
        msg = await self._await_reply(req_id, fut, timeout_ms / 1000.0 + _HUB_REPLY_GRACE_SECONDS)

        if not isinstance(msg, Response):
            raise RequestError(
                Error(
                    code=ErrorCode.INVALID_COMMAND,
                    message=f"unexpected reply type: {msg.type.value}",
                )
            )
        if not msg.ok:
            # プロトコル規約上、ok=False なら error は必ず入っている（protocol.py
            # の Response 検証で欠落は弾く）。ここで None を許すと RequestError
            # が None を抱えて呼び出し側がさらに混乱するので、`-O` 実行で assert
            # が消えても壊れないよう明示的に防御する。
            if msg.error is None:
                raise RequestError(
                    Error(
                        code=ErrorCode.INVALID_COMMAND,
                        message=("protocol violation: response ok=False carried no error payload"),
                    )
                )
            raise RequestError(msg.error)
        return msg.result

    async def list_instances(self, *, include_disconnected: bool = False) -> list[InstanceInfo]:
        """Hub から Worker の一覧を取得する。

        既定は接続中（``active`` / ``unresponsive``）のみ。
        ``include_disconnected=True`` で grace 中（切断済み・再接続待ち）の
        entry も ``state="disconnected"`` として含める（診断用）。
        """
        self._require_connected()
        req_id = self._new_id()
        fut = self._make_future(req_id)
        await self._send(ListInstancesRequest(id=req_id, include_disconnected=include_disconnected))
        msg = await self._await_reply(req_id, fut, self._request_timeout)
        if not isinstance(msg, ListInstancesResponse):
            raise RequestError(
                Error(
                    code=ErrorCode.INVALID_COMMAND,
                    message=f"unexpected reply type: {msg.type.value}",
                )
            )
        return list(msg.instances)

    async def abandon_launch(self, launch_token: str) -> None:
        """この launch_token で起こした QGIS をもう待たない、と Hub に知らせる（#16）。

        Hub はこの token の登録を以後断り、その QGIS は自分で閉じる。応答は
        待たない（古い Hub は読めずにエラーを返すが、知らない id の返事として
        捨てられる）。
        """
        self._require_connected()
        await self._send(AbandonLaunch(id=self._new_id(), launch_token=launch_token))

    async def list_rejected(self) -> list[RejectedRegistration]:
        """Hub が断った Worker の登録を返す（#13、`RejectedRegistration`）。

        旧 Hub は覚えていないので、常に空になる。
        """
        self._require_connected()
        req_id = self._new_id()
        fut = self._make_future(req_id)
        await self._send(ListInstancesRequest(id=req_id, include_rejected=True))
        msg = await self._await_reply(req_id, fut, self._request_timeout)
        if not isinstance(msg, ListInstancesResponse):
            raise RequestError(
                Error(
                    code=ErrorCode.INVALID_COMMAND,
                    message=f"unexpected reply type: {msg.type.value}",
                )
            )
        return list(msg.rejected)

    async def current_max_seq(self) -> int:
        """現在の最大 registered_seq を返す（ADR-0005 D6 best-effort 相関の基点）。

        QGIS を起動依頼する **直前** に呼んで戻り値を控えておき、起動後に
        ``wait_for_new_instance(since_seq=...)`` に渡すと「自分が依頼した後に
        register された 1 件」を拾える。規約なし非制御 launch（U8）向けの
        当て推量経路で、同時起動が重なると曖昧になり得る点に注意。
        """
        instances = await self.list_instances()
        return max((i.registered_seq for i in instances), default=0)

    async def wait_for_instance(
        self,
        selector: str,
        *,
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.5,
    ) -> InstanceInfo:
        """selector が一意に解決するまで poll する（ADR-0005 D6 / U9）。

        ``launch_token`` を渡せば公式 launch helper 起動分を **決定的** に
        待ち受けできる（推奨）。``label`` でも可。

        ``state == "active"`` の instance だけを候補にする。register 直後に
        プロジェクト読み込みで GUI スレッドが塞がると Hub 上は unresponsive に
        なるが、それは「まだ使えない」なので待ち続ける。

        Raises:
            TimeoutError: ``timeout_s`` 以内に一意解決しなかった。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            instances = [i for i in await self.list_instances() if i.state == "active"]
            match = _match_selector(selector, instances)
            if match is not None:
                return match
            if loop.time() >= deadline:
                raise TimeoutError(f"No instance matched {selector!r} within {timeout_s}s")
            await asyncio.sleep(poll_interval_s)

    async def wait_for_ready(
        self,
        selector: str | None = None,
        *,
        timeout_s: float = 60.0,
        poll_interval_s: float = 0.5,
        probe_command: str = "qgis_get_canvas_extent",
        require_project: bool = False,
    ) -> InstanceInfo:
        """instance が **使える** 状態になるまで待つ。

        「一覧に載っている」と「呼び出しが処理される」は別物。register 直後は
        プロジェクト読み込みで GUI スレッドが塞がり、Hub 上は unresponsive に
        なる。ここでは 2 段で確認する:

        1. ``state == "active"`` の instance が selector に一意に解決する
           （selector 省略時は active が 1 台だけであること）
        2. その instance に軽い read-only コマンド（既定 ``qgis_get_canvas_extent``）
           を投げて応答が返る（= GUI スレッドが空いている）

        2 の往復は GUI が塞がっていれば空くまで返らないので、残り時間いっぱい
        待つ。

        **ready ≠ プロジェクト読み込み完了。** QGIS は起動直後、プロジェクトを
        読み始める前に一瞬 GUI が空く。実測では ready が 11 秒、レイヤが出そろう
        のが 31 秒で、20 秒ずれた。レイヤを触りたいなら ``require_project=True``
        を付ける — ``project`` は ``iface.projectRead``（読み込み **完了後**）で
        Hub に届くので、それが入ってから probe する。プロジェクトを開かない
        QGIS では永遠に満たされないので、その場合は付けないこと。

        Raises:
            TimeoutError: ``timeout_s`` 以内に使える状態にならなかった。
            RequestError(instance_ambiguous): selector 省略で active が複数。
            RegistrationRejectedError: selector が launch_token で、その Worker の
                登録を Hub が断った（待っても登録されないので待たない）。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            actives = [i for i in await self.list_instances() if i.state == "active"]
            if require_project:
                actives = [i for i in actives if i.project]
            candidate: InstanceInfo | None
            if selector is None:
                if len(actives) > 1:
                    raise RequestError(
                        Error(
                            code=ErrorCode.INSTANCE_AMBIGUOUS,
                            message=f"{len(actives)} active instances; pass a selector",
                            details={"candidates": [i.instance_id for i in actives]},
                        )
                    )
                candidate = actives[0] if actives else None
            else:
                candidate = _match_selector(selector, actives)
                if candidate is None:
                    rejection = find_rejection(selector, await self.list_rejected())
                    if rejection is not None:
                        raise RegistrationRejectedError(rejection)

            if candidate is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await self.call(
                        probe_command,
                        {},
                        instance=candidate.instance_id,
                        timeout_ms=max(1, int(remaining * 1000)),
                    )
                    return candidate
                except RequestError as e:
                    # Worker が応答したなら（コマンド不明でも）経路は通っている。
                    # 経路そのものの失敗だけ「まだ」とみなして待ち直す。
                    if e.code not in (
                        ErrorCode.INSTANCE_NOT_FOUND,
                        ErrorCode.INSTANCE_DISCONNECTED,
                        ErrorCode.INSTANCE_TIMEOUT,
                    ):
                        return candidate
                except asyncio.TimeoutError:
                    break

            if loop.time() >= deadline:
                break
            await asyncio.sleep(poll_interval_s)

        raise TimeoutError(
            f"No instance became ready within {timeout_s}s"
            + (f" for selector {selector!r}" if selector is not None else "")
            + (" (require_project: no project loaded yet)" if require_project else "")
        )

    async def wait_for_new_instance(
        self,
        since_seq: int,
        *,
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.5,
    ) -> InstanceInfo:
        """``registered_seq > since_seq`` の新規 1 件を待つ（ADR-0005 D6 / U8）。

        best-effort 経路。ADR-0005 H4: **silent に取り違えない**。窓内に複数の
        新規 instance が現れたら「自分の起動分」を一意に決められないので
        ``AttributionAmbiguousError`` を送出する（黙って先頭を返さない）。
        決定的にしたい場合は公式 launch helper + ``wait_for_instance(token)``。

        Raises:
            TimeoutError: ``timeout_s`` 以内に新規 instance が現れなかった。
            AttributionAmbiguousError: 窓内に 2 件以上の新規 instance が出現。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            instances = await self.list_instances()
            newer = [i for i in instances if i.registered_seq > since_seq and i.state == "active"]
            if len(newer) > 1:
                raise AttributionAmbiguousError(
                    f"{len(newer)} instances registered after seq={since_seq}; "
                    "cannot attribute deterministically — use the official "
                    "launch helper + wait_for_instance(launch_token)"
                )
            if newer:
                return newer[0]
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"No instance registered after seq={since_seq} within {timeout_s}s"
                )
            await asyncio.sleep(poll_interval_s)

    # ------------------------------------------------------------
    # 内部ヘルパ
    # ------------------------------------------------------------

    def _require_connected(self) -> None:
        if self._ws is None:
            raise RequestNotSentError("connect() has not completed")
        if self._lost:
            raise RequestNotSentError("connection to the Hub was lost")

    def _new_id(self) -> str:
        return uuid.uuid4().hex

    def _make_future(self, req_id: str) -> asyncio.Future[Message]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Message] = loop.create_future()
        self._pending[req_id] = fut
        return fut

    async def _await_reply(
        self,
        req_id: str,
        fut: asyncio.Future[Message],
        timeout_s: float,
    ) -> Message:
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            # ペンディングから除去（遅延応答は読み捨てになる）
            self._pending.pop(req_id, None)
            raise

    async def _send(self, msg: Message) -> None:
        """送る。送れなかったら待っている Future を外して RequestNotSentError。

        websockets は閉じた（閉じかけの）接続への send を書き込む前に
        ConnectionClosed で断るので、ここで落ちた要求は Hub に届いていない。
        """
        assert self._ws is not None
        try:
            await self._ws.send(encode_message(msg))
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            msg_id = getattr(msg, "id", None)
            if msg_id is not None:
                self._pending.pop(msg_id, None)
            raise RequestNotSentError(f"could not send to the Hub: {e}") from e

    # ------------------------------------------------------------
    # reader：受信ループ
    # ------------------------------------------------------------

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    # バイナリは非対応（プロトコルは text のみ）
                    logger.warning("Ignoring binary frame from hub")
                    continue
                try:
                    msg = decode_message(raw)
                except ProtocolDecodeError as e:
                    logger.warning("Decode error: %s", e)
                    continue
                self._dispatch(msg)
        except websockets.exceptions.ConnectionClosed:
            if not self._closing:
                logger.info("Hub connection closed unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reader loop crashed")
        finally:
            self._lost = True
            self._fail_pending(NotConnectedError("connection closed"))

    def _dispatch(self, msg: Message) -> None:
        """受信メッセージを id から pending Future に紐づける。"""
        if msg.type in (
            MessageType.REGISTER_ACK,
            MessageType.RESPONSE,
            MessageType.LIST_INSTANCES_RESPONSE,
        ):
            fut = self._pending.pop(msg.id, None)  # type: ignore[attr-defined]
            if fut is None or fut.done():
                logger.debug(
                    "Dropping reply with unknown or completed id=%s",
                    getattr(msg, "id", None),
                )
                return
            fut.set_result(msg)
        else:
            logger.warning("Unexpected inbound type %s", msg.type)

    def _fail_pending(self, exc: BaseException) -> None:
        """切断時、未完了の Future を全部失敗させる。"""
        pending = self._pending
        self._pending = {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)


def _match_selector(selector: str, instances: list[InstanceInfo]) -> InstanceInfo | None:
    """selector → InstanceInfo（無 / 曖昧は None）。

    ADR-0005 H-1: 解決順は Hub と共有する純関数 ``select_by_selector`` に
    一本化（drift 排除）。`wait_for_instance` のクライアント側 poll で使う。
    """
    from qgis_puppeteer.hub_state import select_by_selector

    matched = select_by_selector(selector, instances)
    return matched[0] if len(matched) == 1 else None
