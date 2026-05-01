"""AutomationClient のテスト。

asyncio `websockets.serve` で立てた Fake Hub にクライアントを接続し、
register → list_instances / request / response の往復と例外ケースを検証する。
実 Hub (PyQt) との統合は `test_integration_hub_client.py` で扱う。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from qgis_puppeteer.client import (
    AutomationClient,
    NotConnectedError,
    RegisterError,
    RequestError,
)
from qgis_puppeteer.protocol import ErrorCode, Role
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

# ============================================================
# Fake Hub：asyncio websockets.serve で立てる簡易 Hub
# ============================================================


class FakeHub:
    """テスト用の最小 Hub。プロトコルに準拠した応答を返す。

    - register を受信したら register_ack を返す
    - list_instances を受信したら現在の worker 一覧を返す
    - request を受信したら routing して worker に転送、response を
      発信元に返送する
    - worker は別コネクションで register した扱いで in-memory 登録
    """

    def __init__(self) -> None:
        # instance_id → worker ws
        self.workers: dict[str, ServerConnection] = {}
        # request id → 発信元 ws
        self.inflight: dict[str, ServerConnection] = {}
        # テスト検証用：受信した全リクエスト
        self.received_requests: list[dict[str, Any]] = []
        # register_ack で常に ok=False を返す（エラー系テスト用）
        self.reject_register: bool = False
        # Origin 検証モード（ws ハンドラでは検証できないのでテストでは無視）

    async def handler(self, ws: ServerConnection) -> None:
        """クライアント 1 接続分のライフサイクル。"""
        # 最初は register を期待
        first = await ws.recv()
        if isinstance(first, bytes):
            await ws.close()
            return
        msg = json.loads(first)
        assert msg["type"] == "register"

        role = msg["role"]
        reg_id = msg["id"]

        if self.reject_register:
            await ws.send(
                json.dumps(
                    {
                        "type": "register_ack",
                        "id": reg_id,
                        "ok": False,
                        "error": {
                            "code": ErrorCode.LABEL_CONFLICT.value,
                            "message": "test-induced failure",
                            "details": {},
                        },
                    }
                )
            )
            await ws.close()
            return

        instance_id: str | None = None
        if role == Role.WORKER.value:
            label = msg.get("label") or "fake-worker"
            pid = msg.get("pid") or 1234
            instance_id = f"worker-{label.lower()}-{pid}"
            self.workers[instance_id] = ws

        await ws.send(
            json.dumps(
                {
                    "type": "register_ack",
                    "id": reg_id,
                    "ok": True,
                    "instance_id": instance_id,
                    "resumed": False,
                }
            )
        )

        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                data = json.loads(raw)
                await self._handle(ws, data)
        except ConnectionClosed:
            pass
        finally:
            # Worker 切断時は workers から除去
            for iid, wws in list(self.workers.items()):
                if wws is ws:
                    del self.workers[iid]

    async def _handle(self, ws: ServerConnection, data: dict[str, Any]) -> None:
        t = data["type"]
        if t == "list_instances":
            # instance_id は `worker-<label>-<pid>` 形式で登録されるので
            # 末尾から pid を復元する（テストが pid の違いで worker を区別できるように）
            def _parse(iid: str) -> dict[str, Any]:
                parts = iid.split("-")
                try:
                    pid_int = int(parts[-1])
                except (ValueError, IndexError):
                    pid_int = 1234
                return {
                    "instance_id": iid,
                    "label": parts[1] if len(parts) >= 2 else iid,
                    "pid": pid_int,
                    "project": None,
                }

            await ws.send(
                json.dumps(
                    {
                        "type": "list_instances_response",
                        "id": data["id"],
                        "instances": [_parse(iid) for iid in self.workers],
                    }
                )
            )
            return

        if t == "request":
            self.received_requests.append(data)
            selector = data.get("instance")
            target_ws = self._pick_worker(selector)
            if target_ws is None:
                await ws.send(
                    json.dumps(
                        {
                            "type": "response",
                            "id": data["id"],
                            "ok": False,
                            "error": {
                                "code": ErrorCode.INSTANCE_NOT_FOUND.value,
                                "message": f"no worker for {selector!r}",
                                "details": {},
                            },
                        }
                    )
                )
                return
            self.inflight[data["id"]] = ws
            await target_ws.send(json.dumps(data))
            return

        if t == "response":
            origin = self.inflight.pop(data["id"], None)
            if origin is not None:
                await origin.send(json.dumps(data))
            return

        if t == "bye":
            # worker からの明示的 bye はそのまま close
            for iid, wws in list(self.workers.items()):
                if wws is ws:
                    del self.workers[iid]
            await ws.close()
            return

    def _pick_worker(self, selector: str | None) -> ServerConnection | None:
        if not self.workers:
            return None
        if selector is None:
            # 1 台のみなら自動選択、それ以外は None（ambiguous 代わりに not_found）
            if len(self.workers) == 1:
                return next(iter(self.workers.values()))
            return None
        # label / instance_id で探す（label は instance_id の 2 要素目）
        for iid, ws in self.workers.items():
            if iid == selector or iid.split("-")[1] == selector:
                return ws
        return None


@asynccontextmanager
async def fake_hub_server() -> AsyncIterator[tuple[FakeHub, str]]:
    hub = FakeHub()
    # port=0 で OS に空きポートを割り当てさせる
    async with serve(hub.handler, "127.0.0.1", 0) as server:
        sock = next(iter(server.sockets))
        port = sock.getsockname()[1]
        yield hub, f"ws://127.0.0.1:{port}"


def _run(coro: Any) -> Any:
    """pytest から async テストを呼ぶユーティリティ。"""
    return asyncio.run(coro)


# ============================================================
# Fake Worker：別クライアントとして register し、request を echo する
# ============================================================


class _FakeWorker:
    """テスト用の最小 Worker クライアント。

    register(role=worker) して、届いた request を受けて想定の
    response を返す。背景タスクで動く。
    """

    def __init__(self, url: str, label: str = "w1", pid: int = 1234) -> None:
        self.url = url
        self.label = label
        self.pid = pid
        self._ws = None
        self._task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        # request を受けて返す response dict を構築するコールバック（差し替え可）
        self.responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    async def start(self) -> None:
        from websockets.asyncio.client import connect

        self._ws = await connect(self.url, additional_headers={"Origin": "http://localhost"})
        reg_id = "worker-reg-1"
        await self._ws.send(
            json.dumps(
                {
                    "type": "register",
                    "id": reg_id,
                    "role": "worker",
                    "protocol_version": 1,
                    "pid": self.pid,
                    "label": self.label,
                }
            )
        )
        ack = json.loads(await self._ws.recv())
        assert ack["type"] == "register_ack"
        assert ack["ok"] is True
        self.ready.set()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                data = json.loads(raw)
                if data["type"] == "request":
                    resp = self._respond(data)
                    await self._ws.send(json.dumps(resp))
        except ConnectionClosed:
            pass

    def _respond(self, req: dict[str, Any]) -> dict[str, Any]:
        if self.responder is not None:
            return self.responder(req)
        # デフォルト：echo
        return {
            "type": "response",
            "id": req["id"],
            "ok": True,
            "result": {"echo": req.get("params", {}), "command": req["command"]},
        }

    async def stop(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()


# ============================================================
# connect / register
# ============================================================


class TestConnect:
    def test_connect_and_close(self) -> None:
        async def inner() -> None:
            async with fake_hub_server() as (_, url), AutomationClient(url=url):
                pass  # register_ack まで届けば成功

        _run(inner())

    def test_connect_rejected_raises_register_error(self) -> None:
        async def inner() -> None:
            async with fake_hub_server() as (hub, url):
                hub.reject_register = True
                client = AutomationClient(url=url, register_timeout=2.0)
                with pytest.raises(RegisterError) as exc_info:
                    await client.connect()
                assert exc_info.value.error is not None
                assert exc_info.value.error.code == ErrorCode.LABEL_CONFLICT

        _run(inner())

    def test_call_before_connect_raises(self) -> None:
        async def inner() -> None:
            client = AutomationClient()
            with pytest.raises(NotConnectedError):
                await client.call("cmd")

        _run(inner())


# ============================================================
# list_instances
# ============================================================


class TestListInstances:
    def test_empty_list_when_no_workers(self) -> None:
        async def inner() -> None:
            async with (
                fake_hub_server() as (_, url),
                AutomationClient(url=url) as client,
            ):
                instances = await client.list_instances()
                assert instances == []

        _run(inner())

    def test_lists_registered_worker(self) -> None:
        async def inner() -> None:
            async with fake_hub_server() as (_, url):
                worker = _FakeWorker(url, label="alpha", pid=4321)
                await worker.start()
                await worker.ready.wait()

                try:
                    async with AutomationClient(url=url) as client:
                        instances = await client.list_instances()
                        assert len(instances) == 1
                        assert instances[0].instance_id == "worker-alpha-4321"
                finally:
                    await worker.stop()

        _run(inner())


# ============================================================
# call（request / response の往復）
# ============================================================


class TestCall:
    def test_call_roundtrip_default_selector(self) -> None:
        async def inner() -> None:
            async with fake_hub_server() as (_, url):
                worker = _FakeWorker(url, label="solo", pid=1111)
                await worker.start()

                try:
                    async with AutomationClient(url=url) as client:
                        result = await client.call("qgis_list_layers", {"x": 1}, timeout_ms=5_000)
                        assert result == {
                            "echo": {"x": 1},
                            "command": "qgis_list_layers",
                        }
                finally:
                    await worker.stop()

        _run(inner())

    def test_call_with_selector_routes_correctly(self) -> None:
        async def inner() -> None:
            async with fake_hub_server() as (hub, url):
                w1 = _FakeWorker(url, label="alpha", pid=1)
                w2 = _FakeWorker(url, label="beta", pid=2)
                await w1.start()
                await w2.start()

                w1.responder = lambda req: {
                    "type": "response",
                    "id": req["id"],
                    "ok": True,
                    "result": "from-alpha",
                }
                w2.responder = lambda req: {
                    "type": "response",
                    "id": req["id"],
                    "ok": True,
                    "result": "from-beta",
                }

                try:
                    async with AutomationClient(url=url) as client:
                        r1 = await client.call("cmd", instance="alpha")
                        r2 = await client.call("cmd", instance="beta")
                        assert r1 == "from-alpha"
                        assert r2 == "from-beta"
                        # Hub が受信したリクエストの selector を確認
                        selectors = [r["instance"] for r in hub.received_requests]
                        assert selectors == ["alpha", "beta"]
                finally:
                    await w1.stop()
                    await w2.stop()

        _run(inner())

    def test_call_instance_not_found_raises_request_error(self) -> None:
        async def inner() -> None:
            async with (
                fake_hub_server() as (_, url),
                AutomationClient(url=url) as client,
            ):
                with pytest.raises(RequestError) as exc_info:
                    await client.call("cmd", instance="nonexistent")
                assert exc_info.value.code == ErrorCode.INSTANCE_NOT_FOUND

        _run(inner())

    def test_call_worker_error_propagates(self) -> None:
        async def inner() -> None:
            async with fake_hub_server() as (_, url):
                worker = _FakeWorker(url, label="bad", pid=9)
                await worker.start()
                worker.responder = lambda req: {
                    "type": "response",
                    "id": req["id"],
                    "ok": False,
                    "error": {
                        "code": ErrorCode.WORKER_EXECUTION_ERROR.value,
                        "message": "boom",
                        "details": {"detail": "trace"},
                    },
                }

                try:
                    async with AutomationClient(url=url) as client:
                        with pytest.raises(RequestError) as exc_info:
                            await client.call("cmd")
                        assert exc_info.value.code == ErrorCode.WORKER_EXECUTION_ERROR
                        assert exc_info.value.details == {"detail": "trace"}
                finally:
                    await worker.stop()

        _run(inner())

    def test_concurrent_calls_are_multiplexed(self) -> None:
        """複数 call を並行起動し、id ごとに正しく対応付けられることを確認。"""

        async def inner() -> None:
            async with fake_hub_server() as (_, url):
                worker = _FakeWorker(url, label="m", pid=5)
                await worker.start()
                try:
                    async with AutomationClient(url=url) as client:
                        results = await asyncio.gather(
                            client.call("c1", {"n": 1}),
                            client.call("c2", {"n": 2}),
                            client.call("c3", {"n": 3}),
                        )
                        assert [r["echo"]["n"] for r in results] == [1, 2, 3]
                finally:
                    await worker.stop()

        _run(inner())

    def test_call_timeout(self) -> None:
        async def inner() -> None:
            async with fake_hub_server() as (_, url):
                worker = _FakeWorker(url, label="slow", pid=7)
                await worker.start()
                # 応答を返さないレスポンダ
                worker.responder = lambda req: {
                    "type": "response",
                    "id": "never-matches-original",
                    "ok": True,
                    "result": None,
                }

                try:
                    async with AutomationClient(url=url) as client:
                        with pytest.raises(asyncio.TimeoutError):
                            await client.call("cmd", timeout_ms=200)
                finally:
                    await worker.stop()

        _run(inner())
