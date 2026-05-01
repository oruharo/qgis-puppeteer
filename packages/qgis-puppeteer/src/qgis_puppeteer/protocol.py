"""QGIS Puppeteer 内部プロトコル定義。

ADR-0001 §3 に基づく JSON over WebSocket プロトコルの型定義。
Hub / Worker / AutomationClient / McpGateway すべてが共有する。

## 設計原則

- 純 dataclass + JSON のみ。Qt 固有型は一切含めない（Qt/asyncio 両実装で共有可）
- `@dataclass(frozen=True)` で不変化（送受信メッセージはイミュータブル）
- Python 3.10+ 組み込み型ヒント（`X | None`, `list[X]`）
- `encode_message()` / `decode_message()` で JSON と dataclass 間を相互変換
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any

# プロトコル互換性管理（ADR-0001 Open Questions 参照）
PROTOCOL_VERSION: int = 1


# ============================================================
# 列挙型
# ============================================================


class Role(str, Enum):
    """Hub への接続時に自己申告する役割。"""

    WORKER = "worker"
    MCP_GATEWAY = "mcp_gateway"
    AUTOMATION_CLIENT = "automation_client"


class MessageType(str, Enum):
    """メッセージ種別（`type` フィールドの値）。"""

    # 登録系
    REGISTER = "register"
    REGISTER_ACK = "register_ack"

    # コマンド呼び出し系
    REQUEST = "request"
    RESPONSE = "response"

    # インスタンス一覧系
    LIST_INSTANCES = "list_instances"
    LIST_INSTANCES_RESPONSE = "list_instances_response"

    # 終了通知
    BYE = "bye"


class ErrorCode(str, Enum):
    """エラーコード（ADR-0001 §3 "エラーコード初期セット"）。"""

    INSTANCE_NOT_FOUND = "instance_not_found"
    INSTANCE_AMBIGUOUS = "instance_ambiguous"
    INSTANCE_TIMEOUT = "instance_timeout"
    PROTOCOL_VERSION_MISMATCH = "protocol_version_mismatch"
    INVALID_COMMAND = "invalid_command"
    WORKER_EXECUTION_ERROR = "worker_execution_error"
    LABEL_CONFLICT = "label_conflict"
    # ADR-0001 §12.6 / §12.7 ハンドラ拡張機構
    RESERVED_NAMESPACE = "reserved_namespace"
    REGISTRATION_DENIED = "registration_denied"
    HANDLER_ALREADY_REGISTERED = "handler_already_registered"
    HANDLER_NOT_VISIBLE = "handler_not_visible"


# ============================================================
# ペイロード型
# ============================================================


@dataclass(frozen=True)
class InstanceInfo:
    """list_instances のレスポンスで返される Worker インスタンス情報。"""

    instance_id: str
    label: str
    pid: int
    project: str | None = None


@dataclass(frozen=True)
class Error:
    """エラーレスポンスの詳細。"""

    code: ErrorCode
    message: str
    # ambiguous の場合の候補、label_conflict の suggested_label など、
    # エラーコード固有の付加情報を格納する
    details: dict[str, Any] = field(default_factory=dict)


# ============================================================
# メッセージ本体
# ============================================================


@dataclass(frozen=True)
class RegisterRequest:
    """Hub への初回 register メッセージ。

    Worker の場合は pid/label/project を必須とし、再接続時は
    previous_instance_id を付けて instance_id 引き継ぎを要求する
    (ADR-0001 §4 "Worker 再接続時の instance_id 引き継ぎ")。
    """

    id: str
    role: Role
    protocol_version: int = PROTOCOL_VERSION
    # Worker 専用フィールド（role が WORKER 以外では None）
    pid: int | None = None
    label: str | None = None
    project: str | None = None
    started_at: str | None = None  # ISO 8601 文字列
    previous_instance_id: str | None = None

    @property
    def type(self) -> MessageType:
        return MessageType.REGISTER


@dataclass(frozen=True)
class RegisterAck:
    """register への応答。

    成功時は instance_id を返す。再接続で前回 id を引き継いだ場合は
    resumed=True。失敗時は error にコードを含める（例：label_conflict）。
    """

    id: str
    ok: bool
    instance_id: str | None = None
    resumed: bool = False
    error: Error | None = None

    @property
    def type(self) -> MessageType:
        return MessageType.REGISTER_ACK


@dataclass(frozen=True)
class Request:
    """McpGateway / AutomationClient → Hub → Worker の呼び出し。

    Hub が instance セレクタを解決し、該当 Worker 接続に同じ id で転送する。
    """

    id: str
    command: str
    params: dict[str, Any] = field(default_factory=dict)
    # selector：label / instance_id / project basename / pid のいずれか
    # （解決順は ADR-0001 §5）。None の場合は Hub が自動選択を試みる
    instance: str | None = None
    # リクエストごとのタイムアウト（未指定なら Hub のデフォルトを使う）
    timeout_ms: int | None = None
    # Hub → Worker 転送時に Hub が付与する発信元 role（ADR-0001 §12.7）。
    # Client → Hub 経路ではクライアントが申告しても Hub が握りつぶして
    # register 時の role で上書きする（クライアントの申告は信用しない）。
    caller_role: Role | None = None

    @property
    def type(self) -> MessageType:
        return MessageType.REQUEST


@dataclass(frozen=True)
class Response:
    """Request への応答（Worker → Hub → 発信元クライアント）。"""

    id: str
    ok: bool
    result: Any = None
    error: Error | None = None

    @property
    def type(self) -> MessageType:
        return MessageType.RESPONSE


@dataclass(frozen=True)
class ListInstancesRequest:
    """インスタンス一覧取得リクエスト。Hub に直接処理される。"""

    id: str

    @property
    def type(self) -> MessageType:
        return MessageType.LIST_INSTANCES


@dataclass(frozen=True)
class ListInstancesResponse:
    """インスタンス一覧レスポンス。"""

    id: str
    instances: list[InstanceInfo] = field(default_factory=list)

    @property
    def type(self) -> MessageType:
        return MessageType.LIST_INSTANCES_RESPONSE


@dataclass(frozen=True)
class Bye:
    """Worker → Hub への明示的終了通知。

    受信した Hub は grace 期間を経ずに即エントリ削除する
    （ADR-0001 §4 "Graceful close"）。
    """

    id: str
    instance_id: str

    @property
    def type(self) -> MessageType:
        return MessageType.BYE


# Union 型：プロトコルで流通する全メッセージ型
Message = (
    RegisterRequest
    | RegisterAck
    | Request
    | Response
    | ListInstancesRequest
    | ListInstancesResponse
    | Bye
)


# ============================================================
# シリアライズ / デシリアライズ
# ============================================================


class ProtocolDecodeError(ValueError):
    """不正な JSON / 未知の type / 必須フィールド欠落などの解読失敗。"""


def encode_message(message: Message) -> str:
    """dataclass メッセージを JSON 文字列に変換する。

    `type` フィールドを dataclass の property から注入し、`Enum` は
    `.value`、ネストした dataclass は dict、`QVariant` など JSON 化不可能な
    Qt 固有型は `None` / プリミティブに coerce する。

    `dataclasses.asdict()` は内部で `copy.deepcopy` にフォールバックするため、
    QVariant を含むハンドラ戻り値で `TypeError: cannot pickle 'QVariant'`
    を踏む。ここでは自前で再帰的に辞書化して安全側に倒す。
    """
    payload = _to_jsonable(message)
    assert isinstance(payload, dict)
    payload["type"] = message.type.value
    return json.dumps(payload, ensure_ascii=False)


def _to_jsonable(obj: Any) -> Any:
    """dataclass ツリーを JSON 化可能な素の値へ再帰展開する。

    `dataclasses.asdict` の代替。QVariant など deepcopy できない型に
    ぶつかっても落ちないよう、未知型は `_coerce_leaf` 経由で素値に落とす。
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    return _coerce_leaf(obj)


