"""qgis_puppeteer.hub_file のユニットテスト（Qt 非依存）。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from qgis_puppeteer.hub_file import (
    ENV_HUB_FILE,
    FORMAT,
    HubFileError,
    HubFileInfo,
    default_hub_file_path,
    now_iso,
    read_hub_file,
    remove_hub_file,
    resolve_hub_file_path,
    write_hub_file,
)
from qgis_puppeteer.protocol import ProtocolRange

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX のファイル権限")


def _info(token: str = "tok-1", **fields: object) -> HubFileInfo:
    return HubFileInfo(addresses=("127.0.0.1:9876",), token=token, **fields)  # type: ignore[arg-type]


def _write_raw(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestWriteAndRead:
    def test_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "hub.json"
        info = _info(
            pid=1234,
            package_version="0.1.0",
            protocol=ProtocolRange(2, 3),
            started_at="2026-10-30T00:00:00Z",
        )
        write_hub_file(path, info)
        assert read_hub_file(path) == info

    def test_the_file_carries_its_format(self, tmp_path: Path) -> None:
        path = tmp_path / "hub.json"
        write_hub_file(path, _info())
        assert json.loads(path.read_text(encoding="utf-8"))["format"] == FORMAT

    def test_creates_missing_folders(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "hub.json"
        write_hub_file(path, _info())
        assert read_hub_file(path).token == "tok-1"

    def test_writing_again_replaces_the_file_and_leaves_no_temporary(self, tmp_path: Path) -> None:
        path = tmp_path / "hub.json"
        write_hub_file(path, _info("old"))
        write_hub_file(path, _info("new"))
        assert read_hub_file(path).token == "new"
        assert [p.name for p in tmp_path.iterdir()] == ["hub.json"]

    def test_unknown_fields_are_kept_when_written_back(self, tmp_path: Path) -> None:
        """新しい版の Hub が足した項目は、読んで書き戻しても消えない。"""
        path = tmp_path / "hub.json"
        _write_raw(path, {"format": FORMAT, "token": "t", "addresses": [], "added_later": [1]})
        info = read_hub_file(path)
        assert info.extra == {"added_later": [1]}
        write_hub_file(path, info)
        assert json.loads(path.read_text(encoding="utf-8"))["added_later"] == [1]

    def test_fields_of_the_wrong_type_are_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "hub.json"
        _write_raw(
            path,
            {
                "format": FORMAT,
                "token": "t",
                "addresses": ["127.0.0.1:1", 7],
                "pid": True,
                "tls": "yes",
                "package_version": 1,
                "protocol": "2",
                "started_at": 0,
            },
        )
        info = read_hub_file(path)
        assert info.addresses == ("127.0.0.1:1",)
        assert (info.pid, info.tls, info.package_version, info.protocol, info.started_at) == (
            None,
            None,
            None,
            None,
            None,
        )

    @posix_only
    def test_only_the_owner_can_read_it(self, tmp_path: Path) -> None:
        path = tmp_path / "new-folder" / "hub.json"
        write_hub_file(path, _info())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


class TestReadErrors:
    def test_a_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "hub.json"
        with pytest.raises(HubFileError, match="is the Hub running") as ei:
            read_hub_file(path)
        assert ei.value.path == path
        # 「Hub につながらない」と同じに扱えるように
        assert isinstance(ei.value, OSError)

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            ("{not json", "not valid JSON"),
            ("[1, 2]", "not a JSON object"),
            (json.dumps({"format": FORMAT + 1, "token": "t"}), "has format"),
            (json.dumps({"token": "t"}), "has format None"),
            (json.dumps({"format": FORMAT}), "no token"),
            (json.dumps({"format": FORMAT, "token": ""}), "no token"),
            (json.dumps({"format": FORMAT, "token": 5}), "no token"),
        ],
    )
    def test_a_broken_file(self, tmp_path: Path, payload: str, match: str) -> None:
        path = tmp_path / "hub.json"
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(HubFileError, match=match):
            read_hub_file(path)


class TestRemove:
    def test_removes_its_own_file(self, tmp_path: Path) -> None:
        path = tmp_path / "hub.json"
        write_hub_file(path, _info("mine"))
        assert remove_hub_file(path, token="mine") is True
        assert not path.exists()

    def test_leaves_another_hubs_file(self, tmp_path: Path) -> None:
        path = tmp_path / "hub.json"
        write_hub_file(path, _info("theirs"))
        assert remove_hub_file(path, token="mine") is False
        assert read_hub_file(path).token == "theirs"

    def test_a_missing_or_broken_file_is_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "hub.json"
        assert remove_hub_file(path, token="mine") is False
        path.write_text("{broken", encoding="utf-8")
        assert remove_hub_file(path, token="mine") is False
        assert path.exists()


class TestWhereTheFileIs:
    def test_explicit_then_environment_then_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_HUB_FILE, raising=False)
        assert resolve_hub_file_path() == default_hub_file_path()
        monkeypatch.setenv(ENV_HUB_FILE, str(tmp_path / "env.json"))
        assert resolve_hub_file_path() == tmp_path / "env.json"
        # 空の明示は「指定なし」
        assert resolve_hub_file_path("") == tmp_path / "env.json"
        assert resolve_hub_file_path(tmp_path / "explicit.json") == tmp_path / "explicit.json"

    def test_an_empty_environment_variable_means_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_HUB_FILE, "")
        assert resolve_hub_file_path() == default_hub_file_path()

    @pytest.mark.skipif(os.name != "nt", reason="Windows の既定の場所")
    def test_default_on_windows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_hub_file_path() == tmp_path / "qgis_puppeteer" / "hub.json"
        monkeypatch.delenv("LOCALAPPDATA")
        assert default_hub_file_path() == (
            Path.home() / "AppData" / "Local" / "qgis_puppeteer" / "hub.json"
        )

    @posix_only
    def test_default_on_posix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert default_hub_file_path() == tmp_path / "qgis_puppeteer" / "hub.json"
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        assert default_hub_file_path() == (
            Path.home() / ".local" / "state" / "qgis_puppeteer" / "hub.json"
        )


def test_started_at_is_utc_to_the_second() -> None:
    value = now_iso()
    assert value.endswith("Z")
    assert "." not in value
