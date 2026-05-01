"""qgis_puppeteer.worker_state のユニットテスト。

ADR-0001 §4 "Worker 側" ロジックを Qt 非依存で検証する。
register メッセージ構築、Request → Response 変換、切断時の
instance_id 引き継ぎ保全を網羅する。
"""

from __future__ import annotations

import pytest
from qgis_puppeteer.protocol import (
    Error,
    ErrorCode,
    RegisterAck,
    Request,
    Role,
)
from qgis_puppeteer.worker_state import (
    ENV_ALLOW_TEST_HANDLERS,
    HandlerAlreadyRegisteredError,
    RegistrationDeniedError,
    ReservedNamespaceError,
    WorkerConfig,
    WorkerRegisterError,
    WorkerState,
)

# ============================================================
# build_register
# ============================================================


class TestBuildRegister:
    def test_fills_config_fields(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1234, label="alpha", project="D:/a.qgz"))
        msg = state.build_register("msg-1")
        assert msg.id == "msg-1"
        assert msg.role is Role.WORKER
        assert msg.pid == 1234
        assert msg.label == "alpha"
        assert msg.project == "D:/a.qgz"
        assert msg.previous_instance_id is None

    def test_includes_previous_instance_id_when_set(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1, label="l"))
        state.previous_instance_id = "worker-l-1"
        msg = state.build_register("msg-2")
        assert msg.previous_instance_id == "worker-l-1"

    def test_label_and_project_optional(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        msg = state.build_register("m")
        assert msg.label is None
        assert msg.project is None


# ============================================================
# on_register_ack
# ============================================================


class TestOnRegisterAck:
    def test_success_sets_instance_id(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        ack = RegisterAck(id="r-1", ok=True, instance_id="worker-l-1")
        state.on_register_ack(ack)
        assert state.instance_id == "worker-l-1"
        assert state.is_registered is True

    def test_success_clears_previous_instance_id(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.previous_instance_id = "worker-old-1"
        ack = RegisterAck(id="r-1", ok=True, instance_id="worker-new-1")
        state.on_register_ack(ack)
        assert state.previous_instance_id is None

    def test_failure_raises(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        ack = RegisterAck(
            id="r-1",
            ok=False,
            error=Error(code=ErrorCode.LABEL_CONFLICT, message="taken"),
        )
        with pytest.raises(WorkerRegisterError) as exc_info:
            state.on_register_ack(ack)
        assert exc_info.value.error is not None
        assert exc_info.value.error.code == ErrorCode.LABEL_CONFLICT
        assert state.instance_id is None


# ============================================================
# on_request（ハンドラ dispatch）
# ============================================================


class TestOnRequest:
    def test_dispatches_to_registered_handler(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_handler("ping", lambda params: {"pong": params})

        req = Request(id="req-1", command="ping", params={"v": 42})
        resp = state.on_request(req)

        assert resp.ok is True
        assert resp.id == "req-1"
        assert resp.result == {"pong": {"v": 42}}
        assert resp.error is None

    def test_unknown_command_returns_invalid_command(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        req = Request(id="req-1", command="nonexistent")
        resp = state.on_request(req)

        assert resp.ok is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.INVALID_COMMAND
        assert resp.error.details == {"command": "nonexistent"}

    def test_handler_exception_becomes_worker_execution_error(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))

        def boom(_: dict[str, object]) -> object:
            raise ValueError("something went wrong")

        state.register_handler("broken", boom)
        req = Request(id="req-1", command="broken")
        resp = state.on_request(req)

        assert resp.ok is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.WORKER_EXECUTION_ERROR
        assert "something went wrong" in resp.error.message
        assert resp.error.details["type"] == "ValueError"

    def test_response_id_matches_request_id(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_handler("cmd", lambda _: None)
        req = Request(id="unique-id-xyz", command="cmd")
        resp = state.on_request(req)
        assert resp.id == "unique-id-xyz"

    def test_unregister_handler_restores_not_found(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_handler("cmd", lambda _: "ok")
        state.unregister_handler("cmd")
        resp = state.on_request(Request(id="r", command="cmd"))
        assert resp.ok is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.INVALID_COMMAND

    def test_unregister_unknown_command_is_noop(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        # 例外が出ないことを確認
        state.unregister_handler("never-registered")

    def test_register_handler_overwrite_requires_flag(self) -> None:
        """既定では同名再登録は拒否、allow_overwrite=True で上書き可能（ADR-0001 §12.4）。"""
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_handler("sample.cmd", lambda _: "v1")
        resp = state.on_request(Request(id="r", command="sample.cmd"))
        assert resp.result == "v1"
        state.register_handler("sample.cmd", lambda _: "v2", allow_overwrite=True)
        resp = state.on_request(Request(id="r", command="sample.cmd"))
        assert resp.result == "v2"


# ============================================================
# register_core_handler / register_handler の namespace 検証
# ADR-0001 §12.4
# ============================================================


class TestHandlerNamespaceValidation:
    def test_register_core_handler_requires_qgis_prefix(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        with pytest.raises(ReservedNamespaceError):
            state.register_core_handler("sample.cmd", lambda _: None)

    def test_register_core_handler_accepts_qgis_prefix(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_core_handler("qgis_list_layers", lambda _: "ok")
        resp = state.on_request(Request(id="r", command="qgis_list_layers"))
        assert resp.result == "ok"

    def test_register_handler_rejects_reserved_qgis_prefix(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        with pytest.raises(ReservedNamespaceError):
            state.register_handler("qgis_custom", lambda _: None)

    def test_register_handler_allows_non_reserved_prefix(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_handler("sample.foo_action", lambda _: "ok")

    def test_register_handler_rejects_test_prefix_without_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_ALLOW_TEST_HANDLERS, raising=False)
        state = WorkerState(config=WorkerConfig(pid=1))
        with pytest.raises(RegistrationDeniedError):
            state.register_handler("test.reset_db", lambda _: None)

    def test_register_handler_accepts_test_prefix_with_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_ALLOW_TEST_HANDLERS, "1")
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_handler("test.reset_db", lambda _: "ok")

    def test_register_handler_rejects_duplicate_without_overwrite(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_handler("sample.cmd", lambda _: None)
        with pytest.raises(HandlerAlreadyRegisteredError):
            state.register_handler("sample.cmd", lambda _: None)

    def test_register_core_handler_rejects_duplicate_without_overwrite(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_core_handler("qgis_cmd", lambda _: None)
        with pytest.raises(HandlerAlreadyRegisteredError):
            state.register_core_handler("qgis_cmd", lambda _: None)


# ============================================================
# caller_role による Tier 3 可視性（ADR-0001 §12.7）
# ============================================================


class TestCallerRoleVisibility:
    @pytest.fixture
    def state_with_test_handler(self, monkeypatch: pytest.MonkeyPatch) -> WorkerState:
        monkeypatch.setenv(ENV_ALLOW_TEST_HANDLERS, "1")
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_handler("test.reset_db", lambda _: "ok")
        return state

    def test_test_handler_accessible_to_automation_client(
        self, state_with_test_handler: WorkerState
    ) -> None:
        resp = state_with_test_handler.on_request(
            Request(
                id="r",
                command="test.reset_db",
                caller_role=Role.AUTOMATION_CLIENT,
            )
        )
        assert resp.ok
        assert resp.result == "ok"

    def test_test_handler_hidden_from_mcp_gateway(
        self, state_with_test_handler: WorkerState
    ) -> None:
        resp = state_with_test_handler.on_request(
            Request(
                id="r",
                command="test.reset_db",
                caller_role=Role.MCP_GATEWAY,
            )
        )
        assert not resp.ok
        assert resp.error is not None
        assert resp.error.code == ErrorCode.HANDLER_NOT_VISIBLE

    def test_test_handler_hidden_when_caller_role_missing(
        self, state_with_test_handler: WorkerState
    ) -> None:
        """caller_role が付与されていない request（不正 Hub 等）は拒否。"""
        resp = state_with_test_handler.on_request(Request(id="r", command="test.reset_db"))
        assert not resp.ok
        assert resp.error is not None
        assert resp.error.code == ErrorCode.HANDLER_NOT_VISIBLE

    def test_non_test_handler_unaffected_by_caller_role(self) -> None:
        """`test.*` 以外は caller_role の影響を受けない。"""
        state = WorkerState(config=WorkerConfig(pid=1))
        state.register_handler("sample.cmd", lambda _: "ok")
        resp = state.on_request(
            Request(
                id="r",
                command="sample.cmd",
                caller_role=Role.MCP_GATEWAY,
            )
        )
        assert resp.ok
        assert resp.result == "ok"


# ============================================================
# on_disconnect / build_bye
# ============================================================


class TestDisconnect:
    def test_disconnect_moves_instance_id_to_previous(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.on_register_ack(RegisterAck(id="r", ok=True, instance_id="iid-1"))
        state.on_disconnect()
        assert state.instance_id is None
        assert state.previous_instance_id == "iid-1"

    def test_disconnect_without_registration_is_noop(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.on_disconnect()
        assert state.instance_id is None
        assert state.previous_instance_id is None

    def test_next_register_includes_previous_instance_id(self) -> None:
        """切断後の再 register で resume が試みられることを確認。"""
        state = WorkerState(config=WorkerConfig(pid=1, label="l"))
        state.on_register_ack(RegisterAck(id="r", ok=True, instance_id="iid-1"))
        state.on_disconnect()
        msg = state.build_register("r2")
        assert msg.previous_instance_id == "iid-1"

    def test_multiple_disconnects_preserve_last_known_id(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.on_register_ack(RegisterAck(id="r", ok=True, instance_id="iid-1"))
        state.on_disconnect()
        state.on_disconnect()  # 2 回目は instance_id が None なので noop
        assert state.previous_instance_id == "iid-1"


class TestBuildBye:
    def test_bye_built_when_registered(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.on_register_ack(RegisterAck(id="r", ok=True, instance_id="iid-1"))
        bye = state.build_bye("bye-1")
        assert bye is not None
        assert bye.id == "bye-1"
        assert bye.instance_id == "iid-1"

    def test_bye_is_none_when_unregistered(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        assert state.build_bye("bye-1") is None


# ============================================================
# is_registered プロパティ
# ============================================================


class TestIsRegistered:
    def test_false_initially(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        assert state.is_registered is False

    def test_true_after_register_ack(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.on_register_ack(RegisterAck(id="r", ok=True, instance_id="iid"))
        assert state.is_registered is True

    def test_false_after_disconnect(self) -> None:
        state = WorkerState(config=WorkerConfig(pid=1))
        state.on_register_ack(RegisterAck(id="r", ok=True, instance_id="iid"))
        state.on_disconnect()
        assert state.is_registered is False
