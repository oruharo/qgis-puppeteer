"""QGIS 環境探索ヘルパ（Qt 非依存）。

QGIS インストールに付随する Python ランチャーや関連パスを発見するための
ユーティリティを提供する。pytest-qgis-puppeteer / qgis_puppet どちらからでも
使える "中立" レイヤ。

## 公開 API

- `find_qgis_python_launcher(*, override_env=None)` — `python-qgis-ltr.bat` の探索

## 設計方針

- `os.environ` / `sys.executable` 以外への副作用なし
- 戻り値は `Path | None`。見つからない場合は呼び出し側で対処（フォールバック /
  エラー表示）を選べる
- 呼び出し側固有の env 変数名（QPUPPETEER_HUB_PYTHON 等）はここでは持たず、
  `override_env` 引数で都度受け取る
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("qgis_puppeteer.qgis_env")

# QGIS が同梱する Python launcher ファイル名（QGIS バンドル版 Python を、
# `QT_PLUGIN_PATH` / `GDAL_DATA` / `PROJ_LIB` 等を整えてから起動するラッパ）。
_QGIS_PYTHON_LAUNCHER_BAT = "python-qgis-ltr.bat"


def find_qgis_python_launcher(*, override_env: str | None = None) -> Path | None:
    """QGIS 同梱の Python launcher (`python-qgis-ltr.bat`) を探す。

    QGIS バンドル版 Python は PyQt5 / GDAL / PROJ などをバンドル内で解決できる
    ため、Hub subprocess を spawn する際に有用。本関数はその launcher の
    絶対パスを発見するだけで、起動は行わない。

    ## 探索順

    1. `override_env` が与えられ、その env 変数に有効なファイルパスが入って
       いればそれを返す。値が立っているがファイルが存在しない場合は警告ログ
       を出して次の候補へフォールスルーする（fail-loud + still-resilient）。
    2. `OSGEO4W_ROOT/bin/python-qgis-ltr.bat`
       — QGIS プラグイン環境では QGIS が必ず `OSGEO4W_ROOT` をセットしている
    3. `<sys.executable>` の兄弟ディレクトリにある `python-qgis-ltr.bat`
       — `qgis-ltr-bin.exe` と同じ `bin/` に置かれているレイアウト

    Args:
        override_env: ユーザ明示指定用 env 変数名（例: `"QPUPPETEER_HUB_PYTHON"`）。
            None ならステップ 1 を skip して 2 から開始。

    Returns:
        見つかった `Path`、見つからなければ `None`。
    """
    # 1. ユーザ明示指定（呼び出し側が env 名を渡してきた場合のみ）
    if override_env:
        override = os.environ.get(override_env)
        if override:
            p = Path(override)
            if p.is_file():
                return p
            logger.warning(
                "%s=%s does not exist; falling through to auto-detection",
                override_env,
                override,
            )

    # 2. OSGEO4W_ROOT/bin/python-qgis-ltr.bat
    osgeo_root = os.environ.get("OSGEO4W_ROOT")
    if osgeo_root:
        candidate = Path(osgeo_root) / "bin" / _QGIS_PYTHON_LAUNCHER_BAT
        if candidate.is_file():
            return candidate

    # 3. sys.executable の兄弟
    exe_dir = Path(sys.executable).parent
    candidate = exe_dir / _QGIS_PYTHON_LAUNCHER_BAT
    if candidate.is_file():
        return candidate

    return None
