"""ライブラリの中身の指紋（#17）。pytest 側と QGIS のプラグインの版の食い違いを見つける。

QGIS のプラグインのフォルダはライブラリの写し（`scripts/sync_plugin.py`）で、
パッケージの情報（版、git の commit）を持たない。そこで、写しの対象になる
ファイルの中身から指紋を作って比べる:

- ライブラリ（``packages/qgis-puppeteer/src/qgis_puppeteer/``、pytest や MCP の
  gateway が import するもの）と、プラグインのフォルダ（QGIS が import するもの）の
  どちらでも、同じ規則で同じファイルを読む
- プラグイン固有のもの（``qgis_plugin/`` など、`PLUGIN_OWNED`）と ``__pycache__`` は
  入れない。改行（CRLF / LF）は checkout の設定で変わるので、LF にそろえてから読む
- つまり、``sync_plugin.py --check`` が通る組み合わせなら指紋は同じになる

指紋は版の名前ではない。同じかどうかしか分からない。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

# プラグイン側が正のもの（写しの対象外、トップレベルの名前）。
# scripts/sync_plugin.py の OWNED と同じ（テストで突き合わせている）
PLUGIN_OWNED = frozenset({"qgis_plugin", "metadata.txt", "icon.png", "LICENSE", "README.md"})
_IGNORED_DIRS = frozenset({"__pycache__"})
_LENGTH = 12


def compute_fingerprint(root: Path) -> str:
    """``root``（``qgis_puppeteer`` パッケージのフォルダ）の中身の指紋。"""
    digest = hashlib.sha256()
    files = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if not path.is_file() or _IGNORED_DIRS & set(rel.parts):
            continue
        if rel.parts[0] in PLUGIN_OWNED or path.suffix in (".pyc", ".pyo"):
            continue
        files.append((rel.as_posix(), path))
    for rel_name, path in sorted(files):
        digest.update(rel_name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:_LENGTH]


@lru_cache(maxsize=1)
def source_fingerprint() -> str:
    """いま import されている ``qgis_puppeteer`` の指紋（プロセスで 1 回だけ計算）。"""
    return compute_fingerprint(Path(__file__).resolve().parent)
