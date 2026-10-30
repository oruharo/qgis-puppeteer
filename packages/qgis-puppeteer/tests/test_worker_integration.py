"""Worker (PyQt5) と Hub subprocess + AutomationClient の end-to-end テスト。

PyQt5 の `Worker` が実 Hub に接続して handler で request を処理し、
asyncio 側の AutomationClient が受け取った結果と一致することを確認する。

## 構成

- プロセス外：Hub subprocess（`python -m qgis_puppeteer.hub`）
- pytest メインスレッド：QCoreApplication + Worker（PyQt5）
- pytest 補助スレッド：asyncio AutomationClient（結果を送り返す）

## Qt / asyncio の同居戦略

Qt と asyncio のイベントループは同一スレッドに同居できないので、
asyncio クライアントを別スレッドで走らせ、Qt 側は
`processEvents()` + `sleep` で回す。スレッド終了を検知したら
Qt ループを抜けて assert に移る。
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest

# PyQt5 が無い環境では全件 skip
pytest.importorskip("PyQt5.QtWebSockets")

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from qgis_puppeteer._version import __version__  # noqa: E402
from qgis_puppeteer.client import AutomationClient  # noqa: E402
from qgis_puppeteer.handshake import (  # noqa: E402
    client_proof,
    generate_nonce,
    generate_token,
    hub_proof,
)
from qgis_puppeteer.hub_file import HubFileInfo, write_hub_file  # noqa: E402
from qgis_puppeteer.hub_probe import find_live_hub  # noqa: E402
from qgis_puppeteer.protocol import (  # noqa: E402
    SUPPORTED_PROTOCOL,
    Error,
    ErrorCode,
    Hello,
    RegisterAck,
    encode_message,
)
from qgis_puppeteer.worker import Worker  # noqa: E402

from tests._handshake_helpers import hub_file_env  # noqa: E402

pytestmark = pytest.mark.integration


# ============================================================
# フィクスチャ：Hub subprocess
# ============================================================


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_hub_live(hub_file: Path, timeout: float = 10.0) -> None:
    """Hub が Hub ファイルを書き、挨拶が通るまで待つ（つなぐ側はファイルを読む）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_live_hub(hub_file) is not None:
            return
        time.sleep(0.1)
    raise TimeoutError(f"Hub did not become live within {timeout}s at {hub_file}")


@contextmanager
def _hub_subprocess(port: int) -> Iterator[subprocess.Popen[bytes]]:
    """Hub プロセスを起動し、終了時に確実に停止させる。

    Hub ファイルは一時フォルダに書かせ、その間 ``QPUPPETEER_HUB_FILE`` で
    同じ場所を指す（このプロセスの Worker と ``AutomationClient`` がそこから
    アドレスと token を読む）。
    """
    with hub_file_env() as hub_file, _hub_process(port, hub_file) as proc:
        yield proc


def hub_command(port: int, hub_file: Path) -> list[str]:
    """Hub を起こすコマンド（Worker の ``hub_spawn_command`` にも使う）。"""
    return [
        sys.executable,
        "-m",
        "qgis_puppeteer.hub",
        "--port",
        str(port),
        "--hub-file",
        str(hub_file),
    ]


@contextmanager
def _hub_process(port: int, hub_file: Path) -> Iterator[subprocess.Popen[bytes]]:
    # 出力はファイルに向ける（読まない PIPE だと数 KB で詰まり、Hub がログの書き込みで止まる）
    with hub_file.with_name("hub.log").open("ab") as log:
        proc = subprocess.Popen(
            hub_command(port, hub_file),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_hub_live(hub_file)
        yield proc
    finally:
        if proc.poll() is None:
            with suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)


# ============================================================
# Qt ヘルパ
# ============================================================


