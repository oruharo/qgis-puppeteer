"""McpGateway — Claude Desktop 向け MCP サーバー。

ADR-0001 §5, §7 に基づき、MCP プロトコル層を自動化層の内部プロトコル
（WebSocket + JSON）にブリッジする。Claude Desktop は stdio で本モジュール
(`python -m qgis_puppeteer.gateways.mcp`) を spawn する。

## 責務
- `MCPServer`（mcp 2.x。1.x の `FastMCP`）で `qgis-puppeteer` 用の MCP ツール群を公開する
- 接続ごとに `AutomationClient` を 1 つ開き、呼び出しごとに `.call(...)` で
  Hub に転送する
- `GatewayContext` に sticky instance を保持し、lifespan 全体で共有する
  （MCPServer の各 tool call は別 Task で走るため ContextVar では伝播しない）
- `qgis_list_instances` / `qgis_use_instance` は Hub 直接照会／設定

## スレッド・ライフサイクル
- MCPServer は asyncio ベースの単一イベントループ上で動作する
- AutomationClient は `@asynccontextmanager` のライフスパンで open/close する
- sticky instance は `GatewayContext.current_instance` に保存される

## 引数注入
- 環境変数 `QPUPPETEER_HUB_URL` / `QPUPPETEER_HUB_ORIGIN` でテスト・開発用に切り替え可能
- デフォルトは `ws://127.0.0.1:9876` / `http://localhost`
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import websockets.exceptions

from qgis_puppeteer.client import (
    AutomationClient,
    NotConnectedError,
    RequestError,
)
from qgis_puppeteer.hub_state import select_by_selector
from qgis_puppeteer.protocol import Role

try:
    from mcp.server.mcpserver import Context, MCPServer
except ImportError as e:  # pragma: no cover - optional deps
    raise ImportError(
        "McpGateway requires the 'mcp' optional dependency (mcp 2.x). "
        "Install with: uv pip install 'qgis-puppeteer[mcp]'"
    ) from e

logger = logging.getLogger("qgis_puppeteer.gateways.mcp")

# ============================================================
# 定数
# ============================================================

DEFAULT_HUB_URL: str = "ws://127.0.0.1:9876"
DEFAULT_ORIGIN: str = "http://localhost"
ENV_HUB_URL: str = "QPUPPETEER_HUB_URL"
ENV_ORIGIN: str = "QPUPPETEER_HUB_ORIGIN"

MCP_SERVER_NAME: str = "qgis-puppeteer"


# ============================================================
# ライフスパン：AutomationClient を 1 接続共有
# ============================================================


@dataclass
class GatewayContext:
    """lifespan 経由で各 tool 関数に渡される共有状態。

    MCP サーバは「Claude Desktop 起動時＝Gateway 起動時」であり、QGIS が
    まだ立ち上がっていないのが普通。ここで先回りして Hub に接続しにいくと、
    Hub 不在時に Gateway ごと落ちてしまう。そこで接続は **tool が呼ばれた
    時に遅延実行** する（`get_client()`）。未接続のまま再利用される限りは
    Hub・Worker の状態に影響されない。

    - `url` / `origin` 指定で構築すると、`get_client()` 初回呼び出し時に
      `AutomationClient` を自前で接続する（所有権も Context 側）。
    - `client` を直接注入して構築するとそれをそのまま使う（テスト用。
      close は呼び出し側が lifespan 内で管理する前提）。
    - `current_instance` は `qgis_use_instance` で設定された sticky な
      **selector**（ADR-0005 D3: instance_id を凍結せず、label 等の安定キーを
      保持して dispatch ごとに Hub が再解決する。worker 再起動を跨いで
      「現在 live なそのロール」へ追従する）。`instance` 引数省略時の既定値。
    """

    client: AutomationClient | None = None
    url: str | None = None
    origin: str = DEFAULT_ORIGIN
    current_instance: str | None = field(default=None)
    _owns_client: bool = field(default=False, init=False)

    async def get_client(self) -> AutomationClient:
        """接続済み `AutomationClient` を返す。未接続なら初回のみ接続する。

        `ConnectionRefusedError` 等の接続失敗は上位に素通しする。tool 層で
        ユーザー向けエラー JSON に整形する。
        """
        if self.client is None:
            if self.url is None:
                raise RuntimeError("GatewayContext has neither url nor injected client")
            # McpGateway は Role.MCP_GATEWAY として Hub に自己申告する。
            # これにより Hub が Worker 転送時に `caller_role=MCP_GATEWAY` を
            # 付与し、Worker 側で Tier 3（`test.*`）ハンドラへのアクセスが
            # 自動的に遮断される（ADR-0001 §12.7）。
            c = AutomationClient(url=self.url, origin=self.origin, role=Role.MCP_GATEWAY)
            await c.connect()
            self.client = c
            self._owns_client = True
        return self.client

    async def close(self) -> None:
        """自前で接続した client だけ close する（注入された client は触らない）。"""
        if self._owns_client and self.client is not None:
            await self.client.close()
            self.client = None
            self._owns_client = False

    async def reset_client(self) -> None:
        """接続が壊れた client を破棄する。次回 `get_client()` で再接続される。

        tool 呼び出し中に QGIS/Worker がダウンした場合、cached `self.client`
        は使いものにならない socket を抱えたままになる。呼び出し側で
        `ConnectionClosed` 等を検知したらこれを呼んで client を捨てる。
        注入された client（テスト用）の場合は触らず owned flag だけ戻す。
        """
        if self._owns_client and self.client is not None:
            try:
                await self.client.close()
            except Exception:  # noqa: BLE001 - best-effort
                logger.debug("close() failed during reset", exc_info=True)
        self.client = None
        self._owns_client = False


@asynccontextmanager
async def _default_lifespan(
    server: MCPServer,
) -> AsyncIterator[GatewayContext]:
    """URL/origin を覚えた `GatewayContext` を返すだけ。接続は遅延させる。

    環境変数 `QPUPPETEER_HUB_URL` / `QPUPPETEER_HUB_ORIGIN` があればそれを使う。
    """
    url = os.environ.get(ENV_HUB_URL, DEFAULT_HUB_URL)
    origin = os.environ.get(ENV_ORIGIN, DEFAULT_ORIGIN)
    ctx = GatewayContext(url=url, origin=origin)
    try:
        yield ctx
    finally:
        await ctx.close()


# ============================================================
# 共通ディスパッチ
# ============================================================


def _resolve_target(gateway: GatewayContext, instance: str | None) -> str | None:
    """引数優先、未指定時は sticky（GatewayContext）を返す。"""
    if instance is not None:
        return instance
    return gateway.current_instance


def _format_result(value: Any) -> str:
    """handler の戻り値を MCP クライアントに渡す文字列表現に揃える。

    - 既に str ならそのまま
    - それ以外は JSON 化（dict/list/数値/None）
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _format_unreachable(gateway: GatewayContext, exc: BaseException) -> str:
    """Hub に届かない旨を一貫した JSON でユーザーに返す。

    Gateway 起動時点では QGIS が立ち上がっていないのが普通で、tool 呼び出し
    時に初めて接続を試みる。接続失敗は珍しくないので、例外を投げず Claude
    に読める形のエラー JSON に落とす。
    """
    return _format_result(
        {
            "error": {
                "code": "hub_unreachable",
                "message": (
                    f"QGIS Puppeteer Hub ({gateway.url}) に接続できません。QGIS が起動し、"
                    "QGIS Puppet プラグインが有効になっているか確認してください。"
                ),
                "details": {
                    "type": type(exc).__name__,
                    "reason": str(exc),
                    "url": gateway.url,
                },
            }
        }
    )


