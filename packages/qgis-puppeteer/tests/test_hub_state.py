"""qgis_puppeteer.hub_state のユニットテスト。

ADR-0001 §4, §5, §9 の Hub 純ロジック仕様を検証する。
Qt 非依存部分（Worker 登録・再接続・selector 解決・idle 自動終了判定・
Origin 検証・label 検証）を `HubState` と検証ヘルパで網羅する。

Qt 配線側（QWebSocketServer の接続管理）は別テストで扱う。
"""

from __future__ import annotations

from qgis_puppeteer.hub_state import (
    GRACE_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS,
    IDLE_SHUTDOWN_DELAY_SECONDS,
    HubState,
    RegisterOutcome,
    ResolveOutcome,
    generate_auto_label,
    generate_instance_id,
    validate_label,
    validate_origin,
)
from qgis_puppeteer.protocol import (
    ErrorCode,
    RegisterRequest,
    Role,
)

# ============================================================
# Clock ヘルパ：決定的に進む時計
# ============================================================


class FakeClock:
    """テスト用の決定的な時計。`advance()` で任意秒進める。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _worker_req(
    *,
    id: str = "uuid-1",
    pid: int = 1234,
    label: str | None = "A",
    project: str | None = "D:/work/a.qgz",
    previous_instance_id: str | None = None,
    launch_token: str | None = None,
) -> RegisterRequest:
    return RegisterRequest(
        id=id,
        role=Role.WORKER,
        pid=pid,
        label=label,
        project=project,
        previous_instance_id=previous_instance_id,
        launch_token=launch_token,
    )


def _register_resolving(
    state: HubState,
    clock: FakeClock,
    conn_id: str,
    req: RegisterRequest,
    *,
    incumbent_responds: bool,
):
    """ADR-0005 C1=(b): register → (pending なら) probe をシミュレートして finalize。

    ``incumbent_responds=True`` は pong 受信を模擬（probe 中に各 incumbent の
    last_seen_at を前進させる）。即時 outcome（pending でない）はそのまま返す。
    """
    outcome = state.register_worker(conn_id, req)
    if not outcome.pending:
        return outcome
    clock.advance(0.5)
    if incumbent_responds:
        for inc in outcome.liveness_probe_conn_ids:
            state.mark_seen(inc)
    return state.finalize_pending_registration(conn_id)


# ============================================================
# Worker 登録（新規）
# ============================================================


class TestRegisterWorkerNew:
    def test_first_worker_is_accepted(self) -> None:
        state = HubState(clock=FakeClock())
        outcome = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        assert outcome.ok is True
        assert outcome.instance_id is not None
        assert outcome.resumed is False
        assert outcome.error is None

    def test_instance_id_is_opaque_nonce(self) -> None:
        """ADR-0005 D1: instance_id は pid/label 非依存の不透明 nonce。"""
        state = HubState(clock=FakeClock())
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        r2 = state.register_worker("conn-2", _worker_req(label="B", pid=1234))
        assert r1.instance_id.startswith("w-")
        # pid/label が同じでも instance_id は毎回ユニーク
        assert r1.instance_id != r2.instance_id
        assert "1234" not in r1.instance_id

    def test_label_auto_generated_when_omitted(self) -> None:
        state = HubState(clock=FakeClock())
        outcome = state.register_worker(
            "conn-1",
            _worker_req(label=None, pid=1234, project="D:/work/myproject.qgz"),
        )
        assert outcome.ok is True
        # {project_basename}-{pid 下4桁} 形式
        assert "myproject" in outcome.assigned_label.lower()
        assert "1234" in outcome.assigned_label

    def test_two_workers_with_distinct_labels_both_accepted(self) -> None:
        state = HubState(clock=FakeClock())
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        r2 = state.register_worker("conn-2", _worker_req(label="B", pid=5678))
        assert r1.ok and r2.ok
        assert r1.instance_id != r2.instance_id

    def test_duplicate_label_conflict_when_incumbent_alive(self) -> None:
        # ADR-0005 C1=(b): incumbent が ping に応答（生存）→ 真の衝突で reject。
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        r2 = _register_resolving(
            state,
            clock,
            "conn-2",
            _worker_req(label="A", pid=5678),
            incumbent_responds=True,
        )
        assert r2.ok is False
        assert r2.error is not None
        assert r2.error.code == ErrorCode.LABEL_CONFLICT
        assert "suggested_label" in r2.error.details
        assert r2.error.details["suggested_label"] != "A"


# ============================================================
# Worker 再接続（instance_id 引き継ぎ）
# ============================================================


class TestRegisterWorkerResume:
    def test_resume_with_previous_id_and_label(self) -> None:
        """ADR-0005 D2: previous_instance_id + label 一致で resume（pid 不問）。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        original_id = r1.instance_id

        # 切断（grace 保持）
        state.disconnect_worker("conn-1")

        # 再接続：previous_instance_id + label 一致（pid が変わっても OK）
        r2 = state.register_worker(
            "conn-2",
            _worker_req(label="A", pid=9999, previous_instance_id=original_id),
        )
        assert r2.ok is True
        assert r2.resumed is True
        assert r2.instance_id == original_id

    def test_resume_by_launch_token_ignores_pid(self) -> None:
        """ADR-0005 D6: launch_token 一致で resume（previous_instance_id 不要）。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        r1 = state.register_worker(
            "conn-1", _worker_req(label="A", pid=1234, launch_token="lt-abc")
        )
        original_id = r1.instance_id
        state.disconnect_worker("conn-1")

        # token だけで resume（pid 再利用・previous_instance_id なし）
        r2 = state.register_worker(
            "conn-2", _worker_req(label="A", pid=4321, launch_token="lt-abc")
        )
        assert r2.ok is True
        assert r2.resumed is True
        assert r2.instance_id == original_id

    def test_previous_id_mismatch_supersedes_grace(self) -> None:
        """ADR-0005 D2 (U1): bogus previous_id + grace のみ → SUPERSEDE（衝突しない）。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.disconnect_worker("conn-1")

        # previous_instance_id が嘘でも、active が居なければ grace を継承
        r2 = state.register_worker(
            "conn-2",
            _worker_req(label="A", pid=5678, previous_instance_id="bogus-id"),
        )
        assert r2.ok is True
        assert r2.resumed is False
        assert r2.superseded is True
        assert r2.instance_id != r1.instance_id
        # 旧 grace entry は evict 済み
        assert state.has_grace_held_workers() is False

    def test_resume_after_grace_expires_is_fresh_registration(self) -> None:
        """grace 満了後は resume も supersede もせず純粋な新規登録。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        original_id = r1.instance_id

        state.disconnect_worker("conn-1")
        clock.advance(GRACE_SECONDS + 1.0)
        state.sweep_expired()

        r2 = state.register_worker(
            "conn-2",
            _worker_req(label="A", pid=1234, previous_instance_id=original_id),
        )
        assert r2.ok is True
        assert r2.resumed is False
        assert r2.superseded is False  # grace 残骸も無いので素の新規
        assert r2.instance_id != original_id  # nonce は常にユニーク


# ============================================================
# bye と切断
# ============================================================


class TestDisconnectAndBye:
    def test_disconnect_marks_grace_does_not_remove(self) -> None:
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.disconnect_worker("conn-1")
        # まだ保持されている（再接続待ち）
        assert state.has_grace_held_workers() is True

    def test_bye_removes_immediately(self) -> None:
        state = HubState(clock=FakeClock())
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.bye_worker("conn-1")
        # grace を経ずに消える
        assert state.has_grace_held_workers() is False
        assert state.active_worker_count() == 0
        # 同じ label を即座に再利用できる
        r2 = state.register_worker("conn-2", _worker_req(label="A", pid=9999))
        assert r2.ok is True
        assert r2.instance_id != r1.instance_id

    def test_sweep_expired_removes_past_grace(self) -> None:
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.disconnect_worker("conn-1")

        clock.advance(GRACE_SECONDS - 1.0)
        removed = state.sweep_expired()
        assert removed == []  # まだ grace 中

        clock.advance(2.0)  # grace 超過
        removed = state.sweep_expired()
        assert len(removed) == 1


# ============================================================
# active_worker_count / idle 自動終了判定
# ============================================================


class TestIdleShutdownCondition:
    def test_no_workers_no_clients_should_idle_shutdown(self) -> None:
        state = HubState(clock=FakeClock())
        assert state.should_idle_shutdown() is True

    def test_active_worker_prevents_idle_shutdown(self) -> None:
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        assert state.should_idle_shutdown() is False

    def test_grace_worker_blocks_idle_shutdown(self) -> None:
        """ADR-0005 H3: grace entry がある間は idle-shutdown を抑止する
        （ADR-0001 §4 の旧ルールを上書き。restart valley で Hub 自殺を防ぐ）。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.disconnect_worker("conn-1")
        # active=0 だが grace entry があるので idle-shutdown しない
        assert state.active_worker_count() == 0
        assert state.has_grace_held_workers() is True
        assert state.should_idle_shutdown() is False
        # grace 満了で entry が消えれば通常どおり idle-shutdown 可
        clock.advance(GRACE_SECONDS + 1.0)
        state.sweep_expired()
        assert state.should_idle_shutdown() is True

    def test_client_prevents_idle_shutdown(self) -> None:
        state = HubState(clock=FakeClock())
        state.register_client("client-1")
        assert state.should_idle_shutdown() is False

    def test_idle_shutdown_delay_constant(self) -> None:
        assert IDLE_SHUTDOWN_DELAY_SECONDS == 30.0

    def test_grace_constant(self) -> None:
        assert GRACE_SECONDS == 60.0


