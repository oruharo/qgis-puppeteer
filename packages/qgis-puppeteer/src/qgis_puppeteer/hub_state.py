"""Hub の純ロジック（Qt 非依存）。

ADR-0001 §4, §5, §9 に基づく状態管理・selector 解決・検証ヘルパを
Qt 配線から分離し、ユニットテスト可能な形で提供する。

Qt 側（`hub.py`）はこの `HubState` にイベントを委譲し、結果を
WebSocket への応答と接続ライフサイクル管理へ反映させる。

## スレッドモデル

`HubState` 自体はロックを持たない。呼び出し側（Qt 単一スレッド /
asyncio 単一タスク）が順序を保証する前提。ADR-0001 §4 参照。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

from qgis_puppeteer.protocol import (
    Error,
    ErrorCode,
    InstanceInfo,
    RegisterRequest,
)

# ============================================================
# 定数
# ============================================================

# Worker 切断後に instance_id を保持する期間（ADR-0001 §4）
GRACE_SECONDS: float = 60.0

# 全接続 0 → idle 自動終了までの猶予（ADR-0001 §4）
IDLE_SHUTDOWN_DELAY_SECONDS: float = 30.0

# ADR-0005 D5: ハートビート（ping/pong）の既定。10s 間隔・2 回欠落で
# disconnected 認定 → 旧 entry を速やかに grace へ落とし SUPERSEDE を促す。
HEARTBEAT_INTERVAL_SECONDS: float = 10.0
HEARTBEAT_TIMEOUT_SECONDS: float = 20.0

# ADR-0005 C1=(b): active 同 label 衝突時、incumbent へ ping して生死を確認
# する待ち時間。生きていれば pong がこの窓内に返る（通常ミリ秒）。
LIVENESS_PROBE_SECONDS: float = 2.0

# ADR-0005 H3: タイマ不変条件。
#   LIVENESS_PROBE < HEARTBEAT_TIMEOUT < GRACE
# かつ「grace entry が 1 件でも存在する間は idle-shutdown を抑止する」
# （ADR-0001 §4 の『grace は idle-shutdown を妨げない』を ADR-0005 が上書き）。
# これにより kill→再起動の谷間で Hub が自殺して grace entry を失い、
# SUPERSEDE / sticky / registered_seq が飛ぶのを防ぐ。Hub は誰も戻らなければ
# grace 満了（最長 GRACE 秒）後に通常どおり idle-shutdown する。

# label の最大長（ADR-0001 §5）
LABEL_MAX_LENGTH: int = 32

# Origin 検証の許可ホスト（ADR-0001 §9.2）
_ALLOWED_ORIGIN_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1"})


# ============================================================
# データ型
# ============================================================


@dataclass
class WorkerEntry:
    """Hub が保持する Worker 登録情報。

    `disconnected_at is None` のものが「active」。grace 期間中は
    値セット済みだが `sweep_expired()` までエントリ自体は残る。
    """

    conn_id: str
    instance_id: str
    label: str
    pid: int
    project: str | None
    disconnected_at: float | None = None
    # ADR-0005: label が明示指定か（auto 採番でないか）。sticky 安定キー判定用。
    label_explicit: bool = False
    # ADR-0005 D6: 公式 launch helper 由来の相関トークン（無ければ None）。
    launch_token: str | None = None
    # ADR-0005 D6: best-effort attribution 用。登録（or supersede/resume）時に採番。
    registered_seq: int = 0
    registered_at: float | None = None
    # ADR-0005 D5: 最後に生存確認（register / pong）した時刻。liveness sweep 用。
    last_seen_at: float | None = None


@dataclass(frozen=True)
class RegisterOutcome:
    """Worker 登録結果。Qt 側が register_ack を組み立てる材料。"""

    ok: bool
    instance_id: str | None = None
    assigned_label: str | None = None
    resumed: bool = False
    # ADR-0005 D2: grace 中 entry を継承して新規登録した（U1）。
    superseded: bool = False
    # ADR-0005 D4: takeover で active entry を強制 evict した場合、その
    # instance_id 群（Qt 配線層が旧 worker へ通知/切断するために使う）。
    evicted_instance_ids: tuple[str, ...] = ()
    # ADR-0005 C1=(b): active 同 label 衝突を即決せず incumbent の生死を
    # ping 確認するために保留中。Qt 配線層は register_ack を送らず、
    # liveness_probe_conn_ids へ ping を撃ち LIVENESS_PROBE_SECONDS 後に
    # finalize_pending_registration() を呼ぶ。
    pending: bool = False
    liveness_probe_conn_ids: tuple[str, ...] = ()
    error: Error | None = None


@dataclass
class _PendingRegistration:
    """C1=(b) liveness probe 待ちの保留登録。

    ``incumbent_last_seen`` は probe 開始時点の各 incumbent の last_seen_at。
    finalize 時にこれより前進していれば pong を返した=生存とみなす。
    """

    req: RegisterRequest
    label: str
    incumbent_last_seen: dict[str, float]


@dataclass(frozen=True)
class ResolveOutcome:
    """selector 解決結果。"""

    ok: bool
    instance_id: str | None = None
    error: Error | None = None


@dataclass(frozen=True)
class LabelValidation:
    """label 入力検証の結果。"""

    ok: bool
    reason: str = ""


# ============================================================
# 検証ヘルパ
# ============================================================


def validate_origin(origin: str | None) -> bool:
    """WebSocket Origin ヘッダの hostname 完全一致検証。

    ADR-0001 §9.2：`startswith` 禁止。`urlparse().hostname` で
    正確に hostname を取り出し、許可集合と完全一致比較する。
    """
    if not origin:
        return True
    hostname = urlparse(origin).hostname
    if hostname is None:
        return False
    return hostname in _ALLOWED_ORIGIN_HOSTS


def validate_label(label: str) -> LabelValidation:
    """label 文字列の妥当性検証（ADR-0001 §5）。"""
    if not label:
        return LabelValidation(ok=False, reason="empty")
    if len(label) > LABEL_MAX_LENGTH:
        return LabelValidation(ok=False, reason="too_long")
    if label.isdigit():
        # 数字のみは pid との衝突回避のため禁止
        return LabelValidation(ok=False, reason="numeric_only")
    if label.startswith("@"):
        # selector プレフィックスとの混同を避けるため
        return LabelValidation(ok=False, reason="at_prefix_reserved")
    return LabelValidation(ok=True)


def _project_basename(project: str | None) -> str | None:
    """project パスから basename（拡張子なし）を抽出。

    Windows / POSIX 両方のパス区切りに対応。
    """
    if not project:
        return None
    # Windows パスと POSIX パスどちらでも最後のセグメントを取る
    # PureWindowsPath は POSIX 区切りも扱えるので安全
    name = PureWindowsPath(project).name or PurePosixPath(project).name
    if not name:
        return None
    # 拡張子を除く（.qgz / .qgs など）
    return PureWindowsPath(name).stem


def generate_auto_label(project: str | None, pid: int) -> str:
    """label 省略時の自動採番（ADR-0001 §3）。

    `{project_basename}-{pid 下4桁}` 形式。project 不明時は `worker-{pid}`。
    """
    basename = _project_basename(project)
    pid_suffix = str(pid)[-4:].zfill(4)
    if basename:
        return f"{basename}-{pid_suffix}"
    return f"worker-{pid}"


# ADR-0005 M: launch_token の検証パラメータ（巨大/不正トークンで list_instances
# を肥大させない・selector lookup を汚さないため register 時に弾く）。
_LAUNCH_TOKEN_MAX_LENGTH: int = 64
_LAUNCH_TOKEN_PREFIX: str = "lt-"


def select_by_selector(selector: str, items: list) -> list:
    """ADR-0005 D6: selector を解決順に評価し、最初にヒットした tier の
    マッチ集合を返す（0 件 / 1 件 / 曖昧>1 件 の判定は呼び出し側）。

    Hub (`WorkerEntry` actives) / MCP gateway・client (`InstanceInfo`) の
    **唯一の解決ロジック**。3 箇所が本関数を共有し semantics drift を防ぐ
    （レビュー H-1）。``items`` の各要素は ``launch_token`` / ``label`` /
    ``instance_id`` / ``project`` 属性を持つ前提。

    解決順: ``launch_token`` → ``@label`` → ``label`` → ``instance_id`` →
    ``project`` basename（``pid`` 一致は ADR-0005 D1 で廃止）。
    """
    if not selector:
        return []

    tok = [i for i in items if getattr(i, "launch_token", None) and i.launch_token == selector]
    if tok:
        return tok

    if selector.startswith("@"):
        raw = selector[1:]
        return [i for i in items if i.label == raw]

    lbl = [i for i in items if i.label == selector]
    if lbl:
        return lbl

    iid = [i for i in items if i.instance_id == selector]
    if iid:
        return iid

    sel_stem = PureWindowsPath(selector).stem
    return [
        i
        for i in items
        if getattr(i, "project", None)
        and (
            _project_basename(i.project) == selector
            or _project_basename(i.project) == sel_stem
            or PureWindowsPath(i.project).name == selector
        )
    ]


def generate_instance_id() -> str:
    """instance_id を pid 非依存の不透明 nonce として採番（ADR-0005 D1）。

    `worker-{label}-{pid}` 形式を廃止。pid を identity から外し、登録ごとに
    衝突しない 80bit 乱数を 16 進 (hex) で返す（例: ``w-7k3p9q2m4x8a0b1c2d``）。
    pid 再利用や label 変更で identity がブレないことを保証する。
    """
    return "w-" + secrets.token_hex(10)


def validate_launch_token(token: str | None) -> LabelValidation:
    """ADR-0005 M: launch_token の妥当性検証。

    None / 空は「helper 非経由起動」として OK（検証対象外）。値がある場合は
    ``lt-`` prefix と最大長を要求する（公式 helper の発番形式）。
    """
    if not token:
        return LabelValidation(ok=True)
    if not token.startswith(_LAUNCH_TOKEN_PREFIX):
        return LabelValidation(ok=False, reason="bad_prefix")
    if len(token) > _LAUNCH_TOKEN_MAX_LENGTH:
        return LabelValidation(ok=False, reason="too_long")
    return LabelValidation(ok=True)


# ============================================================
# HubState 本体
# ============================================================


class HubState:
    """Hub の純ロジック状態。

    Qt / asyncio 配線から分離し、純関数的に操作できる設計にしている。
    Worker 登録・切断・再接続（grace）・selector 解決・idle 自動終了判定を担う。

    Args:
        clock: 現在時刻（秒）を返す呼び出し可能オブジェクト。テストで
               FakeClock を注入するため Callable 化してある。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # conn_id → WorkerEntry（active も grace 中も同じ dict に入る）
        self._workers_by_conn: dict[str, WorkerEntry] = {}
        # instance_id → conn_id（selector 解決の逆引き用）
        self._conn_by_instance: dict[str, str] = {}
        # Client 接続 ID の集合
        self._clients: set[str] = set()
        # ADR-0005 D6: 規約なし attribution 用の単調増加登録連番。
        self._seq_counter: int = 0
        # ADR-0005 C1=(b): liveness probe 待ちの保留登録（conn_id → 保留情報）。
        self._pending: dict[str, _PendingRegistration] = {}

    # ----- 登録 -----

    def register_worker(self, conn_id: str, req: RegisterRequest) -> RegisterOutcome:
        """Worker 登録。新規 / 再接続（resume）/ label 衝突を判定。"""
        # 1. label を確定（自動採番）
        label = req.label or generate_auto_label(req.project, req.pid or 0)

        # 2. label 妥当性チェック
        lv = validate_label(label)
        if not lv.ok:
            return RegisterOutcome(
                ok=False,
                error=Error(
                    code=ErrorCode.LABEL_CONFLICT,
                    message=f"Invalid label '{label}': {lv.reason}",
                    details={"reason": lv.reason},
                ),
            )

        # 2b. launch_token 妥当性（ADR-0005 M）。不正なら登録拒否。
        tv = validate_launch_token(req.launch_token)
        if not tv.ok:
            return RegisterOutcome(
                ok=False,
                error=Error(
                    code=ErrorCode.LABEL_CONFLICT,
                    message=f"Invalid launch_token: {tv.reason}",
                    details={"reason": tv.reason},
                ),
            )

        # 3. RESUME 判定（ADR-0005 D2/D6）：
        #    launch_token 一致 もしくは (previous_instance_id + label) 一致の
        #    grace 中 entry があれば同一 instance_id を引き継ぐ。pid は一致条件
        #    から除外（pid 再利用での誤 resume を防ぐ）。
        resumed = self._try_resume(
            conn_id=conn_id,
            previous_instance_id=req.previous_instance_id,
            launch_token=req.launch_token,
            label=label,
        )
        if resumed is not None:
            return resumed

        # 4. label の現在の占有状況を確認（ADR-0005 D2 / C1=(b)）。
        active_same_label = [
            e
            for e in self._workers_by_conn.values()
            if e.label == label and e.disconnected_at is None
        ]

        evicted_ids: list[str] = []
        if active_same_label:
            policy = (req.conflict_policy or "").lower()

            if policy == "takeover":
                # 明示 opt-in：生死を問わず即 evict（ADR-0005 D4）。
                for victim in active_same_label:
                    self._workers_by_conn.pop(victim.conn_id, None)
                    self._conn_by_instance.pop(victim.instance_id, None)
                    evicted_ids.append(victim.instance_id)
            elif policy == "suffix":
                # 明示 opt-in：衝突しない代替 label へ自動 rename。
                label = self._suggest_alternate_label(label)
            else:
                # 既定（ADR-0005 C1=(b)）：即 reject せず incumbent の生死を
                # ping で確認する。Qt 配線層が liveness_probe_conn_ids へ ping
                # を撃ち、LIVENESS_PROBE_SECONDS 後に
                # finalize_pending_registration() を呼ぶ。生きていれば reject、
                # 死んでいれば SUPERSEDE で継承（U1/U7 を env 無しで救う）。
                self._pending[conn_id] = _PendingRegistration(
                    req=req,
                    label=label,
                    incumbent_last_seen={
                        e.conn_id: (e.last_seen_at or 0.0) for e in active_same_label
                    },
                )
                return RegisterOutcome(
                    ok=False,
                    pending=True,
                    liveness_probe_conn_ids=tuple(e.conn_id for e in active_same_label),
                )

        return self._register_fresh(conn_id, req, label, evicted_ids=tuple(evicted_ids))

    def _register_fresh(
        self,
        conn_id: str,
        req: RegisterRequest,
        label: str,
        *,
        evicted_ids: tuple[str, ...] = (),
    ) -> RegisterOutcome:
        # label_explicit: worker が明示指定した label がそのまま採用された時のみ
        # True（auto 採番 / suffix rename は再起動で変わるので False）。
        label_explicit = bool(req.label) and label == req.label
        """grace 残骸を掃除して新しい不透明 instance_id で登録する共通経路。

        通常の FRESH / SUPERSEDE / takeover / suffix / liveness 確認後の
        いずれからも呼ばれる。grace 残骸（同 label・disconnected）があれば
        evict して ``superseded=True`` を返す（U1）。
        """
        grace_for_label = [
            e
            for e in self._workers_by_conn.values()
            if e.label == label and e.disconnected_at is not None
        ]
        superseded = False
        if grace_for_label:
            for stale in grace_for_label:
                self._workers_by_conn.pop(stale.conn_id, None)
                self._conn_by_instance.pop(stale.instance_id, None)
            superseded = True

        instance_id = generate_instance_id()
        self._seq_counter += 1
        now = self._clock()
        entry = WorkerEntry(
            conn_id=conn_id,
            instance_id=instance_id,
            label=label,
            pid=req.pid or 0,
            project=req.project,
            launch_token=req.launch_token,
            registered_seq=self._seq_counter,
            registered_at=now,
            last_seen_at=now,
            label_explicit=label_explicit,
        )
        self._workers_by_conn[conn_id] = entry
        self._conn_by_instance[instance_id] = conn_id

        return RegisterOutcome(
            ok=True,
            instance_id=instance_id,
            assigned_label=label,
            resumed=False,
            superseded=superseded or bool(evicted_ids),
            evicted_instance_ids=evicted_ids,
        )

    def finalize_pending_registration(self, conn_id: str) -> RegisterOutcome | None:
        """ADR-0005 C1=(b)：liveness probe 後の保留登録を確定する。

        Qt 配線層が incumbent へ ping を撃ち、LIVENESS_PROBE_SECONDS 経過後に
        呼ぶ。incumbent が ping に応答（pong → mark_seen で last_seen_at 前進）
        していれば**真の衝突**として reject。誰も応答していなければ全員死亡と
        みなして evict し、保留 worker を SUPERSEDE 登録する。

        Returns:
            確定 outcome。保留が見つからない（newcomer 切断等）場合は None。
        """
        pending = self._pending.pop(conn_id, None)
        if pending is None:
            return None

        label = pending.label
        alive_responders = []
        for inc_conn, base_seen in pending.incumbent_last_seen.items():
            entry = self._workers_by_conn.get(inc_conn)
            if entry is None or entry.disconnected_at is not None:
                continue  # 切断/grace 落ち = 死亡扱い
            if entry.label != label:
                continue  # 衝突対象でなくなった
            if (entry.last_seen_at or 0.0) > base_seen:
                alive_responders.append(entry)  # pong を返した = 生存

        if alive_responders:
            # 真に同時 active な同 label（U3 の事故）→ fail-safe で reject。
            suggested = self._suggest_alternate_label(label)
            return RegisterOutcome(
                ok=False,
                error=Error(
                    code=ErrorCode.LABEL_CONFLICT,
                    message=f"Label '{label}' is already in use by a live instance",
                    details={"suggested_label": suggested, "policy": "reject"},
                ),
            )

        # 全員 ping 無応答 = 死亡。evict して SUPERSEDE 登録（U1/U7）。
        evicted: list[str] = []
        for inc_conn in list(pending.incumbent_last_seen):
            entry = self._workers_by_conn.pop(inc_conn, None)
            if entry is not None:
                self._conn_by_instance.pop(entry.instance_id, None)
                evicted.append(entry.instance_id)
        return self._register_fresh(conn_id, pending.req, label, evicted_ids=tuple(evicted))

    def cancel_pending_registration(self, conn_id: str) -> None:
        """保留中の登録を破棄する（newcomer が probe 中に切断した場合等）。"""
        self._pending.pop(conn_id, None)

    def _try_resume(
        self,
        *,
        conn_id: str,
        previous_instance_id: str | None,
        launch_token: str | None,
        label: str,
    ) -> RegisterOutcome | None:
        """grace 中の entry と一致すれば resume。なければ None（ADR-0005 D2/D6）。

        一致条件（pid は使わない）:
            1. ``launch_token`` 一致（同一 launch helper 起動プロセスの再接続。
               token は 1 プロセス内で不変かつ一意なので理想的な resume キー）。
            2. ``previous_instance_id`` + ``label`` 一致（token 無し worker の
               後方互換経路。Hub 瞬断→同一プロセス再接続のケース）。
        """
        prev_entry: WorkerEntry | None = None

        # 1. launch_token 一致（grace 中のみ）
        if launch_token:
            for e in self._workers_by_conn.values():
                if (
                    e.disconnected_at is not None
                    and e.launch_token is not None
                    and e.launch_token == launch_token
                ):
                    prev_entry = e
                    break

        # 2. previous_instance_id + label 一致（grace 中のみ）
        if prev_entry is None and previous_instance_id:
            prev_conn = self._conn_by_instance.get(previous_instance_id)
            if prev_conn is not None:
                candidate = self._workers_by_conn.get(prev_conn)
                if (
                    candidate is not None
                    and candidate.disconnected_at is not None
                    and candidate.label == label
                ):
                    prev_entry = candidate

        if prev_entry is None:
            return None

        # conn_id を更新し active に復帰。ADR-0005 H-2: 2 つの index
        # (_workers_by_conn / _conn_by_instance) を不整合窓なしで張り替える。
        # 旧 conn を pop → entry を変異 → 新 conn で両 index を同時に張り直す。
        old_conn = prev_entry.conn_id
        self._workers_by_conn.pop(old_conn, None)
        prev_entry.conn_id = conn_id
        prev_entry.disconnected_at = None
        prev_entry.last_seen_at = self._clock()
        self._workers_by_conn[conn_id] = prev_entry
        self._conn_by_instance[prev_entry.instance_id] = conn_id

        return RegisterOutcome(
            ok=True,
            instance_id=prev_entry.instance_id,
            assigned_label=prev_entry.label,
            resumed=True,
        )

    def _suggest_alternate_label(self, label: str) -> str:
        """衝突時の代替 label を提案。`{label} (2)`, `(3)`, ... の順で空きを探す。"""
        used = {e.label for e in self._workers_by_conn.values()}
        for n in range(2, 100):
            candidate = f"{label} ({n})"
            if candidate not in used:
                return candidate
        return f"{label}-alt"

    def register_client(self, conn_id: str) -> None:
        self._clients.add(conn_id)

    # ----- 切断 -----

    def disconnect_worker(self, conn_id: str) -> None:
        """Worker 切断：grace 期間開始（即削除しない）。"""
        # probe 中の newcomer が切断したら保留登録を破棄（C1=(b)）。
        self._pending.pop(conn_id, None)
        entry = self._workers_by_conn.get(conn_id)
        if entry is None:
            return
        entry.disconnected_at = self._clock()

    def bye_worker(self, conn_id: str) -> None:
        """Worker からの明示的 bye：grace を経ずに即削除。"""
        self._pending.pop(conn_id, None)
        entry = self._workers_by_conn.pop(conn_id, None)
        if entry is not None:
            self._conn_by_instance.pop(entry.instance_id, None)

    def disconnect_client(self, conn_id: str) -> None:
        self._clients.discard(conn_id)

    def mark_seen(self, conn_id: str) -> None:
        """ADR-0005 D5: 生存確認（pong 受信等）で last_seen_at を更新する。

        Qt 配線層が QWebSocket の pong / 任意の受信フレームで呼ぶ想定。
        未知 conn_id は無視（既に切断・evict 済みなら何もしない）。
        """
        entry = self._workers_by_conn.get(conn_id)
        if entry is not None and entry.disconnected_at is None:
            entry.last_seen_at = self._clock()

    def sweep_stale_active(self, timeout_s: float = HEARTBEAT_TIMEOUT_SECONDS) -> list[str]:
        """ADR-0005 D5: ハートビート途絶の active entry を grace へ落とす。

        kill -9 等で TCP が half-open になり close フレームが来ないケースで、
        旧 entry が active のまま居座ると同一 label 再起動が reject される。
        last_seen_at が timeout を超えた active を disconnected 扱いにし、
        以降の同 label 登録が SUPERSEDE 経路に乗れるようにする。

        Returns:
            grace に落とした instance_id 一覧（Qt 層が通知に使える）。
        """
        now = self._clock()
        demoted: list[str] = []
        for entry in self._workers_by_conn.values():
            if entry.disconnected_at is not None:
                continue
            # ADR-0005 H-3: last_seen_at 未設定は「一度も生存確認できていない」
            # = 即 stale 扱い（永久免除の穴を塞ぐ）。
            last_seen = entry.last_seen_at if entry.last_seen_at is not None else 0.0
            if now - last_seen >= timeout_s:
                entry.disconnected_at = now
                demoted.append(entry.instance_id)
        return demoted

    def sweep_expired(self) -> list[str]:
        """grace を超過した entry を削除し、削除した instance_id 一覧を返す。"""
        now = self._clock()
        removed: list[str] = []
        for conn_id in list(self._workers_by_conn.keys()):
            entry = self._workers_by_conn[conn_id]
            if entry.disconnected_at is None:
                continue
            if now - entry.disconnected_at >= GRACE_SECONDS:
                del self._workers_by_conn[conn_id]
                self._conn_by_instance.pop(entry.instance_id, None)
                removed.append(entry.instance_id)
        return removed

    # ----- クエリ -----

    def active_worker_count(self) -> int:
        """disconnected_at is None のものだけを数える（ADR-0001 §4）。"""
        return sum(1 for e in self._workers_by_conn.values() if e.disconnected_at is None)

    def client_count(self) -> int:
        return len(self._clients)

    def has_grace_held_workers(self) -> bool:
        return any(e.disconnected_at is not None for e in self._workers_by_conn.values())

    def should_idle_shutdown(self) -> bool:
        """idle 自動終了条件：active Worker 0 かつ Client 0。

        ADR-0005 H3: ただし grace entry が 1 件でも残っている間は抑止する
        （ADR-0001 §4 の旧ルールを上書き）。kill→再起動の谷間で Hub が自殺
        して grace entry を失い SUPERSEDE / sticky / registered_seq が飛ぶのを
        防ぐ。誰も戻らなければ grace 満了（最長 GRACE 秒）後に通常終了する。
        """
        if self.has_grace_held_workers():
            return False
        return self.active_worker_count() == 0 and self.client_count() == 0

    def list_instances(self) -> list[InstanceInfo]:
        """active Worker のみを InstanceInfo 化して返す。"""
        out: list[InstanceInfo] = []
        for entry in self._workers_by_conn.values():
            if entry.disconnected_at is not None:
                continue
            out.append(
                InstanceInfo(
                    instance_id=entry.instance_id,
                    label=entry.label,
                    pid=entry.pid,
                    project=entry.project,
                    launch_token=entry.launch_token,
                    registered_seq=entry.registered_seq,
                    registered_at=entry.registered_at,
                    label_explicit=entry.label_explicit,
                )
            )
        return out

    def find_worker_conn(self, instance_id: str) -> str | None:
        """instance_id からルーティング先 conn_id を引く（active のみ）。"""
        conn_id = self._conn_by_instance.get(instance_id)
        if conn_id is None:
            return None
        entry = self._workers_by_conn.get(conn_id)
        if entry is None or entry.disconnected_at is not None:
            return None
        return conn_id

    # ----- selector 解決 -----

    def resolve_instance(self, selector: str | None) -> ResolveOutcome:
        """selector 文字列から instance_id を解決（ADR-0005 D6 最終形）。

        解決順：`launch_token` → `@label` → label → instance_id →
        project basename。`pid` 一致は廃止（pid を identity から外したため）。
        selector=None は active が 1 台のみなら自動選択、複数なら ambiguous。
        """
        actives = [e for e in self._workers_by_conn.values() if e.disconnected_at is None]

        # selector 未指定
        if selector is None:
            if len(actives) == 0:
                return self._not_found_outcome("No active instances")
            if len(actives) == 1:
                return ResolveOutcome(ok=True, instance_id=actives[0].instance_id)
            return self._ambiguous_outcome(actives)

        # ADR-0005 H-1: 解決順は共有純関数 select_by_selector に一本化。
        matched = select_by_selector(selector, actives)
        if matched:
            return self._single_or_error(matched, selector)

        return self._not_found_outcome(f"No instance matches {selector!r}")

    def _single_or_error(self, matched: list[WorkerEntry], selector: str) -> ResolveOutcome:
        if len(matched) == 1:
            return ResolveOutcome(ok=True, instance_id=matched[0].instance_id)
        if len(matched) == 0:
            return self._not_found_outcome(f"No instance matches {selector!r}")
        return self._ambiguous_outcome(matched)

    def _not_found_outcome(self, message: str) -> ResolveOutcome:
        return ResolveOutcome(
            ok=False,
            error=Error(code=ErrorCode.INSTANCE_NOT_FOUND, message=message),
        )

    def _ambiguous_outcome(self, matched: list[WorkerEntry]) -> ResolveOutcome:
        return ResolveOutcome(
            ok=False,
            error=Error(
                code=ErrorCode.INSTANCE_AMBIGUOUS,
                message="Multiple instances match",
                details={
                    "candidates": [
                        {
                            "instance_id": e.instance_id,
                            "label": e.label,
                            "pid": e.pid,
                            "project": e.project,
                        }
                        for e in matched
                    ]
                },
            ),
        )
