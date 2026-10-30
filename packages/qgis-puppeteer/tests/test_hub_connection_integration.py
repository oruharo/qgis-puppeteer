"""Hub が切れた接続を後始末する形の統合テスト。

Hub は受け付けた接続（QWebSocket）を自分の子にして C++ 側に持たせ、切れたら
``deleteLater`` で消す。Python 側が持ち主のままだと、``_sockets`` から外した
瞬間に、その接続の ``disconnected`` の最中に C++ のオブジェクトが消えて落ちる
（起動直後の生存確認の接続が切れたところで、ときどき 0xC0000005 で落ちていた）。

落ちるかどうかはタイミング次第で手元では再現しにくいので、ここでは「接続は Hub
の子で、切れたら消える」という形と、つないで切るのを何度も繰り返しても Hub が
生きていることを見張る。PyQt5 が無い環境では全件 skip。
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWebSockets")

from PyQt5.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl  # noqa: E402
from PyQt5.QtNetwork import QNetworkRequest  # noqa: E402
from PyQt5.QtWebSockets import QWebSocket  # noqa: E402
from qgis_puppeteer.hub import Hub  # noqa: E402
from qgis_puppeteer.hub_file import read_hub_file  # noqa: E402
from qgis_puppeteer.hub_probe import find_live_hub  # noqa: E402

pytestmark = pytest.mark.integration

_REPEAT = 30
_PROBES = 100


@pytest.fixture(scope="module", autouse=True)
def _qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    return app if app is not None else QCoreApplication([])


def _spin_until(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Qt のイベントループを回しながら ``cond`` が真になるのを待つ。

    ``deleteLater`` を確実に処理させるため、``processEvents`` ではなく入れ子の
    イベントループを短く回す。
    """
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        loop = QEventLoop()
        QTimer.singleShot(20, loop.quit)
        loop.exec_()
    return True


def _hub_connections(hub: Hub) -> list[QWebSocket]:
    return hub.findChildren(QWebSocket)


def _open(url: str) -> QWebSocket:
    """Hub に WebSocket でつなぎ、つながるまで待つ（Origin は Hub の許す localhost）。"""
    client = QWebSocket()
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Origin", b"http://localhost")
    client.open(request)
    connected = []
    client.connected.connect(lambda: connected.append(True))
    assert _spin_until(lambda: bool(connected)), "could not connect to the Hub"
    return client


class TestConnectionOwnership:
    def test_an_open_connection_is_a_child_of_the_hub(self, tmp_path: Path) -> None:
        hub = Hub(hub_file=tmp_path / "hub.json")
        hub.start()
        try:
            url = f"ws://{read_hub_file(tmp_path / 'hub.json').addresses[0]}"
            client = _open(url)
            try:
                assert _spin_until(lambda: len(_hub_connections(hub)) == 1)
                (ws,) = _hub_connections(hub)
                assert ws.parent() is hub
            finally:
                client.close()
                client.deleteLater()
        finally:
            hub.stop()

    def test_a_closed_connection_is_deleted(self, tmp_path: Path) -> None:
        """切れた接続は Hub の子から消える（disconnected の後に deleteLater で）。"""
        hub = Hub(hub_file=tmp_path / "hub.json")
        hub.start()
        try:
            url = f"ws://{read_hub_file(tmp_path / 'hub.json').addresses[0]}"
            for _ in range(_REPEAT):
                client = _open(url)
                assert _spin_until(lambda: len(_hub_connections(hub)) == 1)
                client.close()
                client.deleteLater()
                assert _spin_until(lambda: not _hub_connections(hub)), (
                    "a closed connection was left in the Hub"
                )
            # 後始末の後も、新しい接続を受け付ける
            client = _open(url)
            client.close()
            client.deleteLater()
        finally:
            hub.stop()


@contextmanager
def _hub_process(tmp: Path) -> Iterator[tuple[subprocess.Popen[bytes], Path]]:
    """Hub を子プロセスで起こす。

    Hub は接続ごとにログを書くので、出力はファイルに向ける（読まない PIPE だと
    数 KB で詰まり、Hub がログの書き込みで止まる）。
    """
    hub_file = tmp / "hub.json"
    log_path = tmp / "hub.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "qgis_puppeteer.hub", "--hub-file", str(hub_file)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 10.0
        while find_live_hub(hub_file) is None:
            if proc.poll() is not None:
                raise RuntimeError(f"Hub exited with {proc.returncode}: {_tail(log_path)}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"the Hub file {hub_file} did not become live")
            time.sleep(0.1)
        yield proc, hub_file
    finally:
        if proc.poll() is None:
            with suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)


def _tail(log_path: Path) -> str:
    return log_path.read_text(encoding="utf-8", errors="replace")[-2000:]


class TestRepeatedProbes:
    def test_the_hub_survives_many_probes(self, tmp_path: Path) -> None:
        """生存確認（挨拶してすぐ切る）を何度も受けても、Hub は落ちない。

        落ちていたのは、ちょうどこの「確認の接続が切れた直後」だった。
        """
        with _hub_process(tmp_path) as (proc, hub_file):
            log_path = tmp_path / "hub.log"
            for i in range(_PROBES):
                assert find_live_hub(hub_file) is not None, f"probe {i} failed: {_tail(log_path)}"
                assert proc.poll() is None, (
                    f"the Hub exited with {proc.returncode} after probe {i}: {_tail(log_path)}"
                )
            # 最後の切断の後始末が済むまで少し待って、まだ生きている
            time.sleep(0.5)
            assert proc.poll() is None, f"the Hub exited with {proc.returncode}: {_tail(log_path)}"
            assert find_live_hub(hub_file) is not None
