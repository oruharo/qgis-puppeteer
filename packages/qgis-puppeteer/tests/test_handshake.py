"""qgis_puppeteer.handshake のユニットテスト（Qt 非依存）。

挨拶：Hub が hello で nonce を送り、相手は Hub ファイルの token で作った proof を
register に載せる。Hub は ack に hub_proof を載せ、相手はそれを確かめる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from qgis_puppeteer._version import __version__
from qgis_puppeteer.handshake import (
    ack_is_trusted,
    check_register,
    choose_protocol_version,
    client_proof,
    generate_nonce,
    generate_token,
    hub_proof,
    proof_matches,
)
from qgis_puppeteer.protocol import (
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL,
    Error,
    ErrorCode,
    ProtocolRange,
    RegisterAck,
    RegisterRequest,
    Role,
)

REPO = Path(__file__).resolve().parents[3]


class TestSecrets:
    def test_tokens_and_nonces_are_fresh_and_url_safe(self) -> None:
        tokens = {generate_token() for _ in range(20)}
        nonces = {generate_nonce() for _ in range(20)}
        assert len(tokens) == 20
        assert len(nonces) == 20
        for value in tokens | nonces:
            assert re.fullmatch(r"[A-Za-z0-9_-]+", value)
        assert min(len(t) for t in tokens) >= 43  # 32 バイト


class TestProofs:
    def test_a_proof_depends_on_the_token_and_both_nonces(self) -> None:
        base = client_proof("tok", "hn", "cn")
        assert client_proof("tok", "hn", "cn") == base
        assert client_proof("other", "hn", "cn") != base
        assert client_proof("tok", "hn2", "cn") != base
        assert client_proof("tok", "hn", "cn2") != base

    def test_the_two_sides_prove_differently(self) -> None:
        """Hub が相手の proof をそのまま返しても hub_proof にならない（鏡写しにできない）。"""
        assert hub_proof("tok", "cn", "hn") != client_proof("tok", "hn", "cn")
        # nonce を入れ替えても同じにならない
        assert hub_proof("tok", "hn", "cn") != client_proof("tok", "hn", "cn")

    @pytest.mark.parametrize("given", [None, 1, b"abc", ""])
    def test_only_a_matching_string_matches(self, given: object) -> None:
        assert proof_matches("abc", "abc") is True
        assert proof_matches("abc", given) is False


class TestChooseProtocolVersion:
    def test_the_highest_version_both_speak(self) -> None:
        assert choose_protocol_version(ProtocolRange(1, 5), ProtocolRange(2, 3)) == 3
        assert choose_protocol_version(ProtocolRange(2, 2), ProtocolRange(1, 4)) == 2

    def test_no_common_version_sends_our_own_highest(self) -> None:
        """重ならなければ自分の最大を送り、Hub に版違いで断らせる。"""
        assert choose_protocol_version(ProtocolRange(5, 6), ProtocolRange(2, 3)) == 3
        assert choose_protocol_version(ProtocolRange(1, 1), ProtocolRange(2, 3)) == 3

    def test_the_default_is_this_version(self) -> None:
        assert choose_protocol_version(SUPPORTED_PROTOCOL) == PROTOCOL_VERSION


def _register(**fields: object) -> RegisterRequest:
    return RegisterRequest(id="r-1", role=Role.AUTOMATION_CLIENT, **fields)  # type: ignore[arg-type]


class TestCheckRegister:
    def test_a_good_proof_is_accepted(self) -> None:
        req = _register(nonce="cn", proof=client_proof("tok", "hn", "cn"))
        assert check_register(req, token="tok", hub_nonce="hn") is None

    @pytest.mark.parametrize(
        "fields",
        [
            {"nonce": "cn", "proof": client_proof("other", "hn", "cn")},
            {"nonce": "cn", "proof": client_proof("tok", "other-hn", "cn")},
            {"nonce": "cn", "proof": None},
            {"nonce": None, "proof": client_proof("tok", "hn", "")},
            {"nonce": "", "proof": client_proof("tok", "hn", "")},
        ],
    )
    def test_a_bad_proof_is_unauthorized(self, fields: dict[str, object]) -> None:
        error = check_register(_register(**fields), token="tok", hub_nonce="hn")
        assert error is not None
        assert error.code == ErrorCode.UNAUTHORIZED

    def test_the_version_is_checked_before_the_proof(self) -> None:
        """版 1 の古い相手（proof 無し）には「unauthorized」でなく「版違い」を返す。"""
        req = _register(protocol_version=1, package_version="0.0.9")
        error = check_register(
            req,
            token="tok",
            hub_nonce="hn",
            hub_range=ProtocolRange(2, 2),
            hub_package_version="0.1.0",
        )
        assert error is not None
        assert error.code == ErrorCode.PROTOCOL_VERSION_MISMATCH
        assert error.details == {
            "hub_protocol": {"min": 2, "max": 2},
            "hub_package_version": "0.1.0",
            "client_protocol_version": 1,
            "client_protocol": None,
            "client_package_version": "0.0.9",
        }
        assert "0.0.9" in error.message
        assert "same qgis-puppeteer version" in error.message

    def test_the_mismatch_carries_the_clients_range(self) -> None:
        req = _register(protocol_version=3, protocol=ProtocolRange(3, 4))
        error = check_register(req, token="tok", hub_nonce="hn", hub_range=ProtocolRange(2, 2))
        assert error is not None
        assert error.details["client_protocol"] == {"min": 3, "max": 4}
        assert error.details["hub_package_version"] == __version__


class TestAckIsTrusted:
    def _check(self, ack: RegisterAck) -> bool:
        return ack_is_trusted(ack, token="tok", client_nonce="cn", hub_nonce="hn")

    def test_an_ack_with_the_hub_proof(self) -> None:
        ack = RegisterAck(id="r", ok=True, instance_id="i", hub_proof=hub_proof("tok", "cn", "hn"))
        assert self._check(ack) is True

    @pytest.mark.parametrize(
        "proof",
        [None, "0" * 64, hub_proof("other", "cn", "hn"), client_proof("tok", "hn", "cn")],
    )
    def test_an_ok_without_the_hub_proof_is_not_trusted(self, proof: str | None) -> None:
        ack = RegisterAck(id="r", ok=True, instance_id="i", hub_proof=proof)
        assert self._check(ack) is False

    @pytest.mark.parametrize("code", [ErrorCode.UNAUTHORIZED, ErrorCode.PROTOCOL_VERSION_MISMATCH])
    def test_failures_before_the_hub_knows_us_need_no_proof(self, code: ErrorCode) -> None:
        ack = RegisterAck(id="r", ok=False, error=Error(code=code, message="x"))
        assert self._check(ack) is True

    @pytest.mark.parametrize("code", [ErrorCode.LAUNCH_ABANDONED, ErrorCode.LABEL_CONFLICT])
    def test_other_failures_need_the_proof(self, code: ErrorCode) -> None:
        """とくに launch_abandoned（QGIS を閉じる）を偽の Hub に言わせない。"""
        ack = RegisterAck(id="r", ok=False, error=Error(code=code, message="x"))
        assert self._check(ack) is False
        proven = RegisterAck(
            id="r",
            ok=False,
            error=Error(code=code, message="x"),
            hub_proof=hub_proof("tok", "cn", "hn"),
        )
        assert self._check(proven) is True


def _version_line(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"no version in {path}"
    return match.group(1)


@pytest.mark.parametrize(
    ("relative", "pattern"),
    [
        ("packages/qgis-puppeteer/pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
        ("packages/pytest-qgis-puppeteer/pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
        ("plugins/qgis_puppeteer/metadata.txt", r"^version\s*=\s*(\S+)"),
    ],
)
def test_the_package_version_matches_the_releases(relative: str, pattern: str) -> None:
    """Hub と相手が版を名乗るのに使う ``__version__`` は、配る物の版と同じ。"""
    assert _version_line(REPO / relative, pattern) == __version__


def test_the_plugin_copy_has_the_same_version() -> None:
    copy = REPO / "plugins" / "qgis_puppeteer" / "_version.py"
    assert f'__version__ = "{__version__}"' in copy.read_text(encoding="utf-8")
