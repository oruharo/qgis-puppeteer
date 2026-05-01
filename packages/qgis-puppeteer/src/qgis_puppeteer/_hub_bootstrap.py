"""Hub subprocess 起動用 bootstrap。

`python-qgis-ltr.bat` 経由で起動する場合、bat が読み込む
`etc/ini/python3.bat` が `SET PYTHONPATH=` で親プロセスの PYTHONPATH を
完全消去してしまうため、親で設定した PYTHONPATH は子に届かない。
結果として `python -m qgis_puppeteer.hub` は
`ModuleNotFoundError: No module named 'qgis_puppeteer'` で即死する。

このファイルはフルパスで直接実行される前提のスクリプトで、自身の位置から
`qgis_puppeteer/` の親ディレクトリを算出して `sys.path` に差し込み、
さらに後述の独自 env で渡された追加パスも `sys.path` に積んだ上で
`qgis_puppeteer.hub.main()` を呼ぶ。

## 環境変数で sys.path を引き継ぐ仕組み

ホストアプリケーション (qgis_puppet プラグインを組み込むホスト) が独自に
ベンダ同梱したライブラリ等を Hub から参照させたいことがある。`PYTHONPATH`
経由は上記の通り消去されるため、`qgis_puppeteer` 中立の独自 env として

    QPUPPETEER_HUB_EXTRA_SYSPATH

を定義する。値は OS 区切り (`;` / `:`) で連結したパスリスト。
spawn 側 (`plugin_helpers._build_hub_spawn_env`) は親プロセスの
`sys.path` をこの env に詰めて子に渡し、本 bootstrap が読み込んで
`sys.path` に prepend する。これで親と等価な解決能力を持つ。

配置：`packages/qgis-puppeteer/src/qgis_puppeteer/_hub_bootstrap.py`
呼び出し例：
    python-qgis-ltr.bat <path>/qgis_puppeteer/_hub_bootstrap.py --port 9876
"""

from __future__ import annotations

import os
import sys

# このファイル = `<src>/qgis_puppeteer/_hub_bootstrap.py`
# その親 = `<src>/qgis_puppeteer`
# さらに親 = `<src>`  ← ここを sys.path へ
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# ホストアプリ (qgis_puppet 側 _build_hub_spawn_env) が親の sys.path を
# `QPUPPETEER_HUB_EXTRA_SYSPATH` 経由で渡してくる。`PYTHONPATH` は
# QGIS の python-qgis-ltr.bat (etc/ini/python3.bat) が消去するため、
# 中立な独自 env を経由する必要がある。
_ENV_EXTRA_SYSPATH = "QPUPPETEER_HUB_EXTRA_SYSPATH"
for _path in os.environ.get(_ENV_EXTRA_SYSPATH, "").split(os.pathsep):
    if _path and _path not in sys.path:
        sys.path.insert(0, _path)

from qgis_puppeteer.hub import main  # noqa: E402

if __name__ == "__main__":
    # argv には bootstrap ファイル名が sys.argv[0]、残りが --port 等。
    # main() は None を渡すと sys.argv[1:] を使う想定。
    raise SystemExit(main())