# ============================================================
# Selector 解決（ADR-0001 §5）
# ============================================================


class TestResolveInstance:
    def _populate(self) -> HubState:
        state = HubState(clock=FakeClock())
        state.register_worker(
            "conn-1", _worker_req(label="A", pid=1234, project="D:/work/alpha.qgz")
        )
        state.register_worker(
            "conn-2", _worker_req(label="B", pid=5678, project="D:/work/beta.qgz")
        )
        return state

    def _id_of(self, state: HubState, label: str) -> str:
        return next(i.instance_id for i in state.list_instances() if i.label == label)

    def test_at_prefix_matches_label(self) -> None:
        state = self._populate()
        result = state.resolve_instance("@A")
        assert result.ok
        assert result.instance_id == self._id_of(state, "A")

    def test_plain_label_matches(self) -> None:
        state = self._populate()
        result = state.resolve_instance("B")
        assert result.ok
        assert result.instance_id == self._id_of(state, "B")

    def test_instance_id_matches(self) -> None:
        state = self._populate()
        list_result = state.list_instances()
        target = list_result[0].instance_id
        result = state.resolve_instance(target)
        assert result.ok
        assert result.instance_id == target

    def test_project_basename_matches_without_extension(self) -> None:
        state = self._populate()
        result = state.resolve_instance("alpha")
        assert result.ok
        assert result.instance_id == self._id_of(state, "A")

    def test_project_basename_matches_with_extension(self) -> None:
        state = self._populate()
        result = state.resolve_instance("beta.qgz")
        assert result.ok
        assert result.instance_id == self._id_of(state, "B")

    def test_launch_token_has_top_precedence(self) -> None:
        """ADR-0005 D6: launch_token は最優先 tier。"""
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234, launch_token="lt-zzz"))
        result = state.resolve_instance("lt-zzz")
        assert result.ok
        assert result.instance_id == self._id_of(state, "A")

    def test_pid_string_no_longer_resolves(self) -> None:
        """ADR-0005 D1: pid 一致 tier は廃止。pid 文字列は not_found。"""
        state = self._populate()
        result = state.resolve_instance("1234")
        assert result.ok is False
        assert result.error.code == ErrorCode.INSTANCE_NOT_FOUND

    def test_none_selector_single_worker_auto_selects(self) -> None:
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        result = state.resolve_instance(None)
        assert result.ok

    def test_none_selector_multiple_workers_ambiguous(self) -> None:
        state = self._populate()
        result = state.resolve_instance(None)
        assert result.ok is False
        assert result.error.code == ErrorCode.INSTANCE_AMBIGUOUS
        assert "candidates" in result.error.details

    def test_not_found_returns_error(self) -> None:
        state = self._populate()
        result = state.resolve_instance("nonexistent")
        assert result.ok is False
        assert result.error.code == ErrorCode.INSTANCE_NOT_FOUND

    def test_grace_held_worker_not_selectable(self) -> None:
        """grace 中の Worker はルーティング対象外。"""
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.disconnect_worker("conn-1")
        result = state.resolve_instance("A")
        # grace 中は not_found（新規接続を待つ間のルーティングは不可）
        assert result.ok is False
        assert result.error.code == ErrorCode.INSTANCE_NOT_FOUND


