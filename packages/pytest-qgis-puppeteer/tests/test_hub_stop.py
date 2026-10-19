"""pytest が起こした Hub の片付け（#14）。

Windows では Hub を ``python-qgis-ltr.bat`` で起こすので、``Popen`` が握るのは
``cmd.exe`` で、Hub の python はその子になる。``terminate()`` では Hub が残っていた。
実プロセスは使わず、``subprocess.run`` とポートの確認を差し替えて手順を確かめる。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from pytest_qgis_puppeteer import plugin

# 日本語版 Windows の netstat。状態の語は言語で訳されることがある（独語版は ABHÖREN）
_NETSTAT = """
アクティブな接続

  プロトコル  ローカル アドレス      外部アドレス           状態            PID
  TCP         0.0.0.0:135            0.0.0.0:0              LISTENING       1204
  TCP         127.0.0.1:60150        0.0.0.0:0              ABHÖREN         17700
  TCP         127.0.0.1:60150        127.0.0.1:60151        ESTABLISHED     23456
  TCP         127.0.0.1:601500       0.0.0.0:0              LISTENING       999
"""


class _Proc:
    def __init__(self, *, alive: bool = True) -> None:
        self.pid = 4242
        self.returncode: int | None = None if alive else 1
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 1
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class TestParseListeningPid:
    def test_finds_the_listener_whatever_the_state_word(self) -> None:
        assert plugin._parse_listening_pid(_NETSTAT, 60150) == 17700

    def test_ignores_connections_and_other_ports(self) -> None:
        assert plugin._parse_listening_pid(_NETSTAT, 60151) is None
        assert plugin._parse_listening_pid(_NETSTAT, 1234) is None


class TestStopHub:
    @pytest.fixture
    def runs(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []
        monkeypatch.setattr(plugin.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd))
        return calls

    def test_windows_kills_the_whole_tree(
        self, monkeypatch: pytest.MonkeyPatch, runs: list[list[str]]
    ) -> None:
        monkeypatch.setattr(plugin.sys, "platform", "win32")
        monkeypatch.setattr(plugin, "_wait_port_closed", lambda port, timeout_s: True)
        plugin._stop_hub(_Proc(), hub_port=60150)  # type: ignore[arg-type]
        assert runs == [["taskkill", "/PID", "4242", "/T", "/F"]]

    def test_a_hub_left_after_its_launcher_died_is_found_by_port(
        self, monkeypatch: pytest.MonkeyPatch, runs: list[list[str]]
    ) -> None:
        """起動用の cmd が先に終わっていても、ポートで待ち受けている Hub を止める。"""
        monkeypatch.setattr(plugin.sys, "platform", "win32")
        closed = iter([False, True])
        monkeypatch.setattr(plugin, "_wait_port_closed", lambda port, timeout_s: next(closed))
        monkeypatch.setattr(plugin, "_listening_pid", lambda port: 17700)
        plugin._stop_hub(_Proc(alive=False), hub_port=60150)  # type: ignore[arg-type]
        assert runs == [["taskkill", "/PID", "17700", "/T", "/F"]]

    def test_warns_when_the_port_stays_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runs: list[list[str]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(plugin.sys, "platform", "win32")
        monkeypatch.setattr(plugin, "_wait_port_closed", lambda port, timeout_s: False)
        monkeypatch.setattr(plugin, "_listening_pid", lambda port: None)
        plugin._stop_hub(_Proc(alive=False), hub_port=60150)  # type: ignore[arg-type]
        assert runs == []
        assert "still open" in caplog.text

    @pytest.mark.skipif(sys.platform == "win32", reason="process groups are posix only")
    def test_posix_stops_the_process_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import signal

        sent: list[tuple[int, Any]] = []
        monkeypatch.setattr(plugin.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
        monkeypatch.setattr(plugin, "_wait_port_closed", lambda port, timeout_s: True)
        plugin._stop_hub(_Proc(), hub_port=60150)  # type: ignore[arg-type]
        assert sent == [(4242, signal.SIGTERM)]


def test_wait_port_closed_on_a_free_port() -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert plugin._wait_port_closed(port, timeout_s=0.5) is True


def test_wait_port_closed_while_listening() -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert plugin._wait_port_closed(port, timeout_s=0.3) is False
