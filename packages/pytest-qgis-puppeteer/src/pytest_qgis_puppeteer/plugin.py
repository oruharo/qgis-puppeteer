"""pytest-qgis-puppeteer の pytest plugin。

ADR-0002 に従い、pytest session が **Hub subprocess を所有** し、QGIS を
`qgis_bin` で指定された実行ファイル経由で spawn する構成。QGIS 内の
QGIS Puppeteer プラグインが `QPUPPETEER_HUB_PORT` env を見て外部所有者モードに
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
（`qgis_bin` の隣、次に `OSGEO4W_ROOT/bin`）。

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
skip する。書き始めや UI 操作系のデバッグ用途を想定（信頼モードが付かないので
`execute_python` を多用するテストは confirm ゲートで弾かれ、
`ConfirmationRequiredError` で落ちる）。
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
import warnings
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from pytest_qgis_puppeteer import _leftover, _message_check
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
from pytest_qgis_puppeteer.automation_client import (
    E2EAutomationClient,
    QgisUnusedDialogHandlerWarning,
    _unused_message,
)

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
        "others are deselected. Forms: '--env=NAME' (single env), "
        "'--env=A,B' (multiple envs run sequentially), "
        "'--env=all' (all envs). For multi/all forms each env runs in "
        "its own pytest session and the exit codes are aggregated.",
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
        "Capture per-action screenshots into the diagnostic bundle on test failure. "
        "Set 'false' to disable (default: 'true').",
        default="true",
    )
    # ADR-0002 §10.7 / §17.5: 失敗連鎖防止の自動 fresh_qgis。前テストの call フェーズが
    # failed なら、次テスト setup 時に ``fresh_qgis`` marker を動的付与して QGIS を
    # 再起動する。1 件の失敗で QGIS 状態が壊れて以降全滅する事故への保険。
    # ``session`` なら次の 1 本ではなく session の QGIS そのものを起こし直す（#11）。
    # 既定 OFF（QGIS の起動は ~10s のコスト）。
    parser.addini(
        "qgis_auto_fresh_after_failure",
        "After a test failure, 'true' runs the next test on a fresh QGIS "
        "(@pytest.mark.fresh_qgis); 'session' restarts the session QGIS before the "
        "next test that uses `qgis`. Default: 'false'.",
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
        "call phase. Catches Qt slot exceptions that would "
        "otherwise be swallowed. Default: 'true'.",
        default="true",
    )
    # #5 / ロードマップ B5c: QgsMessageLog に流れたメッセージで落とす（opt-in）。
    # 例外を捕まえてログに流すプラグインの失敗は excepthook を通らないので、
    # 上の uncaught exception の仕組みでは拾えない。
    parser.addini(
        "qgis_fail_on_message_level",
        "Fail the test if the Worker logged a QgsMessageLog message at this level "
        "or above during the call phase: info, warning or critical. Includes "
        "message bar messages. Default: '' (off).",
        default="",
    )
    # #28: テストのあとに残った状態（閉じ忘れの窓、編集中のレイヤなど）を見つける。
    # 項目は _leftover.ITEMS から。既定はどれも見ない（opt-in）。
    parser.addini(
        "qgis_leftover_fail",
        "State a test must not leave behind; found after teardown, it errors the "
        "test. Items: dialogs popup editing editing_modified map_tool selection "
        "layer_tree layers active_layer project_dirty message_bar. Default: none.",
        type="args",
        default=[],
    )
    parser.addini(
        "qgis_leftover_warn",
        "Like qgis_leftover_fail, but only warns (QgisLeftoverWarning). Default: none.",
        type="args",
        default=[],
    )
    parser.addini(
        "qgis_message_allowlist",
        "Messages ignored by qgis_fail_on_message_level, one per line: a regex "
        "searched in the message, or '<tag>::<regex>'. Lines starting with # are "
        "comments.",
        type="linelist",
        default=[],
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
# #11: ``qgis_auto_fresh_after_failure = session`` のとき「session の QGIS を起こし
# 直す」flag。次に ``qgis`` を使うテストの setup で読まれ、起こし直したらクリアされる。
_CONFIG_ATTR_RESTART_SESSION_QGIS = "_qgis_puppeteer_restart_session_qgis"
# #15: QGIS の起動の間に出たメッセージ（QGIS の key → メッセージ）と、判定に
# 引っかかったもの（[{"qgis": 説明, "level": ..., "messages": [...]}]）。最後のまとめに出す
_CONFIG_ATTR_STARTUP_MESSAGES = "_qgis_puppeteer_startup_messages"
_CONFIG_ATTR_STARTUP_PROBLEMS = "_qgis_puppeteer_startup_problems"
# #15: そのテストが動く QGIS の key（qgis_startup_messages fixture が読む）
_STARTUP_KEY = pytest.StashKey[str]()
# #28: テストの始めに読んだ状態（テストのあとと比べる）
_LEFTOVER_BASELINE_KEY = pytest.StashKey[dict]()
# #28: call で落ちたか（落ちたテストの残った状態は、エラーを重ねずに警告にする）
_CALL_FAILED_KEY = pytest.StashKey[bool]()
# #28: 状態を読めなかったことを、session で 1 回だけ警告した印
_CONFIG_ATTR_LEFTOVER_UNREADABLE_WARNED = "_qgis_puppeteer_leftover_unreadable_warned"
# ADR-0003 Phase 3: 子側で test outcomes を集計するための buffer
# (counts dict + failed_tests list + start time)。``pytest_sessionfinish`` で
# ``_env_stats.json`` に書き出される。
_CONFIG_ATTR_ENV_STATS = "_qgis_puppeteer_env_stats"
# 複数の env を回した親が、子の JUnit XML をまとめたもの（書き出し先, tree）。
# pytest 自身の junitxml が空のレポートを書いた後に、pytest_unconfigure で上書きする
_CONFIG_ATTR_MERGED_JUNIT = "_qgis_puppeteer_merged_junit"

# 集約レポートの schema_version（``summary.json`` / ``_env_stats.json``）
_SUMMARY_SCHEMA_VERSION = 1

# 子 invocation が「自分は meta-parent から呼ばれた」と分かるよう env 変数で signal する。
# 子側ではこの env を見て更なる recursion を抑止する保険として使う。
_ENV_META_CHILD = "QPUPPETEER_META_CHILD"


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    """``pytest_qgis_ready`` / ``pytest_qgis_before_spawn`` を登録する（#19 / #29）。"""
    from pytest_qgis_puppeteer import hookspecs

    pluginmanager.add_hookspecs(hookspecs)


