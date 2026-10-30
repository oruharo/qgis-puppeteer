"""McpGateway のテスト。

FakeHub + FakeWorker（`test_client.py` を流用）に対して in-memory の
MCP セッションを張り、ツール呼び出しが Hub → Worker へ正しくルーティング
されることを end-to-end で検証する。

in-memory 接続には mcp 2.x の `mcp.Client` を使い、`build_gateway()` が返す
`MCPServer` を直接渡してプロセス内で繋ぐ（接続時の握手も `Client` が行う）。
`raise_exceptions=True` は、サーバ側の例外を汎用メッセージに丸めずに
表へ出すテスト用の設定。

既定の `mode="auto"` はサーバと DirectDispatcher で直結し、JSON-RPC の
直列化も initialize 握手も通らない。stdio 本番と同じ経路（`Server.run`）は
`TestMcpGatewayLegacyHandshake` が `mode="legacy"` で押さえる。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from qgis_puppeteer.client import AutomationClient, RegisterError
from qgis_puppeteer.hub_file import ENV_HUB_FILE

# skip するのは mcp extra が入っていない環境だけ。gateway 側の import 失敗
# （mcp のメジャー不一致など）まで skip に巻き込むと、テストが 1 本も
# 実行されないまま緑に見えるので、ここでは `mcp` パッケージの有無だけを見る。
pytest.importorskip("mcp", reason="qgis_puppeteer[mcp] extras not installed")

from mcp import Client
from mcp.types import CallToolResult
from qgis_puppeteer.gateways.mcp import (
    DEFAULT_ORIGIN,
    GatewayContext,
    build_gateway,
)

from tests.test_client import _FakeWorker, fake_hub_server

# ============================================================
# テスト用ヘルパ
# ============================================================


def _build_test_lifespan(
    hub_url: str,
) -> Any:
    """テスト用 lifespan：指定 URL の AutomationClient を開く。"""

    @asynccontextmanager
    async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
        client = AutomationClient(url=hub_url)
        await client.connect()
        try:
            yield GatewayContext(client=client)
        finally:
            await client.close()

    return _lifespan


def _extract_text(result: CallToolResult) -> str:
    """`CallToolResult` から最初の TextContent の文字列を取り出す。"""
    assert result.content, "tool returned empty content"
    first = result.content[0]
    # MCP 仕様では TextContent / ImageContent / EmbeddedResource のいずれか
    assert hasattr(first, "text"), f"unexpected content: {first!r}"
    return first.text  # type: ignore[attr-defined]


def _run(coro: Any) -> Any:
    """テスト用 coroutine を独立イベントループで実行する薄いラッパ。"""
    return asyncio.run(coro)


# ============================================================
# テスト
# ============================================================


class TestMcpGatewayListInstances:
    def test_list_returns_registered_instance(self) -> None:
        async def run() -> list[dict[str, Any]]:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="alpha", pid=1111)
                await worker.start()
                await worker.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_list_instances", {})
                    return json.loads(_extract_text(result))

        instances = _run(run())
        assert len(instances) == 1
        assert instances[0]["label"] == "alpha"
        assert instances[0]["pid"] == 1111
        assert instances[0]["state"] == "active"
        assert "last_seen_ago" in instances[0]
        assert instances[0]["instance_id"] == "worker-alpha-1111"
        # #17: プラグインの指紋の欄がある（この偽の Hub は返さないので null）
        assert "source_fingerprint" in instances[0]

    def test_discovery_tools_reconnect_after_the_hub_connection_went_stale(self) -> None:
        """Hub を入れ替えた直後でも list_instances / use_instance が張り直して通る。

        以前は worker コマンド（_call）だけが stale 再接続を持っていて、discovery
        系は cached client の死んだ socket をそのまま踏んで落ちていた。
        """

        async def run() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            async with fake_hub_server() as (_, hub_url):
                worker = _FakeWorker(hub_url, label="alpha", pid=1111)
                await worker.start()
                await worker.ready.wait()
                ctx = GatewayContext(url=hub_url, origin=DEFAULT_ORIGIN)

                @asynccontextmanager
                async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
                    try:
                        yield ctx
                    finally:
                        await ctx.reset_client()

                gateway = build_gateway(lifespan=_lifespan)
                async with Client(gateway, raise_exceptions=True) as client:
                    first = json.loads(
                        _extract_text(await client.call_tool("qgis_list_instances", {}))
                    )
                    assert first[0]["label"] == "alpha"
                    # Hub 再起動相当：cached client の socket を足元から閉じる
                    assert ctx.client is not None
                    await ctx.client.close()
                    listed = json.loads(
                        _extract_text(await client.call_tool("qgis_list_instances", {}))
                    )
                    await ctx.client.close()
                    used = json.loads(
                        _extract_text(
                            await client.call_tool("qgis_use_instance", {"selector": "alpha"})
                        )
                    )
                    return listed, used

        listed, used = _run(run())
        assert listed[0]["label"] == "alpha"
        assert used["ok"] is True
        assert used["current"]["label"] == "alpha"

    def test_unexpected_client_error_is_reported_with_type_and_not_sent_again(
        self, monkeypatch: Any
    ) -> None:
        """想定外の例外は送り直さず、型付き JSON で返す。接続は張り直す。

        検証 3: Hub 入れ替え後に qgis_list_instances が本文の無い
        "Error executing tool" で落ち、何度呼んでも回復しなかった。gateway の
        stderr は呼び出し側から見えないので、例外を外へ漏らさない。要求が
        届いたかどうか分からないので、同じ呼び出しの中では送り直さない。
        """

        async def run() -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
            async with fake_hub_server() as (_, hub_url):
                worker = _FakeWorker(hub_url, label="alpha", pid=1111)
                await worker.start()
                await worker.ready.wait()
                ctx = GatewayContext(url=hub_url, origin=DEFAULT_ORIGIN)

                @asynccontextmanager
                async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
                    try:
                        yield ctx
                    finally:
                        await ctx.reset_client()

                original = AutomationClient.list_instances
                calls = {"n": 0}

                async def broken_once(self: AutomationClient, **kw: Any) -> Any:
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("boom from a place nobody expected")
                    return await original(self, **kw)

                gateway = build_gateway(lifespan=_lifespan)
                async with Client(gateway, raise_exceptions=True) as client:
                    monkeypatch.setattr(AutomationClient, "list_instances", broken_once)
                    result = await client.call_tool("qgis_list_instances", {})
                    assert result.is_error
                    broken = json.loads(_extract_text(result))
                    tried = calls["n"]
                    after = json.loads(
                        _extract_text(await client.call_tool("qgis_list_instances", {}))
                    )
                    await worker.stop()
                    return broken, tried, after

        broken, tried, after = _run(run())
        assert tried == 1
        # 型と traceback を載せた JSON（SDK の本文なし "Error executing tool" にはしない）
        assert broken["error"]["code"] == "gateway_internal_error"
        assert broken["error"]["details"]["type"] == "RuntimeError"
        assert "boom from a place nobody expected" in broken["error"]["message"]
        assert "broken_once" in broken["error"]["details"]["traceback"]
        # 次の呼び出しは張り直した接続で通る
        assert after[0]["label"] == "alpha"

    def test_a_command_that_never_left_is_sent_again_once(self) -> None:
        """cached client が切れた接続を抱えていたら、張り直して 1 回だけ送る。"""

        async def run() -> tuple[dict[str, Any], list[str]]:
            async with fake_hub_server() as (hub, hub_url):
                worker = _FakeWorker(hub_url, label="alpha", pid=1111)
                await worker.start()
                await worker.ready.wait()
                ctx = GatewayContext(url=hub_url, origin=DEFAULT_ORIGIN)

                @asynccontextmanager
                async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
                    try:
                        yield ctx
                    finally:
                        await ctx.reset_client()

                gateway = build_gateway(lifespan=_lifespan)
                async with Client(gateway, raise_exceptions=True) as client:
                    await client.call_tool("qgis_list_instances", {})
                    assert ctx.client is not None
                    await ctx.client.close()  # Hub 再起動相当
                    result = await client.call_tool("qgis_list_layers", {})
                    assert not result.is_error
                    await worker.stop()
                    return (
                        json.loads(_extract_text(result)),
                        [r["command"] for r in hub.received_requests],
                    )

        result, commands = _run(run())
        assert result["command"] == "qgis_list_layers"
        assert commands == ["qgis_list_layers"]

    def test_a_command_lost_after_sending_is_not_sent_again(self) -> None:
        """送ったあとで切れたら、送り直さずに「実行されたかもしれない」と返す。"""

        async def run() -> tuple[dict[str, Any], list[str], bool]:
            async with fake_hub_server() as (hub, hub_url):
                worker = _FakeWorker(hub_url, label="alpha", pid=1111)
                await worker.start()
                await worker.ready.wait()
                ctx = GatewayContext(url=hub_url, origin=DEFAULT_ORIGIN)

                @asynccontextmanager
                async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
                    try:
                        yield ctx
                    finally:
                        await ctx.reset_client()

                gateway = build_gateway(lifespan=_lifespan)
                async with Client(gateway, raise_exceptions=True) as client:
                    hub.drop_caller_on_request = True
                    result = await client.call_tool(
                        "qgis_click_widget", {"selector": {"object_name": "ok"}}
                    )
                    assert result.is_error
                    payload = json.loads(_extract_text(result))
                    commands = [r["command"] for r in hub.received_requests]
                    hub.drop_caller_on_request = False
                    after = await client.call_tool("qgis_list_layers", {})
                    await worker.stop()
                    return payload, commands, bool(after.is_error)

        payload, commands, after_failed = _run(run())
        assert commands == ["qgis_click_widget"]
        assert payload["error"]["code"] == "connection_lost"
        assert "may have run" in payload["error"]["message"]
        # 接続は張り直されている
        assert after_failed is False

    def test_a_timed_out_command_is_not_sent_again(self, monkeypatch: Any) -> None:
        """応答待ちの時間切れは送り直さない（クリックが 2 回走る）。"""
        from qgis_puppeteer.gateways import mcp as gw

        class _QuickClient(AutomationClient):
            def __init__(self, **kw: Any) -> None:
                super().__init__(request_timeout=0.3, **kw)

        monkeypatch.setattr(gw, "AutomationClient", _QuickClient)

        async def run() -> tuple[dict[str, Any], list[str]]:
            async with fake_hub_server() as (hub, hub_url):
                worker = _FakeWorker(hub_url, label="alpha", pid=1111)
                # 応答を返さない Worker（GUI スレッドが塞がっている QGIS）
                worker.responder = lambda req: {
                    "type": "response",
                    "id": "never-matches",
                    "ok": True,
                    "result": None,
                }
                await worker.start()
                await worker.ready.wait()
                ctx = GatewayContext(url=hub_url, origin=DEFAULT_ORIGIN)

                @asynccontextmanager
                async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
                    try:
                        yield ctx
                    finally:
                        await ctx.reset_client()

                gateway = build_gateway(lifespan=_lifespan)
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool(
                        "qgis_click_widget", {"selector": {"object_name": "ok"}}
                    )
                    assert result.is_error
                    await worker.stop()
                    return (
                        json.loads(_extract_text(result)),
                        [r["command"] for r in hub.received_requests],
                    )

        payload, commands = _run(run())
        assert commands == ["qgis_click_widget"]
        assert payload["error"]["code"] == "request_timeout"
        assert "may have run" in payload["error"]["message"]

    def test_exception_outside_with_client_is_still_structured(self, monkeypatch: Any) -> None:
        """検証 4: `_with_client` の外で出た例外も本文なしにはしない。

        _resolve_target は _with_client の手前で呼ばれる。ここが投げても
        gateway_internal_error の JSON で返る（SDK の "Error executing tool" にしない）。
        """
        from qgis_puppeteer.gateways import mcp as gw

        def boom(*_a: Any, **_k: Any) -> str | None:
            raise RuntimeError("outside the client path")

        monkeypatch.setattr(gw, "_resolve_target", boom)

        async def run() -> tuple[bool, dict[str, Any]]:
            async with fake_hub_server() as (_, hub_url):
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_list_layers", {})
                    return bool(result.is_error), json.loads(_extract_text(result))

        is_error, payload = _run(run())
        assert is_error is True
        assert payload["error"]["code"] == "gateway_internal_error"
        assert payload["error"]["details"]["type"] == "RuntimeError"
        assert "outside the client path" in payload["error"]["message"]
        assert payload["error"]["details"]["log"].endswith("gateway.log")

    def test_server_version_names_the_build(self) -> None:
        """検証 6: 同じ 0.1.0 が続くので、git インストールなら commit を添える。"""
        from qgis_puppeteer.gateways.mcp import _install_source, _server_version

        version, source = _install_source()
        assert version  # editable install でも版は取れる
        assert source  # 空にはならない（path / commit / index のどれか）
        sv = _server_version()
        assert sv.startswith(version)
        if source.startswith("commit "):
            assert sv == f"{version}+g{source[7:14]}"
        else:
            assert sv == version

    def test_gateway_log_path_honours_env(self, monkeypatch: Any) -> None:
        from qgis_puppeteer.gateways.mcp import _gateway_log_path

        monkeypatch.delenv("QPUPPETEER_GATEWAY_LOG", raising=False)
        assert _gateway_log_path().name == "gateway.log"
        assert _gateway_log_path().parent.name == "qgis_puppeteer"
        monkeypatch.setenv("QPUPPETEER_GATEWAY_LOG", "D:/tmp/gw.log")
        assert str(_gateway_log_path()).replace("\\", "/") == "D:/tmp/gw.log"

    def test_register_rejected_on_reconnect_is_hub_unreachable_not_a_crash(
        self, monkeypatch: Any
    ) -> None:
        """再接続で新しい Hub に register を弾かれても、構造化エラーで返る。"""

        async def run() -> dict[str, Any]:
            async with fake_hub_server() as (_, hub_url):
                ctx = GatewayContext(url=hub_url, origin=DEFAULT_ORIGIN)

                @asynccontextmanager
                async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
                    try:
                        yield ctx
                    finally:
                        await ctx.reset_client()

                async def reject(self: AutomationClient, *_args: Any) -> None:
                    raise RegisterError(None)

                monkeypatch.setattr(AutomationClient, "_register", reject)
                gateway = build_gateway(lifespan=_lifespan)
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_list_instances", {})
                    assert result.is_error
                    return json.loads(_extract_text(result))

        payload = _run(run())
        assert payload["error"]["code"] == "hub_unreachable"
        assert payload["error"]["details"]["type"] == "RegisterError"

    def test_a_missing_hub_file_is_hub_unreachable(self) -> None:
        """Hub ファイルが無い（token を読めない）ときも、構造化エラーで返る。"""

        async def run() -> dict[str, Any]:
            async with fake_hub_server() as (_, hub_url):
                Path(os.environ[ENV_HUB_FILE]).unlink()
                ctx = GatewayContext(url=hub_url, origin=DEFAULT_ORIGIN)

                @asynccontextmanager
                async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
                    try:
                        yield ctx
                    finally:
                        await ctx.reset_client()

                gateway = build_gateway(lifespan=_lifespan)
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_list_instances", {})
                    assert result.is_error
                    return json.loads(_extract_text(result))

        payload = _run(run())
        assert payload["error"]["code"] == "hub_unreachable"
        assert payload["error"]["details"]["type"] == "HubFileError"

    def test_wait_ready_keeps_waiting_while_the_hub_is_not_listening(self) -> None:
        """起動直後（Hub がまだ listen していない）に呼んでも即落ちしない。

        他のツールは hub_unreachable を即返すが、wait_ready は「まずこれを呼べ」と
        言っている手前、Hub が立つのも timeout_s の内で待つ。
        """

        async def run() -> tuple[dict[str, Any], dict[str, Any]]:
            # 先にポートを確保して閉じ、その番号で「まだ居ない Hub」を指す
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            ctx = GatewayContext(url=url, origin=DEFAULT_ORIGIN)

            @asynccontextmanager
            async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
                try:
                    yield ctx
                finally:
                    await ctx.reset_client()

            gateway = build_gateway(lifespan=_lifespan)
            async with Client(gateway, raise_exceptions=True) as client:
                # (1) 期限内に Hub が立たない → not_ready（hub_unreachable ではない）
                early = await client.call_tool("qgis_wait_ready", {"timeout_s": 0.6})
                early_payload = json.loads(_extract_text(early))
                assert early.is_error

                # (2) 待っている最中に Hub と Worker が立つ → ok
                async def hub_comes_up_later() -> None:
                    await asyncio.sleep(0.8)
                    async with fake_hub_server(port=port):
                        worker = _FakeWorker(url, label="late", pid=7)
                        await worker.start()
                        await worker.ready.wait()
                        try:
                            await asyncio.sleep(4.0)
                        finally:
                            await worker.stop()

                hub_task = asyncio.create_task(hub_comes_up_later())
                try:
                    late = await client.call_tool("qgis_wait_ready", {"timeout_s": 8})
                    late_payload = json.loads(_extract_text(late))
                finally:
                    hub_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await hub_task
                return early_payload, late_payload

        early, late = _run(run())
        assert early["error"]["code"] == "not_ready"
        assert "did not start listening" in early["error"]["message"]
        assert "hub_unreachable" in early["error"]["details"]
        assert late["ok"] is True
        assert late["instance"]["label"] == "late"
        assert late["waited_s"] >= 0.8

    def test_wait_ready_returns_once_a_round_trip_succeeds(self) -> None:
        async def run() -> dict[str, Any]:
            async with fake_hub_server() as (hub, hub_url):
                worker = _FakeWorker(hub_url, label="alpha", pid=1111)
                await worker.start()
                await worker.ready.wait()
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_wait_ready", {"timeout_s": 5})
                    payload = json.loads(_extract_text(result))
                    payload["_probed"] = [
                        r["command"]
                        for r in hub.received_requests
                        if r["command"] == "qgis_get_canvas_extent"
                    ]
                    return payload

        payload = _run(run())
        assert payload["ok"] is True
        assert payload["instance"]["label"] == "alpha"
        assert payload["instance"]["state"] == "active"
        assert payload["_probed"] == ["qgis_get_canvas_extent"]

    def test_wait_ready_reports_not_ready_with_snapshot(self) -> None:
        async def run() -> tuple[bool, dict[str, Any]]:
            async with fake_hub_server() as (hub, hub_url):
                worker = _FakeWorker(hub_url, label="alpha", pid=1111)
                await worker.start()
                await worker.ready.wait()
                hub.unresponsive.add("worker-alpha-1111")
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_wait_ready", {"timeout_s": 0.3})
                    return bool(result.is_error), json.loads(_extract_text(result))

        is_error, payload = _run(run())
        assert is_error is True
        assert payload["error"]["code"] == "not_ready"
        assert payload["error"]["instances"][0]["state"] == "unresponsive"

    def test_wait_ready_reports_a_rejected_registration_at_once(self) -> None:
        """#13: Hub が断った登録は待たずに理由付きで返す。"""

        async def run() -> tuple[bool, dict[str, Any], float]:
            async with fake_hub_server() as (hub, hub_url):
                hub.rejected.append(
                    {
                        "launch_token": "lt-mine",
                        "pid": 4321,
                        "label": "x" * 200,
                        "code": "label_conflict",
                        "message": "Invalid label: too_long",
                    }
                )
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    loop = asyncio.get_running_loop()
                    started = loop.time()
                    result = await client.call_tool(
                        "qgis_wait_ready", {"instance": "lt-mine", "timeout_s": 30}
                    )
                    took = loop.time() - started
                    return bool(result.is_error), json.loads(_extract_text(result)), took

        is_error, payload, took = _run(run())
        assert is_error is True
        assert payload["error"]["code"] == "registration_rejected"
        assert payload["error"]["rejection"]["pid"] == 4321
        assert took < 5.0


