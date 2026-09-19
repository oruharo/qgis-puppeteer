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
- 失敗は例外ではなく `isError` の tool error として返す（本文は読める JSON のまま）

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
import functools
import importlib.metadata
import json
import logging
import logging.handlers
import os
import sys
import tempfile
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

import websockets.exceptions

from qgis_puppeteer.client import (
    AutomationClient,
    AutomationClientError,
    NotConnectedError,
    RequestError,
)
from qgis_puppeteer.hub_state import select_by_selector
from qgis_puppeteer.protocol import Role

try:
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.types import CallToolResult, TextContent, ToolAnnotations
    from pydantic import Field
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
MCP_SERVER_TITLE: str = "QGIS Puppeteer"
MCP_SERVER_URL: str = "https://github.com/oruharo/qgis-puppeteer"


def _server_version() -> str:
    """serverInfo に載せる版。2026-07-28 では各結果の `_meta` に入る。

    パッケージ版だけでは「どのビルドが応答しているか」を区別できない（同じ
    0.1.0 のまま数十コミット進む）ので、git インストールなら commit を
    ``0.1.0+g<sha7>`` の形で添える。Claude Code は接続時に serverInfo を
    ログに残すので、そこで照合できる。
    """
    version, source = _install_source()
    if source.startswith("commit "):
        return f"{version}+g{source[7:14]}"
    return version


def _install_source() -> tuple[str, str]:
    """(パッケージ版, インストール元の説明) を返す。

    pip / uv が書く ``direct_url.json`` から、git なら commit、ローカルパスなら
    そのパスを拾う。無ければ PyPI 等からの通常インストール。
    """
    try:
        dist = importlib.metadata.distribution("qgis-puppeteer")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - 未インストール実行
        return "", "not installed (running from source)"
    version = dist.version
    raw = dist.read_text("direct_url.json")
    if not raw:
        return version, "installed from an index"
    try:
        info = json.loads(raw)
    except ValueError:  # pragma: no cover - 壊れた metadata
        return version, "unknown"
    vcs = info.get("vcs_info") or {}
    if vcs.get("commit_id"):
        return version, f"commit {vcs['commit_id']} ({info.get('url', '?')})"
    return version, f"path {info.get('url', '?')}"


# ============================================================
# ツール注釈（MCP の hint。保証ではない）
# ============================================================
# `read_only_hint` が立ったツールは、クライアント側で並列に投げられる。
# QGIS の状態を書き換えるか、任意のコード・UI 操作に化けうるかで分ける。

_QUERY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
"""照会のみ。QGIS にも gateway にも副作用がない。"""

_MUTATE_SAFE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
"""状態は変えるが、同じ引数なら同じ状態に落ち着き、データを壊さない。"""

_MUTATE_ARBITRARY = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)
"""任意の Python 実行・UI クリックに化けうる。何が起きるかは呼び出し内容次第。"""

_MUTATE_WIDGET_VALUE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)
"""値の設定自体は冪等だが、signal 経由で任意の処理が走りうる。"""

_MUTATE_STANDING_RULE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
"""以後のダイアログを自動で操作する規則を登録する。登録は冪等だが、効果は将来に及ぶ。"""


# ============================================================
# ツール引数（モデルが読む説明をスキーマに載せる）
# ============================================================

InstanceArg = Annotated[
    str | None,
    Field(
        description=(
            "対象の QGIS インスタンス（launch_token / @label / label / instance_id / "
            "プロジェクト名）。省略時は qgis_use_instance で選んだインスタンス。"
        )
    ),
]

SelectorArg = Annotated[
    dict[str, Any],
    Field(
        description=(
            "対象ウィジェットの指定。照合キーは object_name（最も確実）/ text / class / "
            "title / label / placeholder / role / text_contains / text_re / attr。"
            "絞り込みは scope（既定 'modal'、ほかに 'active_window' / 'any'）/ "
            "root_object_name / index。qgis_snapshot_ui の出力から組み立てる。"
        )
    ),
]

