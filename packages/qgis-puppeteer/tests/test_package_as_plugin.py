"""`qgis_puppeteer` パッケージが QGIS プラグインの土台になるための約束（ライブラリ側）。

QGIS はプラグインフォルダ `qgis_puppeteer`（このパッケージの写し + プラグイン
固有のファイル）を `import qgis_puppeteer` して `classFactory(iface)` を呼ぶ。

- QGIS の Python に websockets が無くても import が通る（client は遅延 import）
- プラグイン固有の部分を持たないコピー（= このライブラリ、wheel）が QGIS に
  読まれたら、そう言って止まる

プラグインフォルダそのものの検証は `plugins/tests/test_plugin_folder.py`。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import qgis_puppeteer


class TestImportWithoutWebsockets:
    def test_package_imports_when_websockets_is_missing(self) -> None:
        code = """
            import sys
            sys.modules["websockets"] = None  # import websockets -> ImportError
            import qgis_puppeteer
            import qgis_puppeteer.hub_spawn, qgis_puppeteer.hub_state
            import qgis_puppeteer.protocol, qgis_puppeteer.worker_state
            assert callable(qgis_puppeteer.classFactory)
            assert "qgis_puppeteer.client" not in sys.modules
            print("ok")
        """
        r = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "ok"

    def test_client_names_still_resolve_from_the_top_level(self) -> None:
        from qgis_puppeteer.client import AutomationClient, RequestError

        assert qgis_puppeteer.AutomationClient is AutomationClient
        assert qgis_puppeteer.RequestError is RequestError
        for name in ("AutomationClient", "RequestError", "NotConnectedError"):
            assert name in qgis_puppeteer.__all__

    def test_unknown_attribute_is_still_an_attribute_error(self) -> None:
        with pytest.raises(AttributeError, match="no_such_name"):
            _ = qgis_puppeteer.no_such_name  # type: ignore[attr-defined]


class TestClassFactoryOnTheLibraryCopy:
    def test_library_has_no_plugin_part(self) -> None:
        """qgis_plugin/ はプラグインフォルダ側が正。ライブラリ（wheel）に混ぜない。"""
        pkg = Path(qgis_puppeteer.__file__).resolve().parent
        assert not (pkg / "qgis_plugin").exists()
        assert not (pkg / "metadata.txt").exists()

    def test_library_copy_loaded_as_a_plugin_says_so(self) -> None:
        """ライブラリのコピーが QGIS に読まれたら、場所を名指しして止まる。"""
        with pytest.raises(ImportError) as ei:
            qgis_puppeteer.classFactory(object())
        message = str(ei.value)
        assert "pip-installed library copy" in message
        assert str(Path(qgis_puppeteer.__file__).resolve().parent) in message