def pytest_configure(config: pytest.Config) -> None:
    """marker 登録 + environments.toml ロード + active env 解決。

    `environments.toml` 不在は許容（後方互換）。`--env` 指定時に toml 不在なら
    UsageError。
    """
    # 残った状態の項目の綴り違いは、テストを回す前に知らせる（#28）
    _leftover.configured_items(config)
    config.addinivalue_line(
        "markers",
        "qgis_env(*names): assign this test to one or more QGIS environments "
        "declared in environments.toml. Tests are filtered by --env=<name>.",
    )
    config.addinivalue_line(
        "markers",
        "fresh_qgis(args=[...], env={...}): spawn a brand-new QGIS for this test "
        "only, on top of the active env's args/env. The qgis fixture is routed "
        "to the new worker via automation_client.use_instance(...). After the "
        "test, the fresh worker is killed and routing is restored.",
    )
    config.addinivalue_line(
        "markers",
        "qgis_allow_message(pattern=None, *, tag=None, source=None): ignore matching "
        "QgsMessageLog messages for qgis_fail_on_message_level in this test. "
        "pattern is a regex searched in the message; source is 'log' or "
        "'message_bar'.",
    )
    config.addinivalue_line(
        "markers",
        "qgis_leftover_expected(*items, reason=None): this test is known to leave "
        "these qgis_leftover_fail / qgis_leftover_warn items behind; they are "
        "not reported. If none of them is left, QgisLeftoverWarning says so.",
    )
    config.addinivalue_line(
        "markers",
        "qgis_restart_before: restart the session QGIS before this test. "
        "pytest_qgis_ready is then called with kind='restart'. No effect on "
        "fresh_qgis tests, in dev mode, or with an overridden qgis_process.",
    )
    # 綴りを誤った ini で黙って判定が無効になるのを避け、起動時に止める
    try:
        _message_check.parse_fail_level(config.getini("qgis_fail_on_message_level"))
        _message_check.rules_from_ini(config.getini("qgis_message_allowlist"))
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc

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
    # 残った状態の印の綴り違いは、テストを回す前に知らせる（#31）
    for item in items:
        _leftover.expected_items(item)

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

    # --junit-xml があれば、子には env ごとのファイルへ書かせ、最後に親が 1 つにまとめる
    junit_target = _junit_target(config)

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
            if junit_target is not None:
                child_junit = _child_junit_path(config, name)
                with contextlib.suppress(FileNotFoundError):
                    child_junit.unlink()  # 前の回のものを、この回の結果と取り違えない
                # 最後に足した値が、親の引数や ini の addopts の --junit-xml より勝つ
                child_argv = [*child_argv, f"--junit-xml={child_junit}"]

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

    if junit_target is not None:
        entries = [
            _EnvJunitEntry(
                env_name=str(r["env_name"]),
                path=_child_junit_path(config, str(r["env_name"])),
                exitcode=int(r.get("exitcode", 0)),
                not_run_reason=_not_run_reason(r),
            )
            for r in env_results
        ]
        try:
            merged = _merge_env_junit_reports(entries)
        except Exception:  # noqa: BLE001 - JUnit のまとめに失敗しても test 結果は変えない
            logger.warning("Failed to merge the JUnit XML of the envs", exc_info=True)
        else:
            setattr(config, _CONFIG_ATTR_MERGED_JUNIT, (junit_target, merged))
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

    # #15: QGIS の起動の間に判定に引っかかったメッセージがあれば、テストが全部
    # 通っていても session を失敗にする（中身は pytest_terminal_summary が出す）
    if (
        not meta_envs
        and getattr(config, _CONFIG_ATTR_STARTUP_PROBLEMS, None)
        and session.exitstatus == pytest.ExitCode.OK
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

    # Phase 3: child 側 (single-env / meta-child) で _env_stats.json を書き出す
    if not meta_envs:
        try:
            _write_env_stats(config, exitstatus=session.exitstatus)
        except Exception:  # noqa: BLE001 - stats dump は best-effort
            logger.warning("Failed to write _env_stats.json", exc_info=True)
        return

    aggregated = _run_envs_and_aggregate(config, meta_envs)
    session.exitstatus = aggregated


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """複数の env を回した親で、まとめた JUnit XML を書き出す。

    pytest 自身の junitxml は ``pytest_sessionfinish`` で、親の（テストが 0 件の）
    レポートを同じパスに書く。それより後の ``pytest_unconfigure`` で上書きする。
    """
    merged = getattr(config, _CONFIG_ATTR_MERGED_JUNIT, None)
    if merged is None:
        return
    path, tree = merged
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    except OSError:
        logger.warning("Failed to write the merged JUnit XML to %s", path, exc_info=True)
        return
    logger.info("Wrote the JUnit XML of %d env(s) to %s", len(tree.getroot()), path)


def _get_active_env(config: pytest.Config) -> EnvironmentSpec | None:
    """plugin 内部用 accessor。fixture から active env 設定を取得。"""
    return getattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)


# ==============================================================
# ADR-0003 Phase 3: per-env stats / summary.json / env ごとの JUnit XML をまとめる
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


@dataclass(frozen=True)
class _EnvJunitEntry:
    """まとめる JUnit XML の 1 env 分。"""

    env_name: str
    # 子が書いたはずのファイル（無いこともある）
    path: Path
    exitcode: int
    # 回さなかった env の理由（``--maxfail`` の累計、前の env の hard error）。回した env は None
    not_run_reason: str | None = None


def _junit_target(config: pytest.Config) -> Path | None:
    """``--junit-xml`` の書き出し先（pytest の junitxml と同じ解決）。無ければ None。"""
    xmlpath = getattr(config.option, "xmlpath", None)
    if not xmlpath:
        return None
    expanded = os.path.expanduser(os.path.expandvars(str(xmlpath)))
    return Path(os.path.normpath(os.path.abspath(expanded)))


def _child_junit_path(config: pytest.Config, env_name: str) -> Path:
    """複数の env を回すとき、子に書かせる JUnit XML（``outputs/<env>/junit.xml``）。"""
    return Path(os.path.abspath(_env_output_dir(config, env_name) / "junit.xml"))


def _not_run_reason(env_stats: dict[str, Any]) -> str | None:
    reason = env_stats.get("skipped_reason")
    if isinstance(reason, str) and reason.startswith("not_run_"):
        return reason
    return None


def _merge_env_junit_reports(entries: Sequence[_EnvJunitEntry]) -> ET.ElementTree:
    """env ごとの JUnit XML を、env ごとの ``<testsuite>`` を並べた 1 つにまとめる。

    - ``<testsuite name>`` は env 名、``<testcase classname>`` は ``<env>::<元の classname>``
      （テスト名だけを並べる集計でも、どの env の結果か分かるように）
    - 回さなかった env は、skipped のテストを 1 件持つ testsuite
    - 回したのにファイルが無い・読めない env は、error のテストを 1 件持つ testsuite
      （黙って抜けないように）
    - ``<testsuites>`` には全体の件数と時間を書く
    """
    root = ET.Element("testsuites", name="pytest tests")
    for entry in entries:
        for suite in _env_testsuites(entry):
            root.append(suite)
    totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    total_time = 0.0
    for suite in root:
        for key in totals:
            totals[key] += int(suite.get(key) or 0)
        total_time += float(suite.get("time") or 0)
    for key, value in totals.items():
        root.set(key, str(value))
    root.set("time", f"{total_time:.3f}")
    return ET.ElementTree(root)


