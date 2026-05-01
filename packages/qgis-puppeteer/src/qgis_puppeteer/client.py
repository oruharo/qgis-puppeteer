"""AutomationClient：Hub への asyncio WebSocket クライアント。

ADR-0001 §2, §3 に基づき、McpGateway やテストハーネスから Worker を
呼び出すための汎用クライアントを提供する。

## 責務

- Hub に `role=automation_client` として接続・登録
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
import uuid
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from qgis_puppeteer.protocol import (
    Error,
    ErrorCode,
    InstanceInfo,
    ListInstancesRequest,
    ListInstancesResponse,
    Message,
    MessageType,
    ProtocolDecodeError,
    RegisterAck,
    RegisterRequest,
    Request,
    Response,
    Role,
    decode_message,
    encode_message,
)

logger = logging.getLogger("qgis_puppeteer.client")

DEFAULT_HUB_URL: str = "ws://127.0.0.1:9876"
# Origin ヘッダはデフォルトで "http://localhost"（ADR-0001 §9.2 で許可）
DEFAULT_ORIGIN: str = "http://localhost"
DEFAULT_REQUEST_TIMEOUT_SECONDS: float = 30.0
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
    """connect() 未実行で呼ばれた操作。"""


class RegisterError(AutomationClientError):
    """register_ack が ok=False を返した。"""

    def __init__(self, error: Error | None) -> None:
        self.error = error
        msg = f"register failed: {error.code.value if error else 'unknown'}"
        if error:
            msg += f" ({error.message})"
        super().__init__(msg)


class RequestError(AutomationClientError):
    """response.ok=False のエラー応答。"""

    def __init__(self, error: Error) -> None:
        self.error = error
        super().__init__(f"{error.code.value}: {error.message}")

    @property
    def code(self) -> ErrorCode:
        return self.error.code

    @property
    def details(self) -> dict[str, Any]:
        return self.error.details


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
    ) -> None:
        """
        Args:
            role: Hub に自己申告する role。McpGateway は `Role.MCP_GATEWAY` を、
                pytest / その他の直接呼び出しは既定の `Role.AUTOMATION_CLIENT`
                を使う（ADR-0001 §12.7 caller_role 伝達）。
        """
        if role == Role.WORKER:
            raise ValueError("AutomationClient cannot register as WORKER role")
        self._url = url
        self._origin = origin
        self._role = role
        self._request_timeout = request_timeout
        self._register_timeout = register_timeout
        self._connect_timeout = connect_timeout

        self._ws: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        # 送信済みリクエスト id → 応答を待つ Future
        self._pending: dict[str, asyncio.Future[Message]] = {}
        # 切断フラグ（close 中の reader 例外を抑止するため）
        self._closing: bool = False

    # ------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------

    async def __aenter__(self) -> AutomationClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Hub に接続して register_ack まで完了させる。

        接続全体に `connect_timeout` を課す。ローカル前提なので短時間で
        成否を確定させたい（特に Windows では TCP SYN の OS retry が長く、
        Hub 不在時に Gateway が数十秒ブロックするのを避ける）。

        タイムアウト時は `ConnectionRefusedError` を投げる（呼び出し側は
        接続不能を単一のエラー型で扱えるようにするため）。
        """
        if self._ws is not None:
            return

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

        # reader タスクを起動してから register を送る
        self._reader_task = asyncio.create_task(self._reader_loop(), name="AutomationClient.reader")

        try:
            await self._register()
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

    async def _register(self) -> None:
        req_id = self._new_id()
        fut = self._make_future(req_id)
        await self._send(RegisterRequest(id=req_id, role=self._role))
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
        if not msg.ok:
            raise RegisterError(msg.error)

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
            timeout_ms: リクエスト個別のタイムアウト（Hub に伝搬）

        Returns:
            Response.result（任意の JSON 値）

        Raises:
            NotConnectedError: connect() 未完了で呼ばれた
            RequestError: response.ok=False（Hub / Worker のエラー応答）
            asyncio.TimeoutError: クライアント側タイムアウト
        """
        self._require_connected()

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
        msg = await self._await_reply(req_id, fut, timeout_ms)

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

    async def list_instances(self) -> list[InstanceInfo]:
        """Hub から active Worker の一覧を取得する。"""
        self._require_connected()
        req_id = self._new_id()
        fut = self._make_future(req_id)
        await self._send(ListInstancesRequest(id=req_id))
        msg = await self._await_reply(req_id, fut, None)
        if not isinstance(msg, ListInstancesResponse):
            raise RequestError(
                Error(
                    code=ErrorCode.INVALID_COMMAND,
                    message=f"unexpected reply type: {msg.type.value}",
                )
            )
        return list(msg.instances)

    # ------------------------------------------------------------
    # 内部ヘルパ
    # ------------------------------------------------------------

    def _require_connected(self) -> None:
        if self._ws is None:
            raise NotConnectedError("connect() has not completed")

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
        timeout_ms: int | None,
    ) -> Message:
        # タイムアウト決定：呼び出し側指定 > デフォルト
        timeout = timeout_ms / 1000.0 if timeout_ms is not None else self._request_timeout
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            # ペンディングから除去（遅延応答は読み捨てになる）
            self._pending.pop(req_id, None)
            raise

    async def _send(self, msg: Message) -> None:
        assert self._ws is not None
        await self._ws.send(encode_message(msg))

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