class TestMcpGatewayToolDispatch:
    def test_list_layers_forwards_command_to_worker(self) -> None:
        async def run() -> tuple[str, dict[str, Any]]:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)

                # Worker 側で qgis_list_layers に固有 payload を返させる
                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    return {
                        "type": "response",
                        "id": req["id"],
                        "ok": True,
                        "result": {
                            "command_seen": req["command"],
                            "layers": ["layer-1", "layer-2"],
                        },
                    }

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_list_layers", {})
                    text = _extract_text(result)
                    return text, json.loads(text)

        raw, parsed = _run(run())
        assert "qgis_list_layers" in raw
        assert parsed["command_seen"] == "qgis_list_layers"
        assert parsed["layers"] == ["layer-1", "layer-2"]

    def test_worker_error_becomes_error_json(self) -> None:
        async def run() -> dict[str, Any]:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)

                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    return {
                        "type": "response",
                        "id": req["id"],
                        "ok": False,
                        "error": {
                            "code": "worker_execution_error",
                            "message": "boom",
                            "details": {"type": "RuntimeError"},
                        },
                    }

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_execute_python", {"code": "1/0"})
                    return json.loads(_extract_text(result))

        payload = _run(run())
        assert "error" in payload
        assert payload["error"]["code"] == "worker_execution_error"
        assert "boom" in payload["error"]["message"]


