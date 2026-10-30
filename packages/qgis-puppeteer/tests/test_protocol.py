"""qgis_puppeteer.protocol のユニットテスト。

ADR-0001 §3 のプロトコル仕様を満たすかを検証する。
エンコード → デコード のラウンドトリップと、
不正入力に対するエラー検出を中心にカバーする。
"""

from __future__ import annotations

import json

import pytest
from qgis_puppeteer.protocol import (
    DEFAULT_REQUEST_TIMEOUT_MS,
    MAX_REQUEST_TIMEOUT_MS,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL,
    AbandonLaunch,
    Bye,
    Error,
    ErrorCode,
    Hello,
    InstanceInfo,
    ListInstancesRequest,
    ListInstancesResponse,
    MessageType,
    ProtocolDecodeError,
    ProtocolRange,
    RegisterAck,
    RegisterRequest,
    RejectedRegistration,
    Request,
    Response,
    Role,
    UnknownMessageTypeError,
    UpdateInfo,
    decode_message,
    effective_timeout_ms,
    encode_message,
    error_code_value,
    reply_to_undecodable,
)

# ============================================================
# ラウンドトリップテスト（encode → decode で同値復元）
# ============================================================


class TestRoundTrip:
    """dataclass → JSON → dataclass で元と等しく復元されることを検証。"""

    def test_register_request_worker_minimal(self) -> None:
        msg = RegisterRequest(
            id="uuid-1",
            role=Role.WORKER,
            pid=1234,
            label="A",
            project="D:/work/a.qgz",
        )
        assert decode_message(encode_message(msg)) == msg

    def test_register_request_worker_with_resume(self) -> None:
        msg = RegisterRequest(
            id="uuid-2",
            role=Role.WORKER,
            pid=1234,
            label="A",
            project="D:/work/a.qgz",
            previous_instance_id="worker-a-1234",
            started_at="2026-04-23T09:00:00Z",
        )
        assert decode_message(encode_message(msg)) == msg

    def test_register_request_mcp_gateway(self) -> None:
        msg = RegisterRequest(id="uuid-3", role=Role.MCP_GATEWAY)
        assert decode_message(encode_message(msg)) == msg

    def test_register_request_automation_client(self) -> None:
        msg = RegisterRequest(id="uuid-4", role=Role.AUTOMATION_CLIENT)
        assert decode_message(encode_message(msg)) == msg

    def test_register_ack_success(self) -> None:
        msg = RegisterAck(id="uuid-5", ok=True, instance_id="worker-a-1234")
        assert decode_message(encode_message(msg)) == msg

    def test_register_ack_resumed(self) -> None:
        msg = RegisterAck(
            id="uuid-6",
            ok=True,
            instance_id="worker-a-1234",
            resumed=True,
        )
        assert decode_message(encode_message(msg)) == msg

    def test_register_ack_label_conflict(self) -> None:
        msg = RegisterAck(
            id="uuid-7",
            ok=False,
            error=Error(
                code=ErrorCode.LABEL_CONFLICT,
                message="Label 'A' is already in use",
                details={"suggested_label": "A (2)"},
            ),
        )
        assert decode_message(encode_message(msg)) == msg

    def test_request_minimal(self) -> None:
        msg = Request(id="uuid-8", command="qgis_list_layers")
        assert decode_message(encode_message(msg)) == msg

    def test_request_with_instance_and_params(self) -> None:
        msg = Request(
            id="uuid-9",
            command="qgis_click_widget",
            instance="A",
            params={"selector": {"object_name": "ok_button"}},
            timeout_ms=5000,
        )
        assert decode_message(encode_message(msg)) == msg

    def test_request_roundtrip_with_caller_role(self) -> None:
        """Hub が付与した caller_role がデコード往復で保持される（ADR-0001 §12.7）。"""
        msg = Request(
            id="uuid-cr",
            command="test.reset_db",
            caller_role=Role.AUTOMATION_CLIENT,
        )
        decoded = decode_message(encode_message(msg))
        assert decoded == msg
        assert isinstance(decoded, Request)
        assert decoded.caller_role is Role.AUTOMATION_CLIENT

    def test_request_keeps_the_deadline_the_hub_set(self) -> None:
        msg = Request(id="uuid-dl", command="qgis_ping", timeout_ms=5000, deadline=1_700_000_000.25)
        decoded = decode_message(encode_message(msg))
        assert decoded == msg
        assert isinstance(decoded, Request)
        assert decoded.deadline == 1_700_000_000.25

    def test_response_success(self) -> None:
        msg = Response(
            id="uuid-10",
            ok=True,
            result={"layers": ["layer1", "layer2"]},
        )
        assert decode_message(encode_message(msg)) == msg

    def test_response_error_instance_ambiguous(self) -> None:
        msg = Response(
            id="uuid-11",
            ok=False,
            error=Error(
                code=ErrorCode.INSTANCE_AMBIGUOUS,
                message="Multiple instances match",
                details={
                    "candidates": [
                        {"instance_id": "worker-a-1234", "label": "A"},
                        {"instance_id": "worker-b-5678", "label": "B"},
                    ]
                },
            ),
        )
        assert decode_message(encode_message(msg)) == msg

    def test_list_instances_request(self) -> None:
        msg = ListInstancesRequest(id="uuid-12")
        assert decode_message(encode_message(msg)) == msg
        full = ListInstancesRequest(id="uuid-12b", include_disconnected=True)
        assert decode_message(encode_message(full)) == full
        # 旧 client の payload（フラグ無し）は既定 False
        old = decode_message('{"type": "list_instances", "id": "uuid-12c"}')
        assert isinstance(old, ListInstancesRequest)
        assert old.include_disconnected is False

    def test_update_info(self) -> None:
        msg = UpdateInfo(id="uuid-15", instance_id="w-abc", project="D:/a.qgz")
        assert decode_message(encode_message(msg)) == msg
        cleared = UpdateInfo(id="uuid-16", instance_id="w-abc", project=None)
        assert decode_message(encode_message(cleared)) == cleared

    def test_list_instances_response(self) -> None:
        msg = ListInstancesResponse(
            id="uuid-13",
            instances=[
                InstanceInfo(
                    instance_id="worker-a-1234",
                    label="A",
                    pid=1234,
                    project="a.qgz",
                ),
                InstanceInfo(
                    instance_id="worker-b-5678",
                    label="B",
                    pid=5678,
                    project=None,
                    state="disconnected",
                    last_seen_ago=47.2,
                    grace_expires_in=12.8,
                ),
            ],
        )
        assert decode_message(encode_message(msg)) == msg

    def test_list_instances_response_from_old_hub_defaults_to_active(self) -> None:
        """state / last_seen_ago を知らない Hub からの payload は active 扱い。"""
        raw = (
            '{"type": "list_instances_response", "id": "uuid-13b", "instances": '
            '[{"instance_id": "worker-a-1", "label": "A", "pid": 1}]}'
        )
        msg = decode_message(raw)
        assert isinstance(msg, ListInstancesResponse)
        assert msg.instances[0].state == "active"
        assert msg.instances[0].last_seen_ago is None

    def test_rejected_registrations_round_trip(self) -> None:
        """#13: 断った登録は include_rejected で頼んだときに rejected に載る。"""
        req = ListInstancesRequest(id="uuid-13c", include_rejected=True)
        assert decode_message(encode_message(req)) == req
        msg = ListInstancesResponse(
            id="uuid-13d",
            rejected=[
                RejectedRegistration(
                    launch_token="lt-abc",
                    pid=4321,
                    label="x" * 200,
                    code="label_conflict",
                    message="Invalid label: too_long",
                    rejected_ago=1.5,
                )
            ],
        )
        assert decode_message(encode_message(msg)) == msg

    def test_source_fingerprint_round_trip(self) -> None:
        """#17: Worker の指紋は登録で送られ、一覧で返る。旧形式では None。"""
        reg = RegisterRequest(id="r", role=Role.WORKER, pid=1, label="A", source_fingerprint="ab12")
        assert decode_message(encode_message(reg)) == reg
        resp = ListInstancesResponse(
            id="l",
            instances=[InstanceInfo(instance_id="w", label="A", pid=1, source_fingerprint="ab12")],
        )
        assert decode_message(encode_message(resp)) == resp
        old = decode_message(
            '{"type": "list_instances_response", "id": "o", "instances": '
            '[{"instance_id": "w", "label": "A", "pid": 1}]}'
        )
        assert isinstance(old, ListInstancesResponse)
        assert old.instances[0].source_fingerprint is None

    def test_abandon_launch_round_trip(self) -> None:
        msg = AbandonLaunch(id="uuid-ab", launch_token="lt-late")
        assert decode_message(encode_message(msg)) == msg

    def test_old_hub_response_has_no_rejected(self) -> None:
        raw = '{"type": "list_instances_response", "id": "uuid-13e", "instances": []}'
        msg = decode_message(raw)
        assert isinstance(msg, ListInstancesResponse)
        assert msg.rejected == []

    def test_bye(self) -> None:
        msg = Bye(id="uuid-14", instance_id="worker-a-1234")
        assert decode_message(encode_message(msg)) == msg

    def test_hello_round_trip_has_no_id(self) -> None:
        """hello は応答を待たないので id を持たない。"""
        msg = Hello(hub_version="0.1.0", protocol=ProtocolRange(2, 3), nonce="hn")
        wire = json.loads(encode_message(msg))
        assert wire == {
            "type": "hello",
            "hub_version": "0.1.0",
            "protocol": {"min": 2, "max": 3},
            "nonce": "hn",
        }
        assert decode_message(encode_message(msg)) == msg

    @pytest.mark.parametrize(
        "protocol", [None, {"min": 2}, {"min": "2", "max": 2}, {"min": True, "max": 2}]
    )
    def test_hello_needs_a_protocol_range(self, protocol: object) -> None:
        raw = json.dumps({"type": "hello", "hub_version": "x", "protocol": protocol, "nonce": "n"})
        with pytest.raises(ProtocolDecodeError):
            decode_message(raw)

    def test_register_request_carries_the_handshake(self) -> None:
        msg = RegisterRequest(
            id="uuid-15",
            role=Role.WORKER,
            pid=1,
            protocol_version=2,
            protocol=ProtocolRange(2, 2),
            package_version="0.1.0",
            nonce="cn",
            proof="p",
        )
        assert decode_message(encode_message(msg)) == msg

    def test_register_ack_carries_the_hub_proof_and_versions(self) -> None:
        msg = RegisterAck(
            id="uuid-16",
            ok=True,
            instance_id="w",
            protocol_version=2,
            package_version="0.1.0",
            hub_proof="hp",
        )
        assert decode_message(encode_message(msg)) == msg

    def test_instance_info_carries_the_package_version(self) -> None:
        resp = ListInstancesResponse(
            id="l",
            instances=[InstanceInfo(instance_id="w", label="A", pid=1, package_version="0.1.0")],
        )
        assert decode_message(encode_message(resp)) == resp


