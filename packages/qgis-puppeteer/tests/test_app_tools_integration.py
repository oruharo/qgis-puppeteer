"""`close_qgis` を本物の Qt / QGIS で確かめる（#16 で合意した閉じ方）。

- モーダルが ``exec()`` で開いていても、reject してからメインウィンドウを閉じる
- 編集中のレイヤと dirty なプロジェクトの変更は捨てる（保存の確認を出させない）
- ``reject()`` の中で確認を出し、Cancel なら閉じないダイアログでも、手順は
  回数の上限で終わる（止まらない。最後は呼び出し側が pid で止める）

QGIS の Python でしか走らない（`scripts/qt_tests.py`）。venv と CI では skip。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("qgis.core")

from tests._qgis_child import run_in_qgis  # noqa: E402

pytestmark = pytest.mark.integration

_SETUP = """
import time
from qgis.core import QgsFeature, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QEventLoop, QTimer
from qgis.PyQt.QtWidgets import QDialog, QMainWindow, QMessageBox
from qgis_puppeteer.qgis_tools.app_tools import close_qgis

main = QMainWindow()
main.show()


class _Iface:
    def mainWindow(self):
        return main


def run_until(done, timeout_ms):
    loop = QEventLoop()
    poll = QTimer()
    poll.timeout.connect(lambda: done() and loop.quit())
    poll.start(20)
    QTimer.singleShot(timeout_ms, loop.quit)
    started = time.monotonic()
    loop.exec_()
    poll.stop()
    return time.monotonic() - started
"""


def test_a_modal_dialog_and_unsaved_edits_do_not_stop_the_close() -> None:
    result: dict[str, Any] = run_in_qgis(
        _SETUP
        + """
layer = QgsVectorLayer("Point?field=n:integer", "edits", "memory")
QgsProject.instance().addMapLayer(layer)
layer.startEditing()
layer.addFeature(QgsFeature(layer.fields()))
QgsProject.instance().setDirty(True)

dlg = QDialog(main)
dlg.setObjectName("login")
QTimer.singleShot(0, dlg.exec_)  # ホストのログイン画面のように exec() で出す
QTimer.singleShot(100, lambda: close_qgis(_Iface(), interval_ms=50))
took = run_until(lambda: not main.isVisible(), 5000)
RESULT = {
    "main_visible": main.isVisible(),
    "dialog_visible": dlg.isVisible(),
    "dialog_result": dlg.result(),
    "editable": layer.isEditable(),
    "features": layer.featureCount(),
    "dirty": QgsProject.instance().isDirty(),
    "took": took,
}
""",
        gui=True,
    )
    assert result["main_visible"] is False
    assert result["dialog_visible"] is False
    assert result["dialog_result"] == 0  # Rejected
    assert result["editable"] is False
    assert result["features"] == 0  # ロールバックされた
    assert result["dirty"] is False
    assert result["took"] < 3.0


def test_a_dialog_that_refuses_to_close_does_not_hang_the_procedure() -> None:
    """reject() の中で確認を出し、Cancel 扱いなら残るダイアログ（実在する作り）。"""
    result: dict[str, Any] = run_in_qgis(
        _SETUP
        + """
import logging

asked = []
gave_up = []


class _Catch(logging.Handler):
    def emit(self, record):
        if "gave up" in record.getMessage():
            gave_up.append(time.monotonic())


logging.getLogger("qgis_puppeteer.qgis_tools.app_tools").addHandler(_Catch())


class _Stubborn(QDialog):
    def reject(self):
        answer = QMessageBox.question(self, "unsaved", "discard?")
        asked.append(answer)
        if answer == QMessageBox.Yes:
            super().reject()


def finish():
    # ダイアログが閉じないので、入れ子のイベントループから戻ってこない。
    # 結果はここ（タイマーの中）で書いて終わる
    result = {
        "main_visible": main.isVisible(),
        "dialog_visible": dlg.isVisible(),
        "asked": len(asked),
        "gave_up": len(gave_up),
    }
    sys.stdout.write("@@RESULT@@" + json.dumps(result) + "\\n")
    sys.stdout.flush()
    os._exit(0)


dlg = _Stubborn(main)
QTimer.singleShot(0, dlg.exec_)
QTimer.singleShot(100, lambda: close_qgis(_Iface(), interval_ms=30, max_rounds=10))
QTimer.singleShot(2000, finish)
run_until(lambda: False, 10000)
""",
        gui=True,
        timeout=60,
    )
    # 確認は reject（= Cancel）されるのでダイアログは残り、メインも閉じない。
    # 手順は上限で諦めて止まる（最後は呼び出し側が pid で止める）
    assert result["dialog_visible"] is True
    assert result["main_visible"] is True
    assert 1 <= result["asked"] <= 10
    assert result["gave_up"] == 1