# ============================================================
# list_instances
# ============================================================


class TestListInstances:
    def test_only_active_workers_listed(self) -> None:
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.register_worker("conn-2", _worker_req(label="B", pid=5678))
        state.disconnect_worker("conn-2")
        listed = state.list_instances()
        assert len(listed) == 1
        assert listed[0].pid == 1234

    def test_empty_when_no_workers(self) -> None:
        state = HubState(clock=FakeClock())
        assert state.list_instances() == []

    def test_exposes_launch_token_and_seq(self) -> None:
        """ADR-0005 D6: InstanceInfo に launch_token / registered_seq を含む。"""
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234, launch_token="lt-1"))
        state.register_worker("conn-2", _worker_req(label="B", pid=5678))
        listed = {i.label: i for i in state.list_instances()}
        assert listed["A"].launch_token == "lt-1"
        assert listed["B"].launch_token is None
        # 連番は登録順に単調増加
        assert listed["B"].registered_seq > listed["A"].registered_seq
        assert listed["A"].registered_at is not None


# ============================================================
# ADR-0005 D2: 同一 label 連続再起動（SUPERSEDE / U1）
# ============================================================


class TestSamelabelRestartSupersede:
    def test_serial_restart_within_grace_supersedes(self) -> None:
        """U1: kill→即同 label 起動。grace 中でも衝突せず継承（superseded=True）。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1111))
        state.disconnect_worker("conn-1")  # kill 相当（grace 60s 中）

        clock.advance(1.0)  # grace 窓のど真ん中で再起動
        r2 = state.register_worker("conn-2", _worker_req(label="A", pid=2222))

        assert r2.ok is True
        assert r2.superseded is True
        assert r2.resumed is False
        assert r2.instance_id != r1.instance_id
        # A は 1 台だけ active で、label で素直に引ける
        assert state.active_worker_count() == 1
        assert state.resolve_instance("A").instance_id == r2.instance_id

    def test_live_same_label_still_conflicts(self) -> None:
        """U3: incumbent が ping 応答（生存）なら fail-safe で reject。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("conn-1", _worker_req(label="A", pid=1111))
        r2 = _register_resolving(
            state,
            clock,
            "conn-2",
            _worker_req(label="A", pid=2222),
            incumbent_responds=True,
        )
        assert r2.ok is False
        assert r2.error.code == ErrorCode.LABEL_CONFLICT
        assert r2.error.details["suggested_label"] != "A"

    def test_dead_active_same_label_supersedes_via_probe(self) -> None:
        """ADR-0005 C1=(b) / U7: kill -9 で旧 entry が active のまま残っても、
        ping 無応答なら probe 後に SUPERSEDE で素直に継承（env 不要）。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1111))
        # disconnect せず（half-open 模擬）。incumbent は pong を返さない。
        r2 = _register_resolving(
            state,
            clock,
            "conn-2",
            _worker_req(label="A", pid=2222),
            incumbent_responds=False,
        )
        assert r2.ok is True
        assert r2.superseded is True
        assert r2.instance_id != r1.instance_id
        assert r1.instance_id in r2.evicted_instance_ids
        assert state.active_worker_count() == 1
        assert state.resolve_instance("A").instance_id == r2.instance_id

    def test_invalid_launch_token_rejected(self) -> None:
        # ADR-0005 M: 不正 prefix / 過大長は register 時に弾く。
        state = HubState(clock=FakeClock())
        bad_prefix = RegisterRequest(
            id="x", role=Role.WORKER, pid=1, label="A", launch_token="xx-abc"
        )
        r = state.register_worker("c1", bad_prefix)
        assert r.ok is False
        assert r.error.code == ErrorCode.LABEL_CONFLICT
        assert r.error.details["reason"] == "bad_prefix"

        too_long = RegisterRequest(
            id="y",
            role=Role.WORKER,
            pid=1,
            label="B",
            launch_token="lt-" + "a" * 100,
        )
        r2 = state.register_worker("c2", too_long)
        assert r2.ok is False
        assert r2.error.details["reason"] == "too_long"

    def test_pending_cancelled_if_newcomer_disconnects(self) -> None:
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("conn-1", _worker_req(label="A", pid=1))
        out = state.register_worker("conn-2", _worker_req(label="A", pid=2))
        assert out.pending is True
        state.disconnect_worker("conn-2")  # newcomer が probe 中に切断
        assert state.finalize_pending_registration("conn-2") is None

    def test_supersede_repeats_across_many_restarts(self) -> None:
        """連続再起動を何度繰り返しても毎回 SUPERSEDE で素通り。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        prev_id = state.register_worker("c0", _worker_req(label="A")).instance_id
        for n in range(1, 6):
            state.disconnect_worker(f"c{n - 1}")
            clock.advance(0.5)
            r = state.register_worker(f"c{n}", _worker_req(label="A"))
            assert r.ok and r.superseded and r.instance_id != prev_id
            prev_id = r.instance_id
        assert state.active_worker_count() == 1


