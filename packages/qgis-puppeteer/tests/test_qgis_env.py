"""qgis_puppeteer.qgis_env のユニットテスト。

`find_qgis_python_launcher()` の探索順を網羅する。tmp_path に偽の
`python-qgis-ltr.bat` を配置して `OSGEO4W_ROOT` / `sys.executable` を
monkeypatch することで、実際の QGIS インストールに依存せずに検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qgis_puppeteer import find_qgis_python_launcher
from qgis_puppeteer.qgis_env import _QGIS_PYTHON_LAUNCHER_BAT


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """親プロセスの OSGEO4W_ROOT 等を毎テスト確実にクリア。"""
    monkeypatch.delenv("OSGEO4W_ROOT", raising=False)


# ==============================================================
# OSGEO4W_ROOT 経路
# ==============================================================


class TestOsgeo4wRoot:
    def test_returns_bat_from_osgeo4w_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        bat = osgeo / "bin" / _QGIS_PYTHON_LAUNCHER_BAT
        bat.write_text("")

        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))

        assert find_qgis_python_launcher() == bat

    def test_osgeo4w_root_set_but_bat_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OSGEO4W_ROOT は立っているが配下に .bat がないケース：次の候補へ
        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        # わざと .bat は作らない
        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))

        # sys.executable 兄弟にも置かない
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setattr("sys.executable", str(empty_dir / "python.exe"))

        assert find_qgis_python_launcher() is None


# ==============================================================
# sys.executable 兄弟経路
# ==============================================================


class TestSysExecutableSibling:
    def test_returns_bat_from_sys_executable_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        qgis_bin = bin_dir / "qgis-ltr-bin.exe"
        qgis_bin.write_text("")
        bat = bin_dir / _QGIS_PYTHON_LAUNCHER_BAT
        bat.write_text("")

        monkeypatch.setattr("sys.executable", str(qgis_bin))

        assert find_qgis_python_launcher() == bat


class TestPriorityOrder:
    def test_osgeo4w_root_takes_precedence_over_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 両方に存在する場合は OSGEO4W_ROOT を優先
        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        osgeo_bat = osgeo / "bin" / _QGIS_PYTHON_LAUNCHER_BAT
        osgeo_bat.write_text("")

        sibling_dir = tmp_path / "other" / "bin"
        sibling_dir.mkdir(parents=True)
        qgis_bin = sibling_dir / "qgis-ltr-bin.exe"
        qgis_bin.write_text("")
        (sibling_dir / _QGIS_PYTHON_LAUNCHER_BAT).write_text("")

        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))
        monkeypatch.setattr("sys.executable", str(qgis_bin))

        assert find_qgis_python_launcher() == osgeo_bat


# ==============================================================
# override_env 経路
# ==============================================================


class TestOverrideEnv:
    def test_override_env_used_when_set_and_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        explicit = tmp_path / "my-launcher.bat"
        explicit.write_text("")
        monkeypatch.setenv("MY_OVERRIDE", str(explicit))

        # 他の候補があっても override が勝つ
        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        (osgeo / "bin" / _QGIS_PYTHON_LAUNCHER_BAT).write_text("")
        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))

        assert find_qgis_python_launcher(override_env="MY_OVERRIDE") == explicit

    def test_override_env_missing_file_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 立ってるが指す先が存在しない → 警告を出して次の候補へ進む
        monkeypatch.setenv("MY_OVERRIDE", str(tmp_path / "nope"))

        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        bat = osgeo / "bin" / _QGIS_PYTHON_LAUNCHER_BAT
        bat.write_text("")
        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))

        assert find_qgis_python_launcher(override_env="MY_OVERRIDE") == bat

    def test_override_env_unset_skipped_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # override_env を渡したが env が立っていないケースは普通の探索順
        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        bat = osgeo / "bin" / _QGIS_PYTHON_LAUNCHER_BAT
        bat.write_text("")
        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))

        assert find_qgis_python_launcher(override_env="UNSET_VAR") == bat

    def test_override_env_none_skips_step_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # override_env=None は明示的に「ステップ 1 を skip」する用法
        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        bat = osgeo / "bin" / _QGIS_PYTHON_LAUNCHER_BAT
        bat.write_text("")
        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))

        assert find_qgis_python_launcher(override_env=None) == bat


# ==============================================================
# 何も見つからないケース
# ==============================================================


class TestNotFound:
    def test_returns_none_when_nothing_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OSGEO4W_ROOT 未設定、sys.executable 兄弟にも .bat なし
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr("sys.executable", str(empty / "python.exe"))

        assert find_qgis_python_launcher() is None
