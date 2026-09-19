"""QGIS Puppeteer の QGIS プラグイン部分（Worker を QGIS に載せる薄いラッパ）。

QGIS のプラグインフォルダ（`plugins/qgis_puppeteer/`）は `qgis_puppeteer`
パッケージの写し + プラグイン固有のファイルで、QGIS はパッケージ直下の
`classFactory`（`qgis_puppeteer/__init__.py`）を呼ぶ。実装はこのサブパッケージに
ある。

このサブパッケージと `metadata.txt` / `icon.png` はプラグインフォルダ側が正で、
GPL-3.0-or-later（一つ上の LICENSE）。ライブラリ（`packages/qgis-puppeteer`、
PyPI の wheel）には存在しない。フォルダの残りはライブラリの写しで、
`scripts/sync_plugin.py` が作る — 手で編集しないこと。
"""