class TestMcpGatewayStickyInstance:
    def test_use_instance_sets_sticky_then_tools_route_to_it(self) -> None:
        """use_instance で選んだ instance が以降のツールで使われる。"""

        async def run() -> dict[str, Any]:
            async with fake_hub_server() as (_hub, hub_url):
                w_a = _FakeWorker(hub_url, label="a", pid=1)
                w_b = _FakeWorker(hub_url, label="b", pid=2)

                def make_responder(marker: str):
                    def responder(req: dict[str, Any]) -> dict[str, Any]:
                        return {
                            "type": "response",
                            "id": req["id"],
                            "ok": True,
                            "result": {"from": marker},
                        }

                    return responder

                w_a.responder = make_responder("A")
                w_b.responder = make_responder("B")
                await w_a.start()
                await w_b.start()
                await w_a.ready.wait()
                await w_b.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    # sticky を b に設定
                    use_res = await client.call_tool("qgis_use_instance", {"selector": "b"})
                    assert "ok" in _extract_text(use_res)

                    # 引数 instance を省略すると sticky の b が選ばれる
                    result = await client.call_tool("qgis_list_layers", {})
                    return json.loads(_extract_text(result))

        payload = _run(run())
        assert payload["from"] == "B"

    def test_explicit_instance_overrides_sticky(self) -> None:
        async def run() -> dict[str, Any]:
            async with fake_hub_server() as (_hub, hub_url):
                w_a = _FakeWorker(hub_url, label="a", pid=1)
                w_b = _FakeWorker(hub_url, label="b", pid=2)

                def make_responder(marker: str):
                    def responder(req: dict[str, Any]) -> dict[str, Any]:
                        return {
                            "type": "response",
                            "id": req["id"],
                            "ok": True,
                            "result": {"from": marker},
                        }

                    return responder

                w_a.responder = make_responder("A")
                w_b.responder = make_responder("B")
                await w_a.start()
                await w_b.start()
                await w_a.ready.wait()
                await w_b.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    await client.call_tool("qgis_use_instance", {"selector": "b"})
                    # instance= で明示指定すると sticky を上書き
                    result = await client.call_tool("qgis_list_layers", {"instance": "a"})
                    return json.loads(_extract_text(result))

        payload = _run(run())
        assert payload["from"] == "A"

    def test_use_instance_unknown_selector_returns_error(self) -> None:
        async def run() -> dict[str, Any]:
            async with fake_hub_server() as (_hub, hub_url):
                w = _FakeWorker(hub_url, label="only", pid=7)
                await w.start()
                await w.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool(
                        "qgis_use_instance", {"selector": "does-not-exist"}
                    )
                    return json.loads(_extract_text(result))

        payload = _run(run())
        assert payload["error"]["code"] == "instance_not_found"
        assert payload["error"]["candidates"]


