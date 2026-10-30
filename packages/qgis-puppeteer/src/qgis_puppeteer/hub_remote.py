"""ネットワークから受け付ける Hub（``--listen``）の token・鍵・証明書と、TLS の照合。

ネットワーク用の Hub は、ローカル用（127.0.0.1、起動ごとに token が変わる）とは別に、
次のものを「リモートのフォルダ」（``default_remote_dir``）に一度だけ作って残す。
Hub を起こし直しても変わらないので、別の機械へ写した接続情報ファイルはそのまま
使える。作り直すのは Hub の ``--rotate`` を付けたときだけ。

- ``token``：ネットワーク用の token（ローカル用とは別）
- ``hub.key`` / ``hub.crt``：TLS の鍵と自己署名の証明書（``qgis_puppeteer.selfsigned``）
- ``hub.json``：別の機械へ写す接続情報ファイル（Hub が起きるたびに書く。pid は無い）

クライアントは CA による検証をせず、受け取った証明書の SHA-256 が接続情報ファイルの
``tls.fingerprint_sha256`` と一致するかだけを見る（pin）。一致しなければ挨拶の前に切る。

標準ライブラリだけで書く（QGIS の Python からも import する）。
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis_puppeteer.handshake import generate_token
from qgis_puppeteer.hub_file import HubFileError, HubFileInfo
from qgis_puppeteer.selfsigned import (
    fingerprint_sha256,
    generate_self_signed,
    pem_to_der,
)

logger = logging.getLogger("qgis_puppeteer.hub_remote")

_DIR_NAME = "qgis_puppeteer"
_REMOTE_DIR_NAME = "remote"
TOKEN_FILE = "token"
KEY_FILE = "hub.key"
CERT_FILE = "hub.crt"
REMOTE_HUB_FILE = "hub.json"
# Hub ファイルの ``tls`` の中の指紋の項目
FINGERPRINT_KEY = "fingerprint_sha256"


def default_remote_dir() -> Path:
    """ネットワーク用の token・鍵・証明書を置く既定のフォルダ。

    - Windows：``%LOCALAPPDATA%\\qgis_puppeteer\\remote``
    - POSIX：``$XDG_STATE_HOME/qgis_puppeteer/remote``。無ければ
      ``~/.local/state/qgis_puppeteer/remote``（ログアウトで消える
      ``$XDG_RUNTIME_DIR`` には置かない）
    """
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
    else:
        state = os.environ.get("XDG_STATE_HOME")
        base = Path(state) if state else Path.home() / ".local" / "state"
    return base / _DIR_NAME / _REMOTE_DIR_NAME


@dataclass(frozen=True)
class RemoteIdentity:
    """ネットワーク用の token と TLS の鍵・証明書。"""

    token: str
    key_pem: str
    cert_pem: str
    fingerprint_sha256: str


def _write_private(path: Path, text: str) -> None:
    """本人だけが読めるファイル（0600）に一度に書く。"""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _read_identity(directory: Path) -> RemoteIdentity:
    """残してある token・鍵・証明書を読む。

    Raises:
        OSError: どれかが無い・読めない
        ValueError: 中身が壊れている
    """
    token = (directory / TOKEN_FILE).read_text(encoding="utf-8").strip()
    key_pem = (directory / KEY_FILE).read_text(encoding="utf-8")
    cert_pem = (directory / CERT_FILE).read_text(encoding="utf-8")
    if not token:
        raise ValueError(f"{directory / TOKEN_FILE} is empty")
    pem_to_der(key_pem, "RSA PRIVATE KEY")
    cert_der = pem_to_der(cert_pem, "CERTIFICATE")
    return RemoteIdentity(
        token=token,
        key_pem=key_pem,
        cert_pem=cert_pem,
        fingerprint_sha256=fingerprint_sha256(cert_der),
    )


def load_or_create_remote_identity(
    directory: str | os.PathLike[str], *, rotate: bool = False
) -> RemoteIdentity:
    """``directory`` の token・鍵・証明書を読む。無い・壊れている・``rotate`` なら作る。

    作るときは 3 つとも作り直す（鍵だけ新しくすると、写したファイルの指紋と合わない）。
    フォルダが無ければ 0700 で作る。

    Raises:
        OSError: フォルダやファイルを作れない
    """
    target = Path(directory)
    if not rotate:
        try:
            return _read_identity(target)
        except FileNotFoundError:
            logger.info("No network identity in %s yet; creating one", target)
        except (OSError, ValueError) as e:
            logger.warning(
                "The network identity in %s is unusable (%s); creating a new one", target, e
            )
    else:
        logger.info("Rotating the network token and TLS key in %s", target)

    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    ident = generate_self_signed()
    token = generate_token()
    # 鍵 → 証明書 → token の順に書く。途中で落ちても、次に起きたとき読み合わせで
    # 指紋が変わったことは分かる（写したファイルを配り直す）
    _write_private(target / KEY_FILE, ident.key_pem)
    _write_private(target / CERT_FILE, ident.cert_pem)
    _write_private(target / TOKEN_FILE, token + "\n")
    return RemoteIdentity(
        token=token,
        key_pem=ident.key_pem,
        cert_pem=ident.cert_pem,
        fingerprint_sha256=ident.fingerprint_sha256,
    )


# ============================================================
# クライアント側：TLS の照合
# ============================================================


def normalize_fingerprint(value: str) -> str:
    """``"ab:cd..."`` / ``"ABCD..."`` を ``"AB:CD:..."`` にそろえる。

    Raises:
        ValueError: SHA-256 の 16 進（32 バイト）でない
    """
    hex_digits = value.replace(":", "").strip().upper()
    if len(hex_digits) != 64 or any(c not in "0123456789ABCDEF" for c in hex_digits):
        raise ValueError(f"not a SHA-256 fingerprint: {value!r}")
    return ":".join(hex_digits[i : i + 2] for i in range(0, 64, 2))


def pinned_fingerprint(info: HubFileInfo, path: str | os.PathLike[str]) -> str | None:
    """Hub ファイルの ``tls`` から、照合する証明書の指紋を取り出す（TLS で無ければ None）。

    Raises:
        HubFileError: ``tls`` はあるが、指紋が無い・形が違う（平文でつながずに止める）
    """
    tls: dict[str, Any] | None = info.tls
    if tls is None:
        return None
    raw = tls.get(FINGERPRINT_KEY)
    try:
        if not isinstance(raw, str):
            raise ValueError(f"no {FINGERPRINT_KEY}")
        return normalize_fingerprint(raw)
    except ValueError as e:
        raise HubFileError(
            f"The Hub file {path} asks for TLS but its tls.{FINGERPRINT_KEY} is unusable: {e}",
            path=Path(path),
        ) from e


def client_ssl_context() -> ssl.SSLContext:
    """ネットワーク用の Hub につなぐ TLS の設定。

    CA と名前は確かめない（自己署名なので）。代わりに、つないだあとで
    ``peer_matches`` で証明書の指紋を照合する。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def peer_matches(cert_der: bytes | None, expected: str) -> bool:
    """相手の証明書（DER）の指紋が ``expected`` と一致するか。"""
    if not cert_der:
        return False
    return hmac.compare_digest(fingerprint_sha256(cert_der), normalize_fingerprint(expected))


def fingerprint_mismatch_message(address: str, path: object = None) -> str:
    """指紋が合わなかったときのエラーの文。"""
    where = f" in {path}" if path is not None else ""
    return (
        f"The TLS certificate of {address} does not match the fingerprint{where}: it is "
        "not the Hub that wrote the file, or the Hub's key was rotated. Copy the "
        "Hub's remote hub.json again."
    )