def _env_testsuites(entry: _EnvJunitEntry) -> list[ET.Element]:
    name = entry.env_name
    if entry.not_run_reason is not None:
        return [
            _placeholder_suite(
                name,
                "skipped",
                f"env {name!r} was not run ({entry.not_run_reason})",
            )
        ]
    try:
        child_root = ET.parse(entry.path).getroot()
    except (OSError, ET.ParseError) as e:
        return [
            _placeholder_suite(
                name,
                "error",
                f"env {name!r} exited with code {entry.exitcode} without a readable "
                f"JUnit XML at {entry.path} ({type(e).__name__})",
            )
        ]
    suites = [child_root] if child_root.tag == "testsuite" else child_root.findall("testsuite")
    prefix = f"{name}::"
    for suite in suites:
        suite.set("name", name)
        for testcase in suite.iter("testcase"):
            cls = testcase.get("classname") or ""
            testcase.set("classname", f"{prefix}{cls}" if cls else name)
    return suites


def _placeholder_suite(env_name: str, kind: str, message: str) -> ET.Element:
    """テストの結果が無い env の testsuite（``kind`` は ``skipped`` か ``error``）。"""
    suite = ET.Element(
        "testsuite",
        name=env_name,
        errors="1" if kind == "error" else "0",
        failures="0",
        skipped="1" if kind == "skipped" else "0",
        tests="1",
        time="0.000",
    )
    testcase = ET.SubElement(suite, "testcase", classname=env_name, name="env")
    ET.SubElement(testcase, kind, message=message)
    return suite


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


def _shutdown_registered_workers(client: E2EAutomationClient) -> None:
    """Hub に register されている全 Worker を順に閉じる。

    pid を外から止めるのではなく、まず QGIS 自身に閉じてもらう
    （`spawn.stop_registered_worker`。消えなければその pid だけを強制終了）。
    """
    from pytest_qgis_puppeteer.spawn import stop_registered_worker

    try:
        instances = client.list_instances()
    except Exception:  # noqa: BLE001 - best-effort on teardown
        logger.warning("Could not list workers for teardown", exc_info=True)
        return
    for inst in instances:
        stop_registered_worker(
            client,
            instance_id=inst.instance_id,
            pid=inst.pid,
            graceful_timeout_s=DEFAULT_QGIS_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        )


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


def _resolve_hub_qgis_python(config: pytest.Config, active_env: EnvironmentSpec | None) -> str:
    """Hub を起こす QGIS 付属の Python launcher を決める。

    設定（CLI > 環境変数 > environments.toml > ini）が無ければ探す。``qgis_bin`` を
    設定していればその隣（``qgis-ltr-bin.exe`` なら ``python-qgis-ltr.bat``、
    ``qgis-bin.exe`` なら ``python-qgis.bat``）、次に ``OSGEO4W_ROOT/bin``。
    """
    qgis_python = resolve_qgis_setting(
        cli_value=config.getoption("qgis_python", default=None),
        env_value=os.environ.get(ENV_QGIS_PYTHON),
        env_spec_value=active_env.qgis_python if active_env is not None else None,
        ini_value=config.getini("qgis_python"),
    )
    if qgis_python:
        if not Path(qgis_python).is_file():
            raise pytest.UsageError(f"qgis_python does not exist: {qgis_python}")
        return qgis_python

    from qgis_puppeteer import find_qgis_python_launcher

    qgis_bin = resolve_qgis_setting(
        cli_value=config.getoption("qgis_bin", default=None),
        env_value=os.environ.get(ENV_QGIS_BIN),
        env_spec_value=active_env.qgis_bin if active_env is not None else None,
        ini_value=config.getini("qgis_bin"),
    )
    discovered = find_qgis_python_launcher(qgis_bin=qgis_bin or None)
    if discovered is None:
        where = f"next to qgis_bin ({qgis_bin}) or " if qgis_bin else ""
        raise pytest.UsageError(
            "qgis_python is not configured, and no QGIS Python launcher "
            f"(python-qgis-ltr.bat / python-qgis.bat) was found {where}under "
            f"$OSGEO4W_ROOT/bin. Set --qgis-python, ${ENV_QGIS_PYTHON} or "
            "[tool.pytest.ini_options] qgis_python, or set qgis_bin to the QGIS "
            "executable so the launcher next to it is used."
        )
    logger.info("Auto-discovered qgis_python: %s", discovered)
    return str(discovered)


@pytest.fixture(scope="session")
def hub_process(
    pytestconfig: pytest.Config, hub_port: int
) -> Iterator[subprocess.Popen[bytes] | None]:
    """Hub を QGIS 付属 Python launcher 経由で spawn し、listen 確認後に yield。

    venv の python では PyQt5 が解決できないので、QGIS 付属の Python launcher
    （例: `python-qgis-ltr.bat`）で起こす。`qgis_python` ini /
    `QPUPPETEER_QGIS_PYTHON` env / `--qgis-python` CLI で指定するか、
    `qgis_bin` の隣から見つける（`_resolve_hub_qgis_python`）。

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

    qgis_python = _resolve_hub_qgis_python(pytestconfig, _get_active_env(pytestconfig))
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
        # posix: qgis_python がシェルスクリプトでも、Hub ごとプロセスグループで止められるように
        start_new_session=sys.platform != "win32",
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
        _stop_hub(proc, hub_port=hub_port)
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
        _stop_hub(proc, hub_port=hub_port)
        for t in drain_threads:
            t.join(timeout=2.0)


def _stop_hub(proc: subprocess.Popen[bytes], *, hub_port: int) -> None:
    """pytest が起こした Hub を止める（#14）。

    Hub は ``qgis_python``（Windows の既定は ``python-qgis-ltr.bat``）で起こすので、
    ``proc`` は ``cmd.exe`` で、Hub の python はその子になる。``proc.terminate()``
    では ``cmd.exe`` しか止まらず、Hub が残ってポートを握り続けていた。

    - Windows: ``taskkill /T /F`` でプロセスの木ごと止める（Hub は終わり際の
      後片付けを持たないので、強制終了で困らない）
    - posix: Hub は自分のプロセスグループで起こしてあるので、グループごと止める
    - 最後に Hub のポートが閉じたかを確かめる。閉じなければ、Windows では
      そのポートで待ち受けているプロセスを止め、だめなら警告する
    """
    if proc.poll() is None:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        else:
            import signal as _signal

            try:
                os.killpg(proc.pid, _signal.SIGTERM)
            except OSError:
                logger.debug("killpg(SIGTERM) raised (pid=%d)", proc.pid, exc_info=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
    if _wait_port_closed(hub_port, timeout_s=5.0):
        logger.info("Hub stopped (returncode=%s, port %d closed)", proc.returncode, hub_port)
        return
    # 起動用のプロセスが先に終わっていて、Hub だけが残っている
    leftover = _listening_pid(hub_port) if sys.platform == "win32" else None
    if leftover is not None:
        logger.warning("Hub still listening on port %d (pid=%d); killing it", hub_port, leftover)
        subprocess.run(
            ["taskkill", "/PID", str(leftover), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        if _wait_port_closed(hub_port, timeout_s=5.0):
            return
    logger.warning(
        "Hub port %d is still open after stopping the Hub (returncode=%s)",
        hub_port,
        proc.returncode,
    )


def _wait_port_closed(port: int, *, timeout_s: float) -> bool:
    """127.0.0.1:port で誰も待ち受けなくなるまで待つ。閉じれば True。"""
    deadline = time.monotonic() + timeout_s
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
            except OSError:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _listening_pid(port: int) -> int | None:
    """Windows: 127.0.0.1:port で LISTENING のプロセスの pid（``netstat -ano``）。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_listening_pid(out, port)


