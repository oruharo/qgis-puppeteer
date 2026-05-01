"""qgis_puppeteer.protocol のユニットテスト。

ADR-0001 §3 のプロトコル仕様を満たすかを検証する。
エンコード → デコード のラウンドトリップと、
不正入力に対するエラー検出を中心にカバーする。
"""

from __future__ import annotations

import json

import pytest
from qgis_puppeteer.protocol import (
    PROTOCOL_VERSION,
    Bye,
    Error,
    ErrorCode,
    InstanceInfo,
    ListInstancesRequest,
    ListInstancesResponse,
    MessageType,
    ProtocolDecodeError,
    RegisterAck,
    RegisterRequest,
    Request,
    Response,
    Role,
    decode_message,
    encode_message,
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
                ),
            ],
        )
        assert decode_message(encode_message(msg)) == msg

    def test_bye(self) -> None:
        msg = Bye(id="uuid-14", instance_id="worker-a-1234")
        assert decode_message(encode_message(msg)) == msg


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

    def test_protocol_version_is_integer_one(self) -> None:
        """protocol_version は整数 1 固定。"""
        assert PROTOCOL_VERSION == 1

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
        with pytest.raises(ProtocolDecodeError, match="unknown type"):
            decode_message('{"type": "evil_type", "id": "x"}')

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

    def test_unknown_error_code(self) -> None:
        with pytest.raises(ProtocolDecodeError):
            decode_message(
                '{"type": "response", "id": "x", "ok": false,'
                ' "error": {"code": "made_up_code", "message": "x"}}'
            )


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
