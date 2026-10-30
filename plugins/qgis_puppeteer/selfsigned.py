"""ネットワーク用の Hub の TLS の鍵と自己署名の証明書を、標準ライブラリだけで作る。

QGIS の Python には ``cryptography`` も pyOpenSSL も openssl.exe も無いので、
RSA の鍵の生成と X.509 v3 の証明書の組み立てをここで行う。

- 鍵：RSA 2048 ビット、e = 65537。素数は ``secrets`` の乱数から作り、小さな素数で
  ふるってから Miller–Rabin で確かめる
- 証明書：自己署名、SHA256withRSA（PKCS#1 v1.5）。期限は RFC 5280 の「期限なし」
  （99991231235959Z）
- 書き出し：証明書は PEM（``CERTIFICATE``）、鍵は PEM（PKCS#1 の ``RSA PRIVATE KEY``）

クライアントは CA による検証をせず、証明書の SHA-256 が接続情報ファイルの指紋と
一致するかだけを見る（pin。``qgis_puppeteer.hub_remote``）。なので証明書の署名や
名前は安全に関係せず、安全は鍵が十分に強いことだけに懸かる。署名を正しく付けて
おくのは、Qt（OpenSSL）が証明書として読み込めるようにするため。
"""

from __future__ import annotations

import base64
import hashlib
import math
import secrets
import time
from dataclasses import dataclass

RSA_BITS = 2048
RSA_PUBLIC_EXPONENT = 65537
DEFAULT_COMMON_NAME = "qgis-puppeteer Hub"

# Miller–Rabin の回数。乱数から作った 1024 ビットの数なら、誤って合成数を素数と
# みなす確率は 2^-80 よりずっと小さい（FIPS 186-5 の表）。多めに回す
_MILLER_RABIN_ROUNDS = 40
_SMALL_PRIMES: tuple[int, ...] = tuple(
    n for n in range(3, 2000, 2) if all(n % p for p in range(3, math.isqrt(n) + 1, 2))
)

# OID
_OID_RSA_ENCRYPTION = "1.2.840.113549.1.1.1"
_OID_SHA256_WITH_RSA = "1.2.840.113549.1.1.11"
_OID_COMMON_NAME = "2.5.4.3"
_OID_BASIC_CONSTRAINTS = "2.5.29.19"
_OID_KEY_USAGE = "2.5.29.15"
_OID_EXT_KEY_USAGE = "2.5.29.37"
_OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"

# EMSA-PKCS1-v1_5 の DigestInfo（SHA-256）の頭（RFC 8017 §9.2 の注 1）
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
# RFC 5280 §4.1.2.5：期限の無い証明書の notAfter
_NO_EXPIRY = "99991231235959Z"


@dataclass(frozen=True)
class RsaPrivateKey:
    """RSA の秘密鍵（CRT の値つき）。"""

    n: int
    e: int
    d: int
    p: int
    q: int
    dp: int
    dq: int
    qinv: int

    @property
    def size_bytes(self) -> int:
        return (self.n.bit_length() + 7) // 8


@dataclass(frozen=True)
class SelfSignedIdentity:
    """作った鍵と証明書。"""

    key_pem: str
    cert_pem: str
    cert_der: bytes

    @property
    def fingerprint_sha256(self) -> str:
        return fingerprint_sha256(self.cert_der)


def fingerprint_sha256(cert_der: bytes) -> str:
    """証明書（DER）の SHA-256 を ``"AB:CD:..."`` の形で返す。"""
    return ":".join(f"{b:02X}" for b in hashlib.sha256(cert_der).digest())


# ============================================================
# RSA の鍵
# ============================================================


def _is_probable_prime(n: int, rounds: int = _MILLER_RABIN_ROUNDS) -> bool:
    if n < 2:
        return False
    for p in (2, *_SMALL_PRIMES):
        if n == p:
            return True
        if n % p == 0:
            return False
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = 2 + secrets.randbelow(n - 3)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _random_prime(bits: int, e: int) -> int:
    """``bits`` ビットの素数で、p - 1 が e と互いに素なもの。

    上の 2 ビットを立てるので、2 つ掛けると必ず 2 * bits ビットになる。
    """
    while True:
        candidate = secrets.randbits(bits) | (0b11 << (bits - 2)) | 1
        if math.gcd(candidate - 1, e) != 1:
            continue
        if _is_probable_prime(candidate):
            return candidate


