"""プラグイン固有コード（`plugins/qgis_puppeteer/qgis_plugin/`）のテスト設定。

`qgis_puppeteer` 本体は venv に入っているライブラリ（`packages/qgis-puppeteer`、
正）を使い、`qgis_plugin` サブパッケージだけをプラグインフォルダから足す。
こうすると、プラグインフォルダ内のライブラリの写しが古くてもテストは正の
コードで走る（写しのずれは `scripts/sync_plugin.py --check` が別に見る）。
"""

from __future__ import annotations

from pathlib import Path

import qgis_puppeteer

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "qgis_puppeteer"
if str(PLUGIN_DIR) not in qgis_puppeteer.__path__:
    qgis_puppeteer.__path__.append(str(PLUGIN_DIR))