class TestMcpGatewayToolInventory:
    """登録されているツール一覧が ADR-0001 §5 と一致すること。"""

    def test_expected_tools_registered(self) -> None:
        async def run() -> list[str]:
            async with fake_hub_server() as (_hub, hub_url):
                w = _FakeWorker(hub_url, label="x", pid=1)
                await w.start()
                await w.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    listed = await client.list_tools()
                    return [t.name for t in listed.tools]

        names = _run(run())
        expected = {
            # instance 管理
            "qgis_list_instances",
            "qgis_wait_ready",
            "qgis_use_instance",
            # layer
            "qgis_list_layers",
            "qgis_get_layer_info",
            "qgis_select_features",
            "qgis_get_selected_features",
            # python
            "qgis_execute_python",
            "qgis_execute_with_permission",
            "qgis_get_whitelist",
            "qgis_clear_session_permissions",
            # canvas / screenshot
            "qgis_screenshot",
            "qgis_render_map",
            "qgis_get_canvas_extent",
            "qgis_set_canvas_extent",
            "qgis_canvas_mouse",
            "qgis_canvas_key",
            "qgis_get_leftover_state",
            # ui
            "qgis_snapshot_ui",
            "qgis_click_widget",
            "qgis_set_widget_value",
            "qgis_close_widget",
            "qgis_read_rows",
            "qgis_check_actionability",
            "qgis_wait_for",
            # dialog handler（worker 側 handler の公開。user-guide が MCP ツールと明記）
            "qgis_register_dialog_handler",
            "qgis_unregister_dialog_handler",
            "qgis_list_dialog_handlers",
            "qgis_clear_dialog_handlers",
            # 操作の裏で起きたこと（#5 / #8）
            "qgis_get_recent_exceptions",
            "qgis_clear_recent_exceptions",
            "qgis_get_recent_messages",
            "qgis_clear_recent_messages",
            # 操作記録（#8）
            "qgis_record_start",
            "qgis_record_step",
            "qgis_record_stop",
        }
        assert expected.issubset(set(names)), f"missing tools: {expected - set(names)}"


class TestMcpGatewayRenderMap:
    """`qgis_render_map`：描画した PNG を画像として添える（#6）。"""

    @staticmethod
    def _call(result: dict[str, Any], **args: Any) -> tuple[CallToolResult, dict[str, Any]]:
        seen: dict[str, Any] = {}

        async def run() -> CallToolResult:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)

                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    seen.update(req["params"])
                    return {"type": "response", "id": req["id"], "ok": True, "result": result}

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    return await client.call_tool("qgis_render_map", args)

        return _run(run()), seen

    def test_png_is_attached_as_an_image(self, tmp_path: Any) -> None:
        png = tmp_path / "r.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        result, seen = self._call(
            {"success": True, "path": str(png), "width": 2, "height": 1},
            width=2,
            height=1,
        )
        assert not result.is_error
        assert seen == {
            "antialiasing": True,
            "draw_labels": True,
            "draw_selection": True,
            "width": 2,
            "height": 1,
        }
        assert json.loads(_extract_text(result))["path"] == str(png)
        image = result.content[1]
        assert image.type == "image"
        assert image.mime_type == "image/png"

    def test_include_image_false_returns_text_only(self, tmp_path: Any) -> None:
        png = tmp_path / "r.png"
        png.write_bytes(b"x")
        result, seen = self._call({"success": True, "path": str(png)}, include_image=False)
        assert [c.type for c in result.content] == ["text"]
        assert "include_image" not in seen

    def test_worker_side_failure_is_a_tool_error(self) -> None:
        result, _ = self._call({"success": False, "error": "unknown crs: 'EPSG:1'"})
        assert result.is_error
        assert "unknown crs" in _extract_text(result)


class TestMcpGatewayWidgetFailures:
    """#23: click / 値の設定の success: false は tool error になる。"""

    @staticmethod
    def _call(
        tool: str, result: dict[str, Any], **args: Any
    ) -> tuple[CallToolResult, dict[str, Any]]:
        seen: dict[str, Any] = {}

        async def run() -> CallToolResult:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)

                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    seen.update(req["params"])
                    return {"type": "response", "id": req["id"], "ok": True, "result": result}

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    return await client.call_tool(tool, args)

        return _run(run()), seen

    def test_a_value_not_applied_is_a_tool_error(self) -> None:
        result, seen = self._call(
            "qgis_set_widget_value",
            {"success": False, "error": "value_not_applied", "value_after": 99},
            selector={"object_name": "n"},
            value=150,
            verify=True,
        )
        assert result.is_error
        payload = json.loads(_extract_text(result))
        assert payload["error"]["code"] == "value_not_applied"
        assert payload["error"]["details"] == {"value_after": 99}
        assert seen == {"selector": {"object_name": "n"}, "value": 150}

    def test_verify_false_is_passed(self) -> None:
        result, seen = self._call(
            "qgis_set_widget_value",
            {"success": True, "value_after": 99},
            selector={"object_name": "n"},
            value=150,
            verify=False,
        )
        assert not result.is_error
        assert seen["verify"] is False

    def test_a_failed_click_is_a_tool_error(self) -> None:
        result, _ = self._call(
            "qgis_click_widget",
            {"success": False, "error": "widget_disabled"},
            selector={"object_name": "b"},
        )
        assert result.is_error
        assert json.loads(_extract_text(result))["error"]["code"] == "widget_disabled"

    def test_kind_and_cell_are_passed(self) -> None:
        """#24"""
        result, seen = self._call(
            "qgis_click_widget",
            {"success": True, "cell": {"row": 1, "column": 0}},
            selector={"object_name": "t"},
            kind="double",
            cell={"text": "beta"},
        )
        assert not result.is_error
        assert seen == {
            "selector": {"object_name": "t"},
            "kind": "double",
            "cell": {"text": "beta"},
        }

    def test_read_rows_passes_its_arguments(self) -> None:
        """#36"""
        result, seen = self._call(
            "qgis_read_rows",
            {"success": True, "rows": [], "row_count": 0, "start": 0},
            selector={"object_name": "t"},
            start=1000,
            path=["道路"],
            role="user",
        )
        assert not result.is_error
        assert seen == {
            "selector": {"object_name": "t"},
            "start": 1000,
            "role": "user",
            "path": ["道路"],
        }

    def test_read_rows_with_cell_sends_no_start(self) -> None:
        result, seen = self._call(
            "qgis_read_rows",
            {"success": True, "cell": {"row": 0, "column": 0}, "value": "checked"},
            selector={"object_name": "t"},
            cell={"path": ["a", "b"]},
            role="check",
        )
        assert not result.is_error
        assert seen == {
            "selector": {"object_name": "t"},
            "role": "check",
            "cell": {"path": ["a", "b"]},
        }

    def test_read_rows_passes_start_with_cell(self) -> None:
        """cell と start は一緒に使えない。黙って捨てずに Worker に断らせる"""
        _, seen = self._call(
            "qgis_read_rows",
            {"success": False, "error": "invalid_value"},
            selector={"object_name": "t"},
            cell={"row": 0},
            start=5,
        )
        assert seen["start"] == 5
        assert seen["cell"] == {"row": 0}

    def test_set_widget_value_passes_the_cell(self) -> None:
        result, seen = self._call(
            "qgis_set_widget_value",
            {"success": True, "value_after": "checked"},
            selector={"object_name": "t"},
            value=True,
            cell={"path": ["a", "b"]},
        )
        assert not result.is_error
        assert seen == {
            "selector": {"object_name": "t"},
            "value": True,
            "cell": {"path": ["a", "b"]},
        }

    def test_close_is_passed_with_wait(self) -> None:
        """#34"""
        result, seen = self._call(
            "qgis_close_widget",
            {"success": True, "closed": True},
            selector={"title": "属性"},
            wait=False,
        )
        assert not result.is_error
        assert seen == {"selector": {"title": "属性"}, "wait": False}

    def test_success_is_plain_text(self) -> None:
        result, _ = self._call(
            "qgis_click_widget", {"success": True}, selector={"object_name": "b"}
        )
        assert not result.is_error
        assert json.loads(_extract_text(result)) == {"success": True}