# ============================================================
# ADR-0005 D4: active 同 label 衝突ポリシー（reject/takeover/suffix）
# ============================================================


class TestConflictPolicy:
    def _req(self, **kw: object) -> RegisterRequest:
        return _worker_req(**kw)  # type: ignore[arg-type]

    def test_default_probes_then_rejects_if_alive(self) -> None:
        # 既定（policy 無指定）= C1=(b) probe。incumbent 生存 → reject。
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("c1", _worker_req(label="A", pid=1))
        r = _register_resolving(
            state,
            clock,
            "c2",
            _worker_req(label="A", pid=2),
            incumbent_responds=True,
        )
        assert r.ok is False
        assert r.error.code == ErrorCode.LABEL_CONFLICT
        assert r.error.details["policy"] == "reject"

    def test_takeover_evicts_active_and_registers(self) -> None:
        state = HubState(clock=FakeClock())
        r1 = state.register_worker("c1", _worker_req(label="A", pid=1))
        r2 = state.register_worker(
            "c2",
            RegisterRequest(
                id="x",
                role=Role.WORKER,
                pid=2,
                label="A",
                conflict_policy="takeover",
            ),
        )
        assert r2.ok is True
        assert r2.instance_id != r1.instance_id
        assert r2.evicted_instance_ids == (r1.instance_id,)
        # 旧 active は消え、新 worker だけが label A を持つ
        assert state.active_worker_count() == 1
        assert state.resolve_instance("A").instance_id == r2.instance_id

    def test_suffix_auto_relabels(self) -> None:
        state = HubState(clock=FakeClock())
        state.register_worker("c1", _worker_req(label="A", pid=1))
        r2 = state.register_worker(
            "c2",
            RegisterRequest(
                id="x",
                role=Role.WORKER,
                pid=2,
                label="A",
                conflict_policy="suffix",
            ),
        )
        assert r2.ok is True
        assert r2.assigned_label != "A"
        assert r2.assigned_label.startswith("A ")
        # 両方 active で別 label として引ける
        assert state.active_worker_count() == 2
        assert state.resolve_instance("A").ok
        assert state.resolve_instance(r2.assigned_label).ok

    def test_takeover_with_no_active_is_plain_fresh(self) -> None:
        """衝突が無ければ takeover 指定でも普通の新規登録（evicted 空）。"""
        state = HubState(clock=FakeClock())
        r = state.register_worker(
            "c1",
            RegisterRequest(
                id="x", role=Role.WORKER, pid=1, label="solo", conflict_policy="takeover"
            ),
        )
        assert r.ok is True
        assert r.evicted_instance_ids == ()