def _coerce_leaf(value: Any) -> Any:
    """JSON プリミティブでない葉をプリミティブに coerce する。

    - QVariant（Qt の null ラッパ）: `isNull()` なら `None`、有効なら `.value()`
    - それ以外でプリミティブ（str/int/float/bool/None）ならそのまま
    - 上記以外は `str(value)` にフォールバック（json.dumps で落ちるより安全）

    Qt を import せずに判定するため、型名と duck typing で判別する。
    `protocol.py` は Qt 非依存を保ちたいので `isinstance(value, QVariant)` は
    使わない。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # QVariant: PyQt5 / PyQt6 どちらも class 名は "QVariant"
    if type(value).__name__ == "QVariant" and hasattr(value, "isNull"):
        if value.isNull():
            return None
        return _coerce_leaf(value.value())
    # それ以外の Qt 型（QDate / QDateTime / QByteArray 等）は文字列化で寄せる
    return str(value)


def decode_message(text: str) -> Message:
    """JSON 文字列を対応する dataclass メッセージに復号する。

    Args:
        text: WebSocket で受信した JSON テキスト

    Raises:
        ProtocolDecodeError: JSON が壊れている、type が未知、必須フィールド欠落
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolDecodeError(f"invalid JSON: {e}") from e

    if not isinstance(raw, dict):
        raise ProtocolDecodeError(f"expected JSON object at top level, got {type(raw).__name__}")

    type_str = raw.get("type")
    if type_str is None:
        raise ProtocolDecodeError("missing 'type' field")

    try:
        msg_type = MessageType(type_str)
    except ValueError as e:
        raise ProtocolDecodeError(f"unknown type: {type_str!r}") from e

    # type は dataclass の property で自動付与されるため、payload からは除外
    payload = {k: v for k, v in raw.items() if k != "type"}

    try:
        return _build_message(msg_type, payload)
    except (TypeError, KeyError, ValueError) as e:
        raise ProtocolDecodeError(f"failed to build {msg_type.value} message: {e}") from e