class TestMcpGatewayCheckActionability:
    """Locator が使う QGIS 側の判定を、MCP からも 1 回だけ読める。"""

    def test_forwards_the_selector_and_returns_the_check(self) -> None:
        check = {
            "exists": True,
            "visible": True,
            "enabled": False,
            "not_covered": True,
            "editable": True,
            "actionable": False,
            "geometry": {"x": 0, "y": 0, "width": 80, "height": 24},
            "widget": {"class": "QPushButton", "object_name": "ok"},
            "diagnostics": {"match_count": 1},
        }
        seen: list[dict[str, Any]] = []

        async def run() -> CallToolResult:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)

                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    seen.append({"command": req["command"], "params": req["params"]})
                    return {"type": "response", "id": req["id"], "ok": True, "result": check}

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    return await client.call_tool(
                        "qgis_check_actionability", {"selector": {"object_name": "ok"}}
                    )

        result = _run(run())
        assert not result.is_error
        assert json.loads(_extract_text(result)) == check
        assert seen == [
            {"command": "qgis_check_actionability", "params": {"selector": {"object_name": "ok"}}}
        ]


class TestMcpGatewayWaitFor:
    """`qgis_wait_for`：条件を満たすまで gateway の中で QGIS に問い合わせ続ける。

    判定そのものは ``test_waiting.py``。ここは問い合わせの組み立て・くり返し・結果の形。
    """

    @pytest.fixture(autouse=True)
    def _fast_polling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from qgis_puppeteer.gateways import mcp as gateway_mod

        monkeypatch.setattr(gateway_mod, "WAIT_FOR_POLL_INTERVAL_S", 0.01)

    @staticmethod
    def _call(
        script: dict[str, list[dict[str, Any]]], **args: Any
    ) -> tuple[CallToolResult, list[dict[str, Any]]]:
        """コマンドごとに、呼ばれるたびに返す結果を順に返す（最後のものはくり返す）。

        結果に ``_error`` があれば、Worker の business error（``ok: false``）にする。
        """
        seen: list[dict[str, Any]] = []
        calls: dict[str, int] = {}

        async def run() -> CallToolResult:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)

                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    command = req["command"]
                    seen.append({"command": command, "params": req["params"]})
                    n = calls.get(command, 0)
                    calls[command] = n + 1
                    results = script[command]
                    result = results[min(n, len(results) - 1)]
                    if "_error" in result:
                        return {
                            "type": "response",
                            "id": req["id"],
                            "ok": False,
                            "error": result["_error"],
                        }
                    return {"type": "response", "id": req["id"], "ok": True, "result": result}

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    return await client.call_tool("qgis_wait_for", args)

        return _run(run()), seen

    @staticmethod
    def _check(**overrides: Any) -> dict[str, Any]:
        check: dict[str, Any] = {
            "exists": True,
            "visible": True,
            "enabled": True,
            "not_covered": True,
            "editable": True,
            "actionable": True,
            "geometry": {"x": 0, "y": 0, "width": 80, "height": 24},
            "widget": {"class": "QPushButton", "object_name": "ok"},
            "diagnostics": {"match_count": 1},
        }
        check.update(overrides)
        return check

    def test_polls_until_the_condition_is_met(self) -> None:
        result, seen = self._call(
            {
                "qgis_check_actionability": [
                    self._check(enabled=False, actionable=False),
                    self._check(enabled=False, actionable=False),
                    self._check(),
                ]
            },
            until="enabled",
            selector={"object_name": "ok"},
        )
        assert not result.is_error
        payload = json.loads(_extract_text(result))
        assert payload["ok"] is True
        assert payload["until"] == "enabled"
        assert payload["waited_s"] >= 0
        assert payload["observed"]["enabled"] is True
        assert "geometry" not in payload["observed"]
        assert (
            seen
            == [
                {
                    "command": "qgis_check_actionability",
                    "params": {"selector": {"object_name": "ok"}},
                }
            ]
            * 3
        )

    def test_timeout_reports_what_was_seen_last(self) -> None:
        result, seen = self._call(
            {"qgis_check_actionability": [self._check(visible=False, actionable=False)]},
            until="visible",
            selector={"object_name": "ok"},
            timeout_s=0.1,
        )
        assert result.is_error
        error = json.loads(_extract_text(result))["error"]
        assert error["code"] == "wait_timeout"
        assert error["details"]["until"] == "visible"
        assert error["details"]["timeout_s"] == 0.1
        assert error["details"]["observed"]["visible"] is False
        assert len(seen) >= 2

    def test_disabled_needs_the_widget_to_exist(self) -> None:
        """見つからない widget を「無効」とみなさない。time out して、見たものを返す。"""
        missing = self._check(
            exists=False,
            visible=False,
            enabled=False,
            not_covered=False,
            editable=False,
            actionable=False,
            geometry=None,
            widget=None,
            diagnostics={"reason": "no_match"},
        )
        result, _seen = self._call(
            {
                "qgis_check_actionability": [
                    missing,
                    self._check(enabled=False, actionable=False),
                ]
            },
            until="disabled",
            selector={"object_name": "ok"},
        )
        assert not result.is_error  # 現れて無効になったら満たす
        assert json.loads(_extract_text(result))["observed"]["exists"] is True

        result, _seen = self._call(
            {"qgis_check_actionability": [missing]},
            until="disabled",
            selector={"object_name": "typo"},
            timeout_s=0.1,
        )
        assert result.is_error
        error = json.loads(_extract_text(result))["error"]
        assert error["code"] == "wait_timeout"
        assert error["details"]["observed"]["exists"] is False
        assert error["details"]["observed"]["diagnostics"] == {"reason": "no_match"}

    def test_an_ambiguous_selector_fails_without_waiting(self) -> None:
        ambiguous = self._check(
            exists=False,
            visible=False,
            diagnostics={
                "reason": "selector_ambiguous",
                "match_count": 2,
                "candidates": [{"class": "QPushButton"}, {"class": "QPushButton"}],
            },
        )
        result, seen = self._call(
            {"qgis_check_actionability": [ambiguous]},
            until="hidden",
            selector={"class": "QPushButton"},
        )
        assert result.is_error
        error = json.loads(_extract_text(result))["error"]
        assert error["code"] == "selector_ambiguous"
        assert error["details"]["match_count"] == 2
        assert len(error["details"]["candidates"]) == 2
        assert len(seen) == 1

    def test_count_accepts_several_matches(self) -> None:
        result, seen = self._call(
            {
                "qgis_check_actionability": [
                    self._check(diagnostics={"reason": "selector_ambiguous", "match_count": 2}),
                    self._check(exists=False, diagnostics={"reason": "no_match"}),
                ]
            },
            until="count",
            selector={"class": "QDialog", "index": 1},
            expected=0,
        )
        assert not result.is_error
        assert json.loads(_extract_text(result))["observed"] == {"count": 0}
        # index を外し、見えている widget だけを数える（Locator.count() と同じ）
        assert seen[0]["params"] == {"selector": {"class": "QDialog", "attr": {"visible": True}}}
        assert len(seen) == 2

    def test_text_is_read_from_a_snapshot(self) -> None:
        def snap(text: str) -> dict[str, Any]:
            return {
                "active_modal": None,
                "main_window": {
                    "class": "QMainWindow",
                    "children": [{"class": "QLabel", "object_name": "status", "text": text}],
                },
            }

        result, seen = self._call(
            {
                "qgis_check_actionability": [self._check()],
                "qgis_snapshot_ui": [snap("Loading"), snap("Loaded 3 layers")],
            },
            until="text_contains",
            selector={"object_name": "status"},
            expected="3 layers",
        )
        assert not result.is_error
        observed = json.loads(_extract_text(result))["observed"]
        assert observed == {"found": True, "value": "Loaded 3 layers"}
        snapshots = [c["params"] for c in seen if c["command"] == "qgis_snapshot_ui"]
        assert len(snapshots) == 2
        assert snapshots[0]["include_main_window"] is True

    def test_unchecked_needs_the_widget_to_exist(self) -> None:
        """無い widget を「チェックされていない」とみなさない。time out して found=false。"""
        empty = {"active_modal": None, "main_window": {"class": "QMainWindow", "children": []}}
        result, _seen = self._call(
            {
                "qgis_check_actionability": [self._check(exists=False)],
                "qgis_snapshot_ui": [empty],
            },
            until="unchecked",
            selector={"object_name": "snap_to_grid_typo"},
            timeout_s=0.1,
        )
        assert result.is_error
        error = json.loads(_extract_text(result))["error"]
        assert error["code"] == "wait_timeout"
        assert error["details"]["observed"] == {"found": False, "value": None}

    def test_snapshot_conditions_reject_live_only_keys(self) -> None:
        result, seen = self._call(
            {},
            until="text",
            selector={"object_name": "status", "window": "Layer Properties"},
            expected="x",
        )
        assert result.is_error
        error = json.loads(_extract_text(result))["error"]
        assert error["code"] == "invalid_params"
        assert "window" in error["message"]
        assert seen == []

    def test_bad_arguments_are_rejected_before_asking_qgis(self) -> None:
        result, seen = self._call({}, until="visible")
        assert result.is_error
        error = json.loads(_extract_text(result))["error"]
        assert error == {"code": "invalid_params", "message": "until='visible' needs a selector"}
        assert seen == []

    def test_modal_with_title(self) -> None:
        modal = {
            "class": "QMessageBox",
            "title": "Save changes?",
            "children": [{"class": "QPushButton", "text": "&Save"}],
        }
        result, seen = self._call(
            {
                "qgis_snapshot_ui": [
                    {"active_modal": None},
                    {"active_modal": {"class": "QDialog", "title": "Progress"}},
                    {"active_modal": modal},
                ]
            },
            until="modal",
            title_contains="Save",
        )
        assert not result.is_error
        observed = json.loads(_extract_text(result))["observed"]
        assert observed == {"modal": {"class": "QMessageBox", "title": "Save changes?"}}
        assert len(seen) == 3
        assert seen[0]["params"]["include_main_window"] is False

    def test_popup_closed(self) -> None:
        result, _ = self._call(
            {
                "qgis_snapshot_ui": [
                    {"active_popup": {"class": "QMenu", "title": "&File", "menu_items": []}},
                    {"active_popup": None},
                ]
            },
            until="popup_closed",
        )
        assert not result.is_error
        assert json.loads(_extract_text(result))["observed"] == {"popup": None}

    def test_a_worker_error_is_a_tool_error(self) -> None:
        result, seen = self._call(
            {
                "qgis_check_actionability": [
                    {"_error": {"code": "worker_execution_error", "message": "boom"}}
                ]
            },
            until="visible",
            selector={"object_name": "ok"},
        )
        assert result.is_error
        error = json.loads(_extract_text(result))["error"]
        assert error["code"] == "worker_execution_error"
        assert "boom" in error["message"]
        assert len(seen) == 1

    def test_the_wait_is_one_line_in_the_recording(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """何度問い合わせても、記録には qgis_wait_for の 1 行だけが残る。"""
        monkeypatch.setenv("QPUPPETEER_MCP_RECORD_DIR", str(tmp_path))
        checks = [self._check(visible=False), self._check(visible=False), self._check()]

        async def run() -> dict[str, Any]:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)
                served = iter(checks)

                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    return {"type": "response", "id": req["id"], "ok": True, "result": next(served)}

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    await client.call_tool("qgis_record_start", {"name": "wait"})
                    await client.call_tool(
                        "qgis_wait_for", {"until": "visible", "selector": {"object_name": "ok"}}
                    )
                    return json.loads(_extract_text(await client.call_tool("qgis_record_stop", {})))

        stopped = _run(run())
        assert stopped["actions"] == 1
        with open(stopped["path"], encoding="utf-8") as fh:
            (line,) = [json.loads(raw) for raw in fh.read().splitlines()]
        assert line["command"] == "qgis_wait_for"
        assert line["params"]["until"] == "visible"
        assert line["params"]["selector"] == {"object_name": "ok"}
        assert line["result"]["ok"] is True


