"""``--listen`` の Hub（ネットワーク側は TLS）の統合テスト。

実際の ``python -m qgis_puppeteer.hub --listen ...`` を子プロセスで起こし、Hub が
書いたネットワーク用の Hub ファイル（``remote/hub.json``）で wss につなぐ。Qt
（QSslKey / QSslCertificate、OpenSSL）が ``qgis_puppeteer.selfsigned`` の鍵と
証明書を読めて、TLS で実際につながることもここで確かめる。

token・鍵・証明書のフォルダは必ず ``--remote-dir`` で一時フォルダを渡す（既定の
場所に作らない）。PyQt5 が無い環境では全件 skip。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt5.QtWebSockets")

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from qgis_puppeteer.client import AutomationClient, HubAuthError  # noqa: E402
from qgis_puppeteer.hub import Hub, HubFileWriteError, RemoteListen, network_addresses  # noqa: E402
from qgis_puppeteer.hub_file import HubFileInfo, read_hub_file, write_hub_file  # noqa: E402
from qgis_puppeteer.hub_probe import find_live_hub  # noqa: E402
from qgis_puppeteer.hub_remote import RemoteIdentity, client_ssl_context  # noqa: E402
from websockets.asyncio.client import connect as ws_connect  # noqa: E402

from tests._handshake_helpers import ORIGIN_HEADERS, register_json, register_raw  # noqa: E402
from tests._tls_helpers import tls_identity  # noqa: E402

pytestmark = pytest.mark.integration

# 初回は鍵を作るので（純 Python の RSA 2048）、起動を長めに待つ
_START_TIMEOUT_S = 30.0


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclasses.dataclass
class _RemoteHub:
    proc: subprocess.Popen[bytes]
    hub_file: Path
    remote_file: Path
    listen_port: int

    @property
    def local_url(self) -> str:
        return f"ws://{read_hub_file(self.hub_file).addresses[0]}"

    @property
    def remote_url(self) -> str:
        return f"wss://127.0.0.1:{self.listen_port}"

    @property
    def remote_info(self) -> HubFileInfo:
        return read_hub_file(self.remote_file)


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    with suppress(OSError):
        if sys.platform == "win32":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)


@contextmanager
def _remote_hub(
    tmp: Path,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    rotate: bool = False,
) -> Iterator[_RemoteHub]:
    """``--listen host:port`` の Hub を起こし、remote の hub.json で挨拶が通るまで待つ。"""
    listen_port = _get_free_port() if port is None else port
    hub_file = tmp / "local" / "hub.json"
    remote_dir = tmp / "remote"
    remote_file = remote_dir / "hub.json"
    # 前の回の remote の hub.json が残っていても、新しい Hub が書き直すまで待てるように
    with suppress(FileNotFoundError):
        remote_file.unlink()
    # 出力はファイルに向ける（読まない PIPE だと数 KB で詰まり、Hub がログの書き込みで止まる）
    log_path = tmp / "hub.log"
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "qgis_puppeteer.hub",
                "--hub-file",
                str(hub_file),
                "--listen",
                f"{host}:{listen_port}",
                "--remote-dir",
                str(remote_dir),
                *(["--rotate"] if rotate else []),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _START_TIMEOUT_S
        while find_live_hub(remote_file) is None:
            if proc.poll() is not None:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise RuntimeError(f"Hub exited with {proc.returncode}: {tail}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"the network Hub file {remote_file} did not become live")
            time.sleep(0.2)
        yield _RemoteHub(proc, hub_file, remote_file, listen_port)
    finally:
        _stop(proc)


async def _register_over_tls(url: str, token: str, **fields: Any) -> dict[str, Any]:
    """wss で挨拶して register し、register_ack を返す（証明書は照合しない）。"""
    async with ws_connect(url, additional_headers=ORIGIN_HEADERS, ssl=client_ssl_context()) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
        assert hello["type"] == "hello", hello
        await ws.send(register_json(hello, token=token, **fields))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
        assert ack["type"] == "register_ack", ack
        return ack


# ============================================================
# つながること
# ============================================================


class TestNetworkClient:
    def test_the_network_hub_file_has_no_pid_and_a_fingerprint(self, tmp_path: Path) -> None:
        with _remote_hub(tmp_path) as hub:
            info = hub.remote_info
            local = read_hub_file(hub.hub_file)
        assert info.pid is None
        assert info.addresses == (f"127.0.0.1:{hub.listen_port}",)
        assert info.tls is not None
        assert len(info.tls["fingerprint_sha256"].split(":")) == 32
        # ネットワーク用の token はローカル用と別
        assert info.token != local.token
        assert local.tls is None

    def test_a_client_connects_with_the_network_hub_file(self, tmp_path: Path) -> None:
        async def inner(hub: _RemoteHub) -> None:
            async with AutomationClient(hub_file=hub.remote_file) as client:
                assert client.address == f"127.0.0.1:{hub.listen_port}"
                assert await client.list_instances() == []

        with _remote_hub(tmp_path) as hub:
            asyncio.run(inner(hub))

    def test_a_network_client_sees_a_worker_on_the_local_side(self, tmp_path: Path) -> None:
        async def inner(hub: _RemoteHub) -> None:
            local_token = read_hub_file(hub.hub_file).token
            worker, ack = await register_raw(
                hub.local_url, token=local_token, id="w", role="worker", pid=4242, label="w"
            )
            try:
                assert ack["ok"] is True, ack
                async with AutomationClient(hub_file=hub.remote_file) as client:
                    instances = await client.list_instances()
                assert [i.label for i in instances] == ["w"]
            finally:
                await worker.close()

        with _remote_hub(tmp_path) as hub:
            asyncio.run(inner(hub))

    def test_find_live_hub_greets_over_tls(self, tmp_path: Path) -> None:
        with _remote_hub(tmp_path) as hub:
            assert find_live_hub(hub.remote_file) == hub.remote_info
            # address で宛先だけ変えても、指紋と token はファイルのもの
            assert find_live_hub(hub.remote_file, address=f"127.0.0.1:{hub.listen_port}")

    def test_a_tampered_fingerprint_is_refused(self, tmp_path: Path) -> None:
        async def inner(path: Path) -> None:
            with pytest.raises(HubAuthError, match="does not match"):
                await AutomationClient(hub_file=path, register_timeout=3.0).connect()

        with _remote_hub(tmp_path) as hub:
            info = hub.remote_info
            tampered = tmp_path / "tampered.json"
            write_hub_file(
                tampered, dataclasses.replace(info, tls={"fingerprint_sha256": "AB" * 32})
            )
            asyncio.run(inner(tampered))
            assert find_live_hub(tampered) is None


# ============================================================
# 断ること
# ============================================================


class TestNetworkSideRefusals:
    def test_a_worker_cannot_register_from_the_network(self, tmp_path: Path) -> None:
        with _remote_hub(tmp_path) as hub:
            ack = asyncio.run(
                _register_over_tls(
                    hub.remote_url,
                    hub.remote_info.token,
                    id="w",
                    role="worker",
                    pid=4242,
                    label="w",
                )
            )
        assert ack["ok"] is False
        assert ack["error"]["code"] == "unauthorized"

    def test_the_local_token_does_not_open_the_network_side(self, tmp_path: Path) -> None:
        with _remote_hub(tmp_path) as hub:
            local_token = read_hub_file(hub.hub_file).token
            ack = asyncio.run(
                _register_over_tls(hub.remote_url, local_token, id="c", role="automation_client")
            )
        assert ack["ok"] is False
        assert ack["error"]["code"] == "unauthorized"

    def test_the_network_token_does_not_open_the_local_side(self, tmp_path: Path) -> None:
        async def inner(hub: _RemoteHub) -> dict[str, Any]:
            ws, ack = await register_raw(
                hub.local_url, token=hub.remote_info.token, id="c", role="automation_client"
            )
            await ws.close()
            return ack

        with _remote_hub(tmp_path) as hub:
            ack = asyncio.run(inner(hub))
        assert ack["ok"] is False
        assert ack["error"]["code"] == "unauthorized"


# ============================================================
# token と鍵を残すこと・作り直すこと
# ============================================================


class TestRemoteIdentity:
    def test_a_restarted_hub_keeps_the_token_and_the_certificate(self, tmp_path: Path) -> None:
        with _remote_hub(tmp_path) as hub:
            first = hub.remote_info
        with _remote_hub(tmp_path) as hub:
            second = hub.remote_info
        assert second.token == first.token
        assert second.tls == first.tls

    def test_rotate_makes_a_new_token_and_certificate(self, tmp_path: Path) -> None:
        with _remote_hub(tmp_path) as hub:
            first = hub.remote_info
        with _remote_hub(tmp_path, rotate=True) as hub:
            second = hub.remote_info

            async def inner() -> None:
                async with AutomationClient(hub_file=hub.remote_file) as client:
                    assert await client.list_instances() == []

            asyncio.run(inner())
        assert second.token != first.token
        assert second.tls != first.tls

    def test_the_network_hub_file_is_left_when_the_hub_stops(self, tmp_path: Path) -> None:
        """写した先のファイルと同じものが残る（起こし直せばまた使える）。"""
        with _remote_hub(tmp_path) as hub:
            remote_file = hub.remote_file
        assert remote_file.exists()


# ============================================================
# --listen の値
# ============================================================


class TestStartFailure:
    def test_an_unwritable_hub_file_stops_the_network_side_too(self, tmp_path: Path) -> None:
        """Hub ファイルを書けなければ、ネットワーク側の待ち受けもやめる（プロセス内の Hub）。"""
        if QCoreApplication.instance() is None:
            QCoreApplication([])
        blocker = tmp_path / "a-file"
        blocker.write_text("not a folder")
        identity = tls_identity()
        port = _get_free_port()
        hub = Hub(
            hub_file=blocker / "hub.json",
            remote=RemoteListen(
                host="127.0.0.1",
                port=port,
                identity=RemoteIdentity(
                    token="t",
                    key_pem=identity.key_pem,
                    cert_pem=identity.cert_pem,
                    fingerprint_sha256=identity.fingerprint_sha256,
                ),
            ),
        )
        with pytest.raises(HubFileWriteError):
            hub.start()
        try:
            assert hub.remote_port is None
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                assert s.connect_ex(("127.0.0.1", port)) != 0
        finally:
            hub.stop()


class TestListenArguments:
    @pytest.mark.parametrize(
        "args",
        [
            ["--listen", "127.0.0.1:0"],
            ["--listen", "127.0.0.1"],
            ["--listen", "localhost:9876"],
            ["--rotate"],
            ["--remote-dir", "somewhere"],
        ],
    )
    def test_bad_arguments_exit_with_usage_error(self, tmp_path: Path, args: list[str]) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "qgis_puppeteer.hub",
                "--hub-file",
                str(tmp_path / "hub.json"),
                *args,
            ],
            capture_output=True,
            timeout=60,
        )
        assert proc.returncode == 2, proc.stderr.decode(errors="replace")
        assert not (tmp_path / "hub.json").exists()


# ============================================================
# 127.0.0.1 以外のアドレス
# ============================================================


class TestLanAddress:
    def test_a_client_connects_through_a_lan_address(self, tmp_path: Path) -> None:
        """``0.0.0.0`` で待ち、この機械の LAN のアドレスでつなぐ（無ければ skip）。"""
        port = _get_free_port()
        lan = network_addresses("0.0.0.0", port)
        if not lan:
            pytest.skip("this machine has no non-loopback IPv4 address")

        async def inner(hub: _RemoteHub) -> str | None:
            async with AutomationClient(hub_file=hub.remote_file) as client:
                assert await client.list_instances() == []
                return client.address

        with _remote_hub(tmp_path, host="0.0.0.0", port=port) as hub:
            assert hub.remote_info.addresses == lan
            assert asyncio.run(inner(hub)) in lan
