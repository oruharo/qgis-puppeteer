"""QgsApplication を初期化した子プロセスでコードを走らせる（描画系テスト用）。

Qt テスト一式は 1 プロセスで QCoreApplication / QApplication を使い回すので、
地図の描画に要る QgsApplication をその中には作れない（アプリケーションは
プロセスに 1 つ）。描画を確かめるテストは子プロセスで QGIS を初期化し、
``RESULT`` に入れた値を JSON で受け取る。

``sys.executable`` は `scripts/qt_tests.py` から呼ばれたときの QGIS の Python で、
QGIS の環境変数（QGIS_PREFIX_PATH 等）と PYTHONPATH をそのまま引き継ぐ。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from typing import Any

_MARK = "@@RESULT@@"

_PRELUDE = """
import json, os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qgis.core import QgsApplication
_app = QgsApplication([], False)
_app.initQgis()
RESULT = None
"""

# QGIS は interpreter の終了処理で落ちることがあるので、結果を書いたら即 _exit する
_EPILOGUE = f"""
sys.stdout.write({_MARK!r} + json.dumps(RESULT, default=str) + "\\n")
sys.stdout.flush()
os._exit(0)
"""


def run_in_qgis(body: str, *, timeout: float = 120.0) -> Any:
    """``body`` を QGIS 初期化済みの子プロセスで実行し、``RESULT`` を返す。"""
    code = _PRELUDE + textwrap.dedent(body) + _EPILOGUE
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(_MARK):
            return json.loads(line[len(_MARK) :])
    raise AssertionError(
        f"QGIS child exited {proc.returncode} without a result\n"
        f"--- stdout ---\n{proc.stdout[-2000:]}\n--- stderr ---\n{proc.stderr[-4000:]}"
    )


# 描画テストで使う、色を固定した 1 枚のポリゴンレイヤ。QGIS は新しいベクタ
# レイヤに毎回ランダムな色を付けるので、決定性を見るテストでは必ず固定する。
RED_BOX_LAYER = """
from qgis.core import (
    QgsFeature, QgsFillSymbol, QgsGeometry, QgsProject, QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "box", "memory")
_f = QgsFeature()
_f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"))
layer.dataProvider().addFeatures([_f])
layer.setRenderer(QgsSingleSymbolRenderer(
    QgsFillSymbol.createSimple({"color": "255,0,0,255", "outline_style": "no"})
))
QgsProject.instance().setCrs(layer.crs())
QgsProject.instance().addMapLayer(layer)
"""
