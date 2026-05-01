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


@dataclass(frozen=True)
class RegisterOutcome:
    """Worker 登録結果。Qt 側が register_ack を組み立てる材料。"""

    ok: bool
    instance_id: str | None = None
    assigned_label: str | None = None
    resumed: bool = False
    error: Error | None = None


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


def generate_instance_id(label: str, pid: int) -> str:
    """instance_id を label + pid から生成。"""
    return f"worker-{label.lower()}-{pid}"


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

        # 3. 再接続判定：previous_instance_id 指定 + grace 中に一致 entry あり
        if req.previous_instance_id:
            resumed = self._try_resume(
                conn_id=conn_id,
                previous_instance_id=req.previous_instance_id,
                pid=req.pid or 0,
                label=label,
            )
            if resumed is not None:
                return resumed
            # 一致しなければ新規登録フローに落ちる

        # 4. 新規登録：label 衝突検査（grace 中の entry も予約扱い）
        for entry in self._workers_by_conn.values():
            if entry.label == label:
                suggested = self._suggest_alternate_label(label)
                return RegisterOutcome(
                    ok=False,
                    error=Error(
                        code=ErrorCode.LABEL_CONFLICT,
                        message=f"Label '{label}' is already in use",
                        details={"suggested_label": suggested},
                    ),
                )

        # 5. instance_id 発行して登録
        instance_id = generate_instance_id(label, req.pid or 0)
        entry = WorkerEntry(
            conn_id=conn_id,
            instance_id=instance_id,
            label=label,
            pid=req.pid or 0,
            project=req.project,
        )
        self._workers_by_conn[conn_id] = entry
        self._conn_by_instance[instance_id] = conn_id

        return RegisterOutcome(
            ok=True,
            instance_id=instance_id,
            assigned_label=label,
            resumed=False,
        )

    def _try_resume(
        self,
        *,
        conn_id: str,
        previous_instance_id: str,
        pid: int,
        label: str,
    ) -> RegisterOutcome | None:
        """grace 中の entry と一致すれば resume。なければ None。"""
        prev_conn = self._conn_by_instance.get(previous_instance_id)
        if prev_conn is None:
            return None
        prev_entry = self._workers_by_conn.get(prev_conn)
        if prev_entry is None:
            return None
        # grace 中でなければ resume 対象外（active のまま登録要求が来たら別物）
        if prev_entry.disconnected_at is None:
            return None
        # pid + label が一致すれば引き継ぎ成立
        if prev_entry.pid != pid or prev_entry.label != label:
            return None

        # conn_id を更新し、disconnected_at をクリアして active に復帰
        del self._workers_by_conn[prev_conn]
        prev_entry.conn_id = conn_id
        prev_entry.disconnected_at = None
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
        entry = self._workers_by_conn.get(conn_id)
        if entry is None:
            return
        entry.disconnected_at = self._clock()

    def bye_worker(self, conn_id: str) -> None:
        """Worker からの明示的 bye：grace を経ずに即削除。"""
        entry = self._workers_by_conn.pop(conn_id, None)
        if entry is not None:
            self._conn_by_instance.pop(entry.instance_id, None)

    def disconnect_client(self, conn_id: str) -> None:
        self._clients.discard(conn_id)

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
        """idle 自動終了条件：active Worker 0 かつ Client 0。grace 中は無視。"""
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
        """selector 文字列から instance_id を解決（ADR-0001 §5）。

        解決順：`@label` → label → instance_id → project basename → pid。
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

        # @ プレフィックスは label 限定
        if selector.startswith("@"):
            raw = selector[1:]
            matched = [e for e in actives if e.label == raw]
            return self._single_or_error(matched, selector)

        # 通常解決：優先度順にマッチを集める
        # 1. label 完全一致（数字のみ label は作れないので衝突なし）
        matched = [e for e in actives if e.label == selector]
        if matched:
            return self._single_or_error(matched, selector)

        # 2. instance_id 完全一致
        matched = [e for e in actives if e.instance_id == selector]
        if matched:
            return self._single_or_error(matched, selector)

        # 3. project basename 一致（拡張子有無両対応）
        sel_stem = PureWindowsPath(selector).stem
        matched = [
            e
            for e in actives
            if e.project
            and (
                _project_basename(e.project) == selector
                or _project_basename(e.project) == sel_stem
                or PureWindowsPath(e.project).name == selector
            )
        ]
        if matched:
            return self._single_or_error(matched, selector)

        # 4. pid 文字列一致
        if selector.isdigit():
            matched = [e for e in actives if str(e.pid) == selector]
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