class TestMcpGatewayWorkerRecords:
    """例外・ログメッセージの照会ツールが Worker のコマンドにそのまま渡ること。"""

    @staticmethod
    def _call(tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        seen: dict[str, Any] = {}

        async def run() -> CallToolResult:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)

                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    seen["command"] = req["command"]
                    seen["params"] = req["params"]
                    return {"type": "response", "id": req["id"], "ok": True, "result": {}}

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    return await client.call_tool(tool, args)

        result = _run(run())
        assert not result.is_error
        return seen["command"], seen["params"]

    def test_get_recent_messages_sends_only_given_filters(self) -> None:
        command, params = self._call(
            "qgis_get_recent_messages", {"min_level": "critical", "after_seq": 7}
        )
        assert command == "qgis_get_recent_messages"
        assert params == {"min_level": "critical", "after_seq": 7}

    def test_get_recent_exceptions_limit(self) -> None:
        assert self._call("qgis_get_recent_exceptions", {"limit": 3}) == (
            "qgis_get_recent_exceptions",
            {"limit": 3},
        )

    @pytest.mark.parametrize("tool", ["qgis_clear_recent_messages", "qgis_clear_recent_exceptions"])
    def test_clear_tools(self, tool: str) -> None:
        assert self._call(tool, {}) == (tool, {})


