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
from pathlib import Path
from typing import Any

import pytest
from qgis_puppeteer.client import AutomationClient, RegistrationRejectedError, RequestError
from qgis_puppeteer.hub_file import read_hub_file
from qgis_puppeteer.hub_probe import find_live_hub
from qgis_puppeteer.protocol import ErrorCode
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from tests._handshake_helpers import (
    ORIGIN_HEADERS,
    hub_file_env,
    open_greeted,
    register_json,
    register_raw,
)

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


def _wait_hub_live(hub_file: Path, timeout: float = 10.0) -> None:
    """Hub が Hub ファイルを書き、挨拶が通るまで待つ（listen してから書くまで間がある）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_live_hub(hub_file) is not None:
            return
        time.sleep(0.1)
    raise TimeoutError(f"Hub did not become live within {timeout}s at {hub_file}")


@contextmanager
def _hub_subprocess(port: int, *extra_args: str) -> Iterator[subprocess.Popen[bytes]]:
    """Hub プロセスを起動し、終了時に確実に停止させる。

    Hub ファイルは一時フォルダに書かせ、その間 ``QPUPPETEER_HUB_FILE`` で
    同じ場所を指す（``AutomationClient`` と補助関数がそこから token を読む）。
    """
    with hub_file_env() as hub_file, _hub_process(port, hub_file, *extra_args) as proc:
        yield proc


@contextmanager
def _hub_process(port: int, hub_file: Path, *extra_args: str) -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "qgis_puppeteer.hub",
            "--port",
            str(port),
            "--hub-file",
            str(hub_file),
            *extra_args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_hub_live(hub_file)
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

    def __init__(self, url: str, *, label: str, pid: int, silent: bool = False) -> None:
        self.url = url
        self.label = label
        self.pid = pid
        # True なら request に応答しない（GUI が塞がった QGIS の代わり）
        self.silent = silent
        self._ws = None
        self._task: asyncio.Task[None] | None = None
        self.instance_id: str | None = None

    async def start(self) -> None:
        self._ws, ack = await register_raw(
            self.url, id="worker-reg-1", role="worker", pid=self.pid, label=self.label
        )
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
                if data["type"] != "request" or self.silent:
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
                                "timeout_ms": data.get("timeout_ms"),
                                "deadline": data.get("deadline"),
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
                async with AutomationClient() as client:
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
                    async with AutomationClient() as client:
                        # 少し待って Hub 側の worker 登録を確定させる
                        await asyncio.sleep(0.05)
                        instances = await client.list_instances()
                        assert len(instances) == 1
                        assert instances[0].label == "solo"
                        assert instances[0].pid == 1111

                        result = await client.call("qgis_ping", {"foo": "bar"}, timeout_ms=5_000)
                        deadline = result.pop("deadline")
                        assert result == {
                            "command": "qgis_ping",
                            "params": {"foo": "bar"},
                            "worker_label": "solo",
                            # ADR-0001 §12.7: Hub が呼び出し元 role を付与する
                            "caller_role": "automation_client",
                            "timeout_ms": 5_000,
                        }
                        # Hub が timeout_ms から実行の期限を付けて転送する
                        assert abs(deadline - (time.time() + 5.0)) < 2.0
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
                    async with AutomationClient() as client:
                        await asyncio.sleep(0.05)
                        r_a = await client.call("x", instance="alpha")
                        r_b = await client.call("x", instance="beta")
                        assert r_a["worker_label"] == "alpha"
                        assert r_b["worker_label"] == "beta"
                finally:
                    await w1.stop()
                    await w2.stop()

            _run(inner())

    def test_rejected_registration_is_reported_via_real_hub(self) -> None:
        """#13: Hub が断った登録を、起動した側が待たずに知る。"""
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                ws, ack = await register_raw(
                    url,
                    id="reg-long",
                    role="worker",
                    pid=4321,
                    label="fresh-" + "x" * 200,
                    launch_token="lt-rejected",
                )
                try:
                    assert ack["ok"] is False
                finally:
                    await ws.close()
                async with AutomationClient() as client:
                    [rec] = await client.list_rejected()
                    assert (rec.launch_token, rec.pid) == ("lt-rejected", 4321)
                    assert "too_long" in rec.message
                    loop = asyncio.get_running_loop()
                    started = loop.time()
                    with pytest.raises(RegistrationRejectedError):
                        await client.wait_for_ready("lt-rejected", timeout_s=30.0)
                    assert loop.time() - started < 5.0

            _run(inner())

    def test_abandoned_launch_is_refused_via_real_hub(self) -> None:
        """#16: 起こした側が待つのをやめた token の登録は、理由付きで断られる。"""
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                async with AutomationClient() as client:
                    await client.abandon_launch("lt-gave-up")
                    await asyncio.sleep(0.2)  # 応答は無いので、Hub が処理するのを待つ
                    ws, ack = await register_raw(
                        url,
                        id="reg-late",
                        role="worker",
                        pid=4321,
                        label="fresh-late",
                        launch_token="lt-gave-up",
                    )
                    await ws.close()
                    assert ack["ok"] is False
                    assert ack["error"]["code"] == "launch_abandoned"
                    # 接続は生きていて、続けて使える
                    assert await client.list_instances() == []

            _run(inner())

    def test_unknown_message_type_is_answered_via_real_hub(self) -> None:
        """知らない type でも id があれば unsupported_message で答え、無ければ黙る。"""
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                ws, ack = await register_raw(url, id="c-reg", role="automation_client")
                assert ack["ok"] is True, ack
                try:
                    await ws.send(json.dumps({"type": "added_later", "id": "q-1"}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
                    assert reply["type"] == "response"
                    assert reply["id"] == "q-1"
                    assert reply["error"]["code"] == "unsupported_message"
                    assert reply["error"]["details"] == {"type": "added_later"}
                    # id が無ければ答えない（次に届くのは list_instances の答え）
                    await ws.send(json.dumps({"type": "added_later"}))
                    await ws.send(json.dumps({"type": "list_instances", "id": "l-1"}))
                    nxt = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
                    assert (nxt["type"], nxt["id"]) == ("list_instances_response", "l-1")
                finally:
                    await ws.close()

            _run(inner())

    def test_response_from_another_worker_is_ignored_via_real_hub(self) -> None:
        """Hub は、その request を転送した Worker からの応答だけを届ける。"""
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                target = _AsyncFakeWorker(url, label="target", pid=31, silent=True)
                await target.start()
                other = _AsyncFakeWorker(url, label="other", pid=32, silent=True)
                await other.start()
                client, ack = await register_raw(url, id="c-reg", role="automation_client")
                try:
                    assert ack["ok"] is True, ack
                    await client.send(
                        json.dumps(
                            {
                                "type": "request",
                                "id": "req-1",
                                "command": "qgis_ping",
                                "instance": "target",
                                "timeout_ms": 800,
                            }
                        )
                    )
                    await asyncio.sleep(0.2)
                    assert other._ws is not None
                    await other._ws.send(
                        json.dumps(
                            {"type": "response", "id": "req-1", "ok": True, "result": "forged"}
                        )
                    )
                    reply = json.loads(await asyncio.wait_for(client.recv(), 5.0))
                    assert reply["id"] == "req-1"
                    assert reply["ok"] is False
                    assert reply["error"]["code"] == "instance_timeout"
                finally:
                    await client.close()
                    await target.stop()
                    await other.stop()

            _run(inner())

    def test_an_unanswered_request_times_out_via_real_hub(self) -> None:
        """Worker が応答しないと、Hub が timeout_ms ごろに instance_timeout を返す。"""
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                url = f"ws://127.0.0.1:{port}"
                worker = _AsyncFakeWorker(url, label="busy", pid=2222, silent=True)
                await worker.start()
                try:
                    async with AutomationClient() as client:
                        await asyncio.sleep(0.05)
                        started = time.monotonic()
                        with pytest.raises(RequestError) as excinfo:
                            await client.call("qgis_click", timeout_ms=500)
                        elapsed = time.monotonic() - started
                    assert excinfo.value.code == ErrorCode.INSTANCE_TIMEOUT
                    assert excinfo.value.error.details == {
                        "timeout_ms": 500,
                        "command": "qgis_click",
                    }
                    assert 0.4 <= elapsed < 1.4
                finally:
                    await worker.stop()

            _run(inner())

    def test_instance_not_found_via_real_hub(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                async with AutomationClient() as client:
                    with pytest.raises(RequestError) as exc_info:
                        await client.call("x", instance="nonexistent")
                    assert exc_info.value.code == ErrorCode.INSTANCE_NOT_FOUND

            _run(inner())

    def test_bad_origin_rejected_by_real_hub(self) -> None:
        """ADR-0001 §9.2：Origin が許可集合外ならハンドシェイクで拒否される。"""
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                # localhost.evil.com は hostname 完全一致検査で reject される
                client = AutomationClient(
                    origin="http://localhost.evil.com",
                    register_timeout=2.0,
                )
                with pytest.raises(Exception):
                    # connect 中のハンドシェイクで弾かれるか、register が失敗する
                    await client.connect()

            _run(inner())


# ============================================================
# 挨拶（hello と proof）と版の確認
# ============================================================


async def _closed_within(ws: Any, timeout: float) -> bool:
    """相手が ``timeout`` 秒以内に接続を切れば True（届いたメッセージは読み捨てる）。"""

    async def drain() -> None:
        with suppress(ConnectionClosed):
            async for _ in ws:
                pass

    try:
        await asyncio.wait_for(drain(), timeout)
    except asyncio.TimeoutError:
        return False
    return True


class TestHandshakeIntegration:
    def test_the_hub_writes_its_hub_file(self) -> None:
        port = _get_free_port()
        with hub_file_env() as hub_file, _hub_process(port, hub_file) as proc:
            info = read_hub_file(hub_file)
            assert info.addresses == (f"127.0.0.1:{port}",)
            assert info.pid is not None
            assert proc.poll() is None
            assert info.protocol is not None
            assert len(info.token) >= 32

    def test_a_wrong_proof_is_refused_and_the_connection_closed(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                ws, hello = await open_greeted(f"ws://127.0.0.1:{port}")
                try:
                    await ws.send(
                        register_json(hello, token="not-the-token", id="r", role="worker", pid=1)
                    )
                    ack = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
                    assert (ack["type"], ack["id"], ack["ok"]) == ("register_ack", "r", False)
                    assert ack["error"]["code"] == "unauthorized"
                    # 相手を確かめていないので、hub_proof は載せない
                    assert ack.get("hub_proof") is None
                    assert await _closed_within(ws, 5.0)
                finally:
                    await ws.close()
                # 断った相手は Worker として数えない
                async with AutomationClient() as client:
                    assert await client.list_instances() == []

            _run(inner())

    def test_nothing_but_register_is_accepted_before_registering(self) -> None:
        """register の前の list_instances は unauthorized で断られ、接続は切られる。"""
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                ws, _hello = await open_greeted(f"ws://127.0.0.1:{port}")
                try:
                    await ws.send(json.dumps({"type": "list_instances", "id": "l-1"}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
                    assert (reply["type"], reply["id"], reply["ok"]) == ("response", "l-1", False)
                    assert reply["error"]["code"] == "unauthorized"
                    assert await _closed_within(ws, 5.0)
                finally:
                    await ws.close()

            _run(inner())

    def test_a_connection_that_does_not_register_in_time_is_closed(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port, "--register-timeout-ms", "300"):

            async def inner() -> None:
                ws, _hello = await open_greeted(f"ws://127.0.0.1:{port}")
                try:
                    assert await _closed_within(ws, 5.0)
                finally:
                    await ws.close()

            _run(inner())

    def test_a_version_1_register_gets_protocol_version_mismatch(self) -> None:
        """版 1 の相手は hello を待たずに proof 無しで送る。版違いと分かるように答える。"""
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                ws = await ws_connect(f"ws://127.0.0.1:{port}", additional_headers=ORIGIN_HEADERS)
                try:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "register",
                                "id": "old",
                                "role": "worker",
                                "protocol_version": 1,
                                "pid": 1,
                            }
                        )
                    )
                    msgs = [json.loads(await asyncio.wait_for(ws.recv(), 5.0)) for _ in range(2)]
                    hello, ack = msgs
                    assert hello["type"] == "hello"
                    assert (ack["type"], ack["id"], ack["ok"]) == ("register_ack", "old", False)
                    assert ack["error"]["code"] == "protocol_version_mismatch"
                    details = ack["error"]["details"]
                    assert details["client_protocol_version"] == 1
                    assert details["hub_protocol"] == {"min": 2, "max": 2}
                    assert details["hub_package_version"] == ack["package_version"]
                    assert await _closed_within(ws, 5.0)
                finally:
                    await ws.close()

            _run(inner())

    def test_the_ack_proves_the_hub_knows_the_token(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                async with AutomationClient() as client:
                    # ack_is_trusted を通ったので connect が終わっている
                    assert client.hub_package_version is not None

            _run(inner())

    def test_a_client_holding_an_old_token_is_refused(self) -> None:
        """Hub が起き直すと token が変わる。古い token の相手は unauthorized。"""
        from qgis_puppeteer.client import RegisterError

        port = _get_free_port()
        with _hub_subprocess(port):

            async def inner() -> None:
                client = AutomationClient(
                    address=f"127.0.0.1:{port}", token="from-an-old-hub", register_timeout=5.0
                )
                with pytest.raises(RegisterError) as exc_info:
                    await client.connect()
                assert exc_info.value.error is not None
                assert exc_info.value.error.code == ErrorCode.UNAUTHORIZED

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
                    async with AutomationClient(role=Role.MCP_GATEWAY) as c:
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
                    async with AutomationClient(role=Role.AUTOMATION_CLIENT) as c:
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
                    # Gateway として登録
                    ws, ack = await register_raw(url, id="reg-1", role="mcp_gateway")
                    try:
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