# ============================================================
# JSON 形式の検証（仕様準拠）
# ============================================================


class TestWireFormat:
    """エンコードされた JSON が ADR-0001 §3 の形式に一致するか。"""

    def test_encoded_register_includes_type_and_role_as_string(self) -> None:
        msg = RegisterRequest(id="uuid-1", role=Role.WORKER, pid=1, label="A")
        encoded = json.loads(encode_message(msg))
        assert encoded["type"] == "register"
        assert encoded["role"] == "worker"
        assert encoded["protocol_version"] == PROTOCOL_VERSION

    def test_encoded_request_type(self) -> None:
        msg = Request(id="uuid-x", command="cmd")
        encoded = json.loads(encode_message(msg))
        assert encoded["type"] == "request"

    def test_encoded_response_error_code_is_string(self) -> None:
        msg = Response(
            id="uuid-x",
            ok=False,
            error=Error(
                code=ErrorCode.INSTANCE_NOT_FOUND,
                message="not found",
            ),
        )
        encoded = json.loads(encode_message(msg))
        assert encoded["error"]["code"] == "instance_not_found"

    def test_protocol_version_is_two(self) -> None:
        """版 2 で挨拶（hello と proof）が要るようになった。版 1 とは互換を取らない。"""
        assert PROTOCOL_VERSION == 2
        assert PROTOCOL_VERSION in SUPPORTED_PROTOCOL
        assert 1 not in SUPPORTED_PROTOCOL

    def test_encoded_japanese_label_survives_roundtrip(self) -> None:
        """日本語ラベルが UTF-8 で保持されること（ensure_ascii=False 効果）。"""
        msg = RegisterRequest(
            id="uuid-jp",
            role=Role.WORKER,
            pid=1,
            label="足立区プロジェクト",
        )
        text = encode_message(msg)
        # エスケープされていない（ASCII escape でない）
        assert "足立区プロジェクト" in text
        assert decode_message(text).label == "足立区プロジェクト"


