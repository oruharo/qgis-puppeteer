"""ライブラリの中身の指紋（#17）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from qgis_puppeteer._fingerprint import PLUGIN_OWNED, compute_fingerprint, source_fingerprint

REPO = Path(__file__).resolve().parents[3]


def _write(root: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_line_endings_and_plugin_owned_files_do_not_count(tmp_path: Path) -> None:
    lib = _write(tmp_path / "lib", {"a.py": b"x = 1\n", "sub/b.py": b"y = 2\n"})
    plugin = _write(
        tmp_path / "plugin",
        {
            "a.py": b"x = 1\r\n",  # Windows の checkout
            "sub/b.py": b"y = 2\r\n",
            "qgis_plugin/plugin.py": b"plugin only\n",
            "metadata.txt": b"[general]\n",
            "__pycache__/a.cpython-312.pyc": b"\x00",
            "sub/__pycache__/b.cpython-312.pyc": b"\x00",
        },
    )
    assert compute_fingerprint(lib) == compute_fingerprint(plugin)


def test_a_changed_or_added_file_changes_it(tmp_path: Path) -> None:
    base = compute_fingerprint(_write(tmp_path / "a", {"a.py": b"x = 1\n"}))
    assert compute_fingerprint(_write(tmp_path / "b", {"a.py": b"x = 2\n"})) != base
    assert compute_fingerprint(_write(tmp_path / "c", {"a.py": b"x = 1\n", "n.py": b""})) != base
    assert compute_fingerprint(_write(tmp_path / "d", {"z.py": b"x = 1\n"})) != base


def test_the_committed_plugin_matches_the_library() -> None:
    """`sync_plugin.py --check` が通るリポジトリなら、指紋は同じ。"""
    lib = REPO / "packages" / "qgis-puppeteer" / "src" / "qgis_puppeteer"
    plugin = REPO / "plugins" / "qgis_puppeteer"
    assert compute_fingerprint(lib) == compute_fingerprint(plugin)
    assert source_fingerprint() == compute_fingerprint(lib)


def test_plugin_owned_names_match_the_sync_script() -> None:
    spec = importlib.util.spec_from_file_location(
        "sync_plugin", REPO / "scripts" / "sync_plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.OWNED == PLUGIN_OWNED
