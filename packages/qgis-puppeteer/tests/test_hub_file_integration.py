"""Hub（プロセス内）が Hub ファイルを書き、止まるときに消すことの統合テスト。

プロセスを ``terminate`` で止めると Windows では後始末が走らないので、ここでは
Hub を同じプロセスで起こして ``start`` / ``stop`` を直接呼ぶ。PyQt5 が無い環境
では全件 skip。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWebSockets")

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from qgis_puppeteer._version import __version__  # noqa: E402
from qgis_puppeteer.hub import Hub, HubFileWriteError  # noqa: E402
from qgis_puppeteer.hub_file import HubFileInfo, read_hub_file, write_hub_file  # noqa: E402
from qgis_puppeteer.protocol import SUPPORTED_PROTOCOL  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    return app if app is not None else QCoreApplication([])


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _accepts(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            return False
        return True


class TestHubFileLifecycle:
    def test_start_writes_the_hub_file_and_stop_removes_it(self, tmp_path: Path) -> None:
        hub_file = tmp_path / "hub.json"
        port = _get_free_port()
        hub = Hub(port=port, pid_file=None, hub_file=hub_file)
        hub.start()
        try:
            info = read_hub_file(hub_file)
            assert info.addresses == (f"127.0.0.1:{port}",)
            assert info.token == hub.token
            assert info.pid == os.getpid()
            assert info.package_version == __version__
            assert info.protocol == SUPPORTED_PROTOCOL
            assert info.started_at
        finally:
            hub.stop()
        assert not hub_file.exists()

    def test_each_hub_gets_a_new_token(self, tmp_path: Path) -> None:
        tokens = []
        for _ in range(2):
            hub = Hub(port=_get_free_port(), pid_file=None, hub_file=tmp_path / "hub.json")
            hub.start()
            tokens.append(read_hub_file(tmp_path / "hub.json").token)
            hub.stop()
        assert tokens[0] != tokens[1]

    def test_stop_leaves_another_hubs_file(self, tmp_path: Path) -> None:
        """あとから別の Hub が書いたファイルは、先の Hub が止まっても消さない。"""
        hub_file = tmp_path / "hub.json"
        hub = Hub(port=_get_free_port(), pid_file=None, hub_file=hub_file)
        hub.start()
        other = HubFileInfo(addresses=("127.0.0.1:1",), token="someone-else")
        write_hub_file(hub_file, other)
        hub.stop()
        assert read_hub_file(hub_file).token == "someone-else"

    def test_an_unwritable_hub_file_stops_listening(self, tmp_path: Path) -> None:
        """Hub ファイルを書けなければ、誰もつなげないので listen をやめて知らせる。"""
        blocker = tmp_path / "a-file"
        blocker.write_text("not a folder")
        port = _get_free_port()
        hub = Hub(port=port, pid_file=None, hub_file=blocker / "hub.json")
        with pytest.raises(HubFileWriteError):
            hub.start()
        try:
            assert not _accepts(port)
        finally:
            hub.stop()