def _parse_listening_pid(netstat_output: str, port: int) -> int | None:
    """``netstat -ano -p TCP`` の出力から、port で待ち受けているプロセスの pid を探す。

    状態の語（``LISTENING``）は Windows の言語で訳されることがあるので見ない。
    待ち受けの行は相手側のアドレスが ``0.0.0.0:0`` になる。
    """
    for line in netstat_output.splitlines():
        parts = line.split()
        # TCP  127.0.0.1:60150  0.0.0.0:0  LISTENING  17700
        if (
            len(parts) >= 5
            and parts[0] == "TCP"
            and parts[1].endswith(f":{port}")
            and parts[2] in ("0.0.0.0:0", "[::]:0")
            and parts[-1].isdigit()
        ):
            return int(parts[-1])
    return None


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
    """E2E test の Worker call timeline を蓄積する recorder。

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
) -> Iterator[SessionQgis | None]:
    """session の QGIS を spawn し、`SessionQgis` を yield する。

    QGIS 内の QGIS Puppeteer プラグインが `QPUPPETEER_HUB_PORT` env を見て
    外部所有者モードに入り、自前で Hub を spawn しない。teardown では Hub に
    register された Worker を `_shutdown_registered_workers` で閉じる。

    ``qgis_auto_fresh_after_failure = session`` のときは、失敗の後にこの
    `SessionQgis` を `restart()` する（#11）。conftest から直接呼んでもよい。

    dev モードでは spawn も teardown もせず、None を yield する。
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

    def spawn_session() -> contextlib.AbstractContextManager[Any]:
        # 起こすたびに利用側の準備（新しいプロファイルなど）を聞く（#29）
        kind = "restart" if session.start_count > 0 else "session"
        kwargs, env = _with_spawn_extras(pytestconfig, kind, spawn_kwargs, user_env)
        return spawn_qgis(
            hub_port=hub_port,
            automation_client=automation_client,
            env=env,
            register_timeout_s=register_timeout_s,
            graceful_shutdown_timeout_s=DEFAULT_QGIS_GRACEFUL_SHUTDOWN_TIMEOUT_S,
            label="qgis_process",
            **kwargs,
        )

    session = SessionQgis(spawn_session)
    session.start()
    try:
        yield session
    finally:
        # session 中に fresh_qgis 等で別途 register された Worker が残っていれば
        # ここで一括掃除（spawn_qgis 自身は自分の Worker しか閉じないため）。
        _shutdown_registered_workers(automation_client)
        session.stop()


