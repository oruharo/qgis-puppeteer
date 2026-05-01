"""pytest-qgis-puppeteer の pytest plugin。

ADR-0002 に従い、pytest session が **Hub subprocess を所有** し、QGIS を
`qgis_bin` で指定された実行ファイル経由で spawn する構成。QGIS 内の
`qgis_puppet` プラグインが `QPUPPETEER_HUB_PORT` env を見て外部所有者モードに
入るので、自前で Hub を spawn しない（ADR-0001 §6）。

## 設定の優先順位

CLI 引数 > 環境変数 > pyproject.toml の `[tool.pytest.ini_options]`

| 設定項目 | ini key | env | CLI |
|---|---|---|---|
| QGIS 実行ファイル | `qgis_bin` | `QPUPPETEER_QGIS_BIN` | `--qgis-bin` |
| QGIS 起動引数 | `qgis_args` | `QPUPPETEER_QGIS_ARGS` | `--qgis-args` |
| 起動コマンド全体 | `qgis_command` | `QPUPPETEER_QGIS_COMMAND` | `--qgis-command` |
| Hub spawn 用 Python | `qgis_python` | `QPUPPETEER_QGIS_PYTHON` | `--qgis-python` |
| Worker register 待機タイムアウト (秒) | `qgis_startup_timeout` | — | — |

`qgis_command` を指定すると `qgis_bin + qgis_args` の代わりに使われる。ホスト
独自の launcher (例: ホスト固有の env / DB 接続情報を立てる .bat) 経由で QGIS
を起動する場合に利用する。`qgis_python` を未指定にすると
`qgis_puppeteer.find_qgis_python_launcher()` で自動検出を試みる
（OSGEO4W_ROOT または `sys.executable` 兄弟）。

## fixture 依存順

```
hub_port → hub_process → automation_client → qgis_process → hub_ready
                                                               │
                                                            function: qgis
```

`automation_client` は Client 接続として Hub に残り続け、Hub の idle shutdown
発火を抑止する（Worker 起動が遅延しても Hub が落ちない保険）。

## dev モード

`QPUPPETEER_E2E_USE_RUNNING_QGIS=1` で既存 QGIS / Hub に接続して spawn を全
skip する。書き始めや UI 操作系のデバッグ用途を想定（`execute_python` を
多用するテストは confirm UI で固まるので不向き）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from pytest_qgis_puppeteer._action_recorder import ActionRecorder
from pytest_qgis_puppeteer._environments import (
    EnvironmentsConfig,
    EnvironmentSpec,
    aggregate_exit_codes,
    filter_items_by_env_marker,
    find_environments_toml,
    find_unmarked_items,
    inject_maxfail,
    is_hard_exit_code,
    load_environments,
    parse_env_arg,
    resolve_active_env,
    resolve_qgis_setting,
    strip_env_args,
)
from pytest_qgis_puppeteer.automation_client import E2EAutomationClient

logger = logging.getLogger("pytest_qgis_puppeteer.plugin")

# ==============================================================
# 定数
# ==============================================================

DEFAULT_HUB_LISTEN_TIMEOUT_S = 10.0
DEFAULT_WORKER_REGISTER_TIMEOUT_S = 60.0
DEFAULT_QGIS_GRACEFUL_SHUTDOWN_TIMEOUT_S = 15.0

# dev モード関連
ENV_DEV_MODE = "QPUPPETEER_E2E_USE_RUNNING_QGIS"
ENV_DEV_HUB_URL = "QPUPPETEER_E2E_HUB_URL"
DEFAULT_DEV_HUB_URL = "ws://127.0.0.1:9876"

# QGIS spawn 関連
ENV_QGIS_BIN = "QPUPPETEER_QGIS_BIN"
ENV_QGIS_PYTHON = "QPUPPETEER_QGIS_PYTHON"
ENV_QGIS_ARGS = "QPUPPETEER_QGIS_ARGS"
# `qgis_command` を指定すると `qgis_bin + qgis_args` の代わりに使われる。
# ホストアプリ独自の launcher (ホスト固有の env や DB 接続情報を立てる .bat 等)
# を介して QGIS を起動するシナリオで使う。`QPUPPETEER_HUB_PORT` 等の env は
# 引き続き子プロセスに継承される（launcher 側でそれを QGIS に渡す責任）。
ENV_QGIS_COMMAND = "QPUPPETEER_QGIS_COMMAND"

# diagnostic bundle 出力先（cwd 基準）
_DEFAULT_DIAG_DIR = "outputs/diagnostics"

# Hub stderr/stdout は drain thread で buffer に貯める。失敗時 diagnostic dump で
# 末尾を tail する。memory 抑制のため最大行数で truncate する。
_HUB_LOG_TAIL_LINES = 500


def _drain_pipe_to_list(stream: Any, buf: list[str]) -> None:
    """``subprocess.PIPE`` を line ごとに読んで list に append する drain thread 本体。

    spawn.py の同名関数と本質的に同じ。Hub log は session を通じて累積するため、
    ``_HUB_LOG_TAIL_LINES`` を超えたら古い行を捨てる（memory 上限）。
    """
    if stream is None:
        return
    try:
        for raw in iter(stream.readline, b""):
            with contextlib.suppress(Exception):  # noqa: BLE001
                buf.append(raw.decode("utf-8", errors="replace").rstrip("\n"))
                if len(buf) > _HUB_LOG_TAIL_LINES:
                    # 古い行から削る。pop(0) は O(n) だが頻度は低いので許容。
                    del buf[: len(buf) - _HUB_LOG_TAIL_LINES]
    except Exception:  # noqa: BLE001 - drain thread は何があっても落とさない
        logger.debug("Hub log drain thread exited", exc_info=True)
    finally:
        with contextlib.suppress(Exception):  # noqa: BLE001
            stream.close()


# ==============================================================
# pytest hooks: addoption / addini
# ==============================================================


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("qgis-puppeteer", "qgis-puppeteer E2E options")
    group.addoption(
        "--qgis-bin",
        dest="qgis_bin",
        default=None,
        help="Path to QGIS executable (e.g. qgis-bin.exe). "
        f"Falls back to ${ENV_QGIS_BIN} or [tool.pytest.ini_options] qgis_bin.",
    )
    group.addoption(
        "--qgis-python",
        dest="qgis_python",
        default=None,
        help="Path to QGIS-bundled Python launcher (used to spawn the Hub subprocess "
        "with PyQt5 available). E.g. python-qgis-ltr.bat / python-qgis.bat. "
        f"Falls back to ${ENV_QGIS_PYTHON} or qgis_python ini.",
    )
    group.addoption(
        "--qgis-args",
        dest="qgis_args",
        default=None,
        help="Extra arguments for QGIS executable (shell-style quoting).",
    )
    group.addoption(
        "--qgis-command",
        dest="qgis_command",
        default=None,
        help="Full QGIS launch command (shell-style). Mutually exclusive with "
        "--qgis-bin / --qgis-args. Use this when QGIS must be started via a "
        "host-specific wrapper (e.g. a launcher.bat that sets up app-specific "
        "env vars or DB connection info before launching QGIS). "
        f"Falls back to ${ENV_QGIS_COMMAND} or [tool.pytest.ini_options] qgis_command.",
    )
    # ADR-0003 Phase 1 / 2: Test Environments の active env 選択
    group.addoption(
        "--env",
        dest="qgis_env",
        default=None,
        help="Select environment(s) from environments.toml. "
        "Tests with @pytest.mark.qgis_env('<name>') matching the selection run; "
        "others are deselected. Forms: '--env=NAME' (single env, Phase 1), "
        "'--env=A,B' (multiple envs run sequentially, Phase 2), "
        "'--env=all' (all envs, Phase 2). For multi/all forms each env runs in "
        "its own pytest session and exit codes are aggregated per ADR-0003.",
    )
    # ADR-0003 Phase 4: --list-envs / --env-strict
    group.addoption(
        "--list-envs",
        dest="qgis_list_envs",
        action="store_true",
        default=False,
        help="List all environments declared in environments.toml and exit. "
        "Useful for debugging --env=NAME selection or for shell completion.",
    )
    group.addoption(
        "--env-strict",
        dest="qgis_env_strict",
        action="store_true",
        default=False,
        help="In meta-parent mode, treat 'no tests collected' (pytest exit 5) for "
        "any env as a real failure (aggregated exit 1) instead of absorbing it. "
        "Use when CI must guarantee that the named envs actually contain tests.",
    )

    parser.addini("qgis_bin", "Path to QGIS executable.", default="")
    parser.addini(
        "qgis_python",
        "Path to QGIS-bundled Python launcher (for Hub subprocess spawn).",
        default="",
    )
    parser.addini(
        "qgis_args",
        "Extra arguments for QGIS executable.",
        type="args",
        default=[],
    )
    parser.addini(
        "qgis_command",
        "Full QGIS launch command (mutually exclusive with qgis_bin/qgis_args).",
        type="args",
        default=[],
    )
    parser.addini(
        "qgis_startup_timeout",
        "Worker register wait timeout (seconds).",
        default=str(DEFAULT_WORKER_REGISTER_TIMEOUT_S),
    )
    parser.addini(
        "qgis_diag_dir",
        "Output directory for diagnostic bundles on failure.",
        default=_DEFAULT_DIAG_DIR,
    )
    # B5b (ADR-0002 §13.1): action 完了後に per-action screenshot を撮るかどうか。
    # 既定 ON。RTT が action ごとに +1 回（10〜140ms）増える点だけ留意。
    # 重い test suite で off にしたい場合は false を指定する。
    parser.addini(
        "qgis_diag_capture_screenshots",
        "Capture per-action screenshots into the diagnostic bundle on test failure "
        "(ADR-0002 §13.1, B5b). Set 'false' to disable (default: 'true').",
        default="true",
    )
    # ADR-0002 §10.7 / §17.5: 失敗連鎖防止の自動 fresh_qgis。前テストの call フェーズが
    # failed なら、次テスト setup 時に ``fresh_qgis`` marker を動的付与して QGIS を
    # 再起動する。1 件の失敗で QGIS 状態が壊れて以降全滅する事故への保険。
    # 既定 OFF（fresh_qgis は ~10s/test のコスト）。
    parser.addini(
        "qgis_auto_fresh_after_failure",
        "Automatically mark the next test with @pytest.mark.fresh_qgis after a "
        "test failure (ADR-0002 §17.5). Useful for isolation in long suites. "
        "Default: 'false'.",
        default="false",
    )
    # ADR-0002 §17 / Roadmap "Qt 未捕捉例外 → fail 連動": Worker 内の sys.excepthook
    # に積まれた未捕捉例外を test 終了時に検査し、1 件でも有れば test を fail に
    # 落とす。Qt slot 内の例外 (C++ → Python boundary で握り潰される) も pytest が
    # 反映できるようになる「silent failure 根絶」スイッチ。
    # 既定 ON（テストが緑なら本当に緑、を保証する基本機能）。
    parser.addini(
        "qgis_fail_on_uncaught_exception",
        "Fail the test if the Worker recorded any uncaught exceptions during the "
        "call phase (ADR-0002 §17). Catches Qt slot exceptions that would "
        "otherwise be swallowed. Default: 'true'.",
        default="true",
    )


# ==============================================================
# pytest hooks: configure / collection_modifyitems（ADR-0003 Phase 1）
# ==============================================================


# `pytest.Config` に乗せる attribute 名（_environments_config）。`getattr` で安全に
# アクセスできるよう一意なプレフィックスを付ける。
_CONFIG_ATTR_ENV_CONFIG = "_qgis_puppeteer_envs_config"
_CONFIG_ATTR_ACTIVE_ENV = "_qgis_puppeteer_active_env"
# ADR-0003 Phase 2: meta-parent モードで子 invocation を回す env 名のタプル。
# None なら single-env モード（または env 機能未使用）。
_CONFIG_ATTR_META_ENVS = "_qgis_puppeteer_meta_envs"
_CONFIG_ATTR_HUB_STDOUT_BUF = "_qgis_puppeteer_hub_stdout"
_CONFIG_ATTR_HUB_STDERR_BUF = "_qgis_puppeteer_hub_stderr"
# ADR-0004 Phase 2: fresh_qgis で spawn 中の worker を nodeid → SpawnedWorker で
# tracking する。makereport(call) で diag bundle に spawn_stdout/stderr を書き出す
# ために fixture teardown より先に reference を取れるようにする。
_CONFIG_ATTR_ACTIVE_FRESH_WORKERS = "_qgis_puppeteer_active_fresh_workers"
# ADR-0002 §17.5 失敗連鎖防止: 「直前テストが call で failed した」flag。
# 次テストの setup phase で読まれて fresh_qgis marker が動的付与されたあとクリアされる。
_CONFIG_ATTR_PRIOR_TEST_FAILED = "_qgis_puppeteer_prior_test_failed"
# ADR-0003 Phase 3: 子側で test outcomes を集計するための buffer
# (counts dict + failed_tests list + start time)。``pytest_sessionfinish`` で
# ``_env_stats.json`` に書き出される。
_CONFIG_ATTR_ENV_STATS = "_qgis_puppeteer_env_stats"

# 集約レポートの schema_version（``summary.json`` / ``_env_stats.json``）
_SUMMARY_SCHEMA_VERSION = 1

# 子 invocation が「自分は meta-parent から呼ばれた」と分かるよう env 変数で signal する。
# 子側ではこの env を見て更なる recursion を抑止する保険として使う。
_ENV_META_CHILD = "QPUPPETEER_META_CHILD"


def pytest_configure(config: pytest.Config) -> None:
    """marker 登録 + environments.toml ロード + active env 解決。

    `environments.toml` 不在は許容（後方互換）。`--env` 指定時に toml 不在なら
    UsageError。
    """
    config.addinivalue_line(
        "markers",
        "qgis_env(*names): assign this test to one or more QGIS environments "
        "(see ADR-0003). Tests are filtered by --env=<name>.",
    )
    config.addinivalue_line(
        "markers",
        "fresh_qgis(args=[...], env={...}): spawn a brand-new QGIS for this test "
        "only, on top of the active env's args/env. The qgis fixture is routed "
        "to the new worker via automation_client.use_instance(...). After the "
        "test, the fresh worker is killed and routing is restored. See ADR-0002 "
        "§10.4 and ADR-0003 §fresh_qgis composition rules.",
    )

    rootpath = Path(str(config.rootpath))
    toml_path = find_environments_toml(rootpath)
    envs_config: EnvironmentsConfig | None = None
    if toml_path is not None:
        try:
            envs_config = load_environments(toml_path)
        except ValueError as exc:
            raise pytest.UsageError(f"environments.toml: {exc}") from exc
        logger.info(
            "Loaded environments.toml: %d env(s) from %s",
            len(envs_config.environments),
            toml_path,
        )
    setattr(config, _CONFIG_ATTR_ENV_CONFIG, envs_config)

    # ADR-0003 Phase 4: --list-envs は他 option を評価する前に処理して exit する
    if config.getoption("qgis_list_envs", default=False):
        _print_envs_and_exit(envs_config)
        return  # 実際は pytest.exit で帰ってこないが defensive

    # ADR-0003 Phase 2: --env 値をモード判定（none/single/multi/all）
    cli_env_raw = config.getoption("qgis_env", default=None)
    try:
        mode, names = parse_env_arg(cli_env_raw)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc

    # toml 不在 + --env 指定（任意モード）→ error
    if envs_config is None:
        if mode != "none":
            raise pytest.UsageError(
                f"--env={cli_env_raw!r} was given but environments.toml was not "
                f"found under {rootpath}"
            )
        setattr(config, _CONFIG_ATTR_META_ENVS, None)
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)
        return

    # multi / all → meta-parent モード。env 名を確定して flag を立てるだけ。
    # 実際の子 invocation 起動は pytest_sessionfinish で行う（ADR-0003 Approach (b)）。
    if mode in ("multi", "all"):
        if mode == "all":
            env_names: tuple[str, ...] = tuple(e.name for e in envs_config.environments)
        else:
            env_names = names
            available = {e.name for e in envs_config.environments}
            unknown = [n for n in env_names if n not in available]
            if unknown:
                avail = ", ".join(sorted(available))
                raise pytest.UsageError(
                    f"--env: unknown environment(s) {unknown!r} (available: {avail})"
                )
        setattr(config, _CONFIG_ATTR_META_ENVS, env_names)
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)
        logger.info(
            "Meta-parent mode: will dispatch %d env(s) sequentially: %s",
            len(env_names),
            ", ".join(env_names),
        )
        return

    # single / none → Phase 1 経路
    setattr(config, _CONFIG_ATTR_META_ENVS, None)
    cli_single_name = names[0] if mode == "single" else None
    active_env = resolve_active_env(envs_config, cli_env_name=cli_single_name)
    setattr(config, _CONFIG_ATTR_ACTIVE_ENV, active_env)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """``--env=<name>`` に基づき item を絞り込む。

    `environments.toml` 不在時は no-op（後方互換）。`strategy="fail"` で marker 無し
    item があれば UsageError。

    Phase 2 meta-parent モードでは親 invocation で **全 item を deselect** する
    （実テストは ``pytest_sessionfinish`` で env ごとに子 invocation を立てて回す）。
    """
    # Phase 2: meta-parent では親が 1 件も走らないようにする
    meta_envs: tuple[str, ...] | None = getattr(config, _CONFIG_ATTR_META_ENVS, None)
    if meta_envs is not None:
        if items:
            config.hook.pytest_deselected(items=list(items))
            items[:] = []
        return

    envs_config: EnvironmentsConfig | None = getattr(config, _CONFIG_ATTR_ENV_CONFIG, None)
    if envs_config is None:
        return  # toml 無し → 全 item をそのまま通す
    active_env: EnvironmentSpec | None = getattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)

    strategy = envs_config.default.strategy
    if strategy == "fail":
        unmarked = find_unmarked_items(items)
        if unmarked:
            names = ", ".join(item.nodeid for item in unmarked[:5])
            more = "" if len(unmarked) <= 5 else f" (and {len(unmarked) - 5} more)"
            raise pytest.UsageError(
                f"[default_environment] strategy='fail' but the following tests "
                f"have no @pytest.mark.qgis_env marker: {names}{more}"
            )

    selected, deselected = filter_items_by_env_marker(
        items, active_env=active_env, strategy=strategy
    )
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


# ==============================================================
# pytest hook: meta-parent dispatch（ADR-0003 Phase 2 / Approach (b)）
# ==============================================================


def _print_envs_and_exit(envs_config: EnvironmentsConfig) -> None:
    """``--list-envs`` 用に env 一覧を stdout に出して exit 0 する（ADR-0003 Phase 4）。

    pytest 外部からも grep / shell completion で読みやすい形式（``name<TAB>description``）。
    description が空の env は名前のみを出す。
    """
    lines: list[str] = []
    for env in envs_config.environments:
        if env.description:
            lines.append(f"{env.name}\t{env.description}")
        else:
            lines.append(env.name)
    output = "\n".join(lines) + "\n" if lines else ""
    sys.stdout.write(output)
    sys.stdout.flush()
    pytest.exit(reason="--list-envs", returncode=0)


def _build_child_argv(parent_args: Sequence[str], *, env_name: str) -> list[str]:
    """親 invocation の argv から ``--env=...`` を除去し、``--env=<env_name>`` を append。

    親の argv は ``config.invocation_params.args`` から取れる（pytest 公式 API）。
    """
    stripped = strip_env_args(parent_args)
    return [*stripped, f"--env={env_name}"]


def _run_envs_and_aggregate(config: pytest.Config, env_names: Sequence[str]) -> int:
    """env 名のリストを順に子 invocation で回し、ADR-0003 ルールで exit code を集約。

    hard error（2/3/4）を見たら break して即時伝播。それ以外は最後まで回して
    ``aggregate_exit_codes`` で集約する。各子の結果と最終集約コードは log にも残す。

    Phase 3 追加:

    - 各子の ``_env_stats.json`` を読み、累計 fail を ``--maxfail`` と比較
    - 累計が ``--maxfail`` に達したら break。残 env は ``skipped_reason`` 付きで
      ``summary.json`` に記録
    - 全 env 完了後 ``outputs/summary.json`` を書き出す
    """
    parent_args = tuple(config.invocation_params.args)
    results: list[tuple[str, int]] = []
    env_results: list[dict[str, Any]] = []
    parent_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ``config.option.maxfail`` は argparse-parsed 値。0 = 無制限。
    parent_maxfail = int(getattr(config.option, "maxfail", 0) or 0)
    cumulative_fails = 0

    # 子側で「自分は meta-parent から呼ばれた」と分かるよう env 変数で marker
    saved_meta_child = os.environ.get(_ENV_META_CHILD)
    os.environ[_ENV_META_CHILD] = "1"
    try:
        skipped_envs: list[str] = []
        for idx, name in enumerate(env_names):
            # maxfail 累計チェック（直前 env 完了後の判定）
            if parent_maxfail > 0 and cumulative_fails >= parent_maxfail:
                logger.warning(
                    "Meta-parent: cumulative fails %d reached --maxfail=%d; "
                    "skipping remaining env(s): %s",
                    cumulative_fails,
                    parent_maxfail,
                    ", ".join(env_names[idx:]),
                )
                skipped_envs = list(env_names[idx:])
                for skipped in skipped_envs:
                    env_results.append(
                        _empty_env_stats(
                            skipped,
                            exitcode=0,
                            reason="not_run_due_to_cumulative_maxfail",
                        )
                    )
                break

            child_argv = _build_child_argv(parent_args, env_name=name)
            if parent_maxfail > 0:
                remaining = parent_maxfail - cumulative_fails
                # remaining > 0 はループ先頭の guard で保証
                child_argv = inject_maxfail(child_argv, remaining)

            logger.info(
                "Meta-parent: dispatching env=%s (cumulative_fails=%d) argv=%s",
                name,
                cumulative_fails,
                child_argv,
            )
            try:
                rc = pytest.main(child_argv)
            except SystemExit as exc:
                # pytest.main は通常 int を return するが、念のため SystemExit も拾う
                rc = int(exc.code) if exc.code is not None else 0
            rc_int = int(rc)
            results.append((name, rc_int))

            # 子が書いた _env_stats.json を読み、累計 fail / env_results を更新
            child_stats = _read_child_stats(config, name)
            if child_stats is None:
                # stats 不在 → exit code から推定（fail を 1 件以上と仮定するのは過剰
                # なので、child の counts が見えないままにする。maxfail 累計には影響しない）
                child_stats = _empty_env_stats(name, exitcode=rc_int, reason="env_stats_missing")
            else:
                child_stats["exitcode"] = rc_int
            env_results.append(child_stats)
            counts = child_stats.get("counts", {}) or {}
            cumulative_fails += int(counts.get("failed", 0)) + int(counts.get("errored", 0))

            logger.info(
                "Meta-parent: env=%s finished exit=%d cumulative_fails=%d",
                name,
                rc_int,
                cumulative_fails,
            )
            if is_hard_exit_code(rc_int):
                logger.warning(
                    "Meta-parent: hard exit code %d from env=%s, aborting remaining envs",
                    rc_int,
                    name,
                )
                # hard error 時は残 env を skipped_reason 付きで記録
                for skipped in env_names[idx + 1 :]:
                    env_results.append(
                        _empty_env_stats(skipped, exitcode=0, reason="not_run_due_to_hard_exit")
                    )
                break
    finally:
        if saved_meta_child is None:
            os.environ.pop(_ENV_META_CHILD, None)
        else:
            os.environ[_ENV_META_CHILD] = saved_meta_child

    strict = bool(getattr(config.option, "qgis_env_strict", False))
    aggregated = aggregate_exit_codes(results, strict=strict)
    logger.info(
        "Meta-parent: aggregated exit code = %d (strict=%s) from %d env(s): %s",
        aggregated,
        strict,
        len(results),
        ", ".join(f"{n}={c}" for n, c in results) or "<none>",
    )

    # Phase 3: outputs/summary.json を最後に書き出す
    try:
        _write_summary_json(
            config,
            env_results=env_results,
            aggregated_exit=aggregated,
            started_at=parent_started_at,
        )
    except Exception:  # noqa: BLE001 - summary 書き出し失敗で test 結果を変えない
        logger.warning("Failed to write summary.json", exc_info=True)
    return aggregated


def pytest_sessionstart(session: pytest.Session) -> None:
    """ADR-0003 Phase 3: child 側で session 開始時刻を ``_env_stats.json`` 用に記録する。

    meta-parent では active_env が None なので何もしない。single-env / meta-child の
    両方で呼ばれるが、 ``_ensure_env_session_start`` は冪等。
    """
    config = session.config
    if _get_active_env(config) is None:
        return
    _ensure_env_session_start(config)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """meta-parent モードで env ごとの子 invocation を順次起動し、exit code を集約する。

    ADR-0003 Approach (b) §"親 invocation の finalize 注意点":

    - ``pytest_configure`` で ``sys.exit`` してしまうと親の ``pytest_unconfigure`` が
      skip され、cov / cacheprovider の finalize が漏れる
    - 対策: ``pytest_configure`` では flag を立てるだけ、子 invocation は
      ``pytest_sessionfinish`` で起動、最後に ``session.exitstatus`` を上書き
    - ``tryfirst=True`` で他 plugin より先に走らせ、子 invocation 完了後に親側の
      finalize 群（pytest-cov 等）が動くようにする

    Phase 3: child 側では active_env が解決されている場合に ``_env_stats.json`` を
    書き出す（meta-parent / single-env 共通）。meta-parent 自身では env stats は
    書かない（単に dispatch 役）。

    meta-parent 以外（single-env / 通常）では meta-dispatch 部分は no-op。
    """
    del exitstatus  # session.exitstatus を直接書き換えるので未使用
    config = session.config
    meta_envs: tuple[str, ...] | None = getattr(config, _CONFIG_ATTR_META_ENVS, None)

    # Phase 3: child 側 (single-env / meta-child) で _env_stats.json を書き出す
    if not meta_envs:
        try:
            _write_env_stats(config, exitstatus=session.exitstatus)
        except Exception:  # noqa: BLE001 - stats dump は best-effort
            logger.warning("Failed to write _env_stats.json", exc_info=True)
        try:
            _postprocess_junit_for_env_prefix(config)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to post-process JUnit XML for env prefix", exc_info=True)
        return

    aggregated = _run_envs_and_aggregate(config, meta_envs)
    session.exitstatus = aggregated


def _get_active_env(config: pytest.Config) -> EnvironmentSpec | None:
    """plugin 内部用 accessor。fixture から active env 設定を取得。"""
    return getattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)


# ==============================================================
# ADR-0003 Phase 3: per-env stats / summary.json / JUnit env prefix
# ==============================================================


def _outputs_root(config: pytest.Config) -> Path:
    """``outputs/`` のルートを ``qgis_diag_dir`` の親から推定する。

    ``qgis_diag_dir`` の既定 ``outputs/diagnostics`` から ``.parent`` で ``outputs``
    を取る。ユーザーが diag dir を絶対パスや別構成にしている場合もそれに追随する
    （例: ``/tmp/diag`` → ``/tmp``）。
    """
    diag_dir = Path(config.getini("qgis_diag_dir") or _DEFAULT_DIAG_DIR)
    return diag_dir.parent


def _env_output_dir(config: pytest.Config, env_name: str) -> Path:
    """``outputs/<env_name>/`` パスを返す（ADR-0003 Phase 1 と同じ規則）。"""
    return _outputs_root(config) / env_name


def _ensure_env_session_start(config: pytest.Config) -> dict[str, Any]:
    """child の session start 時刻を ``_CONFIG_ATTR_ENV_STATS`` に記録する（idempotent）。

    ADR-0003 Phase 3: ``pytest_sessionstart`` で初期化、``pytest_sessionfinish`` で
    duration 計算に使う。terminalreporter から count は取得するので、ここでは時刻のみ。
    """
    stats: dict[str, Any] | None = getattr(config, _CONFIG_ATTR_ENV_STATS, None)
    if stats is None:
        stats = {
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "started_monotonic": time.monotonic(),
        }
        setattr(config, _CONFIG_ATTR_ENV_STATS, stats)
    return stats


def _collect_counts_from_terminalreporter(
    config: pytest.Config,
) -> tuple[dict[str, int], list[str]]:
    """child 側で ``terminalreporter.stats`` から outcome 別 count + failed nodeid を集める。

    pytest の terminalreporter.stats は ``{outcome_name: [TestReport, ...]}`` の辞書。
    outcome name の代表値:
    - ``"passed"`` / ``"failed"`` / ``"skipped"`` / ``"error"`` / ``"xfailed"`` / ``"xpassed"``

    failed_tests には call-phase failed と setup/teardown errored の両方の nodeid を
    重複なく入れる（ADR-0003 §"`--maxfail` 累計" の cumulative_fails と整合させる）。
    """
    counts = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errored": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    failed_tests: list[str] = []
    seen: set[str] = set()
    tr = config.pluginmanager.get_plugin("terminalreporter")
    if tr is None:
        return counts, failed_tests
    stats: dict[str, list[Any]] = getattr(tr, "stats", {}) or {}

    name_map = {
        "passed": "passed",
        "failed": "failed",
        "skipped": "skipped",
        "error": "errored",
        "xfailed": "xfailed",
        "xpassed": "xpassed",
    }
    for tr_key, our_key in name_map.items():
        reports = stats.get(tr_key, []) or []
        # passed には setup/teardown phase も入る。pytest_terminalreporter の慣例:
        # stats["passed"] は call passed のみ。stats["failed"] は call failed のみ。
        # stats["error"] は setup/teardown failed。
        # 全部そのまま count しても合計は概ね正しい（pytest 標準集計と同じ）。
        if our_key in ("failed", "errored"):
            for rep in reports:
                nodeid = getattr(rep, "nodeid", None)
                if isinstance(nodeid, str) and nodeid not in seen:
                    failed_tests.append(nodeid)
                    seen.add(nodeid)
        counts[our_key] = len(reports)
    return counts, failed_tests


def _write_env_stats(config: pytest.Config, *, exitstatus: int) -> None:
    """active env がある child 側で ``_env_stats.json`` を書き出す（ADR-0003 Phase 3）。"""
    active_env = _get_active_env(config)
    if active_env is None:
        return
    session_meta = _ensure_env_session_start(config)
    finished_monotonic = time.monotonic()
    duration_s = round(
        finished_monotonic - session_meta.get("started_monotonic", finished_monotonic),
        3,
    )
    counts, failed_tests = _collect_counts_from_terminalreporter(config)
    payload = {
        "schema_version": _SUMMARY_SCHEMA_VERSION,
        "env_name": active_env.name,
        "exitcode": int(exitstatus),
        "started_at": session_meta["started_at"],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": duration_s,
        "counts": counts,
        "failed_tests": failed_tests,
    }
    out_dir = _env_output_dir(config, active_env.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_env_stats.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Phase 3: wrote %s (env=%s, exit=%d, counts=%s)",
        out_dir / "_env_stats.json",
        active_env.name,
        exitstatus,
        counts,
    )


def _postprocess_junit_for_env_prefix(config: pytest.Config) -> None:
    """child 側で JUnit XML の ``testcase.classname`` に env 名を prefix する。

    ADR-0003 §"artifacts / JUnit の衝突回避": CI の集約ツールで全 env を 1 ファイルに
    merge する場合に同名 test を区別するため、``<env_name>::<classname>`` 形式に
    書き換える。``--junit-xml`` 未指定 / active_env 無しなら no-op。

    既に ``<env_name>::`` で始まっている場合は冪等に skip（再実行 / 二重 invocation 対策）。
    """
    active_env = _get_active_env(config)
    if active_env is None:
        return
    xmlpath = getattr(config.option, "xmlpath", None)
    if not xmlpath:
        return
    junit_path = Path(str(xmlpath))
    if not junit_path.is_file():
        return
    prefix = f"{active_env.name}::"
    try:
        # stdlib の ElementTree で XML を読み書き（依存追加なし）
        import xml.etree.ElementTree as ET

        tree = ET.parse(junit_path)
        modified = False
        for testcase in tree.iter("testcase"):
            cls = testcase.get("classname", "")
            if cls and not cls.startswith(prefix):
                testcase.set("classname", f"{prefix}{cls}")
                modified = True
        if modified:
            tree.write(junit_path, encoding="utf-8", xml_declaration=True)
            logger.info(
                "Phase 3: prefixed JUnit testcase classnames with %s in %s",
                prefix,
                junit_path,
            )
    except ET.ParseError:
        logger.warning("JUnit XML parse failed; skipping env prefix", exc_info=True)


def _read_child_stats(config: pytest.Config, env_name: str) -> dict[str, Any] | None:
    """meta-parent が child の ``_env_stats.json`` を読む（無ければ None）。"""
    path = _env_output_dir(config, env_name) / "_env_stats.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("Could not parse %s", path, exc_info=True)
        return None


def _empty_env_stats(env_name: str, *, exitcode: int, reason: str) -> dict[str, Any]:
    """maxfail 累計で skip された env や、stats 不在の env 用の placeholder。"""
    return {
        "schema_version": _SUMMARY_SCHEMA_VERSION,
        "env_name": env_name,
        "exitcode": exitcode,
        "started_at": None,
        "finished_at": None,
        "duration_s": 0.0,
        "counts": {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errored": 0,
            "xfailed": 0,
            "xpassed": 0,
        },
        "failed_tests": [],
        "skipped_reason": reason,
    }


def _write_summary_json(
    config: pytest.Config,
    *,
    env_results: list[dict[str, Any]],
    aggregated_exit: int,
    started_at: str,
) -> None:
    """meta-parent が ``outputs/summary.json`` を書く（ADR-0003 Phase 3）。"""
    totals: dict[str, int] = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errored": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    for env_stats in env_results:
        for k, v in env_stats.get("counts", {}).items():
            totals[k] = totals.get(k, 0) + int(v)
    payload = {
        "schema_version": _SUMMARY_SCHEMA_VERSION,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exit_code": int(aggregated_exit),
        "envs": env_results,
        "totals": totals,
    }
    out = _outputs_root(config) / "summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Phase 3: wrote %s (envs=%d totals=%s)", out, len(env_results), totals)


# ==============================================================
# 設定解決ヘルパ
# ==============================================================


def _resolve_setting(
    config: pytest.Config, *, cli_key: str, env_key: str, ini_key: str
) -> str | None:
    """CLI > env > ini の優先順位で文字列設定を引き出す。空文字列も None 扱い。"""
    cli_val = config.getoption(cli_key, default=None)
    if cli_val:
        return str(cli_val)
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    ini_val = config.getini(ini_key)
    if ini_val:
        return str(ini_val)
    return None


def _resolve_qgis_args(config: pytest.Config) -> list[str]:
    """QGIS 起動引数を CLI / env / ini から解決して list で返す。"""
    cli_val = config.getoption("qgis_args", default=None)
    if cli_val:
        return shlex.split(cli_val)
    env_val = os.environ.get(ENV_QGIS_ARGS)
    if env_val:
        return shlex.split(env_val)
    ini_val = config.getini("qgis_args")
    if isinstance(ini_val, list):
        return [str(x) for x in ini_val]
    return []


def _resolve_qgis_command(config: pytest.Config) -> list[str]:
    """`qgis_command` 設定（QGIS 起動コマンド全体）を CLI / env / ini から解決。

    空 list を返したら未設定（呼び出し側は `qgis_bin + qgis_args` 経路に進む）。

    後方互換のため env spec を見ない。env spec 込みで解決したい場合は
    ``_resolve_qgis_command_with_env`` を使う。
    """
    cli_val = config.getoption("qgis_command", default=None)
    if cli_val:
        return shlex.split(cli_val) if isinstance(cli_val, str) else list(cli_val)
    env_val = os.environ.get(ENV_QGIS_COMMAND)
    if env_val:
        return shlex.split(env_val)
    ini_val = config.getini("qgis_command")
    if isinstance(ini_val, list) and ini_val:
        return [str(x) for x in ini_val]
    return []


def _resolve_qgis_command_with_env(
    config: pytest.Config, active_env: EnvironmentSpec | None
) -> list[str]:
    """`qgis_command` を CLI > env > env_spec > ini で解決（ADR-0003）。

    既存の ``_resolve_qgis_command`` に env spec の値を挟んだ拡張。CLI / env が
    未指定で env spec が ``qgis_command`` を持っていればそれを使う。
    """
    cli_val = config.getoption("qgis_command", default=None)
    if cli_val:
        return shlex.split(cli_val) if isinstance(cli_val, str) else list(cli_val)
    env_val = os.environ.get(ENV_QGIS_COMMAND)
    if env_val:
        return shlex.split(env_val)
    if active_env is not None and active_env.qgis_command:
        return list(active_env.qgis_command)
    ini_val = config.getini("qgis_command")
    if isinstance(ini_val, list) and ini_val:
        return [str(x) for x in ini_val]
    return []


def _resolve_qgis_args_with_env(
    config: pytest.Config, active_env: EnvironmentSpec | None
) -> list[str]:
    """`qgis_args` を CLI > env > env_spec > ini で解決（ADR-0003）。"""
    cli_val = config.getoption("qgis_args", default=None)
    if cli_val:
        return shlex.split(cli_val)
    env_val = os.environ.get(ENV_QGIS_ARGS)
    if env_val:
        return shlex.split(env_val)
    if active_env is not None and active_env.qgis_args:
        return list(active_env.qgis_args)
    ini_val = config.getini("qgis_args")
    if isinstance(ini_val, list):
        return [str(x) for x in ini_val]
    return []


def _is_dev_mode() -> bool:
    return os.environ.get(ENV_DEV_MODE) == "1"


def _dev_hub_url() -> str:
    return os.environ.get(ENV_DEV_HUB_URL, DEFAULT_DEV_HUB_URL)


def _parse_port(url: str) -> int:
    parsed = urlparse(url)
    if parsed.port is None:
        raise ValueError(
            f"Hub URL {url!r} has no explicit port; set {ENV_DEV_HUB_URL} with explicit port"
        )
    return parsed.port


# ==============================================================
# ヘルパ
# ==============================================================


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port_listen(port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.1)
    raise TimeoutError(f"Hub did not start listening on port {port} within {timeout_s}s")


def _hub_bootstrap_path() -> Path:
    """`qgis_puppeteer._hub_bootstrap` のソースファイルを返す。

    QGIS 付属 Python launcher 経由で起動するとき、`-m qgis_puppeteer.hub` だと
    PYTHONPATH が QGIS bundle 側に切り替わって venv 上の qgis_puppeteer が見え
    なくなる。そこで bootstrap スクリプトを直接ファイル指定して起動し、そちら
    で sys.path を組み直して `hub.main()` を呼ぶ仕組み（ADR-0001）。
    """
    import qgis_puppeteer

    return Path(qgis_puppeteer.__file__).parent / "_hub_bootstrap.py"


def _pid_alive(pid: int) -> bool:
    """pid のプロセスがまだ生きているかを cross-platform に判定する（psutil 不使用）。

    posix: ``os.kill(pid, 0)`` が ``ProcessLookupError`` なら死亡。
    Windows: ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` + ``GetExitCodeProcess``
    で exit code が ``STILL_ACTIVE`` (259) なら生存とみなす。

    ADR-0004 Phase 4: ``_terminate_pid_tree`` の固定 sleep を poll に置き換える
    ためのヘルパ。
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not h:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_pid_dead(pid: int, *, timeout_s: float, poll_interval_s: float = 0.1) -> bool:
    """pid のプロセスが消えるまで poll する。timeout 内に消えれば True を返す。

    ADR-0004 Phase 4: 固定 sleep 廃止のための helper。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if not _pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return not _pid_alive(pid)
        time.sleep(poll_interval_s)


def _terminate_pid_tree(pid: int, *, graceful_timeout_s: float) -> None:
    """QGIS プロセスを graceful → timeout で force kill する。

    ADR-0004 Phase 4: 固定 ``time.sleep(graceful_timeout_s)`` ではなく
    ``_wait_pid_dead`` で poll するので、QGIS が graceful 終了したら待ち時間を
    切り上げる（fresh_qgis 多用テストの total time 短縮に効く）。
    """
    if sys.platform != "win32":
        import signal as _signal

        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError:
            logger.debug("SIGTERM(%d) raised", pid, exc_info=True)
            return
        if _wait_pid_dead(pid, timeout_s=graceful_timeout_s):
            return
        try:
            os.kill(pid, _signal.SIGKILL)
        except OSError:
            logger.debug("SIGKILL(%d) raised", pid, exc_info=True)
        return

    logger.info("Terminating QGIS pid=%d (graceful)", pid)
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T"],
        check=False,
        capture_output=True,
    )
    if _wait_pid_dead(pid, timeout_s=graceful_timeout_s):
        return
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        logger.info("Force-killed QGIS pid=%d", pid)


def _shutdown_registered_workers(client: E2EAutomationClient) -> None:
    """Hub に register されている全 Worker を順に graceful kill。"""
    try:
        instances = client.list_instances()
    except Exception:  # noqa: BLE001 - best-effort on teardown
        logger.warning("Could not list workers for teardown", exc_info=True)
        return
    for inst in instances:
        _terminate_pid_tree(inst.pid, graceful_timeout_s=DEFAULT_QGIS_GRACEFUL_SHUTDOWN_TIMEOUT_S)


# ==============================================================
# session fixtures
# ==============================================================


@pytest.fixture(scope="session")
def hub_port() -> int:
    if _is_dev_mode():
        port = _parse_port(_dev_hub_url())
        logger.info("Dev mode: using existing Hub at port %d", port)
        return port
    port = _free_port()
    logger.info("Hub will listen on free port %d", port)
    return port


@pytest.fixture(scope="session")
def hub_url(hub_port: int) -> str:
    if _is_dev_mode():
        return _dev_hub_url()
    return f"ws://127.0.0.1:{hub_port}"


@pytest.fixture(scope="session")
def hub_process(
    pytestconfig: pytest.Config, hub_port: int
) -> Iterator[subprocess.Popen[bytes] | None]:
    """Hub を QGIS 付属 Python launcher 経由で spawn し、listen 確認後に yield。

    venv の python では PyQt5 が解決できないので、`qgis_python` ini /
    `QPUPPETEER_QGIS_PYTHON` env / `--qgis-python` CLI で QGIS 付属 Python
    launcher（例: `python-qgis-ltr.bat`）を指定する必要がある。

    dev モードでは spawn せず、既存 Hub が listen 済みであることだけ確認。
    """
    if _is_dev_mode():
        try:
            _wait_port_listen(hub_port, timeout_s=2.0)
        except TimeoutError as exc:
            raise RuntimeError(
                f"Dev mode: external Hub at port {hub_port} is not listening. "
                f"Start the Hub-bearing QGIS first, or override {ENV_DEV_HUB_URL}."
            ) from exc
        logger.info("Dev mode: external Hub at port %d confirmed listening", hub_port)
        yield None
        return

    active_env = _get_active_env(pytestconfig)
    qgis_python = resolve_qgis_setting(
        cli_value=pytestconfig.getoption("qgis_python", default=None),
        env_value=os.environ.get(ENV_QGIS_PYTHON),
        env_spec_value=active_env.qgis_python if active_env is not None else None,
        ini_value=pytestconfig.getini("qgis_python"),
    )
    if not qgis_python:
        # 未指定なら OSS 公開 API でフォールバック：
        # OSGEO4W_ROOT/bin/python-qgis-ltr.bat または sys.executable 兄弟を探す。
        from qgis_puppeteer import find_qgis_python_launcher

        discovered = find_qgis_python_launcher()
        if discovered is None:
            raise pytest.UsageError(
                "qgis_python is not configured and could not be auto-discovered. "
                f"Set --qgis-python, ${ENV_QGIS_PYTHON}, [tool.pytest.ini_options] "
                "qgis_python, or set OSGEO4W_ROOT to enable auto-discovery."
            )
        qgis_python = str(discovered)
        logger.info("Auto-discovered qgis_python: %s", qgis_python)
    elif not Path(qgis_python).is_file():
        raise pytest.UsageError(f"qgis_python does not exist: {qgis_python}")

    cmd = [
        qgis_python,
        str(_hub_bootstrap_path()),
        "--port",
        str(hub_port),
        "--no-pid-file",
    ]
    logger.info("Spawning Hub: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Hub の stderr/stdout を drain thread で吸い上げ session config に貯める。
    # 失敗時の diagnostic bundle に hub_stderr.log として書き出すため。
    # `_wait_port_listen` で listening 確認が出来なかった場合の早期失敗パスでも
    # 既に少し溜まっていればそれが診断に役立つ。
    hub_stdout_buf: list[str] = []
    hub_stderr_buf: list[str] = []
    setattr(pytestconfig, _CONFIG_ATTR_HUB_STDOUT_BUF, hub_stdout_buf)
    setattr(pytestconfig, _CONFIG_ATTR_HUB_STDERR_BUF, hub_stderr_buf)
    drain_threads: list[threading.Thread] = []
    for stream, buf, name in (
        (proc.stdout, hub_stdout_buf, "stdout"),
        (proc.stderr, hub_stderr_buf, "stderr"),
    ):
        t = threading.Thread(
            target=_drain_pipe_to_list,
            args=(stream, buf),
            daemon=True,
            name=f"hub-{name}-drain",
        )
        t.start()
        drain_threads.append(t)

    try:
        _wait_port_listen(hub_port, timeout_s=DEFAULT_HUB_LISTEN_TIMEOUT_S)
    except TimeoutError:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        # drain thread が回収済みの stderr を使う（communicate は drain と競合する）
        for t in drain_threads:
            t.join(timeout=1.0)
        err_text = "\n".join(hub_stderr_buf) or "<no stderr captured>"
        raise RuntimeError(
            f"Hub subprocess failed to start listening.\nstderr:\n{err_text}"
        ) from None
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for t in drain_threads:
            t.join(timeout=2.0)
        logger.info("Hub subprocess terminated (returncode=%s)", proc.returncode)


@pytest.fixture(scope="session")
def automation_client(
    hub_process: subprocess.Popen[bytes] | None,
    hub_url: str,
    action_recorder: ActionRecorder,
) -> Iterator[E2EAutomationClient]:
    del hub_process
    client = E2EAutomationClient(url=hub_url)
    client.connect()
    # ADR-0002 §13 / B5a: 各 ``call(...)`` を timeline に記録し、test 失敗時の
    # diagnostic bundle に ``actions.jsonl`` として吐き出す。test 単位の clear は
    # ``_per_test_action_recorder`` (autouse) で行う。
    client.set_recorder(action_recorder)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def action_recorder() -> ActionRecorder:
    """E2E test の Worker call timeline を蓄積する recorder（ADR-0002 §13 / B5a）。

    session 1 つに 1 instance。autouse の function-scope fixture
    （``_per_test_action_recorder``）が test ごとに ``clear()`` を呼ぶため、
    test 内で見える actions はその test 内で発火したものだけ。
    """
    return ActionRecorder()


@pytest.fixture(autouse=True)
def _per_test_action_recorder(
    request: pytest.FixtureRequest,
    action_recorder: ActionRecorder,
    pytestconfig: pytest.Config,
) -> Iterator[None]:
    """各 test の冒頭で recorder を ``clear()`` する autouse fixture。

    session-scope の recorder を test 単位の timeline として使うための仕掛け。
    ``automation_client`` が要求されない test では recorder は使われないので、
    clear のコスト（list を空にする O(n)）だけ。

    B5b: ini の ``qgis_diag_capture_screenshots`` が true（既定）なら、test 単位の
    pending screenshot dir を作って recorder.set_screenshot_dir(...) する。失敗時は
    ``_dump_diagnostic_bundle`` が pending dir を bundle/screenshots/ に rename する。
    成功時は teardown で pending dir を削除する。

    diagnostic bundle dump（``_dump_diagnostic_bundle``）は本 fixture とは独立に、
    ``pytest_runtest_makereport`` フックから recorder を直接参照して書き出す。
    """
    action_recorder.clear()
    # step stack も念のため reset（前 test が step 中に SystemExit 等で死んだ保険）
    action_recorder.reset_step_stack()

    # B5b: per-action screenshot capture
    pending_dir: Path | None = None
    if _resolve_diag_capture_screenshots(pytestconfig):
        pending_dir = _pending_screenshot_dir(pytestconfig, request.node)
        try:
            pending_dir.mkdir(parents=True, exist_ok=True)
            action_recorder.set_screenshot_dir(pending_dir)
        except OSError:
            logger.warning("Could not create pending screenshot dir %s", pending_dir, exc_info=True)
            pending_dir = None
            action_recorder.set_screenshot_dir(None)

    try:
        yield
    finally:
        # capture を解除（fixture teardown 後の call が pending に書き残さないように）
        action_recorder.set_screenshot_dir(None)
        # 成功時のみ pending dir を削除。失敗時は ``_dump_diagnostic_bundle`` が
        # 既に bundle/screenshots/ へ rename しているので存在しない。
        if pending_dir is not None and pending_dir.exists():
            import shutil

            shutil.rmtree(pending_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def qgis_process(
    pytestconfig: pytest.Config,
    automation_client: E2EAutomationClient,
    hub_port: int,
) -> Iterator[None]:
    """`qgis_bin` で指定された QGIS 実行ファイルを spawn する。

    QGIS 内の `qgis_puppet` プラグインが `QPUPPETEER_HUB_PORT` env を見て
    外部所有者モードに入り、自前で Hub を spawn しない。teardown では Hub に
    register された Worker の pid を `list_instances` で拾って kill。

    dev モードでは spawn も teardown もしない。
    """
    if _is_dev_mode():
        logger.info("Dev mode: skipping QGIS spawn/teardown (using existing Worker)")
        yield None
        return

    active_env = _get_active_env(pytestconfig)

    # 優先順位（ADR-0003）: CLI > 環境変数 > environments.toml の env spec > ini。
    # `qgis_command` が解決されていればそれを丸ごと使う（ホスト独自の launcher
    # 経路）。未解決なら従来の `qgis_bin + qgis_args` を使う。
    qgis_command = _resolve_qgis_command_with_env(pytestconfig, active_env)
    spawn_kwargs: dict[str, Any] = {}
    if qgis_command:
        spawn_kwargs["command"] = qgis_command
    else:
        qgis_bin = resolve_qgis_setting(
            cli_value=pytestconfig.getoption("qgis_bin", default=None),
            env_value=os.environ.get(ENV_QGIS_BIN),
            env_spec_value=active_env.qgis_bin if active_env is not None else None,
            ini_value=pytestconfig.getini("qgis_bin"),
        )
        if not qgis_bin:
            raise pytest.UsageError(
                "Neither qgis_command nor qgis_bin is configured. Set one of:\n"
                f"  - qgis_command (--qgis-command / ${ENV_QGIS_COMMAND} / "
                "[tool.pytest.ini_options] qgis_command / environments.toml) "
                "for full launch command\n"
                f"  - qgis_bin (--qgis-bin / ${ENV_QGIS_BIN} / qgis_bin ini / "
                "environments.toml) for direct executable spawn"
            )
        if not Path(qgis_bin).is_file():
            raise pytest.UsageError(f"qgis_bin does not exist: {qgis_bin}")
        spawn_kwargs["qgis_bin"] = qgis_bin
        spawn_kwargs["args"] = _resolve_qgis_args_with_env(pytestconfig, active_env)

    # ADR-0004 Phase 4: spawn / register 待機 / drain thread / graceful kill /
    # dangling worker last-resort をすべて spawn_qgis に委譲。重複ロジックを排除し、
    # session worker でも stdout/stderr が capture される副次効果がある（diagnostic
    # bundle 拡張で活用可能）。
    from pytest_qgis_puppeteer.spawn import spawn_qgis

    user_env = dict(active_env.env) if (active_env is not None and active_env.env) else None
    register_timeout_s = float(
        pytestconfig.getini("qgis_startup_timeout") or DEFAULT_WORKER_REGISTER_TIMEOUT_S
    )
    with spawn_qgis(
        hub_port=hub_port,
        automation_client=automation_client,
        env=user_env,
        register_timeout_s=register_timeout_s,
        graceful_shutdown_timeout_s=DEFAULT_QGIS_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        label="qgis_process",
        **spawn_kwargs,
    ):
        try:
            yield None
        finally:
            # session 中に fresh_qgis 等で別途 register された Worker が残っていれば
            # ここで一括掃除（spawn_qgis 自身は自分の Worker しか kill しないため）。
            _shutdown_registered_workers(automation_client)


@pytest.fixture(scope="session")
def hub_ready(
    pytestconfig: pytest.Config,
    automation_client: E2EAutomationClient,
    qgis_process: None,
) -> str:
    del qgis_process
    timeout_s = float(
        pytestconfig.getini("qgis_startup_timeout") or DEFAULT_WORKER_REGISTER_TIMEOUT_S
    )
    return automation_client.wait_for_worker(timeout_s=timeout_s)


# ==============================================================
# function scope fixtures
# ==============================================================


def _resolve_fresh_qgis_spawn(
    config: pytest.Config,
    active_env: EnvironmentSpec | None,
    *,
    extra_args: list[str],
    extra_env: dict[str, str],
) -> tuple[list[str] | None, str | None, list[str], dict[str, str]]:
    """`fresh_qgis` marker の spawn パラメータを active env と合成する（ADR-0003）。

    合成ルール（ADR-0003 §fresh_qgis 合成ルール）:

    - ``qgis_args``: env の args に ``fresh_qgis(args=...)`` を **append**
    - ``env`` table: env の env table に ``fresh_qgis(env=...)`` を shallow merge
      （同一キーは fresh_qgis 優先）
    - ``qgis_command`` 指定 env では args が command 末尾に append される（host
      launcher 側で未知 arg を QGIS に転送する責務）
    - ``qgis_bin`` / ``qgis_python`` は env / 既存設定から解決（``fresh_qgis`` で
      の上書きは不可）

    Returns:
        ``(command, qgis_bin, args, env_vars)``: ``command`` が non-None ならそれが
        起動コマンド全体、それ以外なら ``qgis_bin + args``。``env_vars`` は spawn 時
        に追加で merge する env（base ENV + helper inject の上に乗る）。
    """
    qgis_command = _resolve_qgis_command_with_env(config, active_env)
    if qgis_command:
        # ADR-0003 ルール: qgis_command 指定下では fresh_qgis args を末尾に append
        # （host launcher の責務として未知 arg を QGIS へ転送する）
        merged_command = list(qgis_command) + list(extra_args)
        env_vars = dict(active_env.env) if active_env else {}
        env_vars.update(extra_env)
        return (merged_command, None, [], env_vars)

    qgis_bin = resolve_qgis_setting(
        cli_value=config.getoption("qgis_bin", default=None),
        env_value=os.environ.get(ENV_QGIS_BIN),
        env_spec_value=active_env.qgis_bin if active_env is not None else None,
        ini_value=config.getini("qgis_bin"),
    )
    if not qgis_bin:
        raise pytest.UsageError("fresh_qgis: neither qgis_command nor qgis_bin is configured")

    base_args = _resolve_qgis_args_with_env(config, active_env)
    merged_args = list(base_args) + list(extra_args)
    env_vars = dict(active_env.env) if active_env else {}
    env_vars.update(extra_env)
    return (None, str(qgis_bin), merged_args, env_vars)


@pytest.fixture
def qgis(
    request: pytest.FixtureRequest,
    pytestconfig: pytest.Config,
    automation_client: E2EAutomationClient,
    hub_ready: str,
    hub_port: int,
) -> Iterator[E2EAutomationClient]:
    """テスト用 AutomationClient を供給する function-scope fixture。

    通常は session の ``automation_client`` をそのまま返す。``@pytest.mark.fresh_qgis``
    が付いた test では、その test だけ別 QGIS を spawn してそちらに routing する
    （ADR-0002 §10.4 / ADR-0003 §fresh_qgis 合成ルール）。
    """
    del hub_ready  # session worker が register 済みであることを要求するだけ
    marker = request.node.get_closest_marker("fresh_qgis")
    if marker is None:
        yield automation_client
        return

    # dev mode 下で fresh_qgis は意味を成さない（既存 QGIS を再起動できない）
    if _is_dev_mode():
        pytest.skip(
            f"@pytest.mark.fresh_qgis is incompatible with dev mode "
            f"({ENV_DEV_MODE}=1). Disable dev mode to run this test."
        )

    extra_args = list(marker.kwargs.get("args") or [])
    extra_env = dict(marker.kwargs.get("env") or {})
    register_timeout_s = float(
        marker.kwargs.get("register_timeout_s")
        or pytestconfig.getini("qgis_startup_timeout")
        or DEFAULT_WORKER_REGISTER_TIMEOUT_S
    )

    active_env = _get_active_env(pytestconfig)
    command, qgis_bin, args, env_vars = _resolve_fresh_qgis_spawn(
        pytestconfig, active_env, extra_args=extra_args, extra_env=extra_env
    )

    # spawn_qgis を context manager として使う（teardown も任せる）
    from pytest_qgis_puppeteer.spawn import spawn_qgis

    saved_default = automation_client.get_default_instance()
    label = f"fresh-{request.node.name}"
    spawn_kwargs: dict[str, Any] = {
        "hub_port": hub_port,
        "automation_client": automation_client,
        "env": env_vars,
        "register_timeout_s": register_timeout_s,
        "label": label,
    }
    if command is not None:
        spawn_kwargs["command"] = command
    else:
        spawn_kwargs["qgis_bin"] = qgis_bin
        spawn_kwargs["args"] = args

    with spawn_qgis(**spawn_kwargs) as worker:
        # ADR-0004 P2: active worker を tracking して diag bundle に spawn logs を入れる。
        # makereport(call) は fixture teardown より先に走るので、ここで register すれば
        # 失敗時に worker.captured_stdout()/captured_stderr() 経由で snapshot を取れる。
        active_fresh: dict[str, Any] | None = getattr(
            pytestconfig, _CONFIG_ATTR_ACTIVE_FRESH_WORKERS, None
        )
        if active_fresh is None:
            active_fresh = {}
            setattr(pytestconfig, _CONFIG_ATTR_ACTIVE_FRESH_WORKERS, active_fresh)
        active_fresh[request.node.nodeid] = worker
        automation_client.use_instance(worker.instance_id)
        try:
            yield automation_client
        finally:
            automation_client.use_instance(saved_default)
            active_fresh.pop(request.node.nodeid, None)


# ==============================================================
# pytest hook: 失敗時の diagnostic bundle 出力
# ==============================================================


def _safe_filename(name: str) -> str:
    keep = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip("_")[:120]


def _resolve_diag_capture_screenshots(config: pytest.Config) -> bool:
    """``qgis_diag_capture_screenshots`` ini を bool で解釈する。

    値は文字列で来るので "true"/"yes"/"1"/"on" を真とする（pytest ini の慣例）。
    未指定 / 空文字列 / 不明値は **既定 True**（ON）。
    """
    raw = config.getini("qgis_diag_capture_screenshots")
    if raw is None or raw == "":
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _resolve_auto_fresh_after_failure(config: pytest.Config) -> bool:
    """``qgis_auto_fresh_after_failure`` ini を bool で解釈する。

    既定 False（fresh_qgis は重いので opt-in）。truthy 値は "true"/"yes"/"1"/"on"。
    """
    raw = config.getini("qgis_auto_fresh_after_failure")
    if raw is None or raw == "":
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _resolve_fail_on_uncaught_exception(config: pytest.Config) -> bool:
    """``qgis_fail_on_uncaught_exception`` ini を bool で解釈する（既定 ON）。

    silent failure 根絶のための基本機能なので opt-out 方式（既定 True）。truthy 値は
    "true"/"yes"/"1"/"on"。未指定 / 空は既定 True。
    """
    raw = config.getini("qgis_fail_on_uncaught_exception")
    if raw is None or raw == "":
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _format_uncaught_exceptions(excs: list[dict[str, Any]]) -> str:
    """Worker 由来の例外レコード列を pytest の longrepr 用テキストに整形する。"""
    lines = [f"Worker recorded {len(excs)} uncaught exception(s) during the call phase:"]
    for i, e in enumerate(excs, 1):
        ts = e.get("ts", "<no-ts>")
        thread = e.get("thread", "<no-thread>")
        exc_type = e.get("type", "<unknown>")
        message = e.get("message", "")
        tb = e.get("traceback", "") or ""
        lines.append("")
        lines.append(f"--- [{i}] {exc_type} @ {ts} ({thread}) ---")
        if message:
            lines.append(message)
        if tb:
            lines.append(tb.rstrip())
    return "\n".join(lines)


def _pending_screenshot_dir(config: pytest.Config, node: pytest.Item) -> Path:
    """B5b: per-action screenshot の pending dir パス。

    ``<diag_root>/_pending/<safe_nodeid>_<ts>``。失敗時は同じ disk volume に置かれる
    ``<diag_root>/<bundle_dir>/screenshots`` へ rename される（atomic）。

    test ごとに timestamp を含めて衝突を避ける（同じ test を連続で回した時の race も
    防ぐ）。``_safe_filename`` で nodeid を path-safe 化。
    """
    diag_root = Path(config.getini("qgis_diag_dir") or _DEFAULT_DIAG_DIR)
    active_env = _get_active_env(config)
    if active_env is not None:
        diag_root = diag_root.parent / active_env.name / diag_root.name
    safe_name = _safe_filename(node.nodeid)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return diag_root / "_pending" / f"{safe_name}_{timestamp}"


def _build_diag_meta(
    item: pytest.Item,
    *,
    report: pytest.TestReport | None,
    call: pytest.CallInfo[Any] | None,
    bundle_dir: Path,
    active_env: EnvironmentSpec | None,
    instances: list[Any] | None,
) -> dict[str, Any]:
    """diagnostic bundle の meta.json 内容を組み立てる（pure 関数）。

    ADR-0002 §13 の meta.json 仕様に準拠:

    - test 識別: nodeid / safe_name / timestamp
    - environment: env_name / qgis_bin / qgis_args / qgis_command 解決値
    - 失敗情報: exception type / message
    - Worker: instance_id 一覧 + label / pid / project

    どのフィールドも best-effort で、失敗時は None / 空配列を入れる。
    """
    config = item.config
    qgis_bin = resolve_qgis_setting(
        cli_value=config.getoption("qgis_bin", default=None),
        env_value=os.environ.get(ENV_QGIS_BIN),
        env_spec_value=active_env.qgis_bin if active_env is not None else None,
        ini_value=config.getini("qgis_bin"),
    )
    qgis_command = _resolve_qgis_command_with_env(config, active_env)
    qgis_args = _resolve_qgis_args_with_env(config, active_env)

    exc_type: str | None = None
    exc_message: str | None = None
    if call is not None and call.excinfo is not None:
        exc_type = call.excinfo.type.__name__
        try:
            exc_message = str(call.excinfo.value)
        except Exception:  # noqa: BLE001
            exc_message = "<unrepresentable>"

    when: str | None = None
    if report is not None:
        when = report.when

    return {
        "schema_version": 1,
        "nodeid": item.nodeid,
        "bundle_dir": str(bundle_dir),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "outcome": "failed" if report and report.failed else "unknown",
        "when": when,
        "exception_type": exc_type,
        "exception_message": exc_message,
        "env_name": active_env.name if active_env is not None else None,
        "env_description": (active_env.description if active_env is not None else None),
        "qgis_bin": qgis_bin if isinstance(qgis_bin, str) else None,
        "qgis_args": list(qgis_args),
        "qgis_command": list(qgis_command) if qgis_command else [],
        "instances": [
            {
                "instance_id": getattr(i, "instance_id", None),
                "label": getattr(i, "label", None),
                "pid": getattr(i, "pid", None),
                "project": getattr(i, "project", None),
            }
            for i in (instances or [])
        ],
    }


def _dump_diagnostic_bundle(
    item: pytest.Item,
    *,
    report: pytest.TestReport | None = None,
    call: pytest.CallInfo[Any] | None = None,
) -> None:
    """失敗テストの Worker 状態 + メタ情報を diag dir に dump（best-effort）。

    出力ファイル（ADR-0002 §13）:

    - ``screenshot.png``: QGIS メインウィンドウ
    - ``snapshot_ui.json``: ``qgis_snapshot_ui`` の出力
    - ``list_instances.json``: Hub に register 中の Worker 一覧
    - ``meta.json``: nodeid / env / qgis_bin / exception / instances 等
    - ``traceback.txt``: pytest report.longrepr のテキスト形
    - ``hub_stdout.log`` / ``hub_stderr.log``: Hub プロセスの直近出力
    - ``actions.jsonl``: B5a 追加。test 中の各 Worker call の timeline
      （schema_version=1。``ts`` / ``command`` / ``params`` / ``result`` /
      ``error`` / ``duration_ms`` / ``step_path`` / ``instance``）
    """
    if "automation_client" not in item.fixturenames:
        return
    try:
        client = item.funcargs.get("automation_client")
    except Exception:  # noqa: BLE001
        return
    if client is None:
        return

    diag_root = Path(item.config.getini("qgis_diag_dir") or _DEFAULT_DIAG_DIR)
    # ADR-0003 Phase 1: artifacts を env 別に分離（outputs/<env_name>/diagnostics/...）
    active_env = _get_active_env(item.config)
    if active_env is not None:
        diag_root = diag_root.parent / active_env.name / diag_root.name
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    safe_name = _safe_filename(item.nodeid)
    bundle_dir = diag_root / f"{safe_name}_{timestamp}"
    try:
        bundle_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Could not create diagnostic dir %s", bundle_dir, exc_info=True)
        return

    # screenshot
    try:
        client.screenshot(output_path=str(bundle_dir / "screenshot.png"))
    except Exception:  # noqa: BLE001
        logger.warning("screenshot() failed during diagnostic dump", exc_info=True)

    # snapshot_ui
    try:
        snap = client.snapshot_ui(include_main_window=True, max_depth=12)
        (bundle_dir / "snapshot_ui.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.warning("snapshot_ui() failed during diagnostic dump", exc_info=True)

    # list_instances（meta.json でも使うので一度だけ取って共有）
    instances: list[Any] = []
    try:
        instances = list(client.list_instances())
        (bundle_dir / "list_instances.json").write_text(
            json.dumps(
                [
                    {
                        "instance_id": i.instance_id,
                        "label": i.label,
                        "pid": i.pid,
                        "project": i.project,
                    }
                    for i in instances
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.warning("list_instances() failed during diagnostic dump", exc_info=True)

    # uncaught_exceptions.json（ADR-0002 §17 / Roadmap "Qt 未捕捉例外"）
    # makereport が pass→fail 格上げした場合の証拠物件。失敗オリジナルが Qt slot 例外
    # のとき、longrepr に同じ内容を入れているが bundle にも独立 file として残す。
    try:
        excs = client.get_recent_exceptions()
        if excs:
            (bundle_dir / "uncaught_exceptions.json").write_text(
                json.dumps(excs, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:  # noqa: BLE001
        logger.warning("get_recent_exceptions() failed during diagnostic dump", exc_info=True)

    # meta.json
    try:
        meta = _build_diag_meta(
            item,
            report=report,
            call=call,
            bundle_dir=bundle_dir,
            active_env=active_env,
            instances=instances,
        )
        (bundle_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.warning("meta.json failed during diagnostic dump", exc_info=True)

    # traceback.txt
    if report is not None and report.longrepr is not None:
        try:
            (bundle_dir / "traceback.txt").write_text(str(report.longrepr), encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("traceback.txt failed during diagnostic dump", exc_info=True)

    # hub_stdout.log / hub_stderr.log
    for attr, filename in (
        (_CONFIG_ATTR_HUB_STDOUT_BUF, "hub_stdout.log"),
        (_CONFIG_ATTR_HUB_STDERR_BUF, "hub_stderr.log"),
    ):
        buf = getattr(item.config, attr, None)
        if buf is None:
            continue  # dev mode 等で hub_process が走らなかったケース
        try:
            (bundle_dir / filename).write_text(
                "\n".join(buf) + ("\n" if buf else ""),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            logger.warning("%s failed during diagnostic dump", filename, exc_info=True)

    # ADR-0004 Phase 2: fresh_qgis で spawn 中の worker の captured stdout/stderr。
    # active dict に worker が居るのは「test 中に fresh_qgis で立てて、まだ
    # ``spawn_qgis()`` の with を抜けていない」状態。register timeout 等で setup phase
    # 失敗のケースは worker reference が無いので skip（今後 spawn 内例外への attach は
    # 別タスクで検討）。
    active_fresh: dict[str, Any] | None = getattr(
        item.config, _CONFIG_ATTR_ACTIVE_FRESH_WORKERS, None
    )
    if active_fresh:
        worker = active_fresh.get(item.nodeid)
        if worker is not None:
            try:
                stdout_lines = worker.captured_stdout()
                stderr_lines = worker.captured_stderr()
                (bundle_dir / "spawn_stdout.log").write_text(
                    "\n".join(stdout_lines) + ("\n" if stdout_lines else ""),
                    encoding="utf-8",
                )
                (bundle_dir / "spawn_stderr.log").write_text(
                    "\n".join(stderr_lines) + ("\n" if stderr_lines else ""),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001
                logger.warning("spawn_stdout/stderr failed during diagnostic dump", exc_info=True)

    # actions.jsonl（ADR-0002 §13 / B5a）。recorder が attach されていなければ skip。
    # client.get_recorder() が公開 API。空 actions の test でも空 file が生成される
    # （bundle に「ファイルが在る = 確実に試みた」と分かる方が運用が楽）。
    try:
        recorder = client.get_recorder() if hasattr(client, "get_recorder") else None
    except Exception:  # noqa: BLE001
        recorder = None
    if recorder is not None:
        try:
            recorder.dump_jsonl(bundle_dir / "actions.jsonl")
        except Exception:  # noqa: BLE001
            logger.warning("actions.jsonl failed during diagnostic dump", exc_info=True)

        # B5b: per-action screenshots を pending dir から bundle/screenshots/ へ移動。
        # actions.jsonl 内の screenshot_path は ``screenshots/<seq>.png`` の bundle
        # 相対 path として記録されているので、rename 後にそのまま整合する。
        ss_dir = recorder.screenshot_dir
        if ss_dir is not None and ss_dir.exists():
            target = bundle_dir / "screenshots"
            try:
                if not target.exists():
                    ss_dir.rename(target)  # atomic if same volume
                else:
                    # target が既に在るレアケース（手動再実行等）。中身を merge して移動。
                    import shutil

                    target.mkdir(parents=True, exist_ok=True)
                    for f in ss_dir.iterdir():
                        shutil.move(str(f), str(target / f.name))
                    shutil.rmtree(ss_dir, ignore_errors=True)
            except OSError:
                logger.warning(
                    "Could not move pending screenshots %s -> %s", ss_dir, target, exc_info=True
                )

    logger.info("Diagnostic bundle written to %s", bundle_dir)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:  # type: ignore[misc]
    """test の call phase 直前に Worker 側の例外バッファをクリアする（ADR-0002 §17）。

    setup/teardown phase の Qt slot 例外までは追わない（teardown は次テストにキャリー
    オーバーされ、setup は通常 pytest が直接握る）。call phase の責任 boundary を
    クリーンに切るためにここで clear する。
    """
    if _resolve_fail_on_uncaught_exception(item.config) and not _is_dev_mode():
        client = _try_get_automation_client(item)
        if client is not None:
            try:
                client.clear_recent_exceptions()
            except Exception:  # noqa: BLE001 - 通信失敗で test を落とすのは過剰
                logger.debug(
                    "clear_recent_exceptions() failed before %s call",
                    item.nodeid,
                    exc_info=True,
                )
    yield


def _try_get_automation_client(item: pytest.Item) -> Any | None:
    """fixture 解決済みなら ``automation_client`` を返す。なければ None。

    ``pytest_runtest_call`` 時点では funcargs が populated なので
    ``item.funcargs.get("automation_client")`` で取れる。fixture を使わない test
    では None を返す。
    """
    if "automation_client" not in item.fixturenames:
        return None
    try:
        return item.funcargs.get("automation_client")
    except Exception:  # noqa: BLE001
        return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Iterator[None]:  # type: ignore[misc]
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return

    # ADR-0002 §17 / Roadmap "Qt 未捕捉例外 → fail 連動": call が pass / fail 関わらず
    # Worker に積まれた未捕捉例外を取り、1 件でもあれば fail に格上げする。
    promoted_to_failed = False
    if _resolve_fail_on_uncaught_exception(item.config) and not _is_dev_mode():
        client = _try_get_automation_client(item)
        if client is not None:
            try:
                excs = client.get_recent_exceptions()
            except Exception:  # noqa: BLE001 - 取得失敗は black-box 失敗に格上げしない
                excs = []
                logger.debug(
                    "get_recent_exceptions() failed for %s makereport",
                    item.nodeid,
                    exc_info=True,
                )
            if excs:
                msg = _format_uncaught_exceptions(excs)
                if report.passed:
                    report.outcome = "failed"
                    report.longrepr = msg
                    promoted_to_failed = True
                else:
                    # 既に failed → section に追加（pytest 標準の "Captured stderr" 並び）
                    report.sections.append(("Worker uncaught exceptions", msg))

    if report.failed:
        _dump_diagnostic_bundle(item, report=report, call=call)
        # ADR-0002 §17.5: 失敗連鎖防止の自動 fresh_qgis を opt-in なら flag を立てる。
        # 次テストの ``pytest_runtest_setup`` で flag を読んで fresh_qgis marker を
        # 動的付与する。
        if _resolve_auto_fresh_after_failure(item.config):
            setattr(item.config, _CONFIG_ATTR_PRIOR_TEST_FAILED, True)
    # promoted_to_failed は将来 telemetry に使う想定（現状は info ログのみ）
    if promoted_to_failed:
        logger.info(
            "Test %s promoted pass→fail by uncaught Qt/Python exception (ADR-0002 §17)",
            item.nodeid,
        )


def pytest_runtest_setup(item: pytest.Item) -> None:
    """ADR-0002 §17.5: 直前テストが call で failed していた場合、現テストへ
    動的に ``fresh_qgis`` marker を付与する（失敗連鎖防止）。

    setup phase は qgis fixture の setup より前に走るので、ここで marker を足せば
    fixture 側の ``request.node.get_closest_marker("fresh_qgis")`` が反応する。
    既に marker が付いていれば no-op（重複付与は問題ないが冗長）。

    flag は読んだら必ずクリアする（連続失敗時に毎回 fresh するが、特定 test を
    永続 fresh にするのは avoiding side-effect）。
    """
    config = item.config
    if not getattr(config, _CONFIG_ATTR_PRIOR_TEST_FAILED, False):
        return
    setattr(config, _CONFIG_ATTR_PRIOR_TEST_FAILED, False)
    if item.get_closest_marker("fresh_qgis") is not None:
        return  # 既に明示 marker あり、二重付与しない
    item.add_marker(pytest.mark.fresh_qgis)
    logger.info(
        "Auto-fresh: prior test failed, marking %s with @pytest.mark.fresh_qgis",
        item.nodeid,
    )