def generate_rsa_key(bits: int = RSA_BITS, e: int = RSA_PUBLIC_EXPONENT) -> RsaPrivateKey:
    """RSA の秘密鍵を作る（2048 ビットで 1 秒前後）。"""
    if bits < 1024 or bits % 2:
        raise ValueError(f"RSA key size must be an even number >= 1024, not {bits}")
    half = bits // 2
    while True:
        p = _random_prime(half, e)
        q = _random_prime(half, e)
        # p と q が近すぎると Fermat 法で割られる（FIPS 186-5 B.3.3 の下限）
        if abs(p - q) <= 1 << (half - 100):
            continue
        if p < q:
            p, q = q, p
        n = p * q
        if n.bit_length() != bits:
            continue
        lam = math.lcm(p - 1, q - 1)
        d = pow(e, -1, lam)
        # d が小さいと Wiener の攻撃が効く（FIPS 186-5 の下限は 2^(bits/2)）
        if d <= 1 << half:
            continue
        return RsaPrivateKey(
            n=n, e=e, d=d, p=p, q=q, dp=d % (p - 1), dq=d % (q - 1), qinv=pow(q, -1, p)
        )


def _rsa_sign_pkcs1v15_sha256(key: RsaPrivateKey, message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 で SHA-256 の署名を作る（RFC 8017 §8.2.1）。"""
    k = key.size_bytes
    t = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    m = int.from_bytes(em, "big")
    # CRT で計算する（RFC 8017 §5.1.2）
    s1 = pow(m, key.dp, key.p)
    s2 = pow(m, key.dq, key.q)
    h = (key.qinv * (s1 - s2)) % key.p
    s = s2 + key.q * h
    # 計算の誤りで秘密が漏れないよう、公開鍵で戻して確かめる
    if pow(s, key.e, key.n) != m:
        raise RuntimeError("RSA signature self-check failed")
    return s.to_bytes(k, "big")


# ============================================================
# DER（X.690）
# ============================================================


def _der(tag: int, content: bytes) -> bytes:
    length = len(content)
    if length < 0x80:
        head = bytes([length])
    else:
        raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
        head = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + head + content


def _integer(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative INTEGER is not used here")
    # 先頭のビットが立つときは 0x00 を足して正の数にする
    raw = value.to_bytes(value.bit_length() // 8 + 1, "big")
    return _der(0x02, raw)


def _sequence(*items: bytes) -> bytes:
    return _der(0x30, b"".join(items))


def _set(*items: bytes) -> bytes:
    return _der(0x31, b"".join(sorted(items)))


def _oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    body = bytearray([40 * parts[0] + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        body += bytes(reversed(chunk))
    return _der(0x06, bytes(body))


def _null() -> bytes:
    return b"\x05\x00"


def _boolean(value: bool) -> bytes:
    return _der(0x01, b"\xff" if value else b"\x00")


def _bit_string(data: bytes, unused_bits: int = 0) -> bytes:
    return _der(0x03, bytes([unused_bits]) + data)


def _octet_string(data: bytes) -> bytes:
    return _der(0x04, data)


def _utf8_string(text: str) -> bytes:
    return _der(0x0C, text.encode("utf-8"))


def _utc_time(text: str) -> bytes:
    return _der(0x17, text.encode("ascii"))


def _generalized_time(text: str) -> bytes:
    return _der(0x18, text.encode("ascii"))


def _explicit(tag_number: int, content: bytes) -> bytes:
    return _der(0xA0 | tag_number, content)


def _algorithm(oid: str) -> bytes:
    return _sequence(_oid(oid), _null())


def _name(common_name: str) -> bytes:
    return _sequence(_set(_sequence(_oid(_OID_COMMON_NAME), _utf8_string(common_name))))


def _extension(oid: str, value: bytes, *, critical: bool = False) -> bytes:
    items = [_oid(oid)]
    if critical:
        items.append(_boolean(True))
    items.append(_octet_string(value))
    return _sequence(*items)


# ============================================================
# 鍵と証明書の組み立て
# ============================================================


def rsa_private_key_der(key: RsaPrivateKey) -> bytes:
    """PKCS#1 の RSAPrivateKey（RFC 8017 付録 A.1.2）。"""
    return _sequence(
        _integer(0),
        _integer(key.n),
        _integer(key.e),
        _integer(key.d),
        _integer(key.p),
        _integer(key.q),
        _integer(key.dp),
        _integer(key.dq),
        _integer(key.qinv),
    )


def _subject_public_key_info(key: RsaPrivateKey) -> bytes:
    public_key = _sequence(_integer(key.n), _integer(key.e))
    return _sequence(_algorithm(_OID_RSA_ENCRYPTION), _bit_string(public_key))


def _not_before_now() -> bytes:
    """notBefore：時計のずれを見込んで 1 日前（UTCTime。2050 年より前）。"""
    t = time.gmtime(time.time() - 86400)
    return _utc_time(time.strftime("%y%m%d%H%M%SZ", t))


def self_signed_certificate_der(
    key: RsaPrivateKey, *, common_name: str = DEFAULT_COMMON_NAME
) -> bytes:
    """``key`` で自分に署名した X.509 v3 の証明書（DER）を作る。"""
    algorithm = _algorithm(_OID_SHA256_WITH_RSA)
    name = _name(common_name)
    # 正の 159 ビット以下の乱数（RFC 5280 §4.1.2.2：20 オクテットまで）
    serial = secrets.randbits(159) | 1
    # keyUsage：digitalSignature(0) と keyEncipherment(2)。BIT STRING は 0b101_00000、
    # 使っていない下の 5 ビット
    key_usage = _bit_string(bytes([0b1010_0000]), unused_bits=5)
    extensions = _sequence(
        _extension(_OID_BASIC_CONSTRAINTS, _sequence(), critical=True),
        _extension(_OID_KEY_USAGE, key_usage, critical=True),
        _extension(_OID_EXT_KEY_USAGE, _sequence(_oid(_OID_SERVER_AUTH))),
    )
    tbs = _sequence(
        _explicit(0, _integer(2)),  # v3
        _integer(serial),
        algorithm,
        name,
        _sequence(_not_before_now(), _generalized_time(_NO_EXPIRY)),
        name,
        _subject_public_key_info(key),
        _explicit(3, extensions),
    )
    signature = _rsa_sign_pkcs1v15_sha256(key, tbs)
    return _sequence(tbs, algorithm, _bit_string(signature))


def pem(label: str, der: bytes) -> str:
    """DER を PEM にする（64 文字で折る）。"""
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def pem_to_der(text: str, label: str) -> bytes:
    """PEM の ``label`` の最初のブロックを DER に戻す。

    Raises:
        ValueError: ブロックが無い・base64 が壊れている
    """
    begin = f"-----BEGIN {label}-----"
    end = f"-----END {label}-----"
    start = text.find(begin)
    stop = text.find(end, start + len(begin)) if start >= 0 else -1
    if start < 0 or stop < 0:
        raise ValueError(f"no PEM block {label!r}")
    body = "".join(text[start + len(begin) : stop].split())
    try:
        return base64.b64decode(body, validate=True)
    except ValueError as e:
        raise ValueError(f"PEM block {label!r} is not valid base64: {e}") from e


def generate_self_signed(
    *, bits: int = RSA_BITS, common_name: str = DEFAULT_COMMON_NAME
) -> SelfSignedIdentity:
    """鍵と自己署名の証明書を作り、PEM で返す。"""
    key = generate_rsa_key(bits)
    cert_der = self_signed_certificate_der(key, common_name=common_name)
    return SelfSignedIdentity(
        key_pem=pem("RSA PRIVATE KEY", rsa_private_key_der(key)),
        cert_pem=pem("CERTIFICATE", cert_der),
        cert_der=cert_der,
    )