def _with_spawn_extras(
    config: pytest.Config,
    kind: str,
    spawn_kwargs: dict[str, Any],
    env: dict[str, str] | None,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """``pytest_qgis_before_spawn`` の足す引数と環境変数を、起動の設定に足す（#29）。

    ``args`` は ``qgis_bin`` の ``args`` の後ろに、``command`` があればその後ろに
    つなぐ。元の ``spawn_kwargs`` / ``env`` は変えない（起こし直しのたびに聞くため）。
    """
    extra_args, extra_env = _before_spawn_extras(config, kind)
    kwargs = dict(spawn_kwargs)
    if extra_args:
        key = "command" if kwargs.get("command") is not None else "args"
        kwargs[key] = [*(kwargs.get(key) or []), *extra_args]
    if extra_env:
        # 明示した設定（環境の env、fresh_qgis の印の env）が、フックより勝つ
        overridden = sorted(k for k in extra_env if env and k in env)
        if overridden:
            logger.info(
                "pytest_qgis_before_spawn (%s): %s kept from the environment / marker",
                kind,
                overridden,
            )
        env = {**extra_env, **(env or {})}
    return kwargs, env


def _before_spawn_extras(config: pytest.Config, kind: str) -> tuple[list[str], dict[str, str]]:
    """``pytest_qgis_before_spawn`` を呼び、足す引数と環境変数を集める（#29）。"""
    hook = getattr(config, "hook", None)
    if hook is None or not hasattr(hook, "pytest_qgis_before_spawn"):
        return [], {}
    args: list[str] = []
    env: dict[str, str] = {}
    for result in hook.pytest_qgis_before_spawn(config=config, kind=kind):
        if result is None:
            continue
        if not isinstance(result, dict) or set(result) - {"args", "env"}:
            raise pytest.UsageError(
                "pytest_qgis_before_spawn must return None or a dict with 'args' and/or "
                f"'env', not {result!r}"
            )
        more_args = result.get("args") or []
        more_env = result.get("env") or {}
        if not isinstance(more_args, (list, tuple)) or not all(
            isinstance(a, str) for a in more_args
        ):
            raise pytest.UsageError(
                f"pytest_qgis_before_spawn: 'args' must be a list of str, not {more_args!r}"
            )
        if not isinstance(more_env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in more_env.items()
        ):
            raise pytest.UsageError(
                f"pytest_qgis_before_spawn: 'env' must map str to str, not {more_env!r}"
            )
        for name, value in more_env.items():
            if name in env and env[name] != value:
                raise pytest.UsageError(
                    f"pytest_qgis_before_spawn: two hooks set {name} to different values "
                    f"({env[name]!r} and {value!r})"
                )
            env[name] = value
        args.extend(more_args)
    if args or env:
        # 環境変数は値を出さない（鍵などを入れることがある）
        logger.info(
            "pytest_qgis_before_spawn (%s): extra args %s, extra env %s",
            kind,
            args,
            sorted(env),
        )
    return args, env


class SessionQgis:
    """session の QGIS（`qgis_process` fixture の値）。

    `spawn_qgis` を ``ExitStack`` で持つので、session の途中で止めて起こし
    直せる。失敗したテストが残したもの（閉じ忘れのダイアログなど）で後続が
    落ち続けるのを止めるため（#11）。

    Attributes:
        worker: 今の session の QGIS。止めている間は None。
        startup_messages: 今の QGIS の起動の間に出た QgsMessageLog のメッセージ
            （#15）。最初に ``qgis`` fixture を使うテストの準備で入る。それまでは None。
    """

    def __init__(self, spawn: Callable[[], contextlib.AbstractContextManager[Any]]) -> None:
        self._spawn = spawn
        self._stack: contextlib.ExitStack | None = None
        self.worker: Any = None
        self.startup_messages: list[dict[str, Any]] | None = None
        # 起こした回数。2 回目以降は起こし直し（pytest_qgis_ready の kind="restart"）
        self.start_count = 0

    def start(self) -> None:
        """QGIS を起こし、コマンドを処理できるまで待つ。"""
        if self._stack is not None:
            return
        stack = contextlib.ExitStack()
        self.worker = stack.enter_context(self._spawn())
        self.startup_messages = None
        self.start_count += 1
        self._stack = stack

    def stop(self) -> None:
        """QGIS を閉じる（`spawn.stop_registered_worker`）。止まっていれば何もしない。"""
        stack, self._stack = self._stack, None
        self.worker = None
        if stack is not None:
            stack.close()

    def restart(self) -> None:
        """QGIS を閉じて、同じ設定で起こし直す。

        起こし直した QGIS は instance_id が変わる。session の
        ``automation_client`` は既定の送り先を固定していないので、そのまま
        新しい QGIS に届く。``hub_ready`` の値（最初の instance_id）は古くなる。
        """
        started = time.monotonic()
        self.stop()
        self.start()
        logger.info(
            "Session QGIS restarted in %.1fs (instance_id=%s)",
            time.monotonic() - started,
            getattr(self.worker, "instance_id", None),
        )


@pytest.fixture(scope="session")
def hub_ready(
    pytestconfig: pytest.Config,
    automation_client: E2EAutomationClient,
    qgis_process: SessionQgis | None,
) -> str:
    """Worker が登録され、**コマンドを処理できる**ようになるまで待つ。

    register 直後の QGIS はプロジェクト読み込みで GUI スレッドが塞がっている
    ことがあるので、一覧に載るだけでは足りない（`wait_for_ready` 参照）。
    重いプロジェクトでは ini の ``qgis_startup_timeout`` を伸ばすこと。
    """
    del qgis_process
    timeout_s = float(
        pytestconfig.getini("qgis_startup_timeout") or DEFAULT_WORKER_REGISTER_TIMEOUT_S
    )
    instance_id = automation_client.wait_for_ready(timeout_s=timeout_s)
    _check_plugin_fingerprint(automation_client, instance_id)
    return instance_id


class QgisPluginMismatchWarning(UserWarning):
    """QGIS のプラグインと pytest 側の qgis-puppeteer の中身が食い違っている（#17）。

    片側にしか入っていない修正は効かない。``-W error::pytest_qgis_puppeteer.
    QgisPluginMismatchWarning`` で失敗にもできる。
    """


def _check_plugin_fingerprint(client: E2EAutomationClient, instance_id: str) -> None:
    """session の QGIS のプラグインと、pytest 側のライブラリの指紋を比べる（#17）。

    session の最初に 1 回。食い違っていれば `QgisPluginMismatchWarning` を出す
    （失敗にはしない）。旧プラグインは指紋を送らないので「分からない」と知らせる。
    """
    from qgis_puppeteer._fingerprint import source_fingerprint

    try:
        info = next((i for i in client.list_instances() if i.instance_id == instance_id), None)
    except Exception:  # noqa: BLE001 - 確かめられなくてもテストは進める
        logger.debug("Could not read the plugin fingerprint", exc_info=True)
        return
    if info is None:
        return
    mine = source_fingerprint()
    theirs = getattr(info, "source_fingerprint", None)
    if theirs == mine:
        logger.info("QGIS Puppeteer plugin matches pytest's qgis-puppeteer (%s)", mine)
        return
    import qgis_puppeteer

    here = Path(qgis_puppeteer.__file__).resolve().parent
    if theirs is None:
        message = (
            "The QGIS Puppeteer plugin in QGIS is older than the version check, so it "
            f"cannot be compared with pytest's qgis-puppeteer ({mine}, {here}). "
            "Install the plugin from the same commit."
        )
    else:
        message = (
            f"The QGIS Puppeteer plugin in QGIS ({theirs}) differs from pytest's "
            f"qgis-puppeteer ({mine}, {here}). Fixes on one side only do not take "
            "effect; install the plugin and pin pytest-qgis-puppeteer to the same commit."
        )
    logger.warning(message)
    warnings.warn(message, QgisPluginMismatchWarning, stacklevel=2)


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
    qgis_process: SessionQgis | None,
) -> Iterator[E2EAutomationClient]:
    """テスト用 AutomationClient を供給する function-scope fixture。

    通常は session の ``automation_client`` をそのまま返す。``@pytest.mark.fresh_qgis``
    が付いた test では、その test だけ別 QGIS を spawn してそちらに routing する
    （ADR-0002 §10.4 / ADR-0003 §fresh_qgis 合成ルール）。

    ``qgis_auto_fresh_after_failure = session`` で前のテストが失敗していれば、
    先に session の QGIS を起こし直す（#11）。
    """
    del hub_ready  # session worker が register 済みであることを要求するだけ
    marker = request.node.get_closest_marker("fresh_qgis")
    reason = None
    if getattr(pytestconfig, _CONFIG_ATTR_RESTART_SESSION_QGIS, False):
        reason = "prior test failed"
    elif _wants_session_restart(pytestconfig, request.node, fresh=marker is not None):
        reason = "marked qgis_restart_before"
        setattr(pytestconfig, _CONFIG_ATTR_RESTART_SESSION_QGIS, True)
    if reason is not None:
        # 起こし直しはこのテストの操作ではないので、操作記録に入れない
        with automation_client.unrecorded():
            _restart_session_qgis(pytestconfig, qgis_process, request.node.nodeid, reason=reason)
    if marker is None:
        worker = getattr(qgis_process, "worker", None)
        instance_id = getattr(worker, "instance_id", None)
        key = instance_id or "session"
        messages = _capture_startup_messages(
            pytestconfig,
            automation_client,
            key=key,
            description=f"session QGIS ({key})",
            instance=None,
            ready_instance_id=instance_id,
            kind="restart" if getattr(qgis_process, "start_count", 1) > 1 else "session",
        )
        if isinstance(qgis_process, SessionQgis) and qgis_process.startup_messages is None:
            qgis_process.startup_messages = messages
        request.node.stash[_STARTUP_KEY] = key
        # 残った状態の検査の「始め」（#28）。テストの fixture は qgis に依存するので、
        # その準備より前に読める。fixture が作って片付けたものを「消えた」と出さない
        _take_leftover_baseline(request.node, automation_client)
        yield automation_client
        # テストの fixture の後片付け（qgis に依存するので先に済む）のあとに外す（#40）
        _release_dialog_handlers(request.node, automation_client)
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
    label = _fresh_label(request.node)
    launch: dict[str, Any] = (
        {"command": command} if command is not None else {"qgis_bin": qgis_bin, "args": args}
    )
    launch, env_vars = _with_spawn_extras(pytestconfig, "fresh", launch, env_vars)
    spawn_kwargs: dict[str, Any] = {
        "hub_port": hub_port,
        "automation_client": automation_client,
        "env": env_vars,
        "register_timeout_s": register_timeout_s,
        "label": label,
        **launch,
    }

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
        # ラベルはテスト名を縮めたものになりうるので、どのテストの QGIS かを残す（#13）
        logger.info(
            "fresh_qgis: %s runs on instance_id=%s (label=%s, pid=%d)",
            request.node.nodeid,
            worker.instance_id,
            worker.label,
            worker.pid,
        )
        automation_client.use_instance(worker.instance_id)
        _capture_startup_messages(
            pytestconfig,
            automation_client,
            key=worker.instance_id,
            description=f"fresh QGIS for {request.node.nodeid} (label={worker.label})",
            instance=worker.instance_id,
            ready_instance_id=worker.instance_id,
            kind="fresh",
        )
        request.node.stash[_STARTUP_KEY] = worker.instance_id
        try:
            yield automation_client
        finally:
            # fresh の QGIS が止まる前に外す（#40）
            _release_dialog_handlers(request.node, automation_client)
            automation_client.use_instance(saved_default)
            active_fresh.pop(request.node.nodeid, None)