async def _get_client_or_error(
    gateway: GatewayContext,
) -> tuple[AutomationClient | None, str | None]:
    """Hub 接続を試み、成功なら (client, None)、失敗なら (None, 整形済み JSON)。

    接続エラーのみ catch：`RequestError` などの business error は上位で扱う。

    - `ConnectionRefusedError` / `OSError`：TCP 接続不能（WinError 1225 等）
    - `asyncio.TimeoutError` / `TimeoutError`：ハンドシェイクがタイムアウト
      （`AutomationClient.connect` で ConnectionRefusedError に変換済みだが、
      二重に安全側へ倒して catch する）
    """
    try:
        client = await gateway.get_client()
    except (
        ConnectionRefusedError,
        OSError,
        asyncio.TimeoutError,
        TimeoutError,
    ) as e:  # noqa: BLE001 - intentional
        logger.warning("Hub unreachable at %s: %s", gateway.url, e)
        return None, _format_unreachable(gateway, e)
    return client, None


# コネクション死活検知：これらの例外が出たら cached client は再利用不可。
# `NotConnectedError`: client 側 reader が切断検知済みで Future を fail させた
# `ConnectionClosed*`: websockets 層で close frame または ABORT を受けた
# `OSError`: Windows の WinError 10053 / 10054 等、TCP 層での強制切断
# `asyncio.TimeoutError` / `TimeoutError`: worker が応答不能（過去のハンドラ
#   クラッシュで無応答になったケース等）。client 自体の WebSocket は生きて
#   いる可能性もあるが、stale 扱いで一度リセットして引き直す方が復旧が速い。
_STALE_CONNECTION_EXC = (
    NotConnectedError,
    websockets.exceptions.ConnectionClosed,
    OSError,
    asyncio.TimeoutError,
    TimeoutError,
)


