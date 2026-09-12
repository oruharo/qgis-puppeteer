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
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from qgis_puppeteer.client import AutomationClient

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
        assert instances[0]["instance_id"] == "worker-alpha-1111"


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


# ============================================================
# mcp.Client（sanity check）
# ============================================================


def test_client_type_is_mcp_type() -> None:
    """import が破綻していないことの静的チェック。"""
    assert Client is not None
