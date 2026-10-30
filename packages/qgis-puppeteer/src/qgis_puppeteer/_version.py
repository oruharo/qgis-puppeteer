"""このライブラリの版。

QGIS のプラグインのフォルダはライブラリの写しで、配布物の情報（metadata）を
持たないので、``importlib.metadata`` ではなく定数で持つ。
pyproject.toml（両パッケージ）とプラグインの metadata.txt の版と同じにする
（テストで突き合わせている）。
"""

from __future__ import annotations

__version__ = "0.1.0"
