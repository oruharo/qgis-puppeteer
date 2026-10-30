"""挨拶：Hub とつなぐ側が、同じ token を知っていることと版を確かめ合う。

```
接続 → Hub:   hello        {hub_version, protocol:{min,max}, nonce}            nonce_h
相手:         register     {…, protocol_version, protocol, package_version,
                            nonce, proof}                                       nonce_c
Hub:          register_ack {…, protocol_version, package_version, hub_proof}
相手:         hub_proof を確かめる。違えば信用しない
```

- ``proof     = HMAC-SHA256(token, "qgis-puppeteer/client\\n" + nonce_h + "\\n" + nonce_c)``
- ``hub_proof = HMAC-SHA256(token, "qgis-puppeteer/hub\\n" + nonce_c + "\\n" + nonce_h)``

token は通信に載せない。両方の nonce を混ぜるので、前の挨拶を録って流しても
通らない。向きごとに文字列を変えるので、相手の proof をそのまま返しても通らない。

token は Hub ファイル（``qgis_puppeteer.hub_file``）にあり、本人だけが読める。
ここは Qt にも websockets にも依らない純粋な関数だけを置く（Hub・Worker・
``AutomationClient`` が共有する）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from qgis_puppeteer._version import __version__
from qgis_puppeteer.protocol import (
    SUPPORTED_PROTOCOL,
    Error,
    ErrorCode,
    ProtocolRange,
    RegisterAck,
    RegisterRequest,
    error_code_value,
)

_CLIENT_LABEL = "qgis-puppeteer/client"
_HUB_LABEL = "qgis-puppeteer/hub"

# ok=False でも proof が無くてよい（ただの失敗として受け取る）コード。
# Hub は proof が通らなかった・版が合わなかったときは hub_proof を作らない
_UNPROVEN_FAILURES = frozenset(
    {ErrorCode.UNAUTHORIZED.value, ErrorCode.PROTOCOL_VERSION_MISMATCH.value}
)


def generate_token() -> str:
    """Hub が起きるたびに作る token（32 バイトの乱数、base64url）。"""
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    """挨拶ごとに作る nonce（16 バイトの乱数、base64url）。"""
    return secrets.token_urlsafe(16)


def _mac(token: str, label: str, first: str, second: str) -> str:
    message = f"{label}\n{first}\n{second}".encode()
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def client_proof(token: str, hub_nonce: str, client_nonce: str) -> str:
    """つなぐ側が register に載せる proof。"""
    return _mac(token, _CLIENT_LABEL, hub_nonce, client_nonce)


def hub_proof(token: str, client_nonce: str, hub_nonce: str) -> str:
    """Hub が register_ack に載せる proof。"""
    return _mac(token, _HUB_LABEL, client_nonce, hub_nonce)


def proof_matches(expected: str, given: object) -> bool:
    """proof を比べる（時間で中身が漏れないよう ``hmac.compare_digest`` で）。"""
    if not isinstance(given, str):
        return False
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))


def choose_protocol_version(
    hub_range: ProtocolRange, own_range: ProtocolRange = SUPPORTED_PROTOCOL
) -> int:
    """register に載せる版。両方の範囲の重なりの最大。

    重ならなければ自分の最大をそのまま送る（Hub が protocol_version_mismatch で
    断り、理由を details に載せて返す。断る経路を Hub の 1 か所にまとめるため）。
    """
    high = min(hub_range.max, own_range.max)
    low = max(hub_range.min, own_range.min)
    return high if low <= high else own_range.max


def check_register(
    req: RegisterRequest,
    *,
    token: str,
    hub_nonce: str,
    hub_range: ProtocolRange = SUPPORTED_PROTOCOL,
    hub_package_version: str = __version__,
) -> Error | None:
    """Hub が register を受け付けてよいか。だめならその理由の ``Error``。

    版を先に見る。版 1 の古い相手は hello を待たずに proof 無しの register を
    送ってくるので、proof を先に見ると「unauthorized」になり、版を合わせれば
    よいことが伝わらない。
    """
    if req.protocol_version not in hub_range:
        client_range = req.protocol
        return Error(
            code=ErrorCode.PROTOCOL_VERSION_MISMATCH,
            message=(
                f"The Hub speaks protocol {hub_range.min}..{hub_range.max} "
                f"(qgis-puppeteer {hub_package_version}), but the client asked for "
                f"protocol {req.protocol_version!r}"
                + (f" (qgis-puppeteer {req.package_version})" if req.package_version else "")
                + ". Use the same qgis-puppeteer version for the QGIS plugin, the Hub "
                "and the clients."
            ),
            details={
                "hub_protocol": {"min": hub_range.min, "max": hub_range.max},
                "hub_package_version": hub_package_version,
                "client_protocol_version": req.protocol_version,
                "client_protocol": (
                    None
                    if client_range is None
                    else {"min": client_range.min, "max": client_range.max}
                ),
                "client_package_version": req.package_version,
            },
        )
    nonce = req.nonce
    if (
        not isinstance(nonce, str)
        or not nonce
        or not proof_matches(client_proof(token, hub_nonce, nonce), req.proof)
    ):
        return Error(
            code=ErrorCode.UNAUTHORIZED,
            message=(
                "The Hub did not accept the proof. The client read the token from a "
                "different Hub file, or from a Hub that has since restarted."
            ),
        )
    return None


def ack_is_trusted(ack: RegisterAck, *, token: str, client_nonce: str, hub_nonce: str) -> bool:
    """register_ack を信用してよいか（相手が本当に token を知っている Hub か）。

    ``unauthorized`` と ``protocol_version_mismatch`` の失敗は、proof が無くても
    ただの失敗として受け取る（Hub はこのときまだ相手を確かめていない）。
    それ以外は、成功でも失敗でも hub_proof が合わなければ信用しない。とくに
    ``launch_abandoned`` を受けると QGIS のプラグインは QGIS を閉じるので、
    偽の Hub にそれをさせない。
    """
    if (
        not ack.ok
        and ack.error is not None
        and error_code_value(ack.error.code) in _UNPROVEN_FAILURES
    ):
        return True
    return proof_matches(hub_proof(token, client_nonce, hub_nonce), ack.hub_proof)
