"""テストで使う TLS の鍵と証明書（``qgis_puppeteer.selfsigned`` で作る）。

鍵を作るのに 1〜3 秒かかるので、テストの実行ごとに 1 組だけ作って使い回す。
"""

from __future__ import annotations

import functools
import ssl
import tempfile
from pathlib import Path

from qgis_puppeteer.selfsigned import SelfSignedIdentity, generate_self_signed


@functools.lru_cache(maxsize=2)
def tls_identity(which: int = 0) -> SelfSignedIdentity:
    """テスト用の鍵と証明書。``which`` を変えると別の組（指紋の合わない相手用）。"""
    del which
    return generate_self_signed()


def server_ssl_context(identity: SelfSignedIdentity) -> ssl.SSLContext:
    """``identity`` で待ち受ける TLS サーバーの設定。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory() as tmp:
        cert = Path(tmp) / "hub.crt"
        key = Path(tmp) / "hub.key"
        cert.write_text(identity.cert_pem, encoding="utf-8")
        key.write_text(identity.key_pem, encoding="utf-8")
        ctx.load_cert_chain(cert, key)
    return ctx
