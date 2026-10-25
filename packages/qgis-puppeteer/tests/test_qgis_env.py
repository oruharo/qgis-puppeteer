"""qgis_puppeteer.qgis_env のユニットテスト。

`find_qgis_python_launcher()` の探索順を網羅する。tmp_path に偽の
`python-qgis-ltr.bat` / `python-qgis.bat` を配置して `OSGEO4W_ROOT` /
`sys.executable` を monkeypatch することで、実際の QGIS インストールに依存せずに
検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qgis_puppeteer import find_qgis_python_launcher

# LTR 版の名前。通常版は python-qgis.bat
_QGIS_PYTHON_LAUNCHER_BAT = "python-qgis-ltr.bat"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """親プロセスの OSGEO4W_ROOT 等を毎テスト確実にクリア。"""
    monkeypatch.delenv("OSGEO4W_ROOT", raising=False)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


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


# ==============================================================
# 通常版（LTR でない QGIS）の launcher
# ==============================================================


class TestRegularRelease:
    def test_regular_bat_found_under_osgeo4w_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 通常版のインストールには python-qgis.bat しか無い
        bat = _touch(tmp_path / "QGIS" / "bin" / "python-qgis.bat")
        monkeypatch.setenv("OSGEO4W_ROOT", str(tmp_path / "QGIS"))
        monkeypatch.setattr("sys.executable", str(tmp_path / "venv" / "python.exe"))

        assert find_qgis_python_launcher() == bat

    def test_sys_executable_name_picks_matching_bat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OSGeo4W に LTR 版と通常版を両方入れていても、動いている QGIS
        # （QGIS の中では sys.executable）と同じ版の bat を選ぶ
        bin_dir = tmp_path / "OSGeo4W" / "bin"
        _touch(bin_dir / "python-qgis-ltr.bat")
        regular = _touch(bin_dir / "python-qgis.bat")
        monkeypatch.setenv("OSGEO4W_ROOT", str(tmp_path / "OSGeo4W"))
        monkeypatch.setattr("sys.executable", str(_touch(bin_dir / "qgis-bin.exe")))

        assert find_qgis_python_launcher() == regular

    def test_unknown_executable_prefers_ltr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 実行ファイルから版が分からなければ、これまでどおり LTR を先に
        bin_dir = tmp_path / "OSGeo4W" / "bin"
        ltr = _touch(bin_dir / "python-qgis-ltr.bat")
        _touch(bin_dir / "python-qgis.bat")
        monkeypatch.setenv("OSGEO4W_ROOT", str(tmp_path / "OSGeo4W"))
        monkeypatch.setattr("sys.executable", str(tmp_path / "venv" / "python.exe"))

        assert find_qgis_python_launcher() == ltr


# ==============================================================
# qgis_bin の隣
# ==============================================================


class TestQgisBin:
    @pytest.fixture(autouse=True)
    def _venv_python(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # pytest は venv の Python で動き、その隣に bat は無い
        monkeypatch.setattr("sys.executable", str(tmp_path / "venv" / "python.exe"))

    @pytest.mark.parametrize(
        ("exe", "bat"),
        [
            ("qgis-ltr-bin.exe", "python-qgis-ltr.bat"),
            ("qgis-bin.exe", "python-qgis.bat"),
            ("qgis-ltr.bat", "python-qgis-ltr.bat"),
            ("qgis.bat", "python-qgis.bat"),
            ("qgis-dev-bin.exe", "python-qgis-dev.bat"),
            ("QGIS-LTR-BIN.EXE", "python-qgis-ltr.bat"),
        ],
    )
    def test_bat_matching_the_executable(self, tmp_path: Path, exe: str, bat: str) -> None:
        bin_dir = tmp_path / "QGIS 3.40.1" / "bin"
        for name in ("python-qgis-ltr.bat", "python-qgis.bat", "python-qgis-dev.bat"):
            _touch(bin_dir / name)
        qgis_bin = _touch(bin_dir / exe)

        found = find_qgis_python_launcher(qgis_bin=qgis_bin)
        assert found is not None and found.name == bat

    def test_qgis_bin_wins_over_osgeo4w_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _touch(tmp_path / "other" / "bin" / _QGIS_PYTHON_LAUNCHER_BAT)
        monkeypatch.setenv("OSGEO4W_ROOT", str(tmp_path / "other"))
        bat = _touch(tmp_path / "QGIS" / "bin" / "python-qgis.bat")

        found = find_qgis_python_launcher(qgis_bin=str(tmp_path / "QGIS" / "bin" / "qgis-bin.exe"))
        assert found == bat

    def test_other_name_next_to_qgis_bin(self, tmp_path: Path) -> None:
        # 名前から決まる bat が無ければ、隣にある方を使う
        bat = _touch(tmp_path / "bin" / _QGIS_PYTHON_LAUNCHER_BAT)

        assert find_qgis_python_launcher(qgis_bin=tmp_path / "bin" / "qgis-bin.exe") == bat

    def test_falls_back_to_osgeo4w_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bat = _touch(tmp_path / "QGIS" / "bin" / "python-qgis.bat")
        monkeypatch.setenv("OSGEO4W_ROOT", str(tmp_path / "QGIS"))
        # qgis_bin は launcher の無い場所（ホスト側の配布物など）
        qgis_bin = _touch(tmp_path / "app" / "qgis-bin.exe")

        assert find_qgis_python_launcher(qgis_bin=qgis_bin) == bat

    def test_override_env_wins_over_qgis_bin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        explicit = _touch(tmp_path / "my-launcher.bat")
        monkeypatch.setenv("MY_OVERRIDE", str(explicit))
        _touch(tmp_path / "bin" / "python-qgis.bat")

        found = find_qgis_python_launcher(
            override_env="MY_OVERRIDE", qgis_bin=tmp_path / "bin" / "qgis-bin.exe"
        )
        assert found == explicit
