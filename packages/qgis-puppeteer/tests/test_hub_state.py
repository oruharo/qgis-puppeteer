"""qgis_puppeteer.hub_state のユニットテスト。

ADR-0001 §4, §5, §9 の Hub 純ロジック仕様を検証する。
Qt 非依存部分（Worker 登録・再接続・selector 解決・idle 自動終了判定・
Origin 検証・label 検証）を `HubState` と検証ヘルパで網羅する。

Qt 配線側（QWebSocketServer の接続管理）は別テストで扱う。
"""

from __future__ import annotations

from qgis_puppeteer.hub_state import (
    GRACE_SECONDS,
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
) -> RegisterRequest:
    return RegisterRequest(
        id=id,
        role=Role.WORKER,
        pid=pid,
        label=label,
        project=project,
        previous_instance_id=previous_instance_id,
    )


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

    def test_instance_id_contains_label_and_pid(self) -> None:
        state = HubState(clock=FakeClock())
        outcome = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        assert "a" in outcome.instance_id.lower()
        assert "1234" in outcome.instance_id

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

    def test_duplicate_label_conflict(self) -> None:
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        r2 = state.register_worker("conn-2", _worker_req(label="A", pid=5678))
        assert r2.ok is False
        assert r2.error is not None
        assert r2.error.code == ErrorCode.LABEL_CONFLICT
        # suggested_label を提供する
        assert "suggested_label" in r2.error.details
        assert r2.error.details["suggested_label"] != "A"


# ============================================================
# Worker 再接続（instance_id 引き継ぎ）
# ============================================================


class TestRegisterWorkerResume:
    def test_resume_with_matching_pid_and_label(self) -> None:
        clock = FakeClock()
        state = HubState(clock=clock)
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        original_id = r1.instance_id

        # 切断（grace 保持）
        state.disconnect_worker("conn-1")

        # 再接続：previous_instance_id + pid + label 一致
        r2 = state.register_worker(
            "conn-2",
            _worker_req(label="A", pid=1234, previous_instance_id=original_id),
        )
        assert r2.ok is True
        assert r2.resumed is True
        assert r2.instance_id == original_id

    def test_resume_fails_when_previous_id_mismatch_gives_new_id(self) -> None:
        clock = FakeClock()
        state = HubState(clock=clock)
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.disconnect_worker("conn-1")

        # previous_instance_id が嘘 → 新規発行扱い
        r2 = state.register_worker(
            "conn-2",
            _worker_req(label="A", pid=1234, previous_instance_id="bogus-id"),
        )
        # 新規登録だが label は grace 中に予約されているため衝突
        assert r2.ok is False
        assert r2.error.code == ErrorCode.LABEL_CONFLICT

    def test_resume_after_grace_expires_is_new_registration(self) -> None:
        """grace 満了後は `resumed=False` で新規登録扱い。

        instance_id は label+pid 決定的なので同値になるが、`resumed` フラグが
        False になることで Hub 側では新規登録フローを通ったことがわかる。
        """
        clock = FakeClock()
        state = HubState(clock=clock)
        r1 = state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        original_id = r1.instance_id

        state.disconnect_worker("conn-1")
        clock.advance(GRACE_SECONDS + 1.0)
        state.sweep_expired()

        # grace 満了後は新規扱い（label も解放済み）
        r2 = state.register_worker(
            "conn-2",
            _worker_req(label="A", pid=1234, previous_instance_id=original_id),
        )
        assert r2.ok is True
        assert r2.resumed is False  # resume 経路ではなく新規登録
        # pid が異なれば instance_id も異なる：pid 違いで確認
        r3_different_pid = state.register_worker("conn-3", _worker_req(label="B", pid=9999))
        assert r3_different_pid.instance_id != r2.instance_id


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

    def test_grace_worker_does_not_prevent_idle_shutdown(self) -> None:
        """grace 中 Worker は active 扱いしない（ADR-0001 §4）。"""
        state = HubState(clock=FakeClock())
        state.register_worker("conn-1", _worker_req(label="A", pid=1234))
        state.disconnect_worker("conn-1")
        # grace 中でも active=0 なので idle 自動終了条件成立
        assert state.active_worker_count() == 0
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

    def test_at_prefix_matches_label(self) -> None:
        state = self._populate()
        result = state.resolve_instance("@A")
        assert result.ok
        assert "1234" in result.instance_id

    def test_plain_label_matches(self) -> None:
        state = self._populate()
        result = state.resolve_instance("B")
        assert result.ok
        assert "5678" in result.instance_id

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
        assert "1234" in result.instance_id

    def test_project_basename_matches_with_extension(self) -> None:
        state = self._populate()
        result = state.resolve_instance("beta.qgz")
        assert result.ok
        assert "5678" in result.instance_id

    def test_pid_string_matches(self) -> None:
        state = self._populate()
        result = state.resolve_instance("1234")
        assert result.ok
        assert "1234" in result.instance_id

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

    def test_instance_id_format(self) -> None:
        assert generate_instance_id("A", 1234) == "worker-a-1234"
        assert generate_instance_id("Alpha", 5678) == "worker-alpha-5678"


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
