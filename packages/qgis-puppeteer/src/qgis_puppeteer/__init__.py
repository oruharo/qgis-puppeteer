"""QGIS Puppeteer layer.

QGIS 自動化基盤：Hub / Worker / AutomationClient / McpGateway を提供する。
ADR-0001 (docs/adr/0013-qgis-puppeteer-architecture.md) を参照。
"""

from qgis_puppeteer.client import (
    AutomationClient,
    AutomationClientError,
    NotConnectedError,
    RegisterError,
    RequestError,
)
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