# ============================================================
# ADR-0005 D5: ハートビート liveness sweep
# ============================================================


class TestLivenessSweep:
    def test_stale_active_demoted_to_grace(self) -> None:
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("c1", _worker_req(label="A", pid=1))
        assert state.active_worker_count() == 1

        clock.advance(HEARTBEAT_TIMEOUT_SECONDS + 1.0)
        demoted = state.sweep_stale_active()
        assert len(demoted) == 1
        # active から落ち、grace 保持（sweep_expired まで entry は残る）
        assert state.active_worker_count() == 0
        assert state.has_grace_held_workers() is True

    def test_mark_seen_keeps_active(self) -> None:
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("c1", _worker_req(label="A", pid=1))

        # timeout 直前に pong → last_seen 更新で延命
        clock.advance(HEARTBEAT_TIMEOUT_SECONDS - 1.0)
        state.mark_seen("c1")
        clock.advance(2.0)  # 直近 mark_seen からは 2s しか経ってない
        assert state.sweep_stale_active() == []
        assert state.active_worker_count() == 1

    def test_demoted_then_same_label_supersedes(self) -> None:
        """kill -9 half-open → sweep で grace → 同 label 再起動が SUPERSEDE。"""
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("c1", _worker_req(label="A", pid=1))
        clock.advance(HEARTBEAT_TIMEOUT_SECONDS + 1.0)
        state.sweep_stale_active()  # 旧 A を grace へ

        # reject 既定でも衝突せず継承できる
        r2 = state.register_worker("c2", _worker_req(label="A", pid=2))
        assert r2.ok is True
        assert r2.superseded is True

    def test_mark_seen_unknown_conn_is_noop(self) -> None:
        state = HubState(clock=FakeClock())
        state.mark_seen("nope")  # 例外を投げない


