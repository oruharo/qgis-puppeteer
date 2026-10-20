"""QGIS を閉じる手順（pytest の後片付け・起こし直し、#9 / #11 / #16）。

メインウィンドウを閉じるだけでは、QGIS が閉じないことがある:

- モーダルが開いていると閉じない。ホストのプラグインが、プロジェクトを開く
  途中（openProject のマクロなど）でログイン画面を ``exec()`` で出していると、
  WM_CLOSE でもメインウィンドウの close でも閉じなかった
- 閉じるときに QGIS が確認を出す（プロジェクトを保存するか、編集中のレイヤを
  保存するか）。これも新しいモーダルになる
- モーダルの ``reject()`` の中で、さらに確認を出すダイアログがある（未保存の
  確認を ``QMessageBox.question`` で出し、答えが Cancel なら閉じない、など）。
  ``reject()`` はその確認が閉じるまで戻らない

そこで、少しずつ間を空けて次を繰り返す（`close_qgis`）:

1. 変更を捨てる（プロジェクトの dirty を下ろし、編集中のレイヤをロールバック。
   保存の確認を出させない）
2. 一番上のモーダルがあれば、``QTimer`` で積んだ ``reject()`` で閉じる。戻って
   こなくてもよい形で呼ぶので、中で確認を出すダイアログでも手順は止まらない。
   次の回で、新しく出たモーダル（確認）も同じように閉じる
3. モーダルが無ければ、メインウィンドウを閉じる

回数には上限がある。それでも閉じないダイアログ（確認を Cancel にすると残る
もの）は、呼び出した側の後ろ盾（待ってから pid で止める）に任せる。捨てる
のはテストの QGIS の作業だけで、データベースに書くものは無い。
"""

from __future__ import annotations

import logging
from typing import Any

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QApplication, QDialog

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MS = 200
DEFAULT_MAX_ROUNDS = 50


def close_qgis(
    iface: Any = None,
    *,
    interval_ms: int = DEFAULT_INTERVAL_MS,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> None:
    """QGIS を閉じる手順を始める。すぐ戻る（手順は QTimer で進む）。

    Args:
        iface: ``QgisInterface``。None ならメインウィンドウの代わりに
            ``QApplication.quit()`` を使う。
        interval_ms: 1 回ごとの間隔。モーダルが閉じて次の確認が出るのを待つ。
        max_rounds: 回数の上限。既定で約 10 秒。
    """
    state = {"round": 0}

    def step() -> None:
        state["round"] += 1
        if state["round"] > max_rounds:
            logger.warning(
                "close_qgis: gave up after %d rounds; a dialog would not close", max_rounds
            )
            return
        discard_changes()
        app = QApplication.instance()
        modal = app.activeModalWidget() if app is not None else None
        main = iface.mainWindow() if iface is not None else None
        if modal is not None:
            # reject() は中で確認を出すと戻ってこないので、積んで呼ぶ
            closer = modal.reject if isinstance(modal, QDialog) else modal.close
            QTimer.singleShot(0, closer)
        elif main is not None:
            if not main.isVisible():
                return  # 閉じた（QGIS ならこの前にプロセスが終わっている）
            QTimer.singleShot(0, main.close)
        elif app is not None:
            QTimer.singleShot(0, app.quit)
        # 閉じたつもりでも、確認が出て止まることがあるので、次の回で見直す
        QTimer.singleShot(interval_ms, step)

    QTimer.singleShot(0, step)


def discard_changes() -> None:
    """閉じるときの保存の確認を出させない（プロジェクトとレイヤの変更を捨てる）。"""
    project = QgsProject.instance()
    for layer in project.mapLayers().values():
        if isinstance(layer, QgsVectorLayer) and layer.isEditable():
            try:
                layer.rollBack()
            except Exception:  # noqa: BLE001 - 1 つのレイヤで止めない
                logger.debug("rollBack failed for %s", layer.id(), exc_info=True)
    project.setDirty(False)
