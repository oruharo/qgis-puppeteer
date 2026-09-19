"""`plugins/qgis_puppeteer/` にライブラリ本体の写しを置く。

QGIS プラグインのフォルダ名は `qgis_puppeteer` で、QGIS はそれを
`import qgis_puppeteer` する。つまりプラグインフォルダは `qgis_puppeteer`
パッケージそのものでなければならない。リポジトリ上では

- 正: `packages/qgis-puppeteer/src/qgis_puppeteer/`（ライブラリ、Apache-2.0）
- 正: `plugins/qgis_puppeteer/` のうちプラグイン固有のもの（`OWNED`、GPL）
- 写し: `plugins/qgis_puppeteer/` の残り全部（このスクリプトが作る）

とし、`plugins/qgis_puppeteer/` をそのまま QGIS のプラグインディレクトリへ
コピーできる完成形として commit しておく。写しを手で編集しないこと。

usage (repo root):

    python scripts/sync_plugin.py           # 写しを更新
    python scripts/sync_plugin.py --check   # ずれていたら exit 1（CI 用）
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "packages" / "qgis-puppeteer" / "src" / "qgis_puppeteer"
DST = REPO / "plugins" / "qgis_puppeteer"
# プラグイン側が正のもの（写しの対象外。トップレベルの名前）
OWNED = frozenset({"qgis_plugin", "metadata.txt", "icon.png", "LICENSE", "README.md"})
IGNORED_DIRS = frozenset({"__pycache__"})


def _files(root: Path, *, skip_owned: bool) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if not path.is_file() or IGNORED_DIRS & set(rel.parts):
            continue
        if skip_owned and rel.parts[0] in OWNED:
            continue
        found[rel.as_posix()] = path
    return found


def _normalized(path: Path) -> bytes:
    # 改行は checkout の設定（autocrlf）で変わるので、中身の比較からは外す
    return path.read_bytes().replace(b"\r\n", b"\n")


def main(argv: list[str]) -> int:
    check = "--check" in argv
    clash = OWNED & {p.name for p in SRC.iterdir()}
    if clash:
        print(f"[sync_plugin] library must not contain plugin-owned names: {sorted(clash)}")
        return 1

    src = _files(SRC, skip_owned=False)
    dst = _files(DST, skip_owned=True)
    stale = sorted(set(dst) - set(src))
    changed = sorted(
        rel
        for rel, path in src.items()
        if rel not in dst or _normalized(dst[rel]) != _normalized(path)
    )

    if check:
        for rel in changed:
            print(f"[sync_plugin] out of date: plugins/qgis_puppeteer/{rel}")
        for rel in stale:
            print(f"[sync_plugin] stale:       plugins/qgis_puppeteer/{rel}")
        if changed or stale:
            print("[sync_plugin] run `python scripts/sync_plugin.py` and commit the result")
            return 1
        print(f"[sync_plugin] ok ({len(src)} files)")
        return 0

    for rel in changed:
        target = DST / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src[rel], target)
        print(f"[sync_plugin] copied  {rel}")
    for rel in stale:
        (DST / rel).unlink()
        print(f"[sync_plugin] removed {rel}")
    if not changed and not stale:
        print(f"[sync_plugin] already up to date ({len(src)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
