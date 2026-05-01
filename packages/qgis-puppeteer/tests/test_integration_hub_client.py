"""Hub (PyQt5) subprocess + AutomationClient の統合テスト。

実際の `python -m qgis_puppeteer.hub` を subprocess で起動し、
AutomationClient から register / list_instances / request の往復を
WebSocket 越しに検証する。

## 前提

- PyQt5 / PyQt5.QtWebSockets が import 可能（なければ skip）
- port は各テストで使い捨てのエフェメラルポートを確保

## 遅さについて

Hub プロセスの起動・listen 開始まで 1〜2 秒かかる場合があるため、
通常の CI では `@pytest.mark.integration` で選別可能にしてある。
ローカル開発では `uv run pytest` でそのまま全件実行できる。
"""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress

import pytest
from qgis_puppeteer.client import AutomationClient, RequestError
from qgis_puppeteer.protocol import ErrorCode
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

# PyQt5 が無い環境では全件 skip（CI 最小構成で困らないように）
pytest.importorskip("PyQt5.QtWebSockets")

pytestmark = pytest.mark.integration


# ============================================================
# ヘルパ：エフェメラルポート確保と subprocess 起動
# ============================================================


def _get_free_port() -> int:
    """OS から空きポートを 1 つ借りる。TOCTOU はテスト用途なので許容。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port_open(port: int, timeout: float = 10.0) -> None:
    """指定ポートに TCP 接続できるようになるまで待つ。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.1)
    raise TimeoutError(f"Hub did not start listening on port {port} within {timeout}s")


@contextmanager
def _hub_subprocess(port: int) -> Iterator[subprocess.Popen[bytes]]:
    """Hub プロセスを起動し、終了時に確実に停止させる。"""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "qgis_puppeteer.hub",
            "--port",
            str(port),
            "--no-pid-file",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_port_open(port)
        yield proc
    finally:
        if proc.poll() is None:
            # Windows では SIGTERM は subprocess.terminate() の挙動に吸収される
            try:
                if sys.platform == "win32":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGINT)
            except OSError:
                pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)


# ============================================================
# Fake Worker (subprocess 外、asyncio websockets で簡易実装)
# ============================================================


class _AsyncFakeWorker:
    """asyncio の Worker クライアント。Hub の正規プロトコルで接続する。"""

    def __init__(self, url: str, *, label: str, pid: int) -> None:
        self.url = url
        self.label = label
        self.pid = pid
        self._ws = None
        self._task: asyncio.Task[None] | None = None
        self.instance_id: str | None = None

    async def start(self) -> None:
        self._ws = await ws_connect(self.url, additional_headers={"Origin": "http://localhost"})
        # register
        await self._ws.send(
            json.dumps(
                {
                    "type": "register",
                    "id": "worker-reg-1",
                    "role": "worker",
                    "protocol_version": 1,
                    "pid": self.pid,
                    "label": self.label,
                }
            )
        )
        ack = json.loads(await self._ws.recv())
        assert ack["type"] == "register_ack", ack
        assert ack["ok"] is True, ack
        self.instance_id = ack["instance_id"]
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                data = json.loads(raw)
                if data["type"] != "request":
                    continue
                # command / params / caller_role を echo して検証可能にする
                # （caller_role は Hub が注入するフィールド、ADR-0001 §12.9）
                await self._ws.send(
                    json.dumps(
                        {
                            "type": "response",
                            "id": data["id"],
                            "ok": True,
                            "result": {
                                "command": data["command"],
                                "params": data.get("params", {}),
                                "worker_label": self.label,
                                "caller_role": data.get("caller_role"),
                            },
                        }
                    )
                )
        except ConnectionClosed:
            pass

    async def stop(self) -> None:
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()


def _run(coro) -> None:
    asyncio.run(coro)


# ============================================================
# 統合テスト
# ============================================================