async def _call(ctx: Context, command: str, params: dict[str, Any], *, instance: str | None) -> str:
    """AutomationClient.call(...) を呼び、結果を文字列で返す。

    QGIS/Worker の再起動を挟むと、cached client が stale な WebSocket を
    抱えたまま残るため 1 回目の call が WinError 10053 等で落ちることがある。
    その場合は client をリセットして 1 度だけ再接続→リトライする。

    接続失敗は `hub_unreachable` JSON、`RequestError` は business error JSON。
    どちらも例外を上に投げない（Claude 側のエラー表示を冗長にしないため）。
    """
    gateway: GatewayContext = ctx.request_context.lifespan_context
    target = _resolve_target(gateway, instance)

    for attempt in (0, 1):
        client, err = await _get_client_or_error(gateway)
        if err is not None:
            return err
        assert client is not None
        try:
            result = await client.call(command, params, instance=target)
        except RequestError as e:
            return _format_result(
                {
                    "error": {
                        "code": e.code.value if e.code is not None else None,
                        "message": str(e),
                        "details": e.details,
                    }
                }
            )
        except _STALE_CONNECTION_EXC as e:
            # 初回失敗なら stale client を捨てて 1 回だけ再試行
            if attempt == 0:
                logger.warning(
                    "Stale connection detected (%s); resetting and retrying",
                    type(e).__name__,
                )
                await gateway.reset_client()
                continue
            # 再試行後も失敗：hub_unreachable として降りる
            return _format_unreachable(gateway, e)
        return _format_result(result)

    # ループが break/return なしで抜けることはないが型チェック用
    return _format_unreachable(gateway, RuntimeError("unreachable"))


# ============================================================
# McpGateway ファサード
# ============================================================


def build_gateway(
    *,
    name: str = MCP_SERVER_NAME,
    lifespan: Any | None = None,
) -> MCPServer:
    """`MCPServer` インスタンスを構築し、全 MCP ツールを登録する。

    `lifespan` を渡さない場合は `_default_lifespan` が使われ、
    環境変数経由で AutomationClient に接続する。テスト時は
    外部から自作の lifespan を注入して Fake AutomationClient を差し替える。
    """
    # mcp 2.x で位置引数の並びが変わった（name, title, description, instructions, ...）。
    # 取り違えないよう、すべてキーワード引数で渡す。
    mcp = MCPServer(
        name=name,
        instructions=(
            "QGIS automation gateway. "
            "Use qgis_list_instances / qgis_use_instance to select a target "
            "when multiple QGIS processes are running."
        ),
        lifespan=lifespan or _default_lifespan,
    )

    _register_tools(mcp)
    return mcp


# ============================================================
# ツール登録
# ============================================================


