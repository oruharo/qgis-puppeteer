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

# プロトコルの版。挨拶（hello / register）で確かめ合い、範囲が重ならなければ
# Hub が protocol_version_mismatch で断る（ADR-0001 §3）。
# 2：挨拶（hello と proof）が必須になった。1 とは互換が無い
PROTOCOL_VERSION: int = 2

# request に timeout_ms が無いときに Hub が使う待ち時間（ミリ秒）
DEFAULT_REQUEST_TIMEOUT_MS: int = 30_000
# timeout_ms の上限。Hub は QTimer で待つので、その int の範囲に収める
MAX_REQUEST_TIMEOUT_MS: int = 2**31 - 1


@dataclass(frozen=True)
class ProtocolRange:
    """話せるプロトコルの版の範囲（両端を含む）。"""

    min: int
    max: int

    def __contains__(self, version: object) -> bool:
        return (
            isinstance(version, int)
            and not isinstance(version, bool)
            and self.min <= version <= self.max
        )


# この版が話せる範囲
SUPPORTED_PROTOCOL: ProtocolRange = ProtocolRange(min=PROTOCOL_VERSION, max=PROTOCOL_VERSION)


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

    # 挨拶：接続するとすぐ Hub が送る（ADR-0001 §3 "挨拶"）
    HELLO = "hello"

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

    # Worker → Hub: 登録後の自己申告情報の更新（project の読み込み・切替など）
    UPDATE_INFO = "update_info"

    # Client → Hub: 起こした QGIS をもう待たない（#16）。応答は無い
    ABANDON_LAUNCH = "abandon_launch"


class ErrorCode(str, Enum):
    """エラーコード（ADR-0001 §3 "エラーコード初期セット"）。"""

    INSTANCE_NOT_FOUND = "instance_not_found"
    # selector には一致するが、その Worker は切断済みで grace 中（再接続待ち）。
    # not_found とは違い「居たが今は応答経路が無い」。details に grace_expires_in。
    INSTANCE_DISCONNECTED = "instance_disconnected"
    INSTANCE_AMBIGUOUS = "instance_ambiguous"
    # Worker が timeout_ms 以内に応答しなかった（Hub が返す）。details に timeout_ms と command
    INSTANCE_TIMEOUT = "instance_timeout"
    # Worker が受け取った時点で request の期限（deadline）が過ぎていたので実行しなかった。
    # details に command と late_by_s
    REQUEST_EXPIRED = "request_expired"
    PROTOCOL_VERSION_MISMATCH = "protocol_version_mismatch"
    INVALID_COMMAND = "invalid_command"
    WORKER_EXECUTION_ERROR = "worker_execution_error"
    LABEL_CONFLICT = "label_conflict"
    # ADR-0001 §12.6 / §12.7 ハンドラ拡張機構
    RESERVED_NAMESPACE = "reserved_namespace"
    REGISTRATION_DENIED = "registration_denied"
    HANDLER_ALREADY_REGISTERED = "handler_already_registered"
    HANDLER_NOT_VISIBLE = "handler_not_visible"
    # 受け手の知らない type のメッセージだった（新しい版の相手から来た、など）。
    # 元のメッセージに id があれば、その id で返す。details に type
    UNSUPPORTED_MESSAGE = "unsupported_message"
    # 起こした側がもう待っていない launch_token の Worker の登録を断った（#16）
    LAUNCH_ABANDONED = "launch_abandoned"
    # 挨拶の proof が無い・合わない（Hub ファイルの token を知らない）。
    # 登録の前にほかのメッセージを送ったときも、これで断って切る
    UNAUTHORIZED = "unauthorized"


def error_code_value(code: ErrorCode | str) -> str:
    """``Error.code`` を文字列にする。知らないコード（文字列のまま）もそのまま返す。"""
    return code.value if isinstance(code, ErrorCode) else str(code)


# ============================================================
# ペイロード型
# ============================================================


