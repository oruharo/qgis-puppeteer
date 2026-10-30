"""テストで Hub と挨拶するための補助（Hub ファイルの用意と、生の WebSocket の挨拶）。

本物の Hub・偽の Hub・Worker・``AutomationClient`` は、どれも
``QPUPPETEER_HUB_FILE`` が指す Hub ファイルの token で挨拶する。テストは
``hub_file_env()`` で一時的な Hub ファイルの場所を決め、Hub に ``--hub-file`` で
同じ場所を渡す。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from qgis_puppeteer._version import __version__
from qgis_puppeteer.handshake import client_proof, generate_nonce
from qgis_puppeteer.hub_file import ENV_HUB_FILE, read_hub_file, resolve_hub_file_path
from qgis_puppeteer.protocol import PROTOCOL_VERSION, SUPPORTED_PROTOCOL

ORIGIN_HEADERS = {"Origin": "http://localhost"}


@contextmanager
def hub_file_env() -> Iterator[Path]:
    """一時フォルダの Hub ファイルの場所を ``QPUPPETEER_HUB_FILE`` に入れる（終われば戻す）。"""
    saved = os.environ.get(ENV_HUB_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "hub.json"
        os.environ[ENV_HUB_FILE] = str(path)
        try:
            yield path
        finally:
            if saved is None:
                os.environ.pop(ENV_HUB_FILE, None)
            else:
                os.environ[ENV_HUB_FILE] = saved


async def open_greeted(url: str) -> tuple[Any, dict[str, Any]]:
    """生の WebSocket でつなぎ、Hub の hello を受け取る。"""
    from websockets.asyncio.client import connect as ws_connect

    ws = await ws_connect(url, additional_headers=ORIGIN_HEADERS)
    hello = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
    assert hello["type"] == "hello", hello
    return ws, hello


def register_json(hello: dict[str, Any], *, token: str | None = None, **fields: Any) -> str:
    """``hello`` に proof で答える register（token の省略時は Hub ファイルから読む）。"""
    if token is None:
        token = read_hub_file(resolve_hub_file_path()).token
    nonce = generate_nonce()
    return json.dumps(
        {
            "type": "register",
            "protocol_version": PROTOCOL_VERSION,
            "protocol": {"min": SUPPORTED_PROTOCOL.min, "max": SUPPORTED_PROTOCOL.max},
            "package_version": __version__,
            "nonce": nonce,
            "proof": client_proof(token, hello["nonce"], nonce),
            **fields,
        }
    )


async def register_raw(url: str, **fields: Any) -> tuple[Any, dict[str, Any]]:
    """生の WebSocket で挨拶して register し、(ws, register_ack) を返す。"""
    ws, hello = await open_greeted(url)
    await ws.send(register_json(hello, **fields))
    ack = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
    assert ack["type"] == "register_ack", ack
    return ws, ack