class TestMcpGatewayRecording:
    """操作記録（#8）：全ツールが actions.jsonl と同じ形で記録されること。"""

    def test_exploration_is_recorded_in_actions_jsonl_shape(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pytest_qgis_puppeteer._action_recorder import ActionRecord as PytestRecord

        monkeypatch.setenv("QPUPPETEER_MCP_RECORD_DIR", str(tmp_path))

        async def run() -> tuple[dict[str, Any], dict[str, Any]]:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)

                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    if req["command"] == "qgis_get_layer_info":
                        return {
                            "type": "response",
                            "id": req["id"],
                            "ok": False,
                            "error": {"code": "worker_execution_error", "message": "no such layer"},
                        }
                    return {"type": "response", "id": req["id"], "ok": True, "result": {"n": 1}}

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()
                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    await client.call_tool("qgis_list_layers", {})  # 記録前：残らない
                    started = json.loads(
                        _extract_text(await client.call_tool("qgis_record_start", {"name": "t1"}))
                    )
                    await client.call_tool("qgis_list_instances", {})
                    await client.call_tool("qgis_record_step", {"name": "レイヤを見る"})
                    await client.call_tool("qgis_list_layers", {"instance": "a"})
                    await client.call_tool("qgis_get_layer_info", {"layer_name": "nope"})
                    stopped = json.loads(
                        _extract_text(await client.call_tool("qgis_record_stop", {}))
                    )
                    await client.call_tool("qgis_list_layers", {})  # 停止後：残らない
                    return started, stopped

        started, stopped = _run(run())
        assert stopped == {"recording": False, "path": started["path"], "actions": 3}
        with open(started["path"], encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh.read().splitlines()]
        assert [line["command"] for line in lines] == [
            "qgis_list_instances",
            "qgis_list_layers",
            "qgis_get_layer_info",
        ]
        listed, layers, failed = lines
        assert listed["step_path"] == []
        assert layers["step_path"] == ["レイヤを見る"]
        assert layers["instance"] == "a"
        assert layers["params"] == {"instance": "a"}
        assert layers["result"] == {"n": 1}
        assert failed["result"] is None
        assert "no such layer" in failed["error"]["message"]
        assert all(line["source"] == "mcp" for line in lines)
        # pytest の actions.jsonl と同じ項目（+ source）
        pytest_keys = set(
            PytestRecord(
                ts="",
                duration_ms=0,
                command="",
                params=None,
                result=None,
                error=None,
                instance=None,
                step_path=(),
            ).to_dict()
        )
        assert set(listed) == pytest_keys | {"source"}

    def test_recording_tools_are_not_read_only(self) -> None:
        """記録はファイルを書く。並列に投げられる read-only 扱いにしない。"""
        tools = {t.name: t for t in TestMcpGatewayToolDescriptors()._list_tools()}
        for name in ("qgis_record_start", "qgis_record_step", "qgis_record_stop"):
            assert tools[name].annotations.read_only_hint is False
        assert (
            "選べない"
            in tools["qgis_record_start"].input_schema["properties"]["name"]["description"]
        )


class TestMcpGatewayHubUnreachable:
    """Hub が立ち上がっていない環境での tool 呼び出しの挙動。

    Gateway は Claude Desktop の起動タイミングで立ち上がるので、QGIS 側
    Worker がまだいない状態で tool が呼ばれる。ここで接続失敗を例外として
    伝播すると Claude 側の表示が壊れるので、`hub_unreachable` の
    error JSON を一貫して返すのが仕様。
    """

    def test_tool_returns_hub_unreachable_when_cannot_connect(self) -> None:
        # 127.0.0.1:1 は通常の環境では listen されていないので ConnectionRefused。
        unreachable_url = "ws://127.0.0.1:1"

        @asynccontextmanager
        async def _lifespan_no_hub(_server: Any) -> AsyncIterator[GatewayContext]:
            yield GatewayContext(url=unreachable_url, origin=DEFAULT_ORIGIN)

        async def run() -> dict[str, Any]:
            gateway = build_gateway(lifespan=_lifespan_no_hub)
            async with Client(gateway, raise_exceptions=True) as client:
                result = await client.call_tool("qgis_list_instances", {})
                return json.loads(_extract_text(result))

        payload = _run(run())
        assert payload["error"]["code"] == "hub_unreachable"
        assert "QGIS Puppeteer Hub" in payload["error"]["message"]
        assert payload["error"]["details"]["url"] == unreachable_url

    def test_layer_tool_also_returns_hub_unreachable(self) -> None:
        """`_call` 経路（layer/python/ui など）でも同じ扱い。"""
        unreachable_url = "ws://127.0.0.1:1"

        @asynccontextmanager
        async def _lifespan_no_hub(_server: Any) -> AsyncIterator[GatewayContext]:
            yield GatewayContext(url=unreachable_url, origin=DEFAULT_ORIGIN)

        async def run() -> dict[str, Any]:
            gateway = build_gateway(lifespan=_lifespan_no_hub)
            async with Client(gateway, raise_exceptions=True) as client:
                result = await client.call_tool("qgis_list_layers", {})
                return json.loads(_extract_text(result))

        payload = _run(run())
        assert payload["error"]["code"] == "hub_unreachable"

    def test_timeout_also_returns_hub_unreachable(self) -> None:
        """TCP は通るがハンドシェイクがタイムアウトするケースも
        `hub_unreachable` に帰着すること。

        `AutomationClient.connect` が `ConnectionRefusedError` に変換する
        ため基本的には ConnectionRefusedError ケースで catch されるが、
        何らかの理由で TimeoutError が漏れてきても Gateway 側で同じ
        error JSON に丸める保険レイヤを検証する。
        """
        url = "ws://127.0.0.1:9999"

        @asynccontextmanager
        async def _lifespan_timeout(_server: Any) -> AsyncIterator[GatewayContext]:
            ctx = GatewayContext(url=url, origin=DEFAULT_ORIGIN)

            async def _raise_timeout() -> AutomationClient:
                raise asyncio.TimeoutError("simulated handshake timeout")

            # dataclass インスタンスに bound method を上書きして差し替える
            ctx.get_client = _raise_timeout  # type: ignore[method-assign]
            yield ctx

        async def run() -> dict[str, Any]:
            gateway = build_gateway(lifespan=_lifespan_timeout)
            async with Client(gateway, raise_exceptions=True) as client:
                result = await client.call_tool("qgis_list_instances", {})
                return json.loads(_extract_text(result))

        payload = _run(run())
        assert payload["error"]["code"] == "hub_unreachable"
        # TimeoutError でも "TimeoutError" が details.type に入っていることを確認
        assert "Timeout" in payload["error"]["details"]["type"]


class TestMcpGatewayErrorSignalling:
    """失敗は `isError` で返す（本文の JSON は成功時と同じ書式のまま）。

    例外を投げるとクライアント側で汎用メッセージに丸められる。本文を読める
    まま残しつつ、呼び出しが失敗したことはフラグで伝える。
    """

    def test_hub_unreachable_is_marked_as_error(self) -> None:
        @asynccontextmanager
        async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
            ctx = GatewayContext(url="ws://127.0.0.1:1", origin=DEFAULT_ORIGIN)

            async def _raise_timeout() -> AutomationClient:
                raise asyncio.TimeoutError("simulated handshake timeout")

            ctx.get_client = _raise_timeout  # type: ignore[method-assign]
            yield ctx

        async def run() -> tuple[bool, dict[str, Any]]:
            gateway = build_gateway(lifespan=_lifespan)
            async with Client(gateway, raise_exceptions=True) as client:
                result = await client.call_tool("qgis_list_layers", {})
                return result.is_error, json.loads(_extract_text(result))

        is_error, payload = _run(run())
        assert is_error is True
        assert payload["error"]["code"] == "hub_unreachable"

    def test_worker_error_is_marked_as_error(self) -> None:
        async def run() -> tuple[bool, dict[str, Any]]:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)

                def responder(req: dict[str, Any]) -> dict[str, Any]:
                    return {
                        "type": "response",
                        "id": req["id"],
                        "ok": False,
                        "error": {"code": "worker_execution_error", "message": "boom"},
                    }

                worker.responder = responder
                await worker.start()
                await worker.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_execute_python", {"code": "1/0"})
                    return result.is_error, json.loads(_extract_text(result))

        is_error, payload = _run(run())
        assert is_error is True
        assert payload["error"]["code"] == "worker_execution_error"

    def test_unknown_selector_is_marked_as_error(self) -> None:
        async def run() -> tuple[bool, dict[str, Any]]:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="only", pid=7)
                await worker.start()
                await worker.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool(
                        "qgis_use_instance", {"selector": "does-not-exist"}
                    )
                    return result.is_error, json.loads(_extract_text(result))

        is_error, payload = _run(run())
        assert is_error is True
        assert payload["error"]["code"] == "instance_not_found"

    def test_success_is_not_marked_as_error(self) -> None:
        async def run() -> tuple[bool, dict[str, Any]]:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)
                await worker.start()
                await worker.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_list_layers", {})
                    return result.is_error, json.loads(_extract_text(result))

        is_error, payload = _run(run())
        assert is_error is False
        assert isinstance(payload, dict)