@dataclass(frozen=True)
class InstanceInfo:
    """list_instances のレスポンスで返される Worker インスタンス情報。

    ADR-0005:
        - ``instance_id`` は pid 非依存の不透明 nonce（``w-<base32>``）。
        - ``pid`` は人間向け補助メタデータ（identity ではない）。
        - ``launch_token`` は公式 launch helper が発番した相関ハンドル
          （helper 経由起動でない場合 None）。
        - ``registered_seq`` / ``registered_at`` は規約なし attribution 用の
          best-effort 相関キー（単調増加連番 / 登録時刻）。
    """

    instance_id: str
    label: str
    pid: int
    project: str | None = None
    launch_token: str | None = None
    registered_seq: int = 0
    registered_at: float | None = None
    # ADR-0005: label が worker により明示指定されたか（auto 採番でないか）。
    # sticky の安定キー選択に使う（auto-label は再起動で変わるため不適）。
    label_explicit: bool = False
    # "active" | "unresponsive" | "disconnected"。unresponsive は TCP は繋がって
    # いるがハートビート pong が返っていない状態（QGIS の GUI スレッドが塞がって
    # いるのが典型）で、ルーティングは受け付ける。disconnected は切断済みで
    # grace 中（`include_disconnected=True` で要求したときだけ載る）。
    # 旧 Hub からは省略される。
    state: str = "active"
    # 最後に Hub が応答を確認してからの経過秒。旧 Hub からは None。
    last_seen_ago: float | None = None
    # disconnected のみ: Hub が登録を破棄するまでの残り秒。
    grace_expires_in: float | None = None
    # #17: その QGIS のプラグインのライブラリの指紋。旧プラグイン・旧 Hub では None
    source_fingerprint: str | None = None
    # その QGIS のプラグインの版（``qgis_puppeteer.__version__``）
    package_version: str | None = None


@dataclass(frozen=True)
class RejectedRegistration:
    """Hub が断った Worker の登録（#13）。

    ``launch_token`` 付きで登録を求めてきた Worker だけを、token ごとに最近の
    ものから一定数だけ覚えておく。起動した側（``spawn_qgis``）が「待っても
    登録されない」と分かって早く失敗し、起こした QGIS（``pid``）を片付ける
    ために使う。``list_instances`` を ``include_rejected=True`` で呼んだとき
    だけ返る。旧 Hub からは返らない。
    """

    launch_token: str
    pid: int | None
    label: str | None
    # Error.code の値（"label_conflict" など）と、Hub が Worker に返した文言
    code: str
    message: str
    # Hub の時計で何秒前に断ったか
    rejected_ago: float | None = None


@dataclass(frozen=True)
class Error:
    """エラーレスポンスの詳細。

    ``code`` は、この版が知っているコードなら ``ErrorCode``、知らないコード
    （新しい版の相手が足したもの）なら受け取った文字列のまま。``ErrorCode`` は
    ``str`` の Enum なので、``error.code == ErrorCode.X`` はどちらでも書ける。
    文字列が要るときは ``error_code_value()`` を使う。
    """

    code: ErrorCode | str
    message: str
    # ambiguous の場合の候補、label_conflict の suggested_label など、
    # エラーコード固有の付加情報を格納する
    details: dict[str, Any] = field(default_factory=dict)


# ============================================================
# メッセージ本体
# ============================================================


@dataclass(frozen=True)
class Hello:
    """Hub → 接続してきた相手：最初の挨拶。

    接続するとすぐ Hub が送る。相手はこれを受けてから ``register`` を送る。
    ``nonce`` は proof の計算に使う（``qgis_puppeteer.handshake``）。応答を
    待つメッセージではないので id は無い。
    """

    hub_version: str
    protocol: ProtocolRange
    nonce: str

    @property
    def type(self) -> MessageType:
        return MessageType.HELLO


