"""QGIS 環境探索ヘルパ（Qt 非依存）。

QGIS インストールに付随する Python ランチャーや関連パスを発見するための
ユーティリティを提供する。pytest-qgis-puppeteer / QGIS プラグイン（qgis_plugin）どちらからでも
使える "中立" レイヤ。

## 公開 API

- `find_qgis_python_launcher(*, override_env=None, qgis_bin=None)` — QGIS 同梱の
  Python launcher（`python-qgis-ltr.bat` / `python-qgis.bat`）の探索

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
import re
import sys
from pathlib import Path

logger = logging.getLogger("qgis_puppeteer.qgis_env")

# QGIS が同梱する Python launcher のファイル名（QGIS バンドル版 Python を、
# `QT_PLUGIN_PATH` / `GDAL_DATA` / `PROJ_LIB` 等を整えてから起動するラッパ）。
# 名前は QGIS のパッケージで変わる: LTR 版は `qgis-ltr-bin.exe` の隣に
# `python-qgis-ltr.bat`、通常版は `qgis-bin.exe` の隣に `python-qgis.bat`。
# 実行ファイルが分からないときはこの順に探す。
_QGIS_PYTHON_LAUNCHER_BATS = ("python-qgis-ltr.bat", "python-qgis.bat")

# `qgis-ltr-bin` / `qgis-bin` / `qgis-ltr`（bat）/ `qgis` から、接尾辞（`-ltr` など）を取り出す
_QGIS_EXE_STEM = re.compile(r"^qgis(?P<suffix>(?:-[a-z0-9]+)*?)(?:-bin)?$", re.IGNORECASE)


def _launcher_names(qgis_exe: Path | None) -> list[str]:
    """探す launcher の名前。QGIS の実行ファイルの名前から決まるものを先頭に置く。"""
    names: list[str] = []
    if qgis_exe is not None:
        m = _QGIS_EXE_STEM.match(qgis_exe.stem)
        if m:
            names.append(f"python-qgis{m.group('suffix').lower()}.bat")
    names.extend(n for n in _QGIS_PYTHON_LAUNCHER_BATS if n not in names)
    return names


def _find_in(directory: Path, names: list[str]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def find_qgis_python_launcher(
    *, override_env: str | None = None, qgis_bin: str | os.PathLike[str] | None = None
) -> Path | None:
    """QGIS 同梱の Python launcher（`python-qgis-ltr.bat` / `python-qgis.bat`）を探す。

    QGIS バンドル版 Python は PyQt5 / GDAL / PROJ などをバンドル内で解決できる
    ため、Hub subprocess を spawn する際に有用。本関数はその launcher の
    絶対パスを発見するだけで、起動は行わない。

    ## 探索順

    1. `override_env` が与えられ、その env 変数に有効なファイルパスが入って
       いればそれを返す。値が立っているがファイルが存在しない場合は警告ログ
       を出して次の候補へフォールスルーする（fail-loud + still-resilient）。
    2. `qgis_bin`（QGIS の実行ファイル）が与えられれば、その隣
    3. `OSGEO4W_ROOT/bin/` — QGIS プラグイン環境では QGIS が必ず `OSGEO4W_ROOT`
       をセットしている
    4. `<sys.executable>` の隣 — QGIS の中では `sys.executable` が
       `qgis-ltr-bin.exe` などになる

    各フォルダでは、QGIS の実行ファイル（`qgis_bin`、無ければ `sys.executable`）の
    名前に対応する launcher を先に探す（`qgis-bin.exe` なら `python-qgis.bat`、
    `qgis-ltr-bin.exe` なら `python-qgis-ltr.bat`）。LTR 版と通常版を同じ
    OSGeo4W に入れていても、起こす QGIS と同じ版の Python になる。対応が
    分からなければ `python-qgis-ltr.bat`、`python-qgis.bat` の順。

    Args:
        override_env: ユーザ明示指定用 env 変数名（例: `"QPUPPETEER_HUB_PYTHON"`）。
            None ならステップ 1 を skip して 2 から開始。
        qgis_bin: QGIS の実行ファイルのパス。None ならステップ 2 を skip する。

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

    exe = Path(sys.executable)
    qgis_exe = Path(qgis_bin) if qgis_bin else exe
    names = _launcher_names(qgis_exe)

    # 2. qgis_bin の隣
    if qgis_bin:
        found = _find_in(Path(qgis_bin).parent, names)
        if found is not None:
            return found

    # 3. OSGEO4W_ROOT/bin
    osgeo_root = os.environ.get("OSGEO4W_ROOT")
    if osgeo_root:
        found = _find_in(Path(osgeo_root) / "bin", names)
        if found is not None:
            return found

    # 4. sys.executable の隣
    return _find_in(exe.parent, names)