def _register_tools(mcp: MCPServer) -> None:
    """ADR-0001 §5 の 14 ツール + instance 選択ツールを MCP に公開する。

    - QGIS ツール 14 種に `instance: str | None = None` を追加
    - sticky 管理用の `qgis_list_instances` / `qgis_use_instance` を新設
    """

    # ------------------------------------------------------------
    # Instance 管理
    # ------------------------------------------------------------

    @mcp.tool()
    async def qgis_list_instances(ctx: Context) -> str:
        """接続中の QGIS インスタンス一覧を返す。"""
        gateway: GatewayContext = ctx.request_context.lifespan_context
        client, err = await _get_client_or_error(gateway)
        if err is not None:
            return err
        assert client is not None
        instances = await client.list_instances()
        return _format_result([_instance_info_to_dict(i) for i in instances])

    @mcp.tool()
    async def qgis_use_instance(ctx: Context, selector: str) -> str:
        """以後のツール呼び出しで使う instance を sticky に設定する。

        selector は ADR-0005 D6 の解決順（launch_token > @label > label >
        instance_id > project basename）で Hub 側が照会する。

        ADR-0005 D3: sticky には解決後の instance_id ではなく **安定キー**
        （launch_token > label、無ければ instance_id）を保持する。各 dispatch
        で Hub が再解決するため、worker を再起動しても「現在 live なその
        ロール」へ自動追従する（instance_id 凍結による再起動失効を回避）。
        """
        gateway: GatewayContext = ctx.request_context.lifespan_context
        client, err = await _get_client_or_error(gateway)
        if err is not None:
            return err
        assert client is not None
        instances = await client.list_instances()
        resolved = _resolve_selector(selector, instances)
        if resolved is None:
            return _format_result(
                {
                    "error": {
                        "code": "instance_not_found",
                        "message": f"No instance matches {selector!r}",
                        "candidates": [_instance_info_to_dict(i) for i in instances],
                    }
                }
            )
        gateway.current_instance = _stable_sticky_selector(resolved)
        return _format_result(
            {
                "ok": True,
                "current": _instance_info_to_dict(resolved),
                "sticky_selector": gateway.current_instance,
            }
        )

    # ------------------------------------------------------------
    # Layer Tools
    # ------------------------------------------------------------

    @mcp.tool()
    async def qgis_list_layers(ctx: Context, instance: str | None = None) -> str:
        """QGIS プロジェクトのレイヤ一覧を返す。"""
        return await _call(ctx, "qgis_list_layers", {}, instance=instance)

    @mcp.tool()
    async def qgis_get_layer_info(
        ctx: Context, layer_name: str, instance: str | None = None
    ) -> str:
        """指定レイヤのメタデータを返す。"""
        return await _call(
            ctx,
            "qgis_get_layer_info",
            {"layer_name": layer_name},
            instance=instance,
        )

    @mcp.tool()
    async def qgis_select_features(
        ctx: Context,
        layer_name: str,
        expression: str,
        instance: str | None = None,
    ) -> str:
        """式に一致するフィーチャを選択する。"""
        return await _call(
            ctx,
            "qgis_select_features",
            {"layer_name": layer_name, "expression": expression},
            instance=instance,
        )

    @mcp.tool()
    async def qgis_get_selected_features(
        ctx: Context,
        layer_name: str,
        limit: int = 100,
        instance: str | None = None,
    ) -> str:
        """選択中フィーチャの属性を最大 limit 件返す。"""
        return await _call(
            ctx,
            "qgis_get_selected_features",
            {"layer_name": layer_name, "limit": limit},
            instance=instance,
        )

    # ------------------------------------------------------------
    # Python Execution
    # ------------------------------------------------------------

    @mcp.tool()
    async def qgis_execute_python(ctx: Context, code: str, instance: str | None = None) -> str:
        """ホワイトリストで許可された Python コードを実行する。"""
        return await _call(ctx, "qgis_execute_python", {"code": code}, instance=instance)

    @mcp.tool()
    async def qgis_execute_with_permission(
        ctx: Context,
        code: str,
        permission: str,
        instance: str | None = None,
    ) -> str:
        """ユーザー許可付きで Python コードを実行する。"""
        return await _call(
            ctx,
            "qgis_execute_with_permission",
            {"code": code, "permission": permission},
            instance=instance,
        )

    @mcp.tool()
    async def qgis_get_whitelist(ctx: Context, instance: str | None = None) -> str:
        """現在のホワイトリスト内容を返す。"""
        return await _call(ctx, "qgis_get_whitelist", {}, instance=instance)

    @mcp.tool()
    async def qgis_clear_session_permissions(ctx: Context, instance: str | None = None) -> str:
        """セッション中に付与された実行許可をクリアする。"""
        return await _call(ctx, "qgis_clear_session_permissions", {}, instance=instance)

    # ------------------------------------------------------------
    # Screenshot & Canvas
    # ------------------------------------------------------------

    @mcp.tool()
    async def qgis_screenshot(
        ctx: Context,
        output_path: str | None = None,
        width: int | None = None,
        height: int | None = None,
        instance: str | None = None,
    ) -> str:
        """QGIS キャンバスのスクリーンショットを撮る。"""
        params: dict[str, Any] = {}
        if output_path is not None:
            params["output_path"] = output_path
        if width is not None:
            params["width"] = width
        if height is not None:
            params["height"] = height
        return await _call(ctx, "qgis_screenshot", params, instance=instance)

    @mcp.tool()
    async def qgis_get_canvas_extent(ctx: Context, instance: str | None = None) -> str:
        """現在のキャンバス範囲を返す。"""
        return await _call(ctx, "qgis_get_canvas_extent", {}, instance=instance)

    @mcp.tool()
    async def qgis_set_canvas_extent(
        ctx: Context,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
        instance: str | None = None,
    ) -> str:
        """キャンバス範囲を指定する。"""
        return await _call(
            ctx,
            "qgis_set_canvas_extent",
            {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax},
            instance=instance,
        )

    # ------------------------------------------------------------
    # UI Tools
    # ------------------------------------------------------------

    @mcp.tool()
    async def qgis_snapshot_ui(
        ctx: Context,
        max_depth: int = 8,
        include_invisible: bool = False,
        include_main_window: bool = False,
        instance: str | None = None,
    ) -> str:
        """UI ウィジェットツリーの JSON スナップショットを返す。"""
        return await _call(
            ctx,
            "qgis_snapshot_ui",
            {
                "max_depth": max_depth,
                "include_invisible": include_invisible,
                "include_main_window": include_main_window,
            },
            instance=instance,
        )

    @mcp.tool()
    async def qgis_click_widget(
        ctx: Context,
        selector: dict[str, Any],
        instance: str | None = None,
    ) -> str:
        """UI ウィジェットをクリックする。"""
        return await _call(
            ctx,
            "qgis_click_widget",
            {"selector": selector},
            instance=instance,
        )

    @mcp.tool()
    async def qgis_set_widget_value(
        ctx: Context,
        selector: dict[str, Any],
        value: Any,
        instance: str | None = None,
    ) -> str:
        """UI ウィジェットに値を設定する。"""
        return await _call(
            ctx,
            "qgis_set_widget_value",
            {"selector": selector, "value": value},
            instance=instance,
        )


