"""Qt Worker × 実 Hub subprocess のライフサイクル統合テスト。

mapix 側の実機検証で見つかった挙動を、QGIS 無しで再現する:

- Hub プロセスが死ぬ → Worker が Hub を立て直して再登録する（検証 2 #A）
- GUI スレッドが塞がる → `unresponsive` になるが一覧から消えず、空けば
  `active` に戻る（liveness report 1・2）
- `update_project` → Hub の `project` が更新され、project basename の selector で
  ルーティングできる（検証 1 #3）
- Worker が bye 無しで切れる → `instance_disconnected` と `grace_expires_in`
  （検証 1 #3 / 3）

構成は `test_worker_integration` と同じ（Hub subprocess + Qt メインスレッド +
asyncio クライアントを別スレッド）。PyQt5 が無い環境では全件 skip。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt5.QtWebSockets")

from qgis_puppeteer import hub_spawn  # noqa: E402
from qgis_puppeteer.client import AutomationClient, RequestError  # noqa: E402
from qgis_puppeteer.hub_state import HEARTBEAT_TIMEOUT_SECONDS  # noqa: E402
from qgis_puppeteer.protocol import ErrorCode, InstanceInfo  # noqa: E402
from qgis_puppeteer.worker import Worker  # noqa: E402

from tests.test_worker_integration import (  # noqa: E402
    _asyncio_in_thread,
    _get_free_port,
    _hub_subprocess,
    _qapp,
    _spin_until,
    _spin_while_thread,
)

pytestmark = pytest.mark.integration


def _client_result(app: Any, coro_factory: Any) -> Any:
    """asyncio クライアント処理を別スレッドで走らせ、その間 Qt ループを回して結果を返す。"""
    holder: list[Any] = []
    t = _asyncio_in_thread(coro_factory, holder)
    _spin_while_thread(app, t)
    assert holder, "client thread produced no result"
    kind, value = holder[0]
    assert kind == "ok", f"client failed: {value!r}"
    return value


def _list(port: int, *, include_disconnected: bool = False) -> Any:
    async def inner() -> list[InstanceInfo]:
        async with AutomationClient(url=f"ws://127.0.0.1:{port}") as client:
            return await client.list_instances(include_disconnected=include_disconnected)

    return inner


def _spin_for(app: Any, seconds: float) -> None:
    _spin_until(app, lambda: False, timeout=seconds)


def _wait_project(app: Any, port: int, expected: str | None, timeout: float = 5.0) -> None:
    """Hub 上の project が expected になるまで list_instances を繰り返す。

    update_info は Worker 側で QWebSocket に積まれ、Qt ループが回ったときに
    流れる。直後に別スレッドから list すると先に届くことがあるので、待つ。
    """
    deadline = time.monotonic() + timeout
    seen: Any = object()
    while time.monotonic() < deadline:
        [info] = _client_result(app, _list(port))
        seen = info.project
        if seen == expected:
            return
        _spin_for(app, 0.1)
    raise AssertionError(f"Hub project stayed {seen!r}, expected {expected!r}")


class TestHubRespawn:
    def test_hub_killed_worker_respawns_a_new_hub_and_reregisters(self, tmp_path: Path) -> None:
        """検証 2 #A: Hub を kill しても QGIS（Worker）は孤立しない。

        以前は再接続タイマーが socket を開き直すだけで、Hub を作る経路は初回の
        connect_to_hub にしか無かった。100 秒待っても Hub は戻らなかった。
        """
        port = _get_free_port()
        app = _qapp()
        spawn_cmd = [
            sys.executable,
            "-m",
            "qgis_puppeteer.hub",
            "--port",
            str(port),
            "--no-pid-file",
        ]
        spawned_before = len(hub_spawn._SPAWNED_CHILDREN)
        with _hub_subprocess(port) as hub1:
            worker = Worker(
                hub_url=f"ws://127.0.0.1:{port}",
                pid=9101,
                label="e2e-respawn",
                hub_lock_path=tmp_path / "hub.lock",
                hub_spawn_command=spawn_cmd,
                reconnect_delay_ms=200,
            )
            try:
                worker.connect_to_hub()
                assert _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                first_id = worker.instance_id

                hub1.kill()
                hub1.wait(timeout=5.0)
                assert _spin_until(app, lambda: not worker.is_registered, timeout=5.0), (
                    "Worker must notice the Hub is gone"
                )
                # ここが本題: 新しい Hub を立てて登録し直す
                assert _spin_until(app, lambda: worker.is_registered, timeout=30.0), (
                    "Worker must respawn a Hub and re-register on its own"
                )
                # grace は死んだ Hub のメモリにあったので、instance_id は引き継がれない
                assert worker.instance_id != first_id

                infos = _client_result(app, _list(port))
                assert [(i.label, i.state) for i in infos] == [("e2e-respawn", "active")]
                assert len(hub_spawn._SPAWNED_CHILDREN) == spawned_before + 1
            finally:
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)
                for proc in hub_spawn._SPAWNED_CHILDREN[spawned_before:]:
                    if isinstance(proc, subprocess.Popen) and proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5.0)


class TestGuiStall:
    @pytest.mark.slow
    def test_gui_stall_is_reported_unresponsive_and_recovers(self) -> None:
        """liveness report 1・2: GUI が塞がっても一覧から消えず、空けば戻る。

        Qt ループを processEvents 無しで HEARTBEAT_TIMEOUT より長く止める。
        その間、別スレッドの client が list_instances を観測し続ける。
        """
        port = _get_free_port()
        url = f"ws://127.0.0.1:{port}"
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(hub_url=url, pid=9102, label="e2e-stall")
            observed: list[tuple[float, list[tuple[str, str]]]] = []
            stop = threading.Event()

            async def observe() -> None:
                async with AutomationClient(url=url) as client:
                    t0 = time.monotonic()
                    while not stop.is_set():
                        infos = await client.list_instances()
                        observed.append(
                            (round(time.monotonic() - t0, 1), [(i.label, i.state) for i in infos])
                        )
                        await asyncio.sleep(0.5)

            try:
                worker.connect_to_hub()
                assert _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                holder: list[Any] = []
                t = _asyncio_in_thread(observe, holder)
                _spin_for(app, 1.0)

                # GUI スレッド停止（Qt イベントを一切処理しない）
                time.sleep(HEARTBEAT_TIMEOUT_SECONDS + 8.0)

                # 回復: 溜まっていた ping に pong が返る
                _spin_for(app, 2.0)
                stop.set()
                _spin_while_thread(app, t)
                assert holder and holder[0][0] == "ok", holder

                states = [s for _, infos in observed for (_, s) in infos]
                assert "unresponsive" in states, observed
                # 塞がっている間も一覧から消えない
                assert all(len(infos) == 1 for _, infos in observed), observed
                # 回復後は active
                final = _client_result(app, _list(port))
                assert [(i.label, i.state) for i in final] == [("e2e-stall", "active")]
            finally:
                stop.set()
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)


class TestProjectAndDisconnect:
    def test_update_project_reaches_hub_and_project_selector_routes(self) -> None:
        """検証 1 #3: update_info で project が Hub に届き、basename で引ける。"""
        port = _get_free_port()
        url = f"ws://127.0.0.1:{port}"
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(hub_url=url, pid=9103, label="e2e-proj", project=None)
            worker.register_handler("echo", lambda params: {"who": "e2e-proj"})
            try:
                worker.connect_to_hub()
                assert _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                assert _client_result(app, _list(port))[0].project is None

                worker.update_project("D:/work/city.qgz")
                _wait_project(app, port, "D:/work/city.qgz")

                async def route() -> Any:
                    async with AutomationClient(url=url) as client:
                        return await client.call("echo", {}, instance="city.qgz", timeout_ms=5_000)

                assert _client_result(app, route)["who"] == "e2e-proj"

                worker.update_project(None)
                _wait_project(app, port, None)
            finally:
                worker.disconnect_from_hub()
                _spin_until(app, lambda: not worker.is_registered, timeout=3.0)

    def test_dropped_worker_is_reported_disconnected_with_grace(self) -> None:
        """検証 1 #3: bye 無しで切れた Worker は not_found ではなく disconnected。"""
        port = _get_free_port()
        url = f"ws://127.0.0.1:{port}"
        with _hub_subprocess(port):
            app = _qapp()
            worker = Worker(hub_url=url, pid=9104, label="e2e-drop")
            try:
                worker.connect_to_hub()
                assert _spin_until(app, lambda: worker.is_registered, timeout=5.0)
                # bye を送らずに切る = プロセス死亡と同じ見え方（grace へ）
                worker.disconnect_from_hub(send_bye=False)
                assert _spin_until(app, lambda: not worker.is_registered, timeout=3.0)

                async def probe() -> RequestError:
                    async with AutomationClient(url=url) as client:
                        try:
                            await client.call("echo", {}, instance="e2e-drop", timeout_ms=3_000)
                        except RequestError as e:
                            return e
                        raise AssertionError("call must fail while the worker is in grace")

                err = _client_result(app, probe)
                assert err.code is ErrorCode.INSTANCE_DISCONNECTED
                [info] = err.details["instances"]
                assert info["label"] == "e2e-drop"
                assert 0 < info["grace_expires_in"] <= 60

                full = _client_result(app, _list(port, include_disconnected=True))
                assert [(i.label, i.state) for i in full] == [("e2e-drop", "disconnected")]
                assert _client_result(app, _list(port)) == []
            finally:
                worker.disconnect_from_hub()