# ============================================================
# デコードエラー検出
# ============================================================


class TestDecodeErrors:
    """不正な入力が ProtocolDecodeError として検出されること。"""

    def test_invalid_json(self) -> None:
        with pytest.raises(ProtocolDecodeError, match="invalid JSON"):
            decode_message("{not valid json")

    def test_missing_type_field(self) -> None:
        with pytest.raises(ProtocolDecodeError, match="missing 'type'"):
            decode_message('{"id": "x"}')

    def test_unknown_type(self) -> None:
        with pytest.raises(UnknownMessageTypeError, match="unknown type") as ei:
            decode_message('{"type": "evil_type", "id": "x"}')
        assert (ei.value.message_id, ei.value.message_type) == ("x", "evil_type")

    def test_unhashable_type_is_an_unknown_type(self) -> None:
        with pytest.raises(UnknownMessageTypeError):
            decode_message('{"type": ["request"], "id": "x"}')

    def test_broken_message_keeps_its_id(self) -> None:
        """壊れていても id が読めれば、受け手がその id で答えられる。"""
        with pytest.raises(ProtocolDecodeError) as ei:
            decode_message('{"type": "request", "id": "r-1"}')
        assert not isinstance(ei.value, UnknownMessageTypeError)
        assert (ei.value.message_id, ei.value.message_type) == ("r-1", "request")

    def test_non_string_id_is_not_kept(self) -> None:
        with pytest.raises(ProtocolDecodeError) as ei:
            decode_message('{"type": "evil_type", "id": 7}')
        assert ei.value.message_id is None

    def test_top_level_is_not_object(self) -> None:
        with pytest.raises(ProtocolDecodeError, match="expected JSON object"):
            decode_message("[]")

    def test_missing_required_field_in_register(self) -> None:
        """register の必須フィールド `role` が欠けているとエラー。"""
        with pytest.raises(ProtocolDecodeError):
            decode_message('{"type": "register", "id": "x"}')

    def test_unknown_role(self) -> None:
        with pytest.raises(ProtocolDecodeError):
            decode_message('{"type": "register", "id": "x", "role": "intruder"}')


