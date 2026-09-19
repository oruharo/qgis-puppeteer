"""`plugins/qgis_puppeteer/` が、そのまま QGIS に置けるプラグインフォルダであること。

QGIS と同じく「フォルダの親を sys.path の先頭に置いて `import qgis_puppeteer`」
した別プロセスで確かめる（このプロセスの `qgis_puppeteer` は venv のライブラリ）。
"""

from __future__ import annotations

import configparser
import subprocess
import sys
import textwrap

import pytest
import qgis_puppeteer
from conftest import PLUGIN_DIR


def test_folder_imports_like_qgis_does_without_websockets() -> None:
    code = f"""
        import sys
        sys.path.insert(0, {str(PLUGIN_DIR.parent)!r})
        sys.modules["websockets"] = None  # QGIS の Python には無い
        import qgis_puppeteer
        import qgis_puppeteer.qgis_plugin.plugin_helpers as ph
        import qgis_puppeteer.qgis_plugin.handlers
        from pathlib import Path
        assert Path(qgis_puppeteer.__file__).resolve().parent == Path({str(PLUGIN_DIR)!r}).resolve()
        assert callable(qgis_puppeteer.classFactory)
        assert ph._HUB_BOOTSTRAP.is_file()
        assert "qgis_puppeteer.client" not in sys.modules
        print("ok")
    """
    r = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def test_metadata_names_the_plugin() -> None:
    meta = configparser.ConfigParser()
    meta.read(PLUGIN_DIR / "metadata.txt", encoding="utf-8")
    assert meta["general"]["name"] == "QGIS Puppeteer"
    assert (PLUGIN_DIR / meta["general"]["icon"]).is_file()
    assert (PLUGIN_DIR / "LICENSE").is_file()


def test_missing_pyqt_is_not_reported_as_shadowing(monkeypatch: pytest.MonkeyPatch) -> None:
    """qgis_plugin はあるのに中の import が失敗した場合は、そのまま上げる。"""
    monkeypatch.setitem(sys.modules, "qgis_puppeteer.qgis_plugin.plugin", None)
    with pytest.raises(ModuleNotFoundError) as ei:
        qgis_puppeteer.classFactory(object())
    assert ei.value.name == "qgis_puppeteer.qgis_plugin.plugin"
