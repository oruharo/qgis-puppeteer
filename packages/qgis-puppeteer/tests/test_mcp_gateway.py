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
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from qgis_puppeteer.client import AutomationClient, RegisterError

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

    def test_unexpected_client_error_is_retried_then_reported_with_type(
        self, monkeypatch: Any
    ) -> None:
        """stale 判定に無い例外でも、1 回目はリセット + 再試行、2 回目は型付き JSON。

        検証 3: Hub 入れ替え後に qgis_list_instances が本文の無い
        "Error executing tool" で落ち、何度呼んでも回復しなかった。gateway の
        stderr は呼び出し側から見えないので、例外を外へ漏らさない。
        """

        async def run() -> tuple[dict[str, Any], dict[str, Any]]:
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

                async def flaky(self: AutomationClient, **kw: Any) -> Any:
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("boom from a place nobody expected")
                    return await original(self, **kw)

                async def always_broken(self: AutomationClient, **kw: Any) -> Any:
                    raise RuntimeError("still broken")

                gateway = build_gateway(lifespan=_lifespan)
                async with Client(gateway, raise_exceptions=True) as client:
                    monkeypatch.setattr(AutomationClient, "list_instances", flaky)
                    ok = json.loads(
                        _extract_text(await client.call_tool("qgis_list_instances", {}))
                    )
                    monkeypatch.setattr(AutomationClient, "list_instances", always_broken)
                    result = await client.call_tool("qgis_list_instances", {})
                    assert result.is_error
                    broken = json.loads(_extract_text(result))
                    await worker.stop()
                    return ok, broken

        ok, broken = _run(run())
        # 1 回目の想定外例外は接続を張り直して吸収され、結果は正常
        assert ok[0]["label"] == "alpha"
        # 2 回続けて落ちたら、型と traceback を載せた JSON になる（SDK の
        # 本文なし "Error executing tool" にはしない）
        assert broken["error"]["code"] == "gateway_internal_error"
        assert broken["error"]["details"]["type"] == "RuntimeError"
        assert "still broken" in broken["error"]["message"]
        assert "always_broken" in broken["error"]["details"]["traceback"]

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

                async def reject(self: AutomationClient) -> None:
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
            "qgis_get_canvas_extent",
            "qgis_set_canvas_extent",
            # ui
            "qgis_snapshot_ui",
            "qgis_click_widget",
            "qgis_set_widget_value",
            # dialog handler（worker 側 handler の公開。user-guide が MCP ツールと明記）
            "qgis_register_dialog_handler",
            "qgis_unregister_dialog_handler",
            "qgis_list_dialog_handlers",
            "qgis_clear_dialog_handlers",
        }
        assert expected.issubset(set(names)), f"missing tools: {expected - set(names)}"


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
            "qgis_snapshot_ui",
            "qgis_list_dialog_handlers",
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
