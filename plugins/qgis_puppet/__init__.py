"""QGIS Puppet plugin — QGIS エントリポイント。

QGIS は `classFactory(iface)` を呼んでプラグインインスタンスを得る。
実装は `plugin.py` に委譲する。
"""

from __future__ import annotations

from typing import Any


def classFactory(iface: Any) -> Any:
    """QGIS プラグインマネージャから呼ばれるエントリ。

    :param iface: QGIS インターフェース（`QgsInterface`）
    :return: `QgisPuppetPlugin` インスタンス
    """
    # 遅延 import：PyQt5 のロードを classFactory 呼び出し時まで遅らせる。
    # （QGIS 外部から `import plugins.qgis_puppet` された
    # 場合の副作用回避）
    from .plugin import QgisPuppetPlugin

    return QgisPuppetPlugin(iface)