@dataclass(frozen=True)
class RegisterRequest:
    """Hub への初回 register メッセージ。

    Worker の場合は pid/label/project を必須とし、再接続時は
    previous_instance_id を付けて instance_id 引き継ぎを要求する
    (ADR-0001 §4 "Worker 再接続時の instance_id 引き継ぎ")。

    ``hello`` を受けてから送り、``nonce`` と ``proof`` で Hub ファイルの token を
    知っていることを示す（``qgis_puppeteer.handshake``）。
    """

    id: str
    role: Role
    # 送り主が選んだ版（Hub の範囲と自分の範囲の重なりの最大）
    protocol_version: int = PROTOCOL_VERSION
    # 送り主の話せる範囲と、ライブラリの版（断られたときに理由を示すため）
    protocol: ProtocolRange | None = None
    package_version: str | None = None
    # 挨拶：送り主の nonce と proof（HMAC）
    nonce: str | None = None
    proof: str | None = None
    # Worker 専用フィールド（role が WORKER 以外では None）
    pid: int | None = None
    label: str | None = None
    project: str | None = None
    started_at: str | None = None  # ISO 8601 文字列
    previous_instance_id: str | None = None
    # ADR-0005 D6: 公式 launch helper が発番・env 注入した相関トークン。
    # 半制御 launch での決定的 attribution / pid 非依存 resume キーに使う。
    launch_token: str | None = None
    # ADR-0005 D4: active 同 label 衝突時のポリシー。
    # "reject"（既定 / None）/ "takeover" / "suffix"。worker 側 env で決まる。
    conflict_policy: str | None = None
    # #17: Worker 側（QGIS のプラグイン）のライブラリの中身の指紋
    # （`qgis_puppeteer._fingerprint`）。旧プラグインは送らない
    source_fingerprint: str | None = None

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
    # ADR-0005 D2: 同一 label の grace 中 entry を継承（SUPERSEDE）して
    # 新規登録した場合 True（U1 連続再起動の正常系）。
    superseded: bool = False
    error: Error | None = None
    # Hub が使う版（ok=True のとき）と、Hub のライブラリの版
    protocol_version: int | None = None
    package_version: str | None = None
    # 挨拶：Hub も token を知っていることを示す HMAC。proof が通らなかった
    # （unauthorized）・版が合わなかった（protocol_version_mismatch）ときは無い
    hub_proof: str | None = None

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
    # リクエストごとのタイムアウト（未指定なら Hub は DEFAULT_REQUEST_TIMEOUT_MS を使う）。
    # Hub はこの時間が過ぎても Worker が応答しなければ送り主に instance_timeout を返す
    timeout_ms: int | None = None
    # 実行してよい期限（Unix 時刻・秒）。Hub が Worker へ転送するときに timeout_ms から
    # 付ける。Worker はこれを過ぎた request を実行せずに request_expired で返す
    # （送り主はもう instance_timeout を受け取って諦めているので）。Hub と Worker は
    # 同じ機械で動くので壁時計で比べる
    deadline: float | None = None
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
    """インスタンス一覧取得リクエスト。Hub に直接処理される。

    ``include_disconnected`` を立てると grace 中（切断済み・再接続待ち）の
    entry も ``state="disconnected"`` で返す。既定は従来どおり出さない。
    ``include_rejected`` を立てると、断った登録（`RejectedRegistration`）も
    ``rejected`` に載せる（#13）。旧 Hub はこのフィールドを無視する。
    """

    id: str
    include_disconnected: bool = False
    include_rejected: bool = False

    @property
    def type(self) -> MessageType:
        return MessageType.LIST_INSTANCES


@dataclass(frozen=True)
class ListInstancesResponse:
    """インスタンス一覧レスポンス。"""

    id: str
    instances: list[InstanceInfo] = field(default_factory=list)
    # include_rejected=True のときだけ載る（#13）
    rejected: list[RejectedRegistration] = field(default_factory=list)

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


@dataclass(frozen=True)
class UpdateInfo:
    """Worker → Hub: 登録後に変わった自己申告情報を伝える。

    register は plugin ロード時（プロジェクトを開く前）に一度きりなので、
    その後の読み込み・切替で変わる ``project`` はこれで追いかける。Hub は
    entry を更新するだけで応答しない。
    """

    id: str
    instance_id: str
    project: str | None = None

    @property
    def type(self) -> MessageType:
        return MessageType.UPDATE_INFO


@dataclass(frozen=True)
class AbandonLaunch:
    """Client → Hub: この ``launch_token`` で起こした QGIS をもう待たない（#16）。

    ``spawn_qgis`` は登録を待ちきれなかったときに送る。Hub はこの token の登録を
    以後断り（``ErrorCode.LAUNCH_ABANDONED``）、その QGIS は QGIS Puppeteer
    プラグインが自分で閉じる。待ちが切れた後に遅れて登録してくる QGIS を
    残さないため。Hub は応答しない（古い Hub は読めずにエラーを返すが、
    クライアントは知らない id の返事を捨てる）。
    """

    id: str
    launch_token: str

    @property
    def type(self) -> MessageType:
        return MessageType.ABANDON_LAUNCH


# Union 型：プロトコルで流通する全メッセージ型
Message = (
    Hello
    | RegisterRequest
    | RegisterAck
    | Request
    | Response
    | ListInstancesRequest
    | ListInstancesResponse
    | Bye
    | UpdateInfo
    | AbandonLaunch
)


# ============================================================
# シリアライズ / デシリアライズ
# ============================================================