class TestTolerantDecoding:
    """新しい版の相手が足したものを、読み捨てずに受け取る。"""

    def test_unknown_error_code_is_kept_as_a_string(self) -> None:
        msg = decode_message(
            '{"type": "response", "id": "x", "ok": false,'
            ' "error": {"code": "made_up_code", "message": "m", "details": {"a": 1}}}'
        )
        assert isinstance(msg, Response)
        assert msg.error == Error(code="made_up_code", message="m", details={"a": 1})
        assert error_code_value(msg.error.code) == "made_up_code"

    def test_known_error_code_is_an_error_code(self) -> None:
        msg = decode_message(
            '{"type": "response", "id": "x", "ok": false,'
            ' "error": {"code": "instance_timeout", "message": "m"}}'
        )
        assert isinstance(msg, Response)
        assert msg.error is not None
        assert msg.error.code is ErrorCode.INSTANCE_TIMEOUT
        assert error_code_value(msg.error.code) == "instance_timeout"

    def test_unknown_fields_are_ignored(self) -> None:
        msg = decode_message(
            '{"type": "request", "id": "x", "command": "c", "added_later": {"k": 1}}'
        )
        assert msg == Request(id="x", command="c")


class TestReplyToUndecodable:
    """読めなかったメッセージへの返事（Hub と Worker が使う）。"""

    @staticmethod
    def _error(text: str) -> ProtocolDecodeError:
        with pytest.raises(ProtocolDecodeError) as ei:
            decode_message(text)
        return ei.value

    def test_unknown_type_with_id_gets_unsupported_message(self) -> None:
        reply = reply_to_undecodable(
            self._error('{"type": "added_later", "id": "q-1"}'), receiver="Hub"
        )
        assert reply is not None
        assert (reply.id, reply.ok) == ("q-1", False)
        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNSUPPORTED_MESSAGE
        assert reply.error.details == {"type": "added_later"}
        assert reply.error.message.startswith("Hub ")

    def test_broken_request_with_id_gets_invalid_command(self) -> None:
        reply = reply_to_undecodable(
            self._error('{"type": "request", "id": "q-2"}'), receiver="Hub"
        )
        assert reply is not None
        assert reply.id == "q-2"
        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_COMMAND

    @pytest.mark.parametrize(
        "text",
        [
            '{"type": "added_later"}',
            "{not json",
            '{"type": "response", "id": "q-3"}',
            '{"type": "register_ack", "id": "q-4"}',
            '{"type": "list_instances_response", "id": "q-5", "instances": [{}]}',
        ],
    )
    def test_no_reply_without_an_id_or_to_a_reply(self, text: str) -> None:
        assert reply_to_undecodable(self._error(text), receiver="QGIS") is None