PermissionArg = Annotated[
    Literal["once", "session", "cancel"],
    Field(
        description=(
            "ユーザーの判断。once=この 1 回だけ、session=この QGIS が動いている間、"
            "cancel=実行しない。ホワイトリストへ永続追加する always は MCP からは"
            "選べない（永続的に許可を広げるのは人間の操作に限る）。"
        )
    ),
]

DialogPredicateArg = Annotated[
    dict[str, Any],
    Field(
        description=(
            "対象モーダルの条件。title / class / object_name を主に使い、書いたキーを "
            "すべて満たすものに一致する。空の {} はすべての modal に一致する。"
        )
    ),
]


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


def _error_result(payload: Any) -> CallToolResult:
    """失敗を MCP の tool error（`isError`）として返す。

    本文は成功時と同じ `_format_result` の JSON テキスト。例外を投げると
    クライアント側で汎用メッセージに丸められてしまうので、読める本文は
    そのまま渡しつつ「呼び出しは失敗した」ことだけを立てる。
    """
    return CallToolResult(
        content=[TextContent(type="text", text=_format_result(payload))], is_error=True
    )


def _format_unreachable(gateway: GatewayContext, exc: BaseException) -> CallToolResult:
    """Hub に届かない旨を一貫した形でユーザーに返す。

    Gateway 起動時点では QGIS が立ち上がっていないのが普通で、tool 呼び出し
    時に初めて接続を試みる。接続失敗は珍しくないので、例外は投げずに
    `isError` + 読める JSON 本文に落とす。
    """
    return _error_result(
        {
            "error": {
                "code": "hub_unreachable",
                "message": (
                    f"QGIS Puppeteer Hub ({gateway.url}) に接続できません。QGIS が起動し、"
                    "QGIS Puppeteer プラグインが有効になっているか確認してください。"
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
) -> tuple[AutomationClient | None, CallToolResult | None]:
    """Hub 接続を試み、成功なら (client, None)、失敗なら (None, tool error)。

    接続エラーのみ catch：`RequestError` などの business error は上位で扱う。

    - `ConnectionRefusedError` / `OSError`：TCP 接続不能（WinError 1225 等）
    - `asyncio.TimeoutError` / `TimeoutError`：ハンドシェイクがタイムアウト
      （`AutomationClient.connect` で ConnectionRefusedError に変換済みだが、
      二重に安全側へ倒して catch する）
    """
    client, exc = await _connect_or_exc(gateway)
    if exc is not None:
        return None, _format_unreachable(gateway, exc)
    return client, None


async def _connect_or_exc(
    gateway: GatewayContext,
) -> tuple[AutomationClient | None, BaseException | None]:
    """Hub 接続を試み、成功なら (client, None)、接続失敗なら (None, exc)。

    TCP / ハンドシェイク / register のどこで失敗しても「Hub が使えない」に
    畳む。register 失敗（`RegisterError`）は再接続で新しい Hub に弾かれた
    ケースで、ここで拾わないと tool の外へ抜けて本文の無いエラーになる。
    """
    try:
        client = await gateway.get_client()
    except (
        ConnectionRefusedError,
        OSError,
        asyncio.TimeoutError,
        TimeoutError,
        AutomationClientError,
        websockets.exceptions.WebSocketException,
    ) as e:  # noqa: BLE001 - intentional
        logger.warning("Hub unreachable at %s: %s: %s", gateway.url, type(e).__name__, e)
        return None, e
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


_T = TypeVar("_T")


async def _with_client(
    gateway: GatewayContext, op: Callable[[AutomationClient], Awaitable[_T]]
) -> _T | CallToolResult:
    """Hub 接続を取って ``op`` を走らせる。stale な接続なら 1 度だけ張り直す。

    QGIS/Hub の再起動を挟むと、cached client が死んだ WebSocket を抱えたまま
    残るため 1 回目の呼び出しが ConnectionClosed / WinError 10053 等で落ちる。
    その場合は client をリセットして再接続→リトライする。worker コマンドだけ
    でなく `qgis_list_instances` / `qgis_use_instance` / `qgis_wait_ready` も
    同じ経路を通す — Hub を入れ替えた直後に「list は通るのに use_instance は
    落ちる」のは、どのツールが先に stale socket を踏むかの運だった。

    接続失敗は `hub_unreachable` の tool error。``op`` が投げる business error
    （RequestError 等）は ``op`` 側で結果に変換すること。``op`` から出た
    TimeoutError は stale 扱いになるので、意味のある timeout は ``op`` 内で捕る。
    """
    for attempt in (0, 1):
        client, err = await _get_client_or_error(gateway)
        if err is not None:
            return err
        assert client is not None
        try:
            return await op(client)
        except _STALE_CONNECTION_EXC as e:
            if attempt == 0:
                logger.warning(
                    "Stale connection detected (%s); resetting and retrying",
                    type(e).__name__,
                )
                await gateway.reset_client()
                continue
            return _format_unreachable(gateway, e)
        except Exception as e:  # noqa: BLE001 - 最後の砦
            # ここに来る例外は business error ではない（それは op が結果に変換
            # している）。想定外の型でも接続まわりの可能性が高いので、1 回目は
            # 同じくリセットして張り直す。2 回目も落ちたら、SDK に任せて本文の
            # 無い "Error executing tool" にするのではなく、型と traceback を
            # 載せた JSON で返す — 呼び出し側から gateway の stderr は見えない。
            if attempt == 0:
                logger.exception(
                    "Unexpected %s while talking to the Hub; resetting the connection "
                    "and retrying once",
                    type(e).__name__,
                )
                await gateway.reset_client()
                continue
            return _internal_error_result(e)
    raise AssertionError("unreachable")  # pragma: no cover


def _internal_error_result(exc: BaseException) -> CallToolResult:
    """gateway 内部の想定外例外を、型と traceback 付きの tool error にする。"""
    logger.exception("Gateway internal error: %s", exc)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return _error_result(
        {
            "error": {
                "code": "gateway_internal_error",
                "message": f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                "details": {
                    "type": type(exc).__name__,
                    # 末尾だけ。呼び出し側が原因箇所を特定できれば十分
                    "traceback": tb[-2000:],
                    "log": str(_gateway_log_path()),
                },
            }
        }
    )


def _guard_tool(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """tool 関数の最後の砦。

    `_with_client` の外（引数解決や lifespan 取得）で例外が出ると、MCP SDK は
    それを「クラッシュ」として本文の無い ``Error executing tool <name>`` にし、
    型もメッセージも呼び出し側には渡さない。tool 本体をここで包んで、どこで
    出た例外でも `gateway_internal_error` の JSON に落とす。
    """

    @functools.wraps(fn)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 最後の砦
            return _internal_error_result(e)

    return guarded


def _gateway_log_path() -> Path:
    """gateway のログファイル。呼び出し側からは stderr が見えないので、ここに残す。

    `QPUPPETEER_GATEWAY_LOG` で上書き。既定は ``<TEMP>/qgis_puppeteer/gateway.log``。
    """
    override = os.environ.get("QPUPPETEER_GATEWAY_LOG")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "qgis_puppeteer" / "gateway.log"


async def _call(
    ctx: Context, command: str, params: dict[str, Any], *, instance: str | None
) -> str | CallToolResult:
    """AutomationClient.call(...) を呼び、結果を文字列で返す。

    `RequestError` は worker 側の business error で、例外は投げず `isError` を
    立てた tool error として返す。接続まわりは `_with_client` 参照。
    """
    gateway: GatewayContext = ctx.request_context.lifespan_context
    target = _resolve_target(gateway, instance)

    async def op(client: AutomationClient) -> str | CallToolResult:
        try:
            result = await client.call(command, params, instance=target)
        except RequestError as e:
            return _error_result(
                {
                    "error": {
                        "code": e.code.value if e.code is not None else None,
                        "message": str(e),
                        "details": e.details,
                    }
                }
            )
        return _format_result(result)

    return await _with_client(gateway, op)


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
        title=MCP_SERVER_TITLE,
        version=_server_version(),
        website_url=MCP_SERVER_URL,
        description="Control a running QGIS from Claude via the QGIS Puppeteer plugin.",
        instructions=(
            "QGIS automation gateway.\n"
            "- Several QGIS processes can be connected: qgis_list_instances lists them, "
            "qgis_use_instance pins one for later calls, and every tool also takes an "
            "explicit `instance`.\n"
            "- Each listed instance has a state: active, or unresponsive when QGIS is alive "
            "but its GUI thread is busy (loading a project, running a long script) and has "
            "not answered the Hub's heartbeat for 20s. An unresponsive instance still "
            "accepts calls; they run once the GUI thread is free. If a call is slow, check "
            "last_seen_ago before assuming the selector is wrong.\n"
            "- Right after QGIS starts, call qgis_wait_ready before anything else: it "
            "returns once the instance answers a round trip, not merely once it is listed.\n"
            "- code=instance_disconnected means the instance was registered but its "
            "connection dropped; grace_expires_in says how long the Hub still waits for it "
            "to reconnect (QGIS auto-reconnects when it can).\n"
            "- UI tools take a selector dict; build it from qgis_snapshot_ui output.\n"
            "- qgis_execute_python runs code inside QGIS and returns whatever that code "
            "assigns to `_result`.\n"
            "- A failed call comes back with isError and a JSON body "
            '{"error": {"code", "message", ...}}; code=hub_unreachable means QGIS, or the '
            "QGIS Puppeteer plugin inside it, is not running yet."
        ),
        lifespan=lifespan or _default_lifespan,
    )

    _register_tools(mcp)
    return mcp


# ============================================================
# ツール登録
# ============================================================


def _register_tools(mcp: MCPServer) -> None:  # noqa: C901 - ツール定義の列挙
    """ADR-0001 §5 の 14 ツール + instance 選択ツールを MCP に公開する。

    - QGIS ツール 14 種に `instance` 引数（`InstanceArg`）を追加
    - sticky 管理用の `qgis_list_instances` / `qgis_use_instance` を新設
    - ADR-0002 のダイアログ自動処理 4 種（worker 側 handler の公開）

    各ツールには表示名と注釈を付ける。結果は `_format_result` の JSON テキスト
    1 本に揃える（`structured_output=False`）：`-> str` のまま SDK に任せると
    `{"result": "<同じ JSON 文字列>"}` という outputSchema と structuredContent が
    自動生成され、同じ内容が本文と二重に流れてしまう。
    """
    # 以下の `@mcp.tool(...)` を全部 `_guard_tool` 経由にする。SDK に例外を
    # 渡さない（本文の無い "Error executing tool" を二度と出さない）ため。
    _sdk_tool = mcp.tool

    def _tool(**kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        decorate = _sdk_tool(**kwargs)

        def register(fn: Callable[..., Any]) -> Any:
            return decorate(_guard_tool(fn))

        return register

    mcp.tool = _tool  # type: ignore[method-assign]

    # ------------------------------------------------------------
    # Instance 管理
    # ------------------------------------------------------------

    @mcp.tool(title="QGIS インスタンス一覧", annotations=_QUERY, structured_output=False)
    async def qgis_list_instances(ctx: Context) -> str | CallToolResult:
        """QGIS インスタンス一覧を返す（切断直後のものも含む）。

        各要素の state は "active" / "unresponsive" / "disconnected"。
        unresponsive は QGIS は生きているが GUI スレッドが塞がっていて（プロジェクト
        読み込み中、長いスクリプト実行中など）Hub のハートビートに 20 秒以上応答して
        いない状態。起動直後のプロジェクト読み込み中は普通にこうなる。呼び出しは
        受け付けられ、GUI スレッドが空き次第処理される。disconnected は接続が切れて
        Hub が再接続を待っている状態で、grace_expires_in 秒後に登録が消える。
        last_seen_ago は最後に応答を確認してからの秒数。
        """
        gateway: GatewayContext = ctx.request_context.lifespan_context

        async def op(client: AutomationClient) -> str | CallToolResult:
            instances = await client.list_instances(include_disconnected=True)
            return _format_result([_instance_info_to_dict(i) for i in instances])

        return await _with_client(gateway, op)

    @mcp.tool(
        title="使用する QGIS インスタンスを選ぶ", annotations=_MUTATE_SAFE, structured_output=False
    )
    async def qgis_use_instance(ctx: Context, selector: str) -> str | CallToolResult:
        """以後のツール呼び出しで使う instance を sticky に設定する。

        selector は launch_token > @label > label > instance_id > プロジェクト名
        の順に解決する。以後 instance を省略した呼び出しは、ここで選んだ
        インスタンスに送られる。該当が無い場合と複数一致した場合は
        instance_not_found、該当はあるが接続が切れて再接続待ちなら
        instance_disconnected（grace_expires_in 付き）を返す。
        """
        # ADR-0005 D6 の解決順を Hub と共有する。ADR-0005 D3: sticky には解決後の
        # instance_id ではなく **安定キー**（launch_token > label、無ければ
        # instance_id）を保持する。各 dispatch で Hub が再解決するため、worker を
        # 再起動しても「現在 live なそのロール」へ自動追従する（instance_id 凍結
        # による再起動失効を回避）。
        gateway: GatewayContext = ctx.request_context.lifespan_context

        async def op(client: AutomationClient) -> str | CallToolResult:
            all_instances = await client.list_instances(include_disconnected=True)
            connected = [i for i in all_instances if i.state != "disconnected"]
            resolved = _resolve_selector(selector, connected)
            if resolved is None:
                disconnected = [i for i in all_instances if i.state == "disconnected"]
                in_grace = select_by_selector(selector, disconnected)
                if in_grace:
                    first = in_grace[0]
                    return _error_result(
                        {
                            "error": {
                                "code": "instance_disconnected",
                                "message": (
                                    f"Instance '{first.label}' matches {selector!r} but its "
                                    f"connection dropped; the Hub keeps it for "
                                    f"{first.grace_expires_in}s more in case it reconnects"
                                ),
                                "instances": [_instance_info_to_dict(i) for i in in_grace],
                            }
                        }
                    )
                return _error_result(
                    {
                        "error": {
                            "code": "instance_not_found",
                            "message": f"No instance matches {selector!r}",
                            "candidates": [_instance_info_to_dict(i) for i in connected],
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

        return await _with_client(gateway, op)

    @mcp.tool(title="QGIS が使えるまで待つ", annotations=_QUERY, structured_output=False)
    async def qgis_wait_ready(
        ctx: Context,
        timeout_s: Annotated[
            float,
            Field(
                description="待つ上限秒。重いプロジェクトの読み込みは数分かかることがある。",
                gt=0,
                le=600,
            ),
        ] = 60.0,
        require_project: Annotated[
            bool,
            Field(
                description=(
                    "true にすると、プロジェクトの読み込みが完了して project が入るまで待つ。"
                    "レイヤを扱うなら true。プロジェクトを開かない QGIS では満たされないので、"
                    "その場合は false のまま。"
                )
            ),
        ] = False,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
        """QGIS インスタンスが呼び出しを処理できる状態になるまで待つ。

        QGIS を起動した直後や、重いプロジェクトを開いた直後に使う。起動直後は
        Hub 自体がまだ listen していないことがあるが、それも timeout_s の内で待つ
        （他のツールと違い hub_unreachable で即座には返らない）。「一覧に載っている」
        だけでは足りない — 登録直後は GUI スレッドが読み込みで塞がっていて、その間の
        呼び出しは待たされる。ここでは state が active で、かつ軽い read-only の往復
        （qgis_get_canvas_extent）が返るまで待つ。

        ready はプロジェクト読み込み完了と同じではない。QGIS は起動直後、読み込みを
        始める前に一瞬 GUI が空くので、実測では ready が 11 秒、レイヤが出そろうのが
        31 秒だった。レイヤを触るなら require_project=true を付ける（読み込み完了後に
        project が届いてから往復を確認する）。

        成功すると instance と待った秒数を返す。timeout_s 以内に使えなければ
        isError と code=not_ready、その時点の一覧（Hub に繋がらなかった場合はその理由）
        を返す。
        """
        gateway: GatewayContext = ctx.request_context.lifespan_context
        target = _resolve_target(gateway, instance)
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + timeout_s

        # Hub がまだ listen していない間はここで待つ。起動直後に「まずこれを呼べ」と
        # 言っている以上、1 回目が hub_unreachable で落ちるのでは説明と合わない。
        while True:
            client, exc = await _connect_or_exc(gateway)
            if exc is None:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _error_result(
                    {
                        "error": {
                            "code": "not_ready",
                            "message": (
                                f"Hub ({gateway.url}) did not start listening within "
                                f"{timeout_s}s; QGIS, or the QGIS Puppeteer plugin inside it, "
                                "is probably not running yet"
                            ),
                            "details": {
                                "hub_unreachable": {
                                    "type": type(exc).__name__,
                                    "reason": str(exc),
                                    "url": gateway.url,
                                }
                            },
                            "instances": [],
                        }
                    }
                )
            await asyncio.sleep(min(1.0, remaining))

        async def op(client: AutomationClient) -> str | CallToolResult:
            remaining = max(0.1, deadline - loop.time())
            try:
                info = await client.wait_for_ready(
                    target, timeout_s=remaining, require_project=require_project
                )
            except TimeoutError:
                snapshot = await client.list_instances(include_disconnected=True)
                return _error_result(
                    {
                        "error": {
                            "code": "not_ready",
                            "message": (
                                f"No usable QGIS instance within {timeout_s}s"
                                + (f" for {target!r}" if target is not None else "")
                                + (" with a project loaded" if require_project else "")
                                + "; see instances for their current state"
                            ),
                            "instances": [_instance_info_to_dict(i) for i in snapshot],
                        }
                    }
                )
            except RequestError as e:
                return _error_result(
                    {
                        "error": {
                            "code": e.code.value if e.code is not None else None,
                            "message": str(e),
                            "details": e.details,
                        }
                    }
                )
            return _format_result(
                {
                    "ok": True,
                    "instance": _instance_info_to_dict(info),
                    "waited_s": round(loop.time() - started, 1),
                }
            )

        return await _with_client(gateway, op)

    # ------------------------------------------------------------
    # Layer Tools
    # ------------------------------------------------------------

    @mcp.tool(title="レイヤ一覧", annotations=_QUERY, structured_output=False)
    async def qgis_list_layers(ctx: Context, instance: InstanceArg = None) -> str | CallToolResult:
        """QGIS プロジェクトのレイヤ一覧を返す。"""
        return await _call(ctx, "qgis_list_layers", {}, instance=instance)

    @mcp.tool(title="レイヤ情報", annotations=_QUERY, structured_output=False)
    async def qgis_get_layer_info(
        ctx: Context, layer_name: str, instance: InstanceArg = None
    ) -> str | CallToolResult:
        """指定レイヤのメタデータを返す。"""
        return await _call(
            ctx,
            "qgis_get_layer_info",
            {"layer_name": layer_name},
            instance=instance,
        )

    @mcp.tool(title="フィーチャを選択", annotations=_MUTATE_SAFE, structured_output=False)
    async def qgis_select_features(
        ctx: Context,
        layer_name: str,
        expression: str,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
        """式に一致するフィーチャを選択する（QGIS の式構文）。

        例: "population" > 1000
        """
        return await _call(
            ctx,
            "qgis_select_features",
            {"layer_name": layer_name, "expression": expression},
            instance=instance,
        )

    @mcp.tool(title="選択中フィーチャを取得", annotations=_QUERY, structured_output=False)
    async def qgis_get_selected_features(
        ctx: Context,
        layer_name: str,
        limit: int = 100,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
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

    @mcp.tool(title="Python を実行", annotations=_MUTATE_ARBITRARY, structured_output=False)
    async def qgis_execute_python(
        ctx: Context, code: str, instance: InstanceArg = None
    ) -> str | CallToolResult:
        """ホワイトリストで許可された Python コードを QGIS 内で実行する。

        値を受け取るには、コード内で `_result` に代入する。最終式の自動評価は
        行わないので、代入しなければ result_set=false で返る。ホワイトリストに
        無いコードは qgis_execute_with_permission を使う。
        """
        return await _call(ctx, "qgis_execute_python", {"code": code}, instance=instance)

    @mcp.tool(
        title="許可付きで Python を実行", annotations=_MUTATE_ARBITRARY, structured_output=False
    )
    async def qgis_execute_with_permission(
        ctx: Context,
        code: str,
        permission: PermissionArg,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
        """ユーザー許可付きで Python コードを実行する。

        permission は "once"（この 1 回だけ）/ "session"（この QGIS が動いている
        間）/ "cancel"（実行しない）。この引数はユーザーの判断を代弁するものなので、
        先に本人へ内容を示して確認すること。値の受け取り方は qgis_execute_python と
        同じで、`_result` に代入する。
        """
        return await _call(
            ctx,
            "qgis_execute_with_permission",
            {"code": code, "permission": permission},
            instance=instance,
        )

    @mcp.tool(title="ホワイトリストを取得", annotations=_QUERY, structured_output=False)
    async def qgis_get_whitelist(
        ctx: Context, instance: InstanceArg = None
    ) -> str | CallToolResult:
        """現在のホワイトリスト内容を返す。"""
        return await _call(ctx, "qgis_get_whitelist", {}, instance=instance)

    @mcp.tool(title="セッション許可をクリア", annotations=_MUTATE_SAFE, structured_output=False)
    async def qgis_clear_session_permissions(
        ctx: Context, instance: InstanceArg = None
    ) -> str | CallToolResult:
        """セッション中に付与された実行許可をクリアする。"""
        return await _call(ctx, "qgis_clear_session_permissions", {}, instance=instance)

    # ------------------------------------------------------------
    # Screenshot & Canvas
    # ------------------------------------------------------------

    @mcp.tool(title="キャンバスを撮影", annotations=_MUTATE_SAFE, structured_output=False)
    async def qgis_screenshot(
        ctx: Context,
        output_path: str | None = None,
        width: int | None = None,
        height: int | None = None,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
        """QGIS キャンバスのスクリーンショットを撮り、保存先のパスを返す。

        output_path を省略すると一時ファイルに保存する。
        """
        params: dict[str, Any] = {}
        if output_path is not None:
            params["output_path"] = output_path
        if width is not None:
            params["width"] = width
        if height is not None:
            params["height"] = height
        return await _call(ctx, "qgis_screenshot", params, instance=instance)

    @mcp.tool(title="キャンバス範囲を取得", annotations=_QUERY, structured_output=False)
    async def qgis_get_canvas_extent(
        ctx: Context, instance: InstanceArg = None
    ) -> str | CallToolResult:
        """現在のキャンバス範囲を返す。"""
        return await _call(ctx, "qgis_get_canvas_extent", {}, instance=instance)

    @mcp.tool(title="キャンバス範囲を設定", annotations=_MUTATE_SAFE, structured_output=False)
    async def qgis_set_canvas_extent(
        ctx: Context,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
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

    @mcp.tool(title="UI スナップショット", annotations=_QUERY, structured_output=False)
    async def qgis_snapshot_ui(
        ctx: Context,
        max_depth: int = 8,
        include_invisible: bool = False,
        include_main_window: bool = False,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
        """UI ウィジェットツリーの JSON スナップショットを返す。

        既定ではモーダルと可視ダイアログのみ。QGIS 本体のツリーは巨大なので、
        include_main_window=true のときだけ含む。selector を組み立てる材料にする。
        """
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

    @mcp.tool(
        title="ウィジェットをクリック", annotations=_MUTATE_ARBITRARY, structured_output=False
    )
    async def qgis_click_widget(
        ctx: Context,
        selector: SelectorArg,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
        """UI ウィジェットをクリックする。

        見つからない・複数一致・無効などは error コードで返る
        （widget_not_found / selector_ambiguous / widget_disabled）。
        """
        return await _call(
            ctx,
            "qgis_click_widget",
            {"selector": selector},
            instance=instance,
        )

    @mcp.tool(
        title="ウィジェットに値を設定", annotations=_MUTATE_WIDGET_VALUE, structured_output=False
    )
    async def qgis_set_widget_value(
        ctx: Context,
        selector: SelectorArg,
        value: Any,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
        """UI ウィジェットに値を設定する。

        value は対象の種類に応じて解釈する（LineEdit は文字列、ComboBox は項目
        テキスト、CheckBox は真偽値など）。失敗は widget_readonly /
        combo_item_not_found などの error コードで返る。
        """
        return await _call(
            ctx,
            "qgis_set_widget_value",
            {"selector": selector, "value": value},
            instance=instance,
        )

    # ------------------------------------------------------------
    # Dialog Handlers（ADR-0002: modal が出たときの自動処理）
    # ------------------------------------------------------------

    @mcp.tool(
        title="ダイアログ自動処理を登録",
        annotations=_MUTATE_STANDING_RULE,
        structured_output=False,
    )
    async def qgis_register_dialog_handler(
        ctx: Context,
        name: str,
        predicate: DialogPredicateArg,
        action: Literal["accept", "reject", "close"],
        once: bool = False,
        instance: InstanceArg = None,
    ) -> str | CallToolResult:
        """モーダルダイアログが出たときの自動処理を登録する。

        以後、predicate に一致する modal が現れるたびに action を適用する。
        同じ name で登録し直すと上書きされる。once=true なら 1 度発火した
        時点で自動的に解除される。
        """
        return await _call(
            ctx,
            "qgis_register_dialog_handler",
            {"name": name, "predicate": predicate, "action": action, "once": once},
            instance=instance,
        )

    @mcp.tool(title="ダイアログ自動処理を解除", annotations=_MUTATE_SAFE, structured_output=False)
    async def qgis_unregister_dialog_handler(
        ctx: Context, name: str, instance: InstanceArg = None
    ) -> str | CallToolResult:
        """登録済みのダイアログ自動処理を name で解除する。"""
        return await _call(ctx, "qgis_unregister_dialog_handler", {"name": name}, instance=instance)

    @mcp.tool(title="ダイアログ自動処理の一覧", annotations=_QUERY, structured_output=False)
    async def qgis_list_dialog_handlers(
        ctx: Context, instance: InstanceArg = None
    ) -> str | CallToolResult:
        """登録済みのダイアログ自動処理を一覧する。"""
        return await _call(ctx, "qgis_list_dialog_handlers", {}, instance=instance)

    @mcp.tool(title="ダイアログ自動処理を全解除", annotations=_MUTATE_SAFE, structured_output=False)
    async def qgis_clear_dialog_handlers(
        ctx: Context, instance: InstanceArg = None
    ) -> str | CallToolResult:
        """登録済みのダイアログ自動処理をすべて解除する（teardown 用）。"""
        return await _call(ctx, "qgis_clear_dialog_handlers", {}, instance=instance)


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
        "state": info.state,
        "last_seen_ago": info.last_seen_ago,
        "grace_expires_in": info.grace_expires_in,
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
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=os.environ.get("QPUPPETEER_LOG_LEVEL", "INFO"),
        format=fmt,
        stream=sys.stderr,
    )
    # stderr は MCP クライアントが飲み込んで呼び出し側からは見えない（Claude Code
    # はイベントだけ jsonl に残し、traceback は捨てる）。同じ内容をファイルにも
    # 書く。SDK が "unexpected exception" として記録する traceback もここに来る。
    log_path = _gateway_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(fmt))
        logging.getLogger().addHandler(fh)
        logger.info("Gateway log file: %s", log_path)
    except OSError:  # pragma: no cover - 書けなくても動作は続ける
        logger.warning("Could not open gateway log file %s", log_path, exc_info=True)
    # どのビルドが応答しているかを 1 行で照合できるように名乗る。
    # gateway が二重に立っていた事故（Desktop の shared pool と .mcp.json）で、
    # ログファイルの有無だけでは版を取り違えた。
    version, source = _install_source()
    logger.info(
        "Gateway qgis-puppeteer %s, %s, code at %s, pid %d, python %s",
        version or "?",
        source,
        Path(__file__).resolve().parent.parent,
        os.getpid(),
        sys.version.split()[0],
    )
    gateway = build_gateway()
    gateway.run()  # デフォルト transport=stdio
    return 0


if __name__ == "__main__":
    sys.exit(main())
