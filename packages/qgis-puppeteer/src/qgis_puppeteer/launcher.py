"""公式 launch helper（ADR-0005 D6）。

非制御 launch（人間が素の QGIS を起動）では client が「自分が起こした
インスタンス」を決定的に同定できない（attribution 問題、ADR-0005
"Launch 制御モデルと相関"）。本モジュールは起動前に **相関トークン**
``launch_token`` を発番して QGIS プロセス env に注入し、トークンを呼び出し元へ
返すことで「半制御 launch」を実現する。client は ``launch_token`` を selector に
渡せば Hub 解決順の最上位 tier で決定的に到達できる（U9）。

## 公開 API（純粋・テスト可能）

- ``generate_launch_token()`` — 衝突しない不透明トークンを発番
- ``inject_launch_token(env, token, *, label=None)`` — env dict を組み立てる
  （元の Mapping は破壊しない）

## CLI

``qgis-puppeteer-launch [--label L] [--print-token] -- <qgis 起動コマンド...>``

トークンを stdout に 1 行出力（``launch_token=<token>``）してから QGIS を
起動する。``--print-token`` 単体ならトークンだけ出して起動しない（env を
自前で組みたい呼び出し側向け）。
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from collections.abc import Mapping, Sequence

# ADR-0005 D6: plugin が読む env キー（plugins/qgis_puppet/plugin.py と一致）。
ENV_LAUNCH_TOKEN = "QPUPPETEER_LAUNCH_TOKEN"
ENV_WORKER_LABEL = "QPUPPETEER_WORKER_LABEL"

_TOKEN_PREFIX = "lt-"


def generate_launch_token() -> str:
    """pid 非依存の不透明な相関トークンを発番する（ADR-0005 D6）。

    ``lt-<80bit hex>``。1 起動につき 1 個。instance_id（``w-``）とは prefix で
    区別でき、selector 解決時に launch_token tier だと判別しやすい。
    """
    return _TOKEN_PREFIX + secrets.token_hex(10)


def inject_launch_token(
    env: Mapping[str, str],
    token: str,
    *,
    label: str | None = None,
) -> dict[str, str]:
    """``env`` のコピーに launch_token（+ 任意で label）を注入して返す。

    元の Mapping は変更しない（呼び出し側の ``os.environ`` を汚さない）。
    ``label`` を渡すと ``QPUPPETEER_WORKER_LABEL`` も設定する（人間向け
    安定ロール名を併用したい場合）。
    """
    merged = dict(env)
    merged[ENV_LAUNCH_TOKEN] = token
    if label:
        merged[ENV_WORKER_LABEL] = label
    return merged


def _parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="qgis-puppeteer-launch",
        description=(
            "Generate a launch_token, inject it into the environment, and "
            "launch QGIS. Prints 'launch_token=<token>' on stdout so the "
            "caller can correlate the spawned instance deterministically "
            "(ADR-0005 D6)."
        ),
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Optional human-facing stable role label (QPUPPETEER_WORKER_LABEL).",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="Only print the token and exit (do not launch QGIS).",
    )
    # 区切り `--` 以降を起動コマンドとして受ける
    ns, rest = parser.parse_known_args(list(argv))
    if rest and rest[0] == "--":
        rest = rest[1:]
    return ns, rest


def main(argv: Sequence[str] | None = None) -> int:
    """CLI エントリポイント（``qgis-puppeteer-launch``）。"""
    ns, cmd = _parse_args(sys.argv[1:] if argv is None else argv)
    token = generate_launch_token()
    # 呼び出し元（人間 / エージェント / スクリプト）が拾える形で必ず先に出す。
    print(f"launch_token={token}", flush=True)

    if ns.print_token:
        return 0

    if not cmd:
        print(
            "error: no launch command given. Usage: "
            "qgis-puppeteer-launch [--label L] -- <qgis> [args...]",
            file=sys.stderr,
        )
        return 2

    child_env = inject_launch_token(os.environ, token, label=ns.label)
    # QGIS は長命プロセス。helper はトークンを出したら役目を終えるので、
    # 子の stdio を継承したまま起動して、その終了コードを返す。
    completed = subprocess.run(cmd, env=child_env, check=False)  # noqa: S603
    return completed.returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