class TestHubClientIntegration:
    def test_client_connects_and_lists_empty(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                async with AutomationClient(url=url) as client:
                    instances = await client.list_instances()
                    assert instances == []

            _run(inner())

    def test_request_response_roundtrip_via_real_hub(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                worker = _AsyncFakeWorker(url, label="solo", pid=1111)
                await worker.start()
                try:
                    async with AutomationClient(url=url) as client:
                        # 少し待って Hub 側の worker 登録を確定させる
                        await asyncio.sleep(0.05)
                        instances = await client.list_instances()
                        assert len(instances) == 1
                        assert instances[0].label == "solo"
                        assert instances[0].pid == 1111

                        result = await client.call("qgis_ping", {"foo": "bar"}, timeout_ms=5_000)
                        assert result == {
                            "command": "qgis_ping",
                            "params": {"foo": "bar"},
                            "worker_label": "solo",
                        }
                finally:
                    await worker.stop()

            _run(inner())

    def test_selector_routing_via_real_hub(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                w1 = _AsyncFakeWorker(url, label="alpha", pid=11)
                w2 = _AsyncFakeWorker(url, label="beta", pid=22)
                await w1.start()
                await w2.start()
                try:
                    async with AutomationClient(url=url) as client:
                        await asyncio.sleep(0.05)
                        r_a = await client.call("x", instance="alpha")
                        r_b = await client.call("x", instance="beta")
                        assert r_a["worker_label"] == "alpha"
                        assert r_b["worker_label"] == "beta"
                finally:
                    await w1.stop()
                    await w2.stop()

            _run(inner())

    def test_instance_not_found_via_real_hub(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                async with AutomationClient(url=url) as client:
                    with pytest.raises(RequestError) as exc_info:
                        await client.call("x", instance="nonexistent")
                    assert exc_info.value.code == ErrorCode.INSTANCE_NOT_FOUND

            _run(inner())

    def test_bad_origin_rejected_by_real_hub(self) -> None:
        """ADR-0001 §9.2：Origin が許可集合外ならハンドシェイクで拒否される。"""
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                # localhost.evil.com は hostname 完全一致検査で reject される
                client = AutomationClient(
                    url=url,
                    origin="http://localhost.evil.com",
                    register_timeout=2.0,
                )
                with pytest.raises(Exception):
                    # connect 中のハンドシェイクで弾かれるか、register が失敗する
                    await client.connect()

            _run(inner())


# ============================================================
# caller_role 伝達の統合テスト（ADR-0001 §12.9）
# ============================================================


class TestCallerRoleIntegration:
    """Hub が register 時の role を覚えて request 転送時に注入することを検証する。

    Worker は fake で、受信した request の caller_role を result にエコーバック
    するので、Hub が正しい role を注入したかを response から確認できる。
    """

    def test_mcp_gateway_role_is_injected_to_worker(self) -> None:
        """McpGateway として接続した Client の request は caller_role=mcp_gateway で転送される。"""
        from qgis_puppeteer.protocol import Role

        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                worker = _AsyncFakeWorker(url, label="w1", pid=1111)
                await worker.start()
                try:
                    async with AutomationClient(url=url, role=Role.MCP_GATEWAY) as c:
                        await asyncio.sleep(0.05)
                        result = await c.call("ping", instance="w1", timeout_ms=5_000)
                    assert result["caller_role"] == "mcp_gateway"
                finally:
                    await worker.stop()

            _run(inner())

    def test_automation_client_role_is_injected_to_worker(self) -> None:
        """AutomationClient（pytest 等）接続時は caller_role=automation_client。"""
        from qgis_puppeteer.protocol import Role

        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                worker = _AsyncFakeWorker(url, label="w2", pid=2222)
                await worker.start()
                try:
                    async with AutomationClient(url=url, role=Role.AUTOMATION_CLIENT) as c:
                        await asyncio.sleep(0.05)
                        result = await c.call("ping", instance="w2", timeout_ms=5_000)
                    assert result["caller_role"] == "automation_client"
                finally:
                    await worker.stop()

            _run(inner())

    def test_client_cannot_spoof_caller_role(self) -> None:
        """Client が request payload に caller_role を含めても Hub が上書きする。

        Client 側の自己申告は信用せず、register 時の role を Hub が常に上書きする
        のがセキュリティ境界（ADR-0001 §12.9）。これが破れると、McpGateway 接続で
        あっても payload に `caller_role=automation_client` を入れれば `test.*`
        handler にアクセスできる権限昇格経路が成立してしまう。
        """
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                worker = _AsyncFakeWorker(url, label="w3", pid=3333)
                await worker.start()
                try:
                    # 生 websocket で MCP_GATEWAY として register した後、
                    # AutomationClient を経由せず自分で request を投げる
                    ws = await ws_connect(url, additional_headers={"Origin": "http://localhost"})
                    try:
                        # Gateway として登録
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "register",
                                    "id": "reg-1",
                                    "role": "mcp_gateway",
                                    "protocol_version": 1,
                                }
                            )
                        )
                        ack = json.loads(await ws.recv())
                        assert ack["ok"] is True, ack

                        # worker 登録の反映を少し待つ
                        await asyncio.sleep(0.05)

                        # Client が caller_role=automation_client を偽装して送信
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "request",
                                    "id": "req-spoof",
                                    "instance": "w3",
                                    "command": "ping",
                                    "params": {},
                                    # 自己申告：信用されてはいけない
                                    "caller_role": "automation_client",
                                }
                            )
                        )
                        resp = json.loads(await ws.recv())
                    finally:
                        await ws.close()

                    assert resp["ok"] is True, resp
                    # Worker がエコーバックした caller_role は Hub が上書きした値
                    # （register 時の role = mcp_gateway）になる
                    assert resp["result"]["caller_role"] == "mcp_gateway"
                finally:
                    await worker.stop()

            _run(inner())
