"""``--qgis-hub-address``：dev モードで、Hub ファイルの ``addresses`` の代わりにつなぐ宛先。

Hub ファイルに無い宛先（VM の NAT の先など）で動いている Hub につなぐためのもの。
token と TLS の指紋は Hub ファイルから読む（``AutomationClient(address=...)``）。
実の Hub にはつながず、``find_live_hub`` と ``AutomationClient`` を差し替えて、
宛先がそこまで届くことを確かめる。
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_qgis_puppeteer import automation_client as automation_client_module
from pytest_qgis_puppeteer.automation_client import E2EAutomationClient
from pytest_qgis_puppeteer.plugin import ENV_DEV_MODE, _hub_address
from qgis_puppeteer.hub_file import ENV_HUB_FILE, HubFileInfo

pytest_plugins = ["pytester"]


class _Config:
    """``getoption`` だけを持つ設定の代わり。"""

    def __init__(self, address: str | None) -> None:
        self._address = address

    def getoption(self, name: str, default: Any = None) -> Any:
        assert name == "qgis_hub_address"
        return self._address if self._address is not None else default


# ============================================================
# _hub_address
# ============================================================


class TestHubAddress:
    def test_none_when_not_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_DEV_MODE, raising=False)
        assert _hub_address(_Config(None)) is None  # type: ignore[arg-type]

    def test_the_address_in_dev_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_DEV_MODE, "1")
        assert _hub_address(_Config("192.0.2.1:8765")) == "192.0.2.1:8765"  # type: ignore[arg-type]

    def test_a_bracketed_ipv6_address_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_DEV_MODE, "1")
        assert _hub_address(_Config("[::1]:8765")) == "[::1]:8765"  # type: ignore[arg-type]

    @pytest.mark.parametrize("address", ["192.0.2.1", "192.0.2.1:", "host:port"])
    def test_an_address_without_a_port_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, address: str
    ) -> None:
        monkeypatch.setenv(ENV_DEV_MODE, "1")
        with pytest.raises(pytest.UsageError, match="--qgis-hub-address"):
            _hub_address(_Config(address))  # type: ignore[arg-type]

    def test_outside_dev_mode_is_a_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_DEV_MODE, raising=False)
        with pytest.raises(pytest.UsageError, match="dev mode"):
            _hub_address(_Config("192.0.2.1:8765"))  # type: ignore[arg-type]


# ============================================================
# pytest の実行で
# ============================================================


class TestOption:
    def test_a_bad_value_stops_pytest_before_the_tests(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_DEV_MODE, "1")
        pytester.makepyfile("def test_nothing():\n    pass\n")
        result = pytester.runpytest("--qgis-hub-address=no-port")
        assert result.ret == pytest.ExitCode.USAGE_ERROR
        result.stderr.fnmatch_lines(["*--qgis-hub-address*"])

    def test_outside_dev_mode_stops_pytest(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_DEV_MODE, raising=False)
        pytester.makepyfile("def test_nothing():\n    pass\n")
        result = pytester.runpytest("--qgis-hub-address=192.0.2.1:8765")
        assert result.ret == pytest.ExitCode.USAGE_ERROR
        result.stderr.fnmatch_lines(["*dev mode*"])

    def test_dev_mode_looks_for_the_hub_at_the_address(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """dev モードの ``hub_process`` は、その宛先で Hub ファイルの Hub を探す。"""
        monkeypatch.setenv(ENV_DEV_MODE, "1")
        monkeypatch.setenv(ENV_HUB_FILE, str(tmp_path / "hub.json"))
        calls: list[tuple[Path, str | None]] = []

        def fake_find_live_hub(path: Path, *, address: str | None = None) -> HubFileInfo:
            calls.append((Path(path), address))
            return HubFileInfo(addresses=("127.0.0.1:1",), token="t")

        monkeypatch.setattr("qgis_puppeteer.hub_probe.find_live_hub", fake_find_live_hub)
        pytester.makepyfile(
            textwrap.dedent(
                """
                def test_uses_the_running_hub(hub_process):
                    assert hub_process is None
                """
            )
        )
        result = pytester.runpytest("--qgis-hub-address=192.0.2.1:8765")
        result.assert_outcomes(passed=1)
        assert calls == [(tmp_path / "hub.json", "192.0.2.1:8765")]

    def test_dev_mode_without_the_option_uses_the_files_addresses(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_DEV_MODE, "1")
        monkeypatch.setenv(ENV_HUB_FILE, str(tmp_path / "hub.json"))
        calls: list[str | None] = []

        def fake_find_live_hub(path: Path, *, address: str | None = None) -> None:
            calls.append(address)
            return None

        monkeypatch.setattr("qgis_puppeteer.hub_probe.find_live_hub", fake_find_live_hub)
        pytester.makepyfile("def test_uses_the_running_hub(hub_process):\n    pass\n")
        result = pytester.runpytest()
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*no live Hub behind the Hub file*"])
        assert calls == [None]


# ============================================================
# E2EAutomationClient
# ============================================================


class TestE2EAutomationClientAddress:
    @pytest.mark.parametrize("address", [None, "192.0.2.1:8765"])
    def test_the_address_reaches_the_automation_client(
        self, monkeypatch: pytest.MonkeyPatch, address: str | None
    ) -> None:
        made: list[dict[str, Any]] = []

        def fake_client(**kwargs: Any) -> MagicMock:
            made.append(kwargs)
            inner = MagicMock(name="AutomationClient")
            inner.connect = AsyncMock(return_value=None)
            inner.close = AsyncMock(return_value=None)
            return inner

        monkeypatch.setattr(automation_client_module, "AutomationClient", fake_client)
        e2e = E2EAutomationClient(hub_file="hub.json", address=address)
        e2e.connect()
        e2e.close()
        assert len(made) == 1
        assert made[0]["address"] == address
        assert made[0]["hub_file"] == "hub.json"
