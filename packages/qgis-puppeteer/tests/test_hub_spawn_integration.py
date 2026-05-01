"""hub_spawn + 実 Hub プロセスの統合テスト。

`try_spawn_hub` で実際に `python -m qgis_puppeteer.hub` を起動し、
listen 確認と後始末（pid 削除・プロセス終了）まで検証する。
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

# 統合テストは PyQt5 を要求（Hub 起動側）
pytest.importorskip("PyQt5.QtWebSockets")

import contextlib

from qgis_puppeteer.hub_spawn import (  # noqa: E402
    SpawnOutcome,
    default_hub_command,
    ensure_hub_reachable,
    tcp_probe,
    try_spawn_hub,
)

pytestmark = pytest.mark.integration


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestHubSpawnIntegration:
    def test_try_spawn_launches_real_hub(self, tmp_path: Path) -> None:
        """デフォルトコマンドで実 Hub を spawn して listen を確認。"""
        lock_path = tmp_path / "real.lock"
        port = _get_free_port()

        spawned_procs: list[subprocess.Popen[bytes]] = []

        def tracking_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            proc = subprocess.Popen(*args, **kwargs)  # type: ignore[arg-type]
            spawned_procs.append(proc)
            return proc

        try:
            outcome = try_spawn_hub(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                command=default_hub_command(port=port) + ["--no-pid-file"],
                popen=tracking_popen,
                startup_deadline=10.0,
                poll_interval=0.1,
            )
            assert outcome == SpawnOutcome.SPAWNED
            assert tcp_probe("127.0.0.1", port, timeout=1.0) is True
        finally:
            for proc in spawned_procs:
                if proc.poll() is None:
                    with contextlib.suppress(OSError):
                        proc.terminate()
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2.0)

    def test_ensure_spawns_then_second_call_is_noop(self, tmp_path: Path) -> None:
        """1 回目で spawn、2 回目は ALREADY_LISTENING で spawn されない。"""
        lock_path = tmp_path / "twice.lock"
        port = _get_free_port()

        spawned_procs: list[subprocess.Popen[bytes]] = []

        def tracking_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            proc = subprocess.Popen(*args, **kwargs)  # type: ignore[arg-type]
            spawned_procs.append(proc)
            return proc

        try:
            first = ensure_hub_reachable(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                command=default_hub_command(port=port) + ["--no-pid-file"],
                popen=tracking_popen,
                startup_deadline=10.0,
                poll_interval=0.1,
                connect_backoff=(0.2,),
            )
            from qgis_puppeteer.hub_spawn import EnsureOutcome

            assert first == EnsureOutcome.SPAWNED_BY_US
            assert len(spawned_procs) == 1

            second = ensure_hub_reachable(
                lock_path=lock_path,
                host="127.0.0.1",
                port=port,
                command=default_hub_command(port=port) + ["--no-pid-file"],
                popen=tracking_popen,
                startup_deadline=10.0,
                poll_interval=0.1,
                connect_backoff=(0.2,),
            )
            assert second == EnsureOutcome.ALREADY_LISTENING
            assert len(spawned_procs) == 1, "no additional spawn expected"
        finally:
            for proc in spawned_procs:
                if proc.poll() is None:
                    with contextlib.suppress(OSError):
                        proc.terminate()
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2.0)