class TestMcpGatewayLegacyHandshake:
    """`mode="legacy"`（initialize 握手＋JSON-RPC）でも同じように動くこと。

    既定の `mode="auto"` は DirectDispatcher で直結するので、stdio 本番
    （Claude Code / Claude Desktop）と同じ `Server.run` の経路を通らない。
    ここで握手・ツール一覧・リクエストをまたぐ sticky の保持までを 1 本で押さえる。
    """

    def test_handshake_then_sticky_routing_over_jsonrpc(self) -> None:
        async def run() -> tuple[str | None, list[str], dict[str, Any]]:
            async with fake_hub_server() as (_hub, hub_url):
                w_a = _FakeWorker(hub_url, label="a", pid=1)
                w_b = _FakeWorker(hub_url, label="b", pid=2)

                def make_responder(marker: str):
                    def responder(req: dict[str, Any]) -> dict[str, Any]:
                        return {
                            "type": "response",
                            "id": req["id"],
                            "ok": True,
                            "result": {"from": marker},
                        }

                    return responder

                w_a.responder = make_responder("A")
                w_b.responder = make_responder("B")
                await w_a.start()
                await w_b.start()
                await w_a.ready.wait()
                await w_b.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                # 公式の Testing ガイドに従い、legacy では raise_exceptions を付けない
                async with Client(gateway, mode="legacy") as client:
                    server_info = client.server_info
                    listed = await client.list_tools()
                    use_res = await client.call_tool("qgis_use_instance", {"selector": "b"})
                    assert "ok" in _extract_text(use_res)
                    result = await client.call_tool("qgis_list_layers", {})
                    return (
                        server_info.name if server_info is not None else None,
                        [t.name for t in listed.tools],
                        json.loads(_extract_text(result)),
                    )

        server_name, names, payload = _run(run())
        assert server_name == "qgis-puppeteer"
        assert {"qgis_use_instance", "qgis_list_layers"} <= set(names)
        # 別リクエストで設定した sticky が次のリクエストに効いている
        assert payload["from"] == "B"


class TestMcpGatewayToolDescriptors:
    """クライアントに見せる記述子：表示名・注釈・結果の形。"""

    @staticmethod
    @asynccontextmanager
    async def _lifespan(_server: Any) -> AsyncIterator[GatewayContext]:
        """Hub には触らない lifespan（記述子の確認だけなら接続は不要）。"""
        yield GatewayContext(url="ws://127.0.0.1:1", origin=DEFAULT_ORIGIN)

    def _list_tools(self) -> list[Any]:
        async def run() -> list[Any]:
            gateway = build_gateway(lifespan=self._lifespan)
            async with Client(gateway, raise_exceptions=True) as client:
                return list((await client.list_tools()).tools)

        return _run(run())

    def test_every_tool_has_title_and_annotations(self) -> None:
        tools = self._list_tools()
        missing = [t.name for t in tools if not t.title or t.annotations is None]
        assert not missing, f"title / annotations 未設定: {missing}"

    def test_read_only_hint_marks_exactly_the_query_tools(self) -> None:
        """read_only が立ったツールはクライアント側で並列に投げられる。

        状態を書き換えるものを取り違えると、並列実行で壊れる側に倒れるので
        「照会のみ」の一覧をここで固定する。
        """
        tools = self._list_tools()
        read_only = {t.name for t in tools if t.annotations and t.annotations.read_only_hint}
        assert read_only == {
            "qgis_list_instances",
            "qgis_wait_ready",
            "qgis_list_layers",
            "qgis_get_layer_info",
            "qgis_get_selected_features",
            "qgis_get_whitelist",
            "qgis_get_canvas_extent",
            "qgis_get_leftover_state",
            "qgis_snapshot_ui",
            "qgis_read_rows",
            "qgis_check_actionability",
            "qgis_wait_for",
            "qgis_list_dialog_handlers",
            "qgis_get_recent_exceptions",
            "qgis_get_recent_messages",
        }

    def test_open_world_hint_marks_the_arbitrary_tools(self) -> None:
        """任意の Python 実行・UI 操作に化けうるものだけ open_world を立てる。"""
        tools = self._list_tools()
        open_world = {t.name for t in tools if t.annotations and t.annotations.open_world_hint}
        assert open_world == {
            "qgis_execute_python",
            "qgis_execute_with_permission",
            "qgis_click_widget",
            "qgis_set_widget_value",
            "qgis_close_widget",  # closeEvent で何が起きるかは窓次第（#34）
            # 地図のマップツールが何をするかは呼び出し次第（#26）
            "qgis_canvas_mouse",
            "qgis_canvas_key",
        }

    def test_no_tool_declares_an_output_schema(self) -> None:
        """`-> str` から自動生成される {"result": ...} schema を切っていること。

        付いたままだと同じ JSON が本文と structuredContent に二重に載る
        （`structured_output=False`）。
        """
        tools = self._list_tools()
        with_schema = [t.name for t in tools if t.output_schema is not None]
        assert not with_schema, f"outputSchema が付いている: {with_schema}"

    def test_tricky_parameters_are_documented(self) -> None:
        """モデルが値を組み立てにくい引数と契約は、説明として渡す。

        selector の受け付けるキー、instance の省略時の既定、`_result` に代入
        しないと値が返らないことは、ここに書かれていなければ推測になる。
        """
        tools = {t.name: t for t in self._list_tools()}

        selector = tools["qgis_click_widget"].input_schema["properties"]["selector"]
        assert "object_name" in selector.get("description", "")

        instance = tools["qgis_list_layers"].input_schema["properties"]["instance"]
        assert "qgis_use_instance" in instance.get("description", "")

        assert "_result" in (tools["qgis_execute_python"].description or "")

        # pytest のために入れた QGIS 側の振る舞いも、MCP の説明から分かること
        assert "point_covered" in (tools["qgis_click_widget"].description or "")
        assert "point_covered" in (tools["qgis_canvas_mouse"].description or "")
        assert "QgsFileWidget" in (tools["qgis_set_widget_value"].description or "")
        assert "ファイルダイアログ" in (tools["qgis_click_widget"].description or "")

    def test_wait_ready_exposes_require_project(self) -> None:
        tools = {t.name: t for t in self._list_tools()}
        props = tools["qgis_wait_ready"].input_schema["properties"]
        assert props["require_project"]["type"] == "boolean"
        assert props["require_project"]["default"] is False
        assert "レイヤ" in props["require_project"]["description"]

    def test_permission_cannot_grant_a_persistent_whitelist_entry(self) -> None:
        """`always` は MCP から選べないこと。

        always は実行に加えてコードをホワイトリストのファイルへ書き、以後の全
        セッションで無確認にする。この引数は呼び出し側が埋めるだけで人間に確認
        した保証がないので、永続的な許可の拡大は MCP 経路から外してある。
        """
        tools = {t.name: t for t in self._list_tools()}
        permission = tools["qgis_execute_with_permission"].input_schema["properties"]["permission"]
        assert set(permission["enum"]) == {"once", "session", "cancel"}

    def test_result_carries_text_only(self) -> None:
        """結果は本文の JSON テキスト 1 本で、structuredContent を伴わない。"""

        async def run() -> tuple[Any, dict[str, Any]]:
            async with fake_hub_server() as (_hub, hub_url):
                worker = _FakeWorker(hub_url, label="a", pid=1)
                await worker.start()
                await worker.ready.wait()

                gateway = build_gateway(lifespan=_build_test_lifespan(hub_url))
                async with Client(gateway, raise_exceptions=True) as client:
                    result = await client.call_tool("qgis_list_layers", {})
                    return result.structured_content, json.loads(_extract_text(result))

        structured, payload = _run(run())
        assert structured is None
        assert isinstance(payload, dict)


# ============================================================
# mcp.Client（sanity check）
# ============================================================


def test_client_type_is_mcp_type() -> None:
    """import が破綻していないことの静的チェック。"""
    assert Client is not None
