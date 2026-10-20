"""操作を Qt のイベントループに積んで、呼び出しをすぐ返す（#21）。

ハンドラは GUI のスレッドでその場で処理するので、処理の中で利用側のコードが
``exec()`` のモーダル（``QMessageBox.question`` など）を開くと、モーダルが閉じる
まで呼び出しが返らない。``wait=False`` の操作は、本体を
``QTimer.singleShot(0, ...)`` で積んですぐ返す。モーダルは、そのあと自分の
イベントループの中で開き、その間も Worker は次の呼び出しを受け付ける。

積んだ処理はもう呼び出し元へ結果を返せない。中で出た例外と ``success: false`` は
:func:`exception_recorder.record` で未捕捉例外と同じ場所に残し、
``qgis_get_recent_exceptions`` とテストの判定で見えるようにする。
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from qgis.PyQt.QtCore import QObject, QTimer

from . import exception_recorder

logger = logging.getLogger("qgis_puppeteer.qgis_tools.queued")

# 積んだ処理が success: false を返したときのレコードの type
QUEUED_FAILURE_TYPE = "QueuedOperationFailed"


def run_later(
    fn: Callable[[], Any],
    *,
    what: str,
    target: Any = None,
    gone_is_failure: bool = True,
) -> None:
    """``fn`` をイベントループに積む。失敗は recent_exceptions に残す。

    Args:
        fn: 引数なしで呼ぶ処理。dict を返し、その ``success`` が False なら失敗と
            みなす。それ以外の戻り値は見ない。
        what: レコードに書く操作名（``"qgis_click_widget"`` など）。
        target: ``fn`` が触る QObject（押す widget、答える modal など）。積んでから
            動くまでの間に消えたら ``fn`` を呼ばない。C++ 側で作られた QObject は、
            消えても PyQt の wrapper が気付かないので、触ると落ちる（
            ``QMessageBox.question`` の箱は ``exec()`` が返るとすぐ消える）。
            ``destroyed`` で見張る。
        gone_is_failure: ``target`` が消えていたとき、失敗として残すか。頼まれた
            操作ができなかったなら True、することが無くなっただけなら False。
    """
    gone = [False]
    connection = None
    if target is not None:
        connection = target.destroyed.connect(lambda *_: gone.__setitem__(0, True))

    def run() -> None:
        if connection is not None:
            QObject.disconnect(connection)
        if gone[0]:
            if gone_is_failure:
                logger.warning("queued %s: the target was deleted before it ran", what)
                exception_recorder.record(
                    QUEUED_FAILURE_TYPE,
                    f"queued {what} failed: the target was deleted before it ran",
                )
            return
        _run(fn, what)

    QTimer.singleShot(0, run)


def _run(fn: Callable[[], Any], what: str) -> None:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - slot から投げても誰も受けられない
        logger.warning("queued %s raised %s: %s", what, type(exc).__name__, exc)
        exception_recorder.record(
            type(exc).__name__,
            f"queued {what}: {exc}",
            traceback.format_exc(),
        )
        return
    if isinstance(result, dict) and result.get("success") is False:
        logger.warning("queued %s failed: %s", what, result)
        exception_recorder.record(QUEUED_FAILURE_TYPE, f"queued {what} failed: {result}")
