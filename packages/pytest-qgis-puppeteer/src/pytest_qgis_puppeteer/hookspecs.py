"""pytest-qgis-puppeteer が呼ぶフック（conftest で実装する）。"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.hookspec
def pytest_qgis_ready(
    config: pytest.Config, client: Any, instance_id: str | None, kind: str
) -> None:
    """QGIS を起こしたあと、起動の間のメッセージを取る前に呼ばれる（#19）。

    ホストの準備（プロジェクトを開いたあとに出るログイン画面を済ませる、
    起こし直しの前にデータベースを戻した後の確認、など）をここで済ませると、
    その間に出たメッセージも「起動の間のメッセージ」（``qgis_startup_messages``、
    ``qgis_fail_on_message_level`` の判定）に入る。

    その QGIS を初めて使うテストの準備で、1 回だけ呼ばれる。ここで例外を出すと、
    そのテストの準備のエラーになり、次にその QGIS を使うテストでもう一度呼ばれる。
    ここでの ``client`` の呼び出しは、テストの操作記録（``actions.jsonl``）に入らない。
    dev モード（既に動いている QGIS に相乗り）では呼ばれない（起動の間はもう過ぎて
    いるため）。

    Args:
        config: pytest の config。
        client: ``E2EAutomationClient``。この QGIS に届くよう送り先を切り替えてある。
        instance_id: その QGIS の instance_id。``qgis_process`` を override している
            場合などで分からなければ None。
        kind: ``"session"``（session の最初の QGIS）、``"restart"``（起こし直した
            session の QGIS）、``"fresh"``（``@pytest.mark.fresh_qgis`` の QGIS）。
    """
