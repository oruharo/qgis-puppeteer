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
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

import pytest

# PyQt5 が無い環境では全件 skip
pytest.importorskip("PyQt5.QtWebSockets")

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from qgis_puppeteer.client import AutomationClient  # noqa: E402
from qgis_puppeteer.worker import Worker  # noqa: E402

pytestmark = pytest.mark.integration


# ============================================================
# フィクスチャ：Hub subprocess
# ============================================================


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port_open(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.1)
    raise TimeoutError(f"Hub did not start within {timeout}s on port {port}")


@contextmanager
def _hub_subprocess(port: int) -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "qgis_puppeteer.hub",
            "--port",
            str(port),
            "--no-pid-file",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_port_open(port)
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
# テスト
# ============================================================


class TestWorkerIntegration:
    def test_worker_registers_with_real_hub(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(
                hub_url=f"ws://127.0.0.1:{port}",
                pid=9001,
                label="e2e-reg",
            )
            try:
                worker.connect_to_hub()
                ok = _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                assert ok, "Worker failed to register"
                assert worker.instance_id == "worker-e2e-reg-9001"
            finally:
                worker.disconnect_from_hub()
                # disconnected まで drain
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)

    def test_worker_handles_request_via_automation_client(self) -> None:
        port = _get_free_port()
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(
                hub_url=f"ws://127.0.0.1:{port}",
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
                    async with AutomationClient(url=f"ws://127.0.0.1:{port}") as client:
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
                hub_url=f"ws://127.0.0.1:{port}",
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
                    async with AutomationClient(url=f"ws://127.0.0.1:{port}") as client:
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

    def test_worker_uses_existing_hub_when_lock_path_given(self) -> None:
        """hub_lock_path 指定時でも、既に Hub が listen していれば
        spawn は呼ばれず接続する。"""
        import tempfile
        from pathlib import Path

        port = _get_free_port()
        with _hub_subprocess(port):
            app = _qapp()
            with tempfile.TemporaryDirectory() as td:
                lock_path = Path(td) / "hub.lock"
                worker = Worker(
                    hub_url=f"ws://127.0.0.1:{port}",
                    pid=9005,
                    label="e2e-existing",
                    hub_lock_path=lock_path,
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
        import tempfile
        from pathlib import Path

        # Hub は明示的に起動しない。probe は失敗、spawn で即死 → HubStartupError
        port = _get_free_port()
        app = _qapp()
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "hub.lock"
            worker = Worker(
                hub_url=f"ws://127.0.0.1:{port}",
                pid=9006,
                label="e2e-fail",
                hub_lock_path=lock_path,
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
                hub_url=f"ws://127.0.0.1:{port}",
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
                    async with AutomationClient(url=f"ws://127.0.0.1:{port}") as client:
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