# ============================================================
# 不変性の検証
# ============================================================


class TestImmutability:
    """frozen=True で直接代入できないこと（Python dataclass の仕様確認）。"""

    def test_request_is_frozen(self) -> None:
        msg = Request(id="x", command="cmd")
        with pytest.raises(Exception):  # FrozenInstanceError は dataclasses 内部型
            msg.command = "hijacked"  # type: ignore[misc]

    def test_response_is_frozen(self) -> None:
        msg = Response(id="x", ok=True)
        with pytest.raises(Exception):
            msg.ok = False  # type: ignore[misc]


# ============================================================
# type property の正当性
# ============================================================


class TestTypeProperty:
    """各 dataclass が正しい MessageType property を返すこと。"""

    @pytest.mark.parametrize(
        "message, expected",
        [
            (
                RegisterRequest(id="x", role=Role.WORKER),
                MessageType.REGISTER,
            ),
            (RegisterAck(id="x", ok=True), MessageType.REGISTER_ACK),
            (Request(id="x", command="c"), MessageType.REQUEST),
            (Response(id="x", ok=True), MessageType.RESPONSE),
            (ListInstancesRequest(id="x"), MessageType.LIST_INSTANCES),
            (
                ListInstancesResponse(id="x"),
                MessageType.LIST_INSTANCES_RESPONSE,
            ),
            (Bye(id="x", instance_id="i"), MessageType.BYE),
        ],
    )
    def test_type_property(self, message, expected: MessageType) -> None:
        assert message.type == expected


class TestEffectiveTimeoutMs:
    """Hub が待つ時間の決め方。"""

    @pytest.mark.parametrize("raw", [None, "5000", True, [1], float("nan")])
    def test_missing_or_not_a_number_uses_the_default(self, raw: object) -> None:
        assert effective_timeout_ms(raw) == DEFAULT_REQUEST_TIMEOUT_MS

    def test_a_number_is_used_as_is(self) -> None:
        assert effective_timeout_ms(5000) == 5000
        assert effective_timeout_ms(1500.7) == 1500

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, 1), (-10, 1), (2**40, MAX_REQUEST_TIMEOUT_MS), (float("inf"), MAX_REQUEST_TIMEOUT_MS)],
    )
    def test_out_of_range_is_clamped(self, raw: float, expected: int) -> None:
        assert effective_timeout_ms(raw) == expected