def _qapp() -> QCoreApplication:
    """単一の QCoreApplication を使い回す（pytest プロセス全体で 1 つ）。"""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _spin_until(
    app: QCoreApplication,
    predicate: callable[[], bool],  # type: ignore[name-defined]
    timeout: float = 5.0,
    interval: float = 0.01,
) -> bool:
    """predicate() が True になるまで Qt イベントループを回す。タイムアウトで False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(interval)
    return False


def _spin_while_thread(
    app: QCoreApplication, thread: threading.Thread, interval: float = 0.01
) -> None:
    """スレッドが生きている間 Qt イベントループを回す。"""
    while thread.is_alive():
        app.processEvents()
        time.sleep(interval)
    # 最後に処理し残しがないか念のため drain
    for _ in range(20):
        app.processEvents()


def _asyncio_in_thread(
    coro_factory: callable[[], Any],  # type: ignore[name-defined]
    result_holder: list[Any],
) -> threading.Thread:
    """asyncio.run を別スレッドで走らせる。結果は result_holder に append。"""

    def _target() -> None:
        try:
            value = asyncio.run(coro_factory())
            result_holder.append(("ok", value))
        except BaseException as e:  # noqa: BLE001
            result_holder.append(("err", e))

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t


# ============================================================
# 偽の Hub（挨拶だけ本物と同じにして、ack を好きに返す）
# ============================================================


def _write_fake_hub_file(port: int) -> str:
    """``hub_file_env()`` の場所に偽の Hub の Hub ファイルを書き、token を返す。"""
    import os

    from qgis_puppeteer.hub_file import ENV_HUB_FILE

    token = generate_token()
    write_hub_file(
        os.environ[ENV_HUB_FILE],
        HubFileInfo(addresses=(f"127.0.0.1:{port}",), token=token),
    )
    return token


async def _greet(ws: Any) -> tuple[str, dict[str, Any]]:
    """hello を送り、返ってきた register を (hub_nonce, register) で返す。"""
    hub_nonce = generate_nonce()
    hello = Hello(hub_version=__version__, protocol=SUPPORTED_PROTOCOL, nonce=hub_nonce)
    await ws.send(encode_message(hello))
    reg = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
    assert reg["type"] == "register", reg
    return hub_nonce, reg


async def _wait_closed(ws: Any) -> bool:
    """相手が切るのを待つ。切られたら True、5 秒待っても切られなければ False。"""
    try:
        await asyncio.wait_for(ws.wait_closed(), 5.0)
    except asyncio.TimeoutError:
        return False
    return True


def _run_fake_hub(
    port: int, handler: Callable[[Any], Awaitable[None]], holder: list[Any]
) -> threading.Thread:
    """1 本目の接続を ``handler`` で扱う偽の Hub を別スレッドで起こす。"""
    from websockets.asyncio.server import serve

    server_ready = threading.Event()

    async def fake_hub() -> None:
        done = asyncio.Event()

        async def on_connection(ws: Any) -> None:
            if done.is_set():
                return
            try:
                await handler(ws)
            finally:
                done.set()

        async with serve(on_connection, "127.0.0.1", port):
            server_ready.set()
            await asyncio.wait_for(done.wait(), 10.0)

    t = _asyncio_in_thread(fake_hub, holder)
    assert server_ready.wait(5.0)
    return t


# ============================================================
# テスト
# ============================================================


class TestWorkerIntegration:
    def test_worker_registers_with_real_hub(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(
                pid=9001,
                label="e2e-reg",
            )
            try:
                worker.connect_to_hub()
                ok = _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                assert ok, "Worker failed to register"
                # ADR-0005 D1: instance_id は pid 非依存の不透明 nonce
                assert worker.instance_id is not None
                assert worker.instance_id.startswith("w-")
            finally:
                worker.disconnect_from_hub()
                # disconnected まで drain
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)

    def test_worker_handles_request_via_automation_client(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(
                pid=9002,
                label="e2e-req",
            )
            worker.register_handler("echo", lambda params: {"got": params, "who": "worker"})
            try:
                worker.connect_to_hub()
                ok = _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                assert ok

                result_holder: list[Any] = []

                async def client_call() -> Any:
                    async with AutomationClient() as client:
                        return await client.call("echo", {"v": 123}, timeout_ms=5_000)

                t = _asyncio_in_thread(client_call, result_holder)
                _spin_while_thread(app, t)

                assert len(result_holder) == 1
                kind, value = result_holder[0]
                assert kind == "ok", f"client failed: {value!r}"
                assert value == {"got": {"v": 123}, "who": "worker"}
            finally:
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)

    def test_worker_unknown_command_returns_invalid_command(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(
                pid=9003,
                label="e2e-unknown",
            )
            try:
                worker.connect_to_hub()
                ok = _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                assert ok

                from qgis_puppeteer.client import RequestError
                from qgis_puppeteer.protocol import ErrorCode

                result_holder: list[Any] = []

                async def client_call() -> Any:
                    async with AutomationClient() as client:
                        try:
                            await client.call("no-such-command")
                        except RequestError as e:
                            return e.code
                        return None

                t = _asyncio_in_thread(client_call, result_holder)
                _spin_while_thread(app, t)

                assert len(result_holder) == 1
                kind, value = result_holder[0]
                assert kind == "ok"
                assert value == ErrorCode.INVALID_COMMAND
            finally:
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)

    def test_worker_uses_existing_hub_when_spawn_command_given(self) -> None:
        """auto-spawn でも、Hub ファイルの Hub が生きていれば spawn せずに接続する。"""
        port = _get_free_port()
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(
                pid=9005,
                label="e2e-existing",
                # spawn されたら分かるように、即死する spawn command を渡す
                hub_spawn_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(1)",
                ],
            )
            try:
                worker.connect_to_hub()
                ok = _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                assert ok, "Worker should register against existing Hub"
            finally:
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)

    def test_worker_auto_spawn_failure_emits_signal(self) -> None:
        """起動に失敗する spawn_command で hub_startup_failed が発火する。"""
        # Hub は明示的に起動しない。Hub ファイルが無く、spawn で即死 → HubStartupError
        app = _qapp()
        with hub_file_env():
            worker = Worker(
                pid=9006,
                label="e2e-fail",
                hub_spawn_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(42)",
                ],
                auto_reconnect=False,
            )
            failures: list[str] = []
            worker.hub_startup_failed.connect(failures.append)

            worker.connect_to_hub()
            ok = _spin_until(app, lambda: len(failures) > 0, timeout=15.0)
            assert ok, "hub_startup_failed must fire when spawn fails"
            assert worker.is_registered is False

    def test_worker_handler_exception_becomes_error_response(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(
                pid=9004,
                label="e2e-boom",
            )

            def boom(_: dict[str, Any]) -> Any:
                raise RuntimeError("bang")

            worker.register_handler("boom", boom)
            try:
                worker.connect_to_hub()
                ok = _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                assert ok

                from qgis_puppeteer.client import RequestError
                from qgis_puppeteer.protocol import ErrorCode

                result_holder: list[Any] = []

                async def client_call() -> Any:
                    async with AutomationClient() as client:
                        try:
                            await client.call("boom")
                        except RequestError as e:
                            return (e.code, e.error.message, e.details)
                        return None

                t = _asyncio_in_thread(client_call, result_holder)
                _spin_while_thread(app, t)

                assert len(result_holder) == 1
                kind, value = result_holder[0]
                assert kind == "ok", f"unexpected: {value!r}"
                code, message, details = value
                assert code == ErrorCode.WORKER_EXECUTION_ERROR
                assert "bang" in message
                assert details["type"] == "RuntimeError"
            finally:
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)

    def test_worker_answers_an_unknown_message_type(self) -> None:
        """新しい Hub から知らない type が来ても、id があれば unsupported_message で答える。"""
        port = _get_free_port()
        replies: list[Any] = []
        holder: list[Any] = []
        with hub_file_env():
            token = _write_fake_hub_file(port)

            async def handler(ws: Any) -> None:
                hub_nonce, reg = await _greet(ws)
                assert reg["proof"] == client_proof(token, hub_nonce, reg["nonce"])
                ack = RegisterAck(
                    id=reg["id"],
                    ok=True,
                    instance_id="w-x",
                    protocol_version=reg["protocol_version"],
                    package_version=__version__,
                    hub_proof=hub_proof(token, reg["nonce"], hub_nonce),
                )
                await ws.send(encode_message(ack))
                await ws.send(json.dumps({"type": "added_later", "id": "h-1"}))
                replies.append(json.loads(await asyncio.wait_for(ws.recv(), 5.0)))

            t = _run_fake_hub(port, handler, holder)
            app = _qapp()
            worker = Worker(pid=9005, label="e2e-new-hub")
            try:
                worker.connect_to_hub()
                _spin_while_thread(app, t)
            finally:
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)

        assert holder == [("ok", None)], holder
        [reply] = replies
        assert (reply["type"], reply["id"], reply["ok"]) == ("response", "h-1", False)
        assert reply["error"]["code"] == "unsupported_message"
        assert reply["error"]["details"] == {"type": "added_later"}


class TestWorkerHandshake:
    """Worker の側の挨拶：Hub ファイルの token で proof を作り、Hub の proof を確かめる。"""

    def _run(
        self,
        handler: Callable[[Any, str], Awaitable[None]],
        *,
        label: str = "e2e-handshake",
    ) -> tuple[Worker, list[str], list[str]]:
        """偽の Hub に Worker をつなぎ、(worker, registered, register_failed) を返す。"""
        port = _get_free_port()
        holder: list[Any] = []
        registered: list[str] = []
        failed: list[str] = []
        with hub_file_env():
            token = _write_fake_hub_file(port)

            async def bound(ws: Any) -> None:
                await handler(ws, token)

            t = _run_fake_hub(port, bound, holder)
            app = _qapp()
            worker = Worker(
                pid=9007,
                label=label,
                auto_reconnect=False,
            )
            worker.registered.connect(registered.append)
            worker.register_failed.connect(failed.append)
            try:
                worker.connect_to_hub()
                _spin_while_thread(app, t)
            finally:
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)
        assert holder == [("ok", None)], holder
        return worker, registered, failed

    def test_an_ack_with_a_wrong_hub_proof_is_not_trusted(self) -> None:
        """token を知らない相手の ok=True を信じない：登録せず、切り、QGIS も閉じない。"""
        closed: list[bool] = []

        async def handler(ws: Any, _token: str) -> None:
            _hub_nonce, reg = await _greet(ws)
            ack = RegisterAck(
                id=reg["id"],
                ok=True,
                instance_id="w-fake",
                protocol_version=reg["protocol_version"],
                package_version=__version__,
                hub_proof="0" * 64,
            )
            await ws.send(encode_message(ack))
            closed.append(await _wait_closed(ws))

        worker, registered, failed = self._run(handler)
        assert closed == [True], "the Worker must hang up on an untrusted Hub"
        assert registered == []
        assert failed == [], "an untrusted ack must not stop the Worker (close QGIS)"
        assert worker.instance_id is None

    def test_a_launch_abandoned_without_a_hub_proof_is_ignored(self) -> None:
        """偽の Hub に QGIS を閉じさせない：proof の無い launch_abandoned は無視する。"""
        closed: list[bool] = []

        async def handler(ws: Any, _token: str) -> None:
            _hub_nonce, reg = await _greet(ws)
            ack = RegisterAck(
                id=reg["id"],
                ok=False,
                error=Error(code=ErrorCode.LAUNCH_ABANDONED, message="abandoned"),
            )
            await ws.send(encode_message(ack))
            closed.append(await _wait_closed(ws))

        _worker, registered, failed = self._run(handler)
        assert closed == [True]
        assert registered == []
        assert failed == []

    def test_a_version_mismatch_asks_to_install_the_same_version(self) -> None:
        """版が合わなければ register_failed で「同じ版を入れて」と言う（proof は無くてよい）。"""

        async def handler(ws: Any, _token: str) -> None:
            _hub_nonce, reg = await _greet(ws)
            ack = RegisterAck(
                id=reg["id"],
                ok=False,
                package_version="9.9.9",
                error=Error(
                    code=ErrorCode.PROTOCOL_VERSION_MISMATCH,
                    message="protocol version mismatch",
                    details={"hub_package_version": "9.9.9"},
                ),
            )
            await ws.send(encode_message(ack))
            await _wait_closed(ws)

        _worker, registered, [reason] = self._run(handler)
        assert registered == []
        assert reason.startswith("protocol_version_mismatch")
        assert "9.9.9" in reason
        assert __version__ in reason
        assert "install the same version" in reason

    def test_no_hub_file_means_no_connection(self) -> None:
        """Hub ファイルが読めなければ、どこにもつながない（再接続で読み直す）。"""
        app = _qapp()
        registered: list[str] = []
        failed: list[str] = []
        with hub_file_env() as hub_file:
            worker = Worker(pid=9008, label="e2e-no-file", reconnect_delay_ms=100)
            worker.registered.connect(registered.append)
            worker.register_failed.connect(failed.append)
            try:
                worker.connect_to_hub()
                _spin_until(app, lambda: False, timeout=0.5)
                assert not worker.is_registered
                assert registered == []
                assert failed == []

                # Hub が現れれば、再接続で Hub ファイルを読み直してつながる
                with _hub_process(_get_free_port(), hub_file):
                    assert _spin_until(app, lambda: worker.is_registered, timeout=10.0)
            finally:
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)