# ============================================================
# selector 解決（クライアント側簡易版）
# ============================================================


def _instance_info_to_dict(info: Any) -> dict[str, Any]:
    """`InstanceInfo` dataclass を dict に変換（JSON シリアライズ用）。"""
    return {
        "instance_id": info.instance_id,
        "label": info.label,
        "pid": info.pid,
        "project": info.project,
    }


def _resolve_selector(selector: str, instances: list[Any]) -> Any | None:
    """selector → InstanceInfo（曖昧・不在は None）。

    ADR-0005 H-1: 解決順は Hub と共有する純関数 ``select_by_selector`` に
    一本化（client 側 mirror の drift を排除）。1 件一致のみ採用。
    """
    matched = select_by_selector(selector, instances)
    return matched[0] if len(matched) == 1 else None


def _stable_sticky_selector(info: Any) -> str:
    """ADR-0005 D3: sticky に保持する安定キーを選ぶ。

    優先度: ``launch_token``（決定的・再起動跨ぎで helper が再注入）>
    **明示指定された** ``label``（人間ロール・SUPERSEDE で再起動跨ぎ追従）>
    ``instance_id``（最後の手段。auto-label は再起動で別値になり sticky が
    roam するため使わない＝ADR-0005 D3 の「contract が無ければ凍結退避」）。
    """
    token = getattr(info, "launch_token", None)
    if token:
        return str(token)
    label = getattr(info, "label", None)
    if label and getattr(info, "label_explicit", False):
        return str(label)
    return str(info.instance_id)


# ============================================================
# stdio エントリポイント
# ============================================================


def main() -> int:
    """`python -m qgis_puppeteer.gateways.mcp` から呼ばれる。

    MCPServer を stdio で run する。Claude Desktop がこのプロセスを spawn する。
    """
    logging.basicConfig(
        level=os.environ.get("QPUPPETEER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    gateway = build_gateway()
    gateway.run()  # デフォルト transport=stdio
    return 0


if __name__ == "__main__":
    sys.exit(main())