def _release_dialog_handlers(item: pytest.Item, client: E2EAutomationClient) -> None:
    """テスト中に ``register_dialog_handler`` で登録した handler を外す（#40）。

    次のテストに持ち越さない。使われずに残っていた ``once`` の handler は
    :class:`QgisUnusedDialogHandlerWarning` で知らせる（落ちたテストは途中で止まった
    かもしれないので、知らせない）。
    """
    unused = client._release_dialog_handlers()
    if not unused or item.stash.get(_CALL_FAILED_KEY, False):
        return
    for entry in unused:
        warnings.warn(
            QgisUnusedDialogHandlerWarning(f"{item.nodeid}: {_unused_message(entry, 'the test')}"),
            stacklevel=1,
        )


def _capture_startup_messages(
    config: pytest.Config,
    client: E2EAutomationClient,
    *,
    key: str,
    description: str,
    instance: str | None,
    ready_instance_id: str | None = None,
    kind: str = "session",
) -> list[dict[str, Any]]:
    """その QGIS の起動の間に出たメッセージを、QGIS ごとに 1 回だけ取っておく（#15）。

    取る前に ``pytest_qgis_ready`` を呼び、ホストの準備（ログインを待つ、など）を
    済ませてもらう（#19）。フックが例外を出したら取らずにそのまま出す（次に
    その QGIS を使うテストでもう一度呼ぶ）。

    テスト本体の直前に判定のため記録を空にするので、起動からそこまでの分は
    ここで取らないと残らない。起動が終わった時点ではなく、その QGIS を初めて
    使うテストの準備で取るのは、起動の後もプロジェクトの読み込みが続くことが
    あるため。``qgis_fail_on_message_level`` が有効なら、同じレベルと ini の
    許可リストで判定し、引っかかったものを最後のまとめ用に積む。
    """
    store: dict[str, list[dict[str, Any]]] | None = getattr(
        config, _CONFIG_ATTR_STARTUP_MESSAGES, None
    )
    if store is None:
        store = {}
        setattr(config, _CONFIG_ATTR_STARTUP_MESSAGES, store)
    if key in store:
        return store[key]
    if _is_dev_mode():
        # 既に動いている QGIS の「起動の間」はもう過ぎている
        store[key] = []
        return store[key]
    with client.unrecorded():
        hook = getattr(config, "hook", None)
        if hook is not None and hasattr(hook, "pytest_qgis_ready"):
            hook.pytest_qgis_ready(
                config=config, client=client, instance_id=ready_instance_id, kind=kind
            )
        try:
            messages = client.get_recent_messages(instance=instance)
        except Exception as exc:  # noqa: BLE001 - 取れなくてもテストは進める
            logger.warning(
                "Could not read the startup messages of the %s: %s",
                description,
                exc,
                exc_info=True,
            )
            messages = []
    store[key] = messages
    level = _resolve_fail_on_message_level(config)
    if level is not None:
        bad = _message_check.unexpected_messages(messages, config, min_level=level)
        if bad:
            problems: list[dict[str, Any]] = getattr(config, _CONFIG_ATTR_STARTUP_PROBLEMS, [])
            problems.append({"qgis": description, "level": level, "messages": bad})
            setattr(config, _CONFIG_ATTR_STARTUP_PROBLEMS, problems)
            logger.warning(
                "%d message(s) at level %s or above while the %s started; "
                "the session will fail (see the summary)",
                len(bad),
                level,
                description,
            )
    return messages


@pytest.fixture
def qgis_startup_messages(
    request: pytest.FixtureRequest, qgis: E2EAutomationClient
) -> list[dict[str, Any]]:
    """そのテストが動く QGIS の、起動の間に出た QgsMessageLog のメッセージ。

    起動から、その QGIS を初めて使うテストの準備までに出たもの（古い順、
    ``get_recent_messages()`` と同じ形）。起動の間の処理が出したログを確かめる
    ときに使う。dev モードでは空。QGIS Puppeteer プラグインが読み込まれる前の
    メッセージは入らない。
    """
    del qgis
    key = request.node.stash.get(_STARTUP_KEY, None)
    store = getattr(request.config, _CONFIG_ATTR_STARTUP_MESSAGES, None) or {}
    return list(store.get(key, []))


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    """QGIS の起動の間に判定に引っかかったメッセージを、最後のまとめに出す（#15）。"""
    del exitstatus
    problems = getattr(config, _CONFIG_ATTR_STARTUP_PROBLEMS, None)
    if not problems:
        return
    terminalreporter.section("QGIS startup messages", sep="=", red=True, bold=True)
    for problem in problems:
        terminalreporter.line(problem["qgis"] + ":")
        text = _message_check.format_messages(
            problem["messages"], problem["level"], phase="while QGIS started", markers=False
        )
        for line in text.splitlines():
            terminalreporter.line("  " + line)
    terminalreporter.line(
        "No test failed because of these, but the session exit status is set to failed."
    )


def _fresh_label(item: pytest.Item) -> str:
    """fresh_qgis の QGIS に付けるラベル（#13）。

    ふつうは ``fresh-<テスト名>``。パラメータの値が長くて Hub の上限
    （``LABEL_MAX_LENGTH``）を超えるときは、値の代わりにパラメータの番号を使う
    （``fresh-test_open[2]``、パラメータが複数なら ``[2-0]``）。同じテストの別の
    ケースと同じラベルにならず、人にも何番目のケースか読める。関数名だけで
    上限を超えるときは切る。テストの完全な名前は、起動時のログと診断バンドルの
    ``fresh_qgis.json`` に残る。
    """
    from qgis_puppeteer.hub_state import LABEL_MAX_LENGTH

    label = f"fresh-{item.name}"
    if len(label) <= LABEL_MAX_LENGTH:
        return label
    base = getattr(item, "originalname", None) or item.name
    callspec = getattr(item, "callspec", None)
    if callspec is not None:
        numbers = "-".join(str(i) for i in callspec.indices.values())
        label = f"fresh-{base}[{numbers}]"
    else:
        label = f"fresh-{base}"
    return label[:LABEL_MAX_LENGTH]


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