# ============================================================
# 内部ヘルパ
# ============================================================


def _build_message(msg_type: MessageType, payload: dict[str, Any]) -> Message:
    """type に応じた dataclass を構築する。"""
    if msg_type is MessageType.REGISTER:
        return RegisterRequest(
            id=payload["id"],
            role=Role(payload["role"]),
            protocol_version=payload.get("protocol_version", PROTOCOL_VERSION),
            pid=payload.get("pid"),
            label=payload.get("label"),
            project=payload.get("project"),
            started_at=payload.get("started_at"),
            previous_instance_id=payload.get("previous_instance_id"),
        )

    if msg_type is MessageType.REGISTER_ACK:
        return RegisterAck(
            id=payload["id"],
            ok=bool(payload["ok"]),
            instance_id=payload.get("instance_id"),
            resumed=bool(payload.get("resumed", False)),
            error=_build_error(payload.get("error")),
        )

    if msg_type is MessageType.REQUEST:
        raw_caller_role = payload.get("caller_role")
        caller_role = Role(raw_caller_role) if raw_caller_role is not None else None
        return Request(
            id=payload["id"],
            command=payload["command"],
            params=payload.get("params", {}),
            instance=payload.get("instance"),
            timeout_ms=payload.get("timeout_ms"),
            caller_role=caller_role,
        )

    if msg_type is MessageType.RESPONSE:
        return Response(
            id=payload["id"],
            ok=bool(payload["ok"]),
            result=payload.get("result"),
            error=_build_error(payload.get("error")),
        )

    if msg_type is MessageType.LIST_INSTANCES:
        return ListInstancesRequest(id=payload["id"])

    if msg_type is MessageType.LIST_INSTANCES_RESPONSE:
        raw_instances = payload.get("instances", [])
        instances = [
            InstanceInfo(
                instance_id=item["instance_id"],
                label=item["label"],
                pid=int(item["pid"]),
                project=item.get("project"),
            )
            for item in raw_instances
        ]
        return ListInstancesResponse(id=payload["id"], instances=instances)

    if msg_type is MessageType.BYE:
        return Bye(id=payload["id"], instance_id=payload["instance_id"])

    # MessageType 列挙を網羅的に扱っているが、将来の追加に備えて保険
    raise ProtocolDecodeError(f"unhandled message type: {msg_type}")


def _build_error(raw: dict[str, Any] | None) -> Error | None:
    """dict から Error dataclass を構築する（None 透過）。"""
    if raw is None:
        return None
    return Error(
        code=ErrorCode(raw["code"]),
        message=str(raw.get("message", "")),
        details=dict(raw.get("details", {})),
    )
