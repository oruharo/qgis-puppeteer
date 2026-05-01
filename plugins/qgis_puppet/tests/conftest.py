"""pytest 設定：`plugins/` を sys.path に追加して
`qgis_puppet` を Python パッケージとして import できるようにする。

QGIS が plugins ディレクトリを sys.path に入れるのと同じ扱い。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent  # tests → qgis_puppet → plugins
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))