def _wants_session_restart(config: pytest.Config, item: pytest.Item, *, fresh: bool) -> bool:
    """``@pytest.mark.qgis_restart_before`` で、このテストの前に起こし直すか（#19）。

    ``fresh_qgis`` のテストは session の QGIS を使わないので起こし直さない。
    """
    del config
    return not fresh and item.get_closest_marker("qgis_restart_before") is not None


def _restart_session_qgis(
    config: pytest.Config, session: Any, nodeid: str, *, reason: str = "prior test failed"
) -> None:
    """失敗の後、session の QGIS を起こし直す（#11）。

    起こし直せなければ flag を残す（次に ``qgis`` を使うテストでもう一度試す）。
    ``session`` が `SessionQgis` でない（dev モード、または ``qgis_process`` を
    override している）なら起こし直せないので、警告して flag を捨てる。
    """
    if not isinstance(session, SessionQgis):
        setattr(config, _CONFIG_ATTR_RESTART_SESSION_QGIS, False)
        logger.warning(
            "qgis_auto_fresh_after_failure = session has no effect: the session QGIS "
            "is not one this plugin started (dev mode, %s=1, or an overridden "
            "qgis_process fixture), so it is not restarted",
            ENV_DEV_MODE,
        )
        return
    logger.info("Restarting the session QGIS before %s (%s)", nodeid, reason)
    session.restart()
    setattr(config, _CONFIG_ATTR_RESTART_SESSION_QGIS, False)


def _resolve_auto_fresh_after_failure(config: pytest.Config) -> str | None:
    """``qgis_auto_fresh_after_failure`` ini を解釈する。

    - ``"session"`` → ``"session"``：session の QGIS を起こし直す（#11）
    - truthy（"true"/"yes"/"1"/"on"）→ ``"test"``：次の 1 本を fresh_qgis にする
    - それ以外（既定の "false" を含む）→ None：何もしない（QGIS の起動は重いので opt-in）
    """
    raw = config.getini("qgis_auto_fresh_after_failure")
    if raw is None or raw == "":
        return None
    value = str(raw).strip().lower()
    if value == "session":
        return "session"
    if value in ("1", "true", "yes", "on"):
        return "test"
    return None


def _resolve_fail_on_uncaught_exception(config: pytest.Config) -> bool:
    """``qgis_fail_on_uncaught_exception`` ini を bool で解釈する（既定 ON）。

    silent failure 根絶のための基本機能なので opt-out 方式（既定 True）。truthy 値は
    "true"/"yes"/"1"/"on"。未指定 / 空は既定 True。
    """
    raw = config.getini("qgis_fail_on_uncaught_exception")
    if raw is None or raw == "":
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _resolve_fail_on_message_level(config: pytest.Config) -> str | None:
    """``qgis_fail_on_message_level`` を解釈する（既定 None = 無効）。"""
    return _message_check.parse_fail_level(config.getini("qgis_fail_on_message_level"))


