"""selfsigned のテスト：作った鍵と証明書を、OpenSSL（Python の ssl）が受け付けるか。

Qt（QSslKey / QSslCertificate）が読み込めて、Qt の TLS のサーバーとして実際に
つながることは ``test_hub_remote_integration``（Qt のテスト）で確かめる。
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import threading
from pathlib import Path

import pytest
from qgis_puppeteer.selfsigned import (
    _integer,
    _is_probable_prime,
    _oid,
    _rsa_sign_pkcs1v15_sha256,
    fingerprint_sha256,
    generate_rsa_key,
    pem,
    pem_to_der,
    rsa_private_key_der,
)

from tests._tls_helpers import server_ssl_context, tls_identity


def _write_pems(tmp_path: Path) -> tuple[Path, Path]:
    ident = tls_identity()
    cert = tmp_path / "hub.crt"
    key = tmp_path / "hub.key"
    cert.write_text(ident.cert_pem, encoding="utf-8")
    key.write_text(ident.key_pem, encoding="utf-8")
    return cert, key


# ============================================================
# DER の部品
# ============================================================


class TestDer:
    @pytest.mark.parametrize(
        ("value", "encoded"),
        [
            (0, "020100"),
            (127, "02017f"),
            # 先頭のビットが立つ数は 0x00 を足して正にする
            (128, "02020080"),
            (256, "02020100"),
        ],
    )
    def test_integers(self, value: int, encoded: str) -> None:
        assert _integer(value).hex() == encoded

    def test_oid_of_sha256_with_rsa(self) -> None:
        assert _oid("1.2.840.113549.1.1.11").hex() == "06092a864886f70d01010b"

    def test_pem_round_trips(self) -> None:
        der = bytes(range(200))
        text = pem("CERTIFICATE", der)
        assert all(len(line) <= 64 for line in text.splitlines())
        assert pem_to_der(text, "CERTIFICATE") == der

    def test_a_missing_pem_block_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="no PEM block"):
            pem_to_der("nothing here", "CERTIFICATE")


# ============================================================
# RSA
# ============================================================


class TestRsa:
    @pytest.mark.parametrize("n", [2, 3, 5, 7919, 2**61 - 1])
    def test_primes_are_primes(self, n: int) -> None:
        assert _is_probable_prime(n)

    # 561 は Carmichael 数（Fermat のテストはだまされる）
    @pytest.mark.parametrize("n", [0, 1, 4, 561, 7919 * 7907, (2**61 - 1) * (2**31 - 1)])
    def test_composites_are_not_primes(self, n: int) -> None:
        assert not _is_probable_prime(n)

    def test_a_small_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="1024"):
            generate_rsa_key(512)

    def test_the_key_is_consistent(self) -> None:
        key = generate_rsa_key(1024)
        assert key.n.bit_length() == 1024
        assert key.p * key.q == key.n
        assert key.e == 65537
        m = 0x1234567890ABCDEF
        assert pow(pow(m, key.e, key.n), key.d, key.n) == m

    def test_a_signature_verifies_with_the_public_key(self) -> None:
        key = generate_rsa_key(1024)
        signature = _rsa_sign_pkcs1v15_sha256(key, b"message")
        em = pow(int.from_bytes(signature, "big"), key.e, key.n).to_bytes(key.size_bytes, "big")
        assert em.startswith(b"\x00\x01\xff")
        assert em.endswith(hashlib.sha256(b"message").digest())

    def test_the_private_key_der_starts_as_pkcs1(self) -> None:
        key = generate_rsa_key(1024)
        der = rsa_private_key_der(key)
        assert der[0] == 0x30
        # version 0 の INTEGER が最初に来る
        assert b"\x02\x01\x00" in der[:8]


# ============================================================
# 証明書（OpenSSL が受け付けるか）
# ============================================================


class TestCertificate:
    def test_openssl_reads_the_certificate(self, tmp_path: Path) -> None:
        cert, _ = _write_pems(tmp_path)
        decoded = ssl._ssl._test_decode_cert(str(cert))  # type: ignore[attr-defined]
        assert decoded["version"] == 3
        assert decoded["subject"] == ((("commonName", "qgis-puppeteer Hub"),),)
        assert decoded["issuer"] == decoded["subject"]
        assert decoded["notAfter"] == "Dec 31 23:59:59 9999 GMT"

    def test_the_key_matches_the_certificate(self, tmp_path: Path) -> None:
        cert, key = _write_pems(tmp_path)
        # 鍵と証明書が対でなければ load_cert_chain が SSLError を出す
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)

    def test_the_fingerprint_is_the_sha256_of_the_der(self) -> None:
        ident = tls_identity()
        assert pem_to_der(ident.cert_pem, "CERTIFICATE") == ident.cert_der
        digest = hashlib.sha256(ident.cert_der).hexdigest().upper()
        assert ident.fingerprint_sha256.replace(":", "") == digest
        assert fingerprint_sha256(ident.cert_der) == ident.fingerprint_sha256

    def test_a_tls_connection_works_and_the_signature_verifies(self, tmp_path: Path) -> None:
        """自分を CA として信じさせて検証する：署名が正しくなければつながらない。"""
        cert, _ = _write_pems(tmp_path)
        server_ctx = server_ssl_context(tls_identity())
        with socket.create_server(("127.0.0.1", 0)) as listener:
            port = listener.getsockname()[1]

            def serve() -> None:
                conn, _ = listener.accept()
                with server_ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.sendall(b"ok")

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.check_hostname = False
            client_ctx.load_verify_locations(cert)
            with (
                socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
                client_ctx.wrap_socket(raw) as tls,
            ):
                assert tls.recv(2) == b"ok"
                assert (
                    fingerprint_sha256(tls.getpeercert(binary_form=True))
                    == tls_identity().fingerprint_sha256
                )
            thread.join(timeout=10)
