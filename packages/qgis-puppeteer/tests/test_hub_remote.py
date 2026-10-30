"""hub_remote のテスト：ネットワーク用の token・鍵・証明書を残すこと、指紋の扱い。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from qgis_puppeteer.hub_file import HubFileError, HubFileInfo
from qgis_puppeteer.hub_remote import (
    CERT_FILE,
    KEY_FILE,
    TOKEN_FILE,
    default_remote_dir,
    load_or_create_remote_identity,
    normalize_fingerprint,
    peer_matches,
    pinned_fingerprint,
)
from qgis_puppeteer.selfsigned import pem_to_der

from tests._tls_helpers import tls_identity


class TestDefaultRemoteDir:
    @pytest.mark.skipif(os.name != "nt", reason="Windows の既定の場所")
    def test_windows_uses_localappdata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_remote_dir() == tmp_path / "qgis_puppeteer" / "remote"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX の既定の場所")
    def test_posix_uses_the_state_folder_not_the_runtime_folder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", "/home/someone/.state")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert default_remote_dir() == Path("/home/someone/.state/qgis_puppeteer/remote")


class TestLoadOrCreate:
    def test_the_first_call_creates_token_key_and_certificate(self, tmp_path: Path) -> None:
        directory = tmp_path / "remote"
        ident = load_or_create_remote_identity(directory)
        assert (directory / TOKEN_FILE).read_text(encoding="utf-8").strip() == ident.token
        assert (directory / KEY_FILE).read_text(encoding="utf-8") == ident.key_pem
        assert (directory / CERT_FILE).read_text(encoding="utf-8") == ident.cert_pem
        assert ident.fingerprint_sha256.count(":") == 31

    def test_the_identity_stays_across_restarts(self, tmp_path: Path) -> None:
        first = load_or_create_remote_identity(tmp_path)
        second = load_or_create_remote_identity(tmp_path)
        assert second == first

    def test_rotate_makes_a_new_token_and_key(self, tmp_path: Path) -> None:
        first = load_or_create_remote_identity(tmp_path)
        rotated = load_or_create_remote_identity(tmp_path, rotate=True)
        assert rotated.token != first.token
        assert rotated.fingerprint_sha256 != first.fingerprint_sha256
        assert load_or_create_remote_identity(tmp_path) == rotated

    def test_a_broken_certificate_makes_a_new_identity(self, tmp_path: Path) -> None:
        first = load_or_create_remote_identity(tmp_path)
        (tmp_path / CERT_FILE).write_text("not a certificate", encoding="utf-8")
        again = load_or_create_remote_identity(tmp_path)
        assert again.fingerprint_sha256 != first.fingerprint_sha256
        # 3 つとも作り直す（鍵と証明書が対のまま）
        assert again.token != first.token
        pem_to_der((tmp_path / CERT_FILE).read_text(encoding="utf-8"), "CERTIFICATE")

    def test_an_empty_token_makes_a_new_identity(self, tmp_path: Path) -> None:
        first = load_or_create_remote_identity(tmp_path)
        (tmp_path / TOKEN_FILE).write_text("\n", encoding="utf-8")
        assert load_or_create_remote_identity(tmp_path).token not in ("", first.token)


class TestFingerprint:
    def test_forms_are_normalized(self) -> None:
        expected = ":".join(["AB"] * 32)
        assert normalize_fingerprint("ab" * 32) == expected
        assert normalize_fingerprint(" " + ":".join(["ab"] * 32) + " ") == expected

    @pytest.mark.parametrize("value", ["", "AB:CD", "ZZ" * 32, "AB" * 33])
    def test_non_sha256_values_are_refused(self, value: str) -> None:
        with pytest.raises(ValueError, match="SHA-256"):
            normalize_fingerprint(value)

    def test_a_hub_file_without_tls_pins_nothing(self, tmp_path: Path) -> None:
        info = HubFileInfo(addresses=("127.0.0.1:1",), token="t")
        assert pinned_fingerprint(info, tmp_path / "hub.json") is None

    def test_the_pinned_fingerprint_comes_from_tls(self, tmp_path: Path) -> None:
        info = HubFileInfo(
            addresses=("127.0.0.1:1",), token="t", tls={"fingerprint_sha256": "ab" * 32}
        )
        assert pinned_fingerprint(info, tmp_path / "hub.json") == ":".join(["AB"] * 32)

    @pytest.mark.parametrize("tls", [{}, {"fingerprint_sha256": 1}, {"fingerprint_sha256": "x"}])
    def test_an_unusable_tls_entry_stops_instead_of_going_plain(
        self, tmp_path: Path, tls: dict[str, object]
    ) -> None:
        info = HubFileInfo(addresses=("127.0.0.1:1",), token="t", tls=tls)
        with pytest.raises(HubFileError, match="asks for TLS"):
            pinned_fingerprint(info, tmp_path / "hub.json")

    def test_peer_matches_only_the_same_certificate(self) -> None:
        mine = tls_identity()
        other = tls_identity(1)
        assert peer_matches(mine.cert_der, mine.fingerprint_sha256)
        assert peer_matches(mine.cert_der, mine.fingerprint_sha256.lower().replace(":", ""))
        assert not peer_matches(other.cert_der, mine.fingerprint_sha256)
        assert not peer_matches(None, mine.fingerprint_sha256)