def _unexpected_messages(item: pytest.Item, client: Any, level: str) -> list[dict[str, Any]]:
    """call 中に記録された ``level`` 以上のメッセージから、許可リスト分を除く。"""
    messages = client.get_recent_messages(min_level=level)
    rules = _message_check.rules_from_ini(
        item.config.getini("qgis_message_allowlist")
    ) + _message_check.rules_from_markers(item)
    return _message_check.unexpected(messages, rules)


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

    - ``screenshot.png``: マップキャンバスの画像（``qgis_screenshot``）。
      メインウィンドウやダイアログは写らない
    - ``snapshot_ui.json``: ``qgis_snapshot_ui`` の出力
    - ``list_instances.json``: Hub に register 中の Worker 一覧
    - ``meta.json``: nodeid / env / qgis_bin / exception / instances 等
    - ``traceback.txt``: pytest report.longrepr のテキスト形
    - ``hub_stdout.log`` / ``hub_stderr.log``: Hub プロセスの直近出力
    - ``qgis_messages.jsonl``: #5。Worker が記録した QgsMessageLog のメッセージ
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

    # qgis_messages.jsonl（#5 / ロードマップ B5c）。判定を有効にしていなくても出す。
    # 判定が有効なら call 直前に消去しているので call 中のもの、無効なら QGIS の
    # 起動（プラグインの読み込み）以降の直近の分（リングバッファ）。
    try:
        messages = client.get_recent_messages()
        (bundle_dir / "qgis_messages.jsonl").write_text(
            "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.warning("get_recent_messages() failed during diagnostic dump", exc_info=True)

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
                # どの QGIS で動いたか（ラベルはテスト名を縮めたものになりうる、#13）
                (bundle_dir / "fresh_qgis.json").write_text(
                    json.dumps(
                        {
                            "nodeid": item.nodeid,
                            "instance_id": worker.instance_id,
                            "label": worker.label,
                            "pid": worker.pid,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001
                logger.warning("fresh_qgis.json failed during diagnostic dump", exc_info=True)
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
    if not _is_dev_mode():
        clear_exceptions = _resolve_fail_on_uncaught_exception(item.config)
        clear_messages = _resolve_fail_on_message_level(item.config) is not None
        client = _try_get_automation_client(item) if clear_exceptions or clear_messages else None
        if client is not None:
            if clear_exceptions:
                try:
                    client.clear_recent_exceptions()
                except Exception:  # noqa: BLE001 - 通信失敗で test を落とすのは過剰
                    logger.debug(
                        "clear_recent_exceptions() failed before %s call",
                        item.nodeid,
                        exc_info=True,
                    )
            if clear_messages:
                try:
                    client.clear_recent_messages()
                except Exception:  # noqa: BLE001
                    # 頼まれて有効にした判定なので、黙らない。消せていないと前の
                    # テストのメッセージで落ちうる
                    logger.warning(
                        "clear_recent_messages() failed before %s call; messages from "
                        "earlier tests may be counted",
                        item.nodeid,
                        exc_info=True,
                    )
        stash = getattr(item, "stash", None)
        if stash is not None and _LEFTOVER_BASELINE_KEY not in stash:
            # qgis fixture を使わず automation_client だけを使うテストは、ここで読む
            _take_leftover_baseline(item, _try_get_automation_client(item))
    yield


def _leftover_items(item: pytest.Item) -> tuple[list[str], list[str]] | None:
    """残った状態を見るテストなら ``(失敗の項目, 警告の項目)``（#28）。

    dev モード、項目を選んでいない、``fresh_qgis``（その QGIS は捨てる）のテストは
    見ない。
    """
    if _is_dev_mode() or item.get_closest_marker("fresh_qgis") is not None:
        return None
    fail, warn = _leftover.configured_items(item.config)
    if not (fail or warn):
        return None
    return fail, warn


def _read_leftover_state(client: Any, items: list[str]) -> dict[str, Any]:
    # テストの操作ではないので、操作記録に入れない
    with client.unrecorded():
        result = client.call("qgis_get_leftover_state", {"items": items})
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError(f"qgis_get_leftover_state failed: {result!r}")
    return result["state"]


def _take_leftover_baseline(item: pytest.Item, client: Any) -> None:
    """テストの始めの状態を読んでおく（#28）。読めなければ、このテストは見ない。"""
    items = _leftover_items(item)
    if items is None or client is None:
        return
    fail, warn = items
    try:
        item.stash[_LEFTOVER_BASELINE_KEY] = {
            "client": client,
            "state": _read_leftover_state(client, fail + warn),
        }
    except Exception as exc:  # noqa: BLE001 - 古い plugin などでは読めない
        _warn_leftover_unreadable(item, "before", exc)


def _warn_leftover_unreadable(item: pytest.Item, when: str, exc: BaseException) -> None:
    """状態を読めなかったことを知らせる。黙ると、検査が効いていないのに緑に見える。

    毎テスト出すとうるさいので、警告は session で 1 回、あとはログだけ。
    """
    message = f"qgis_leftover: could not read the QGIS state {when} {item.nodeid}: {exc}"
    if getattr(item.config, _CONFIG_ATTR_LEFTOVER_UNREADABLE_WARNED, False):
        logger.warning(message)
        return
    setattr(item.config, _CONFIG_ATTR_LEFTOVER_UNREADABLE_WARNED, True)
    warnings.warn(
        _leftover.QgisLeftoverWarning(
            message + " (leftover checks are not running; later failures are only logged)"
        ),
        stacklevel=1,
    )


@pytest.fixture(autouse=True)
def _qgis_leftover_check(request: pytest.FixtureRequest) -> Iterator[None]:
    """fixture の後片付けが終わったあとに、残った状態を見る（#28）。

    plugin の autouse fixture は最初に準備され、最後に片付けられるので、ここの後半は
    ほかの function の fixture の後片付けのあとに走る。始め（call の直前）に無かった
    のに残ったものを、``qgis_leftover_fail`` の項目ならエラー（teardown）、
    ``qgis_leftover_warn`` の項目なら警告にする。``qgis_leftover_expected`` の印の
    項目は知らせず、印を付けたのに残らなかったら警告にする（#31）。
    """
    yield
    item = request.node
    taken = item.stash.get(_LEFTOVER_BASELINE_KEY, None)
    items = _leftover_items(item)
    if taken is None or items is None:
        return
    client, baseline = taken["client"], taken["state"]
    fail, warn = items
    call_failed = item.stash.get(_CALL_FAILED_KEY, False)
    if call_failed:
        # call で落ちたテストは、残った状態でもう 1 つエラーを重ねない（落ちれば
        # 窓などが残るのはふつう）。知らせるだけにする
        fail, warn = [], fail + warn
    try:
        after = _read_leftover_state(client, list(baseline))
    except Exception as exc:  # noqa: BLE001
        _warn_leftover_unreadable(item, "after", exc)
        return
    found_all = {
        name: found
        for name in fail + warn
        if (found := _leftover.leftovers(name, baseline[name], after[name]))
    }
    expected = {
        name: reason
        for name, reason in _leftover.expected_items(item).items()
        if name in fail + warn  # 見ていない項目の印は何もしない
    }
    known = {name: found_all[name] for name in expected if name in found_all}
    if known:
        logger.info(
            "qgis_leftover: expected leftovers (not reported) %s",
            _leftover.format_leftovers(item.nodeid, known),
        )
    # 落ちたテストは途中で止まったかもしれないので、「残らなかった」とは言わない
    unmet = {} if call_failed else {n: r for n, r in expected.items() if n not in found_all}
    if unmet:
        warnings.warn(
            _leftover.QgisLeftoverWarning(_leftover.format_unmet(item.nodeid, unmet)),
            stacklevel=1,
        )
    failed = {n: found for n, found in found_all.items() if n in fail and n not in expected}
    warned = {n: found for n, found in found_all.items() if n in warn and n not in expected}
    if warned:
        warnings.warn(
            _leftover.QgisLeftoverWarning(_leftover.format_leftovers(item.nodeid, warned)),
            stacklevel=1,
        )
    if failed:
        pytest.fail(_leftover.format_leftovers(item.nodeid, failed), pytrace=False)


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
    if report.when == "teardown" and report.failed and "automation_client" in item.fixturenames:
        # QGIS を使ったテストの後片付けの失敗（残った状態、#28、や fixture の後片付けの
        # エラー）も、QGIS の状態が分からなくなるので、起こし直しの「落ちた」に数える
        _flag_restart_after_failure(item)
    if report.when != "call":
        return
    if getattr(item, "stash", None) is not None:
        item.stash[_CALL_FAILED_KEY] = report.failed

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

    # #5: QgsMessageLog のメッセージで落とす（opt-in）。流れは例外と同じ。
    message_level = _resolve_fail_on_message_level(item.config)
    if message_level is not None and not _is_dev_mode():
        client = _try_get_automation_client(item)
        if client is not None:
            try:
                bad = _unexpected_messages(item, client, message_level)
            except Exception as exc:  # noqa: BLE001 - 取得失敗でテストは落とさない
                # ただし黙って素通りにもしない。Worker が古くてコマンドを持たない等で
                # 判定が毎回できていないのに、緑に見え続けるのを避ける
                bad = []
                logger.warning(
                    "qgis_fail_on_message_level: could not check %s: %s",
                    item.nodeid,
                    exc,
                    exc_info=True,
                )
                report.sections.append(
                    (
                        "Worker log messages",
                        f"qgis_fail_on_message_level is set but the check could not run: "
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            if bad:
                msg = _message_check.format_messages(bad, message_level)
                if report.passed:
                    report.outcome = "failed"
                    report.longrepr = msg
                    promoted_to_failed = True
                else:
                    report.sections.append(("Worker log messages", msg))

    if report.failed:
        _dump_diagnostic_bundle(item, report=report, call=call)
        # ADR-0002 §17.5: 失敗連鎖防止の自動 fresh_qgis を opt-in なら flag を立てる。
        # 次テストの ``pytest_runtest_setup`` で flag を読んで fresh_qgis marker を
        # 動的付与する。
        _flag_restart_after_failure(item)
    # promoted_to_failed は将来 telemetry に使う想定（現状は info ログのみ）
    if promoted_to_failed:
        logger.info(
            "Test %s promoted pass→fail by uncaught exceptions or logged messages",
            item.nodeid,
        )


def _flag_restart_after_failure(item: pytest.Item) -> None:
    """``qgis_auto_fresh_after_failure`` に従い、次のテストの前に起こし直す印を立てる。"""
    mode = _resolve_auto_fresh_after_failure(item.config)
    if mode == "test":
        setattr(item.config, _CONFIG_ATTR_PRIOR_TEST_FAILED, True)
    elif mode == "session" and item.get_closest_marker("fresh_qgis") is None:
        # fresh_qgis のテストは session の QGIS を使っていないので、起こし直さない
        setattr(item.config, _CONFIG_ATTR_RESTART_SESSION_QGIS, True)


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
    # qgis_allow_message の正規表現の誤りは、判定時（makereport）ではなく
    # ここで test の setup エラーとして出す
    if _resolve_fail_on_message_level(config) is not None:
        _message_check.rules_from_markers(item)
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
