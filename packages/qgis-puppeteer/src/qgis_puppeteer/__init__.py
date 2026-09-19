"""QGIS Puppeteer layer.

QGIS 自動化基盤：Hub / Worker / AutomationClient / McpGateway を提供する。
ADR-0001 (docs/architecture/0001-qgis-puppeteer-architecture.md) を参照。

## QGIS プラグインとしての顔

QGIS プラグインのフォルダ名は `qgis_puppeteer` で、中身はこのパッケージの写し
+ プラグイン固有のファイル（リポジトリの `plugins/qgis_puppeteer/`）。QGIS は
フォルダ名を import するので、プラグインの入口 `classFactory(iface)` はここに
ある。実装は `qgis_plugin/` サブパッケージで、プラグインフォルダにだけ存在する
（このライブラリ側・wheel には無い）。

QGIS の Python には websockets が無いことが多い。websockets を使うのは
`client`（と MCP gateway）だけなので、`client` の公開名は最初に参照された
ときに import する（下の `__getattr__`）。プラグインの読み込みは websockets
無しで通る。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qgis_puppeteer.hub_spawn import (
    DEFAULT_HUB_HOST,
    DEFAULT_HUB_PORT,
    EnsureOutcome,
    HubStartupError,
    SpawnOutcome,
    default_hub_command,
    ensure_hub_reachable,
    tcp_probe,
    try_spawn_hub,
)
from qgis_puppeteer.hub_state import (
    GRACE_SECONDS,
    IDLE_SHUTDOWN_DELAY_SECONDS,
    HubState,
    RegisterOutcome,
    ResolveOutcome,
    WorkerEntry,
    generate_auto_label,
    generate_instance_id,
    validate_label,
    validate_origin,
)
from qgis_puppeteer.protocol import (
    PROTOCOL_VERSION,
    Error,
    ErrorCode,
    InstanceInfo,
    ListInstancesRequest,
    ListInstancesResponse,
    Message,
    MessageType,
    RegisterAck,
    RegisterRequest,
    Request,
    Response,
    Role,
    decode_message,
    encode_message,
)
from qgis_puppeteer.qgis_env import find_qgis_python_launcher
from qgis_puppeteer.worker_state import (
    Handler,
    WorkerConfig,
    WorkerRegisterError,
    WorkerState,
)

if TYPE_CHECKING:
    from qgis_puppeteer.client import (
        AutomationClient,
        AutomationClientError,
        NotConnectedError,
        RegisterError,
        RequestError,
    )

# client の公開名。websockets を要するので、参照されたときに import する。
_CLIENT_EXPORTS = frozenset(
    {
        "AutomationClient",
        "AutomationClientError",
        "NotConnectedError",
        "RegisterError",
        "RequestError",
    }
)


def __getattr__(name: str) -> Any:
    if name in _CLIENT_EXPORTS:
        from qgis_puppeteer import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def classFactory(iface: Any) -> Any:
    """QGIS のプラグインマネージャから呼ばれるエントリ。

    :param iface: QGIS インターフェース（`QgsInterface`）
    :return: `QgisPuppeteerPlugin` インスタンス
    """
    # 遅延 import：PyQt5 / QGIS のロードを classFactory の呼び出しまで遅らせる
    # （QGIS の外から `import qgis_puppeteer` したときに Qt を要求しない）。
    try:
        from qgis_puppeteer.qgis_plugin.plugin import QgisPuppeteerPlugin
    except ModuleNotFoundError as exc:
        if exc.name != "qgis_puppeteer.qgis_plugin":
            raise
        raise ImportError(_shadowed_message()) from exc
    return QgisPuppeteerPlugin(iface)


def _shadowed_message() -> str:
    """プラグインではなく pip で入れた `qgis_puppeteer` が読まれたときの説明。

    `qgis_plugin/` はプラグインフォルダにしか無い。それが無いということは、QGIS が
    プラグインフォルダを見つけたのに、`import qgis_puppeteer` が別のコピーを
    返した、ということ。QGIS はプラグインフォルダを sys.path の先頭に足して
    から import するので、普通はこうならない。なるのは、QGIS がプラグインを
    読む前に誰か（PYQGIS_STARTUP のスクリプト、先に読まれるプラグイン）が
    ホストアプリの vendor や QGIS の Python に入ったコピーを import していた
    場合（実 QGIS で確認済み）。黙って別物で動く前に、場所を名指しして止める。
    """
    from pathlib import Path

    here = Path(__file__).resolve().parent
    return (
        f"QGIS Puppeteer: `import qgis_puppeteer` gave {here}, a pip-installed "
        "library copy, not the QGIS plugin. Something imported that copy before "
        "QGIS loaded the plugin (a PYQGIS_STARTUP script or an earlier plugin, "
        "with a vendor directory or QGIS's own site-packages on sys.path), so it "
        "hides the plugin folder. Remove that copy: the plugin already contains "
        "the whole library. pytest and the MCP gateway should get qgis-puppeteer "
        "from their own environment, not from QGIS's sys.path."
    )


# McpGateway は `from qgis_puppeteer.gateways.mcp import build_gateway`
# で直接利用してもらう（`python -m qgis_puppeteer.gateways.mcp` で走らせる
# 際、ここでトップレベル import すると runpy の二重ロードで RuntimeWarning が
# 出るため、トップレベル __init__.py からは export しない）。

__all__ = [
    # client（AutomationClient）
    "AutomationClient",
    "AutomationClientError",
    "NotConnectedError",
    "RegisterError",
    "RequestError",
    # hub_spawn（Hub 起動競合回避）
    "DEFAULT_HUB_HOST",
    "DEFAULT_HUB_PORT",
    "EnsureOutcome",
    "HubStartupError",
    "SpawnOutcome",
    "default_hub_command",
    "ensure_hub_reachable",
    "tcp_probe",
    "try_spawn_hub",
    # hub_state（Hub 純ロジック）
    "GRACE_SECONDS",
    "HubState",
    "IDLE_SHUTDOWN_DELAY_SECONDS",
    "PROTOCOL_VERSION",
    "RegisterOutcome",
    "ResolveOutcome",
    "WorkerEntry",
    "generate_auto_label",
    "generate_instance_id",
    "validate_label",
    "validate_origin",
    # qgis_env（QGIS 環境探索）
    "find_qgis_python_launcher",
    # protocol
    "Error",
    "ErrorCode",
    "InstanceInfo",
    "ListInstancesRequest",
    "ListInstancesResponse",
    "Message",
    "MessageType",
    "RegisterAck",
    "RegisterRequest",
    "Request",
    "Response",
    "Role",
    "decode_message",
    "encode_message",
    # worker_state（Worker 純ロジック）
    "Handler",
    "WorkerConfig",
    "WorkerRegisterError",
    "WorkerState",
]