# ============================================================
# Origin 検証（ADR-0001 §9.2）
# ============================================================


class TestOriginValidation:
    def test_no_origin_allowed(self) -> None:
        assert validate_origin(None) is True
        assert validate_origin("") is True

    def test_localhost_allowed(self) -> None:
        assert validate_origin("http://localhost") is True
        assert validate_origin("http://localhost:3000") is True
        assert validate_origin("https://localhost:8443") is True

    def test_loopback_ip_allowed(self) -> None:
        assert validate_origin("http://127.0.0.1") is True
        assert validate_origin("http://127.0.0.1:9876") is True

    def test_external_origin_denied(self) -> None:
        assert validate_origin("http://evil.example.com") is False
        assert validate_origin("https://attacker.com:443") is False

    def test_subdomain_attack_denied(self) -> None:
        """localhost.evil.com / 127.0.0.1.evil.com はブロック（startswith 禁止）。"""
        assert validate_origin("http://localhost.evil.com") is False
        assert validate_origin("http://127.0.0.1.evil.com") is False

    def test_similar_hostname_denied(self) -> None:
        assert validate_origin("http://notlocalhost") is False


# ============================================================
# label 検証
# ============================================================


class TestLabelValidation:
    def test_valid_label(self) -> None:
        assert validate_label("A").ok
        assert validate_label("alpha").ok
        assert validate_label("プロジェクトA").ok
        assert validate_label("a-1234").ok  # 数字を含むが数字のみではない

    def test_empty_rejected(self) -> None:
        assert not validate_label("").ok

    def test_too_long_rejected(self) -> None:
        assert not validate_label("x" * 33).ok

    def test_boundary_length_accepted(self) -> None:
        assert validate_label("x" * 32).ok

    def test_numeric_only_rejected(self) -> None:
        """数字のみの label は pid と衝突するため禁止（ADR-0001 §5）。"""
        assert not validate_label("1234").ok
        assert not validate_label("0").ok

    def test_at_prefix_not_allowed_in_label_itself(self) -> None:
        """`@` は selector 側のプレフィックスなので label 値に含めない。"""
        assert not validate_label("@A").ok


# ============================================================
# instance_id / auto-label 生成
# ============================================================


class TestIdGeneration:
    def test_auto_label_includes_project_basename_and_pid_suffix(self) -> None:
        assert generate_auto_label("D:/work/myproject.qgz", 1234) == "myproject-1234"
        assert generate_auto_label("a.qgz", 56789) == "a-6789"  # 下4桁

    def test_auto_label_when_no_project(self) -> None:
        label = generate_auto_label(None, 1234)
        # project 不明時でも何らかの一意な文字列が返ること
        assert label
        assert "1234" in label

    def test_instance_id_is_opaque_and_unique(self) -> None:
        """ADR-0005 D1: 引数なし・nonce 形式・毎回ユニーク。"""
        a = generate_instance_id()
        b = generate_instance_id()
        assert a.startswith("w-")
        assert a != b
        assert len(a) > 4


# ============================================================
# Outcome 型の基本性質
# ============================================================


class TestOutcomeTypes:
    def test_register_outcome_ok(self) -> None:
        outcome = RegisterOutcome(
            ok=True,
            instance_id="worker-a-1234",
            assigned_label="A",
            resumed=False,
            error=None,
        )
        assert outcome.ok

    def test_resolve_outcome_error_holds_details(self) -> None:
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.register_worker("conn-2", _worker_req(label="B", pid=5678))
        result: ResolveOutcome = state.resolve_instance(None)
        assert not result.ok
        candidates = result.error.details["candidates"]
        assert len(candidates) == 2
        for c in candidates:
            assert "instance_id" in c
            assert "label" in c