class ProtocolDecodeError(ValueError):
    """不正な JSON / 未知の type / 必須フィールド欠落などの解読失敗。

    JSON のオブジェクトとしては読めたときは、``message_id`` に ``id``（文字列の
    ときだけ）、``message_type`` に ``type`` の値が入る。受け手はこれを見て、
    送り主が答えを待たずに済むよう、その id でエラーを返せる。
    """

    def __init__(
        self,
        message: str,
        *,
        message_id: str | None = None,
        message_type: object = None,
    ) -> None:
        super().__init__(message)
        self.message_id = message_id
        self.message_type = message_type


class UnknownMessageTypeError(ProtocolDecodeError):
    """``type`` がこの版の知らない値だった。

    新しい版の相手が足したメッセージかもしれないので、壊れたメッセージとは
    分けて扱う（受け手は ``unsupported_message`` で答える）。
    """


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


def effective_timeout_ms(timeout_ms: object) -> int:
    """request の ``timeout_ms`` から、Hub が実際に待つミリ秒を決める。

    無い・数でないなら ``DEFAULT_REQUEST_TIMEOUT_MS``。数なら 1〜``MAX_REQUEST_TIMEOUT_MS``
    に丸める。
    """
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
        return DEFAULT_REQUEST_TIMEOUT_MS
    if timeout_ms != timeout_ms:  # NaN
        return DEFAULT_REQUEST_TIMEOUT_MS
    return int(min(max(timeout_ms, 1), MAX_REQUEST_TIMEOUT_MS))


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

    raw_id = raw.get("id")
    message_id = raw_id if isinstance(raw_id, str) else None
    type_str = raw.get("type")
    if type_str is None:
        raise ProtocolDecodeError("missing 'type' field", message_id=message_id)

    try:
        msg_type = MessageType(type_str)
    except (ValueError, TypeError) as e:
        raise UnknownMessageTypeError(
            f"unknown type: {type_str!r}", message_id=message_id, message_type=type_str
        ) from e

    # type は dataclass の property で自動付与されるため、payload からは除外。
    # 知らないフィールドは各 _build_* が読まないので無視される（新しい版の相手が
    # 足したフィールドで落ちないように）
    payload = {k: v for k, v in raw.items() if k != "type"}

    try:
        return _build_message(msg_type, payload)
    except (TypeError, KeyError, ValueError, AttributeError) as e:
        raise ProtocolDecodeError(
            f"failed to build {msg_type.value} message: {e}",
            message_id=message_id,
            message_type=type_str,
        ) from e


# 応答の type。壊れていても、これには答えない（応答に応答しない）
_REPLY_TYPES = frozenset(
    {
        MessageType.RESPONSE.value,
        MessageType.REGISTER_ACK.value,
        MessageType.LIST_INSTANCES_RESPONSE.value,
    }
)


def reply_to_undecodable(error: ProtocolDecodeError, *, receiver: str) -> Response | None:
    """読めなかったメッセージへの返事を作る。返さなくてよいときは None。

    送り主が答えを待ち続けずに済むよう、id が分かれば、その id でエラーを返す。
    知らない type なら ``unsupported_message``（新しい版の相手が足した
    メッセージかもしれない）、壊れていれば ``invalid_command``。id が分からない
    ときは、返しても送り主が照合できないので返さない。応答が壊れていたときも
    返さない。

    Args:
        error: ``decode_message`` が上げた例外
        receiver: 受け手の名前（"Hub" / "QGIS"）。返事の文言に入る
    """
    if error.message_id is None:
        return None
    if isinstance(error.message_type, str) and error.message_type in _REPLY_TYPES:
        return None
    if isinstance(error, UnknownMessageTypeError):
        return Response(
            id=error.message_id,
            ok=False,
            error=Error(
                code=ErrorCode.UNSUPPORTED_MESSAGE,
                message=(
                    f"{receiver} does not know the message type {error.message_type!r} "
                    "(is it from a newer version?)"
                ),
                details={"type": error.message_type},
            ),
        )
    return Response(
        id=error.message_id,
        ok=False,
        error=Error(code=ErrorCode.INVALID_COMMAND, message=f"Protocol decode error: {error}"),
    )


# ============================================================
# 内部ヘルパ
# ============================================================


