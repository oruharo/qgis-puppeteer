"""hub_probe のテスト：pid の生死、アドレスの分解、挨拶、生きた Hub の見つけ方。

偽の Hub は websockets の同期サーバを別スレッドで立てる（``greet_hub`` は同期で、
標準ライブラリだけの小さな WebSocket クライアントなので、本物の websockets の
サーバ相手に通ることもここで確かめる）。実の Hub（Qt）相手には
``test_worker_integration`` などの ``_wait_hub_live`` が毎回通している。
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from qgis_puppeteer._version import __version__
from qgis_puppeteer.handshake import check_register, generate_nonce, generate_token, hub_proof
from qgis_puppeteer.hub_file import HubFileInfo, write_hub_file
from qgis_puppeteer.hub_probe import (
    HubProbeError,
    describe_hub_file,
    find_live_hub,
    greet_hub,
    pid_alive,
    split_address,
)
from qgis_puppeteer.protocol import (
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL,
    Hello,
    RegisterAck,
    RegisterRequest,
    decode_message,
    encode_message,
)
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

# ============================================================
# 偽の Hub
# ============================================================


class _FakeHub:
    """hello → register の proof を確かめる → hub_proof 付きの register_ack。"""

    def __init__(self, *, wrong_hub_proof: bool = False, silent: bool = False) -> None:
        self.token = generate_token()
        self.wrong_hub_proof = wrong_hub_proof
        # True なら hello を送らない（Hub でないサーバの代わり）
        self.silent = silent

    def handler(self, ws: ServerConnection) -> None:
        if self.silent:
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                ws.recv(timeout=5.0)
            return
        hub_nonce = generate_nonce()
        # 長い（126 バイト以上、長さが 2 バイトの）フレームも読めることを兼ねて確かめる
        ws.send(
            encode_message(
                Hello(hub_version=__version__, protocol=SUPPORTED_PROTOCOL, nonce=hub_nonce)
            )
        )
        with contextlib.suppress(ConnectionClosed, TimeoutError):
            req = decode_message(ws.recv(timeout=5.0))
            assert isinstance(req, RegisterRequest)
            refused = check_register(req, token=self.token, hub_nonce=hub_nonce)
            if refused is not None:
                ws.send(encode_message(RegisterAck(id=req.id, ok=False, error=refused)))
                return
            proof = hub_proof(self.token, req.nonce or "", hub_nonce)
            ws.send(
                encode_message(
                    RegisterAck(
                        id=req.id,
                        ok=True,
                        protocol_version=PROTOCOL_VERSION,
                        package_version=__version__,
                        hub_proof="0" * 64 if self.wrong_hub_proof else proof,
                    )
                )
            )
            # 相手が切るまで待つ
            ws.recv(timeout=5.0)


@contextmanager
def _serve(hub: _FakeHub) -> Iterator[str]:
    """偽の Hub を立て、``"127.0.0.1:port"`` を返す。"""
    with serve(hub.handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.socket.getsockname()[1]
            yield f"127.0.0.1:{port}"
        finally:
            server.shutdown()
            thread.join(timeout=5.0)


def _closed_port() -> int:
    """誰も待っていないポート（取ってすぐ閉じる）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dead_pid() -> int:
    """終わったプロセスの pid。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def _write(path: Path, *addresses: str, token: str, pid: int | None = None) -> HubFileInfo:
    info = HubFileInfo(
        addresses=addresses,
        token=token,
        pid=os.getpid() if pid is None else pid,
        package_version=__version__,
    )
    write_hub_file(path, info)
    return info


# ============================================================
# split_address
# ============================================================


class TestSplitAddress:
    def test_ipv4(self) -> None:
        assert split_address("127.0.0.1:51234") == ("127.0.0.1", 51234)

    def test_bracketed_ipv6(self) -> None:
        assert split_address("[::1]:51234") == ("::1", 51234)

    def test_hostname(self) -> None:
        assert split_address("localhost:80") == ("localhost", 80)

    @pytest.mark.parametrize("address", ["127.0.0.1", "127.0.0.1:", ":51234", "host:port"])
    def test_without_a_port_is_refused(self, address: str) -> None:
        with pytest.raises(ValueError):
            split_address(address)


# ============================================================
# pid_alive
# ============================================================


class TestPidAlive:
    def test_this_process_is_alive(self) -> None:
        assert pid_alive(os.getpid()) is True

    @pytest.mark.parametrize("pid", [0, -1])
    def test_non_positive_pids_are_not_alive(self, pid: int) -> None:
        assert pid_alive(pid) is False

    def test_a_finished_process_is_not_alive(self) -> None:
        assert pid_alive(_dead_pid()) is False


# ============================================================
# greet_hub
# ============================================================


class TestGreetHub:
    def test_a_hub_that_knows_the_token_answers(self) -> None:
        hub = _FakeHub()
        with _serve(hub) as address:
            ack = greet_hub(address, hub.token)
        assert ack.ok is True
        assert ack.package_version == __version__

    def test_a_wrong_token_is_refused(self) -> None:
        hub = _FakeHub()
        with _serve(hub) as address, pytest.raises(HubProbeError, match="unauthorized"):
            greet_hub(address, "not-the-token")

    def test_a_hub_that_does_not_know_the_token_is_not_trusted(self) -> None:
        hub = _FakeHub(wrong_hub_proof=True)
        with _serve(hub) as address, pytest.raises(HubProbeError, match="does not know"):
            greet_hub(address, hub.token)

    def test_a_server_that_does_not_say_hello_times_out(self) -> None:
        hub = _FakeHub(silent=True)
        with _serve(hub) as address, pytest.raises(HubProbeError):
            greet_hub(address, hub.token, timeout=0.5)

    def test_nobody_listening_is_an_error(self) -> None:
        with pytest.raises(HubProbeError, match="Could not connect"):
            greet_hub(f"127.0.0.1:{_closed_port()}", "t", timeout=1.0)

    def test_a_malformed_address_is_an_error(self) -> None:
        with pytest.raises(HubProbeError):
            greet_hub("no-port", "t")


# ============================================================
# find_live_hub
# ============================================================


class TestFindLiveHub:
    def test_a_live_hub_is_found(self, tmp_path: Path) -> None:
        hub = _FakeHub()
        with _serve(hub) as address:
            written = _write(tmp_path / "hub.json", address, token=hub.token)
            assert find_live_hub(tmp_path / "hub.json") == written

    def test_no_file_means_no_hub(self, tmp_path: Path) -> None:
        assert find_live_hub(tmp_path / "hub.json") is None

    def test_a_broken_file_means_no_hub(self, tmp_path: Path) -> None:
        (tmp_path / "hub.json").write_text("{not json", encoding="utf-8")
        assert find_live_hub(tmp_path / "hub.json") is None

    def test_a_dead_pid_means_no_hub(self, tmp_path: Path) -> None:
        """pid が死んでいれば、アドレスで誰かが待っていても見ない（挨拶もしない）。"""
        hub = _FakeHub()
        with _serve(hub) as address:
            _write(tmp_path / "hub.json", address, token=hub.token, pid=_dead_pid())
            assert find_live_hub(tmp_path / "hub.json") is None

    def test_nobody_at_the_address_means_no_hub(self, tmp_path: Path) -> None:
        _write(tmp_path / "hub.json", f"127.0.0.1:{_closed_port()}", token="t")
        assert find_live_hub(tmp_path / "hub.json", timeout=1.0) is None

    def test_another_tokens_hub_at_the_address_means_no_hub(self, tmp_path: Path) -> None:
        """ファイルの Hub が落ち、同じポートに別の Hub が起きた（token が違う）。"""
        hub = _FakeHub()
        with _serve(hub) as address:
            _write(tmp_path / "hub.json", address, token="an-old-token")
            assert find_live_hub(tmp_path / "hub.json") is None

    def test_the_addresses_are_tried_in_order(self, tmp_path: Path) -> None:
        hub = _FakeHub()
        with _serve(hub) as address:
            dead = f"127.0.0.1:{_closed_port()}"
            written = _write(tmp_path / "hub.json", dead, address, token=hub.token)
            assert find_live_hub(tmp_path / "hub.json", timeout=1.0) == written


# ============================================================
# describe_hub_file
# ============================================================


class TestDescribeHubFile:
    def test_a_missing_file_names_the_path_and_the_reason(self, tmp_path: Path) -> None:
        text = describe_hub_file(tmp_path / "hub.json")
        assert str(tmp_path / "hub.json") in text
        assert "(" in text

    def test_a_present_file_shows_version_pid_and_addresses(self, tmp_path: Path) -> None:
        _write(tmp_path / "hub.json", "127.0.0.1:1", "[::1]:2", token="t", pid=4242)
        text = describe_hub_file(tmp_path / "hub.json")
        assert f"Hub qgis-puppeteer {__version__}" in text
        assert "pid 4242" in text
        assert "127.0.0.1:1, [::1]:2" in text
