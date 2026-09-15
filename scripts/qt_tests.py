"""Qt / QGIS 依存の統合テストを、QGIS 同梱の Python で回す。

venv には PyQt5 も qgis も入らない（Windows では PyQt5 wheel と Qt の版が
食い違って Hub 側が heap corruption を起こす）ので、`test_worker_integration` /
`test_integration_hub_client` / `test_hub_spawn_integration` /
`test_worker_liveness_integration` / `test_ui_tools_snapshot` は `uv run pytest`
では skip される。本番の Hub / Worker が走るのは QGIS の Qt なので、そちらで
回すのが正しい。

usage (repo root):

    python scripts/qt_tests.py                # 上記 5 ファイル
    python scripts/qt_tests.py -k respawn -v  # 引数は pytest にそのまま渡す

環境変数 QGIS_ROOT でインストール先を上書きできる（既定は Program Files 配下の
最新 "QGIS 3.*"）。pytest 等は初回に QGIS の pip で ``.qgis-site/`` に入れる
（gitignore 済み）。

CI（Linux）には PyQt5 の wheel が入るので同じテストが uv 経由で走る。
Windows でこのスクリプトを回すのは、push 前に「実際の Qt で」確認するため。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SITE = REPO / ".qgis-site"
QT_TEST_FILES = [
    "packages/qgis-puppeteer/tests/test_worker_integration.py",
    "packages/qgis-puppeteer/tests/test_integration_hub_client.py",
    "packages/qgis-puppeteer/tests/test_hub_spawn_integration.py",
    "packages/qgis-puppeteer/tests/test_worker_liveness_integration.py",
    "packages/qgis-puppeteer/tests/test_ui_tools_snapshot.py",
]
SRC = [
    REPO / "packages" / "qgis-puppeteer" / "src",
    REPO / "packages" / "pytest-qgis-puppeteer" / "src",
]


def _find_qgis_root() -> Path:
    override = os.environ.get("QGIS_ROOT")
    if override:
        return Path(override)
    candidates = sorted(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")).glob("QGIS 3.*"))
    if not candidates:
        sys.exit("QGIS が見つかりません。QGIS_ROOT=<install dir> を設定してください。")
    return candidates[-1]


def _launcher(root: Path) -> Path:
    for name in ("python-qgis-ltr.bat", "python-qgis.bat"):
        p = root / "bin" / name
        if p.exists():
            return p
    sys.exit(f"{root}/bin に python-qgis(-ltr).bat がありません。")


def _outer() -> int:
    """通常の Python から呼ばれた側: QGIS の launcher で自分自身を再実行する。"""
    launcher = _launcher(_find_qgis_root())
    if not (SITE / "pytest").exists():
        print(f"[qt_tests] installing pytest into {SITE} with {launcher.name}")
        subprocess.run(
            [
                str(launcher),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--target",
                str(SITE),
                "pytest",
                "pytest-asyncio",
            ],
            check=True,
        )
    env = {**os.environ, "QPUPPETEER_QT_TESTS_INNER": "1"}
    return subprocess.call(
        [str(launcher), str(Path(__file__).resolve()), *sys.argv[1:]], env=env, cwd=REPO
    )


def _inner() -> None:
    """QGIS の Python で走る側: パスを組んで pytest を起動する。"""
    paths = [str(SITE), *(str(p) for p in SRC)]
    sys.path[:0] = paths
    # 統合テストは sys.executable -m qgis_puppeteer.hub を子プロセスで立てる。
    # o4w_env.bat が PYTHONPATH を消すので、子にはここから渡す。
    os.environ["PYTHONPATH"] = os.pathsep.join([*paths, os.environ.get("PYTHONPATH", "")])
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.chdir(REPO)

    import pytest

    # QGIS の Qt は interpreter 終了時に落ちることがあり、block-buffered な
    # stdout が消える。行バッファにして、pytest の戻り値で即 _exit する。
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    # オプションだけ（-k, -v 等）でファイルが無ければ、Qt テスト一式を対象にする
    args = sys.argv[1:]
    if not any(not a.startswith("-") and Path(a).exists() for a in args):
        args = [*args, *QT_TEST_FILES]
    code = pytest.main(args)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(code))


if __name__ == "__main__":
    if os.environ.get("QPUPPETEER_QT_TESTS_INNER") == "1":
        _inner()
    sys.exit(_outer())
