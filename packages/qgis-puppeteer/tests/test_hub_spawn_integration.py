"""hub_spawn + 実 Hub プロセスの統合テスト。

`try_spawn_hub` / `ensure_hub` で実際に `python -m qgis_puppeteer.hub` を起動し、
Hub ファイルが書かれて挨拶が通るところまでと、Hub の終了コードを確かめる。
Hub ファイルは必ず一時フォルダに置く（使う人の既定の Hub ファイルに触れない）。
"""

from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# 統合テストは PyQt5 を要求（Hub 起動側）
pytest.importorskip("PyQt5.QtWebSockets")

from qgis_puppeteer.hub_file import read_hub_file  # noqa: E402
from qgis_puppeteer.hub_probe import find_live_hub, greet_hub  # noqa: E402
from qgis_puppeteer.hub_spawn import (  # noqa: E402
    HUB_EXIT_ALREADY_RUNNING,
    HUB_EXIT_BIND_FAILED,
    EnsureOutcome,
    SpawnOutcome,
    default_hub_command,
    ensure_hub,
    hub_log_path,
    kill_process_tree,
    try_spawn_hub,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def spawned() -> Iterator[list[subprocess.Popen[bytes]]]:
    """テストが起こした Hub を覚えておき、最後に木ごと止める。"""
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for proc in procs:
        if proc.poll() is None:
            kill_process_tree(proc)
            proc.wait(timeout=5.0)


def _tracking_popen(procs: list[subprocess.Popen[bytes]]):  # noqa: ANN202
    def popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(*args, **kwargs)  # type: ignore[call-overload]
        procs.append(proc)
        return proc

    return popen


class TestHubSpawnIntegration:
    def test_try_spawn_launches_real_hub(
        self, tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
    ) -> None:
        """既定のコマンドで実 Hub を起こし、Hub ファイルのアドレスで挨拶が通る。"""
        hub_file = tmp_path / "hub.json"
        outcome, info = try_spawn_hub(
            hub_file=hub_file,
            popen=_tracking_popen(spawned),
            startup_deadline=15.0,
        )
        assert outcome == SpawnOutcome.SPAWNED
        assert info is not None
        assert info == read_hub_file(hub_file)
        # 空いているポート（port 0）で待ち受け、その実際のポートを書く
        assert not info.addresses[0].endswith(":0")
        ack = greet_hub(info.addresses[0], info.token)
        assert ack.ok
        # Hub のログは Hub ファイルの隣
        assert hub_log_path(hub_file).is_file()

    def test_ensure_spawns_then_second_call_is_noop(
        self, tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
    ) -> None:
        """1 回目で起こし、2 回目は ALREADY_RUNNING で起こさない。"""
        hub_file = tmp_path / "hub.json"
        popen = _tracking_popen(spawned)
        first = ensure_hub(hub_file=hub_file, popen=popen, startup_deadline=15.0)
        assert first.outcome == EnsureOutcome.SPAWNED_BY_US
        assert len(spawned) == 1

        second = ensure_hub(hub_file=hub_file, popen=popen, startup_deadline=15.0)
        assert second.outcome == EnsureOutcome.ALREADY_RUNNING
        assert second.info == first.info
        assert len(spawned) == 1, "no additional spawn expected"


class TestHubExitCodes:
    def test_exits_3_when_a_live_hub_uses_the_same_hub_file(
        self, tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
    ) -> None:
        """同じ Hub ファイルで生きた Hub がいれば、上書きせずに 3 で終わる。"""
        hub_file = tmp_path / "hub.json"
        first = ensure_hub(hub_file=hub_file, popen=_tracking_popen(spawned), startup_deadline=15.0)
        second = subprocess.run(
            default_hub_command(hub_file=hub_file), capture_output=True, timeout=30
        )
        assert second.returncode == HUB_EXIT_ALREADY_RUNNING
        # 生きている Hub の Hub ファイルはそのまま
        assert read_hub_file(hub_file) == first.info
        assert find_live_hub(hub_file) == first.info

    def test_exits_1_when_the_port_is_taken(self, tmp_path: Path) -> None:
        """待ち受けられなければ 1 で終わる。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if sys.platform == "win32":
                # 後から来た Hub が同じポートを取れないように
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            result = subprocess.run(
                [
                    *default_hub_command(hub_file=tmp_path / "hub.json"),
                    "--port",
                    str(port),
                ],
                capture_output=True,
                timeout=30,
            )
        assert result.returncode == HUB_EXIT_BIND_FAILED
        assert not (tmp_path / "hub.json").exists()