def _build_message(msg_type: MessageType, payload: dict[str, Any]) -> Message:
    """type に応じた dataclass を構築する。"""
    if msg_type is MessageType.HELLO:
        protocol = parse_protocol_range(payload["protocol"])
        if protocol is None:
            raise ValueError("hello.protocol must be {'min': int, 'max': int}")
        return Hello(
            hub_version=str(payload.get("hub_version", "")),
            protocol=protocol,
            nonce=str(payload["nonce"]),
        )

    if msg_type is MessageType.REGISTER:
        return RegisterRequest(
            id=payload["id"],
            role=Role(payload["role"]),
            protocol_version=payload.get("protocol_version", PROTOCOL_VERSION),
            protocol=parse_protocol_range(payload.get("protocol")),
            package_version=payload.get("package_version"),
            nonce=payload.get("nonce"),
            proof=payload.get("proof"),
            pid=payload.get("pid"),
            label=payload.get("label"),
            project=payload.get("project"),
            started_at=payload.get("started_at"),
            previous_instance_id=payload.get("previous_instance_id"),
            launch_token=payload.get("launch_token"),
            conflict_policy=payload.get("conflict_policy"),
            source_fingerprint=payload.get("source_fingerprint"),
        )

    if msg_type is MessageType.REGISTER_ACK:
        return RegisterAck(
            id=payload["id"],
            ok=bool(payload["ok"]),
            instance_id=payload.get("instance_id"),
            resumed=bool(payload.get("resumed", False)),
            superseded=bool(payload.get("superseded", False)),
            error=_build_error(payload.get("error")),
            protocol_version=payload.get("protocol_version"),
            package_version=payload.get("package_version"),
            hub_proof=payload.get("hub_proof"),
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
            deadline=payload.get("deadline"),
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
        return ListInstancesRequest(
            id=payload["id"],
            include_disconnected=bool(payload.get("include_disconnected", False)),
            include_rejected=bool(payload.get("include_rejected", False)),
        )

    if msg_type is MessageType.LIST_INSTANCES_RESPONSE:
        raw_instances = payload.get("instances", [])
        instances = [
            InstanceInfo(
                instance_id=item["instance_id"],
                label=item["label"],
                pid=int(item["pid"]),
                project=item.get("project"),
                launch_token=item.get("launch_token"),
                registered_seq=int(item.get("registered_seq", 0)),
                registered_at=item.get("registered_at"),
                label_explicit=bool(item.get("label_explicit", False)),
                state=str(item.get("state", "active")),
                last_seen_ago=item.get("last_seen_ago"),
                grace_expires_in=item.get("grace_expires_in"),
                source_fingerprint=item.get("source_fingerprint"),
                package_version=item.get("package_version"),
            )
            for item in raw_instances
        ]
        rejected = [
            RejectedRegistration(
                launch_token=str(item["launch_token"]),
                pid=None if item.get("pid") is None else int(item["pid"]),
                label=item.get("label"),
                code=str(item.get("code", "")),
                message=str(item.get("message", "")),
                rejected_ago=item.get("rejected_ago"),
            )
            for item in payload.get("rejected", [])
        ]
        return ListInstancesResponse(id=payload["id"], instances=instances, rejected=rejected)

    if msg_type is MessageType.BYE:
        return Bye(id=payload["id"], instance_id=payload["instance_id"])

    if msg_type is MessageType.UPDATE_INFO:
        return UpdateInfo(
            id=payload["id"],
            instance_id=payload["instance_id"],
            project=payload.get("project"),
        )

    if msg_type is MessageType.ABANDON_LAUNCH:
        return AbandonLaunch(id=payload["id"], launch_token=str(payload["launch_token"]))

    # MessageType 列挙を網羅的に扱っているが、将来の追加に備えて保険
    raise ProtocolDecodeError(f"unhandled message type: {msg_type}")


def parse_protocol_range(raw: object) -> ProtocolRange | None:
    """``{"min": int, "max": int}`` を ``ProtocolRange`` にする。形が違えば None。"""
    if not isinstance(raw, dict):
        return None
    low, high = raw.get("min"), raw.get("max")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (low, high)):
        return None
    return ProtocolRange(min=low, max=high)  # type: ignore[arg-type]


def _build_error(raw: dict[str, Any] | None) -> Error | None:
    """dict から Error dataclass を構築する（None 透過）。

    知らないエラーコードは落とさず、文字列のまま持つ（新しい版の相手が足した
    コードで、応答ごと読み捨てて呼び出し側を時間切れまで待たせないため）。
    """
    if raw is None:
        return None
    raw_code = str(raw["code"])
    try:
        code: ErrorCode | str = ErrorCode(raw_code)
    except ValueError:
        code = raw_code
    return Error(
        code=code,
        message=str(raw.get("message", "")),
        details=dict(raw.get("details", {})),
    )
