"""QgisPuppetPlugin — `qgis_puppeteer.Worker` の QGIS ラッパ。

ADR-0001 §4 "Worker 側" に従って、プラグインロード時に `Worker` を生成し
Hub へ逆向き接続する。QGIS 終了時は `aboutToQuit` + close 待機で
`bye` フレームを確実に送る（graceful close）。

## 依存
- `qgis_puppeteer`（リポジトリルート直下）を sys.path に追加して import
- PyQt5（QGIS 付属）

## 設定（環境変数、全て任意）
- `QPUPPETEER_HUB_URL`     Hub の WS URL（デフォルト `ws://127.0.0.1:9876`）
- `QPUPPETEER_HUB_HOST`    Hub ホスト（URL 未指定時に合成、デフォルト `127.0.0.1`）
- `QPUPPETEER_HUB_PORT`    Hub ポート（URL 未指定時に合成、デフォルト `9876`）
- `QPUPPETEER_HUB_ORIGIN`  register 時に送る Origin（デフォルト `http://localhost`）
- `QPUPPETEER_WORKER_LABEL` Worker のラベル（未指定なら Hub が自動採番）
- `QPUPPETEER_HUB_LOCK_PATH` Hub auto-spawn 用ロックファイル（デフォルト TEMP 配下）
- `QPUPPETEER_HUB_LOG_FILE`  Hub プロセスの stdout/stderr 出力先

上記 `HUB_URL` / `HUB_HOST` / `HUB_PORT` のいずれかが設定されていると **外部所有者モード**
になり、Worker は Hub spawn を行わず接続のみ試行する（ADR-0001 §6）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# ==============================================================
# sys.path 調整：qgis_puppeteer は本プラグインの外で配布される別パッケージ
#
# plugin_helpers は PyQt5 / qgis_puppeteer 本体に非依存なので先に import できる。
# `_QGIS_PUPPETEER_ROOT` は `qgis_puppeteer` パッケージの実体ディレクトリを指す
# （pip install 後の site-packages、ホストアプリの vendor 配下、または OSS
# workspace のフォールバック）。その親を sys.path に積めば
# `import qgis_puppeteer` が通る。
#
# 解決の詳細は plugin_helpers._resolve_qgis_puppeteer_root を参照。
# 既に sys.path 経由で見えている場合は重複追加しない。
# ==============================================================
from .plugin_helpers import _QGIS_PUPPETEER_ROOT  # noqa: E402

if _QGIS_PUPPETEER_ROOT is not None:
    _parent_str = str(_QGIS_PUPPETEER_ROOT.parent)
    if _parent_str not in sys.path:
        sys.path.insert(0, _parent_str)

# `Worker` は PyQt5 依存のため top-level `qgis_puppeteer` からは export されない。
# サブモジュール経由で import する（プラグイン実行時は QGIS 付属の PyQt5 が利用可）。
from PyQt5.QtCore import (  # noqa: E402  type: ignore[import-not-found]
    QEventLoop,
    QTimer,
)
from qgis_puppeteer.worker import Worker  # noqa: E402

from .plugin_helpers import (  # noqa: E402
    ENV_HUB_HOST,
    ENV_HUB_PORT,
    ENV_HUB_PYTHON,
    ENV_HUB_URL,
    _build_hub_spawn_command,
    _build_hub_spawn_env,
    _extract_port,
    _resolve_hub_log_file,
    _resolve_hub_python,
    _resolve_hub_url,
    discover_extensions,
)

logger = logging.getLogger("qgis_puppet.plugin")

# ==============================================================
# 環境変数キー
# ==============================================================

# ENV_HUB_URL / ENV_HUB_HOST / ENV_HUB_PORT は plugin_helpers 側に単一定義源。
# ENV_HUB_LOG_FILE / ENV_HUB_PYTHON も plugin_helpers 側（PyQt5 非依存層）。
ENV_HUB_ORIGIN = "QPUPPETEER_HUB_ORIGIN"
ENV_WORKER_LABEL = "QPUPPETEER_WORKER_LABEL"
ENV_HUB_LOCK_PATH = "QPUPPETEER_HUB_LOCK_PATH"

DEFAULT_LOCK_FILENAME_TEMPLATE = "qgis_puppet-hub-{port}.lock"


def _is_external_owner_mode() -> bool:
    """外部所有者モードか判定する。

    `QPUPPETEER_HUB_URL` / `QPUPPETEER_HUB_HOST` / `QPUPPETEER_HUB_PORT` の
    いずれかが設定されている場合、Hub の所有者は外部（pytest / remote 等）
    にあり、Worker は spawn しない。ADR-0001 §6 参照。

    `HUB_URL` もここに含めることで、ユーザが `ws://host:port` 形式で明示した
    場合（host/port を個別指定せず URL まるごと上書き）も外部扱いになる。
    """
    return ENV_HUB_URL in os.environ or ENV_HUB_HOST in os.environ or ENV_HUB_PORT in os.environ


# ==============================================================
# プラグイン本体
# ==============================================================


class QgisPuppetPlugin:
    """QGIS プラグインのライフサイクルを `Worker` に橋渡しする。

    QGIS が `initGui` を呼ぶと Hub に接続、`unload` を呼ぶと bye で明示切断。
    QGIS 終了時は `QgsApplication.aboutToQuit` で graceful close を走らせる。
    """

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        self._worker: Worker | None = None
        self._instance_id: str | None = None
        self._about_to_quit_hooked: bool = False

    # ------------------------------------------------------------
    # QGIS ライフサイクル hook
    # ------------------------------------------------------------

    def initGui(self) -> None:
        """QGIS にロードされた直後に呼ばれる：Worker を起動して Hub に接続。

        Hub auto-spawn は、適切な Python interpreter が見つかった場合のみ有効化する。
        sys.executable が QGIS 本体バイナリだとそれを起動してしまうため（新しい
        QGIS プロセスが立ち上がる）、`_resolve_hub_python()` で明示的に検出する。

        ADR-0001 §6 "外部所有者モード"：`QPUPPETEER_HUB_HOST` / `QPUPPETEER_HUB_PORT`
        が設定されている場合は Hub 所有者が外部にあるので、auto-spawn を完全に
        抑制して接続のみ試行する（E2E テスト / remote 運用）。
        """
        hub_url = _resolve_hub_url()
        origin = os.environ.get(ENV_HUB_ORIGIN, "http://localhost")
        label = os.environ.get(ENV_WORKER_LABEL) or None
        external_owner = _is_external_owner_mode()

        # subprocess の stdout/stderr を常にファイルに残す（DEVNULL にはしない）。
        # `hub.py` 側の Python logger が回り始める前のエラー（import 失敗、Qt fatal 等）は
        # ここでしか拾えない。デフォルトは `<TEMP>\qgis_puppet.spawn.log`。
        log_file = _resolve_hub_log_file()
        logger.info("Hub subprocess log file: %s", log_file)

        hub_port = _extract_port(hub_url)

        if external_owner:
            # 外部所有者モード：Hub は pytest / 外部プロセスが所有しているため
            # Worker は spawn せず接続のみ試行する（ADR-0001 §6）。
            logger.info(
                "External owner mode (hub_url=%s); auto-spawn disabled",
                hub_url,
            )
            lock_path = None
            hub_spawn_command = None
            hub_spawn_env = None
        else:
            # Hub auto-spawn 用のパラメータ。Python interpreter が見つからなければ
            # auto-spawn を諦めて接続のみ試みる（Worker 側は auto-reconnect で待つ）。
            hub_python = _resolve_hub_python()
            if hub_python is None:
                logger.warning(
                    "No suitable Python launcher for Hub auto-spawn (sys.executable=%r). "
                    "Expected OSGEO4W_ROOT/bin/python-qgis-ltr.bat. "
                    "Override with %s=<path>. "
                    "Hub auto-spawn disabled; Worker will connect if Hub is already running.",
                    sys.executable,
                    ENV_HUB_PYTHON,
                )
                lock_path = None
                hub_spawn_command = None
                hub_spawn_env = None
            else:
                logger.info("Hub auto-spawn python: %s", hub_python)
                lock_path = self._resolve_lock_path(hub_port)
                hub_spawn_command = _build_hub_spawn_command(hub_python, hub_port)
                hub_spawn_env = _build_hub_spawn_env()

        self._worker = Worker(
            hub_url=hub_url,
            origin=origin,
            label=label,
            project=self._current_project_path(),
            hub_lock_path=lock_path,
            hub_spawn_command=hub_spawn_command,
            hub_spawn_env=hub_spawn_env,
            hub_log_file=log_file,
        )

        # ADR-0001 §5 の QGIS 操作 handler 群を登録（iface 付きで wrap）。
        # import 失敗時は空 dict が返るので、通信層は動くが個別ツールは
        # INVALID_COMMAND を返す状態になる（診断しやすい）。
        self._register_handlers()

        # 診断用にシグナルをログに流す
        self._worker.registered.connect(self._on_registered)
        self._worker.unregistered.connect(self._on_unregistered)
        self._worker.register_failed.connect(self._on_register_failed)
        self._worker.hub_startup_failed.connect(self._on_hub_startup_failed)

        self._hook_about_to_quit()
        self._worker.connect_to_hub()

        # ADR-0002 Roadmap "Dialog handler": 想定外モーダルへの auto-respond
        # 用 polling timer。registry が空でも tick は cheap（QApplication.
        # activeModalWidget() + dict lookup のみ）なので常時動かす。
        self._start_dialog_handler_polling()

    def _register_handlers(self) -> None:
        """Core `qgis_*` を登録後、外部プラグインから Tier 2/3 ハンドラを discover する。

        ADR-0001 §12.3 に従い、Worker 側は「各プラグインの `puppeteer_api` モジュール
        を qgis_puppet が pull する」方式で拡張ハンドラを収集する。

        discover は以下の 2 段階で実行することで、プラグインロード順に起因する
        取り逃がしを防ぐ:

        1. 現在の event loop tick の末尾（`QTimer.singleShot(0, ...)`）に 1 回。
           この時点で QGIS は他プラグインの `initGui` をすでに呼び終えている。
        2. `iface.initializationCompleted` シグナル発火時に再度。
           QGIS 完全起動後に動的ロードされたプラグインや、`initGui` 内で
           `puppeteer_api` を遅延 import しているプラグインを拾い直す。

        同一ハンドラの再登録は `HandlerAlreadyRegisteredError` で無害に skip
        されるので、2 回呼んでも副作用なし。
        """
        assert self._worker is not None

        # Tier 1 Core：遅延 import で qgis_tools の import 失敗を握る
        from .handlers import build_handlers

        core_handlers = build_handlers(self.iface)
        for command, handler in core_handlers.items():
            # Core ハンドラは `qgis_*` のみ。外部 Tier 2/3 とは別 API 経由で登録。
            self._worker.register_core_handler(command, handler)
        logger.info("Registered %d core QGIS handlers", len(core_handlers))

        # Tier 3 Test handlers (`test.*`): QPUPPETEER_ALLOW_TEST_HANDLERS=1 が
        # 立っていれば signal_spy / wait_for_signal を register。env が無い時は
        # register_handler が RegistrationDeniedError を投げるので silent skip。
        import os as _os

        if _os.environ.get("QPUPPETEER_ALLOW_TEST_HANDLERS") == "1":
            from .handlers import build_test_handlers

            test_handlers = build_test_handlers(self.iface)
            for command, handler in test_handlers.items():
                try:
                    self._worker.register_handler(command, handler)
                except Exception:  # noqa: BLE001 - 二重登録 / 環境差で fail しても plugin 全体を落とさない
                    logger.warning("Could not register test handler %s", command, exc_info=True)
            if test_handlers:
                logger.info(
                    "Registered %d test.* handlers (ALLOW_TEST_HANDLERS=1)",
                    len(test_handlers),
                )

        # Tier 2/3：他プラグインの puppeteer_api.py を discover（遅延実行）
        self._schedule_extension_discovery()

    def _schedule_extension_discovery(self) -> None:
        """Extension discover をイベントループ 1 tick 後 + 完全初期化時の両方で走らせる。

        ADR-0001 §12.3 "ロード順問題の構造的解消"。
        """
        # 段階 1: 現在 tick の末尾（= 他プラグイン initGui 完了後）
        QTimer.singleShot(0, self._discover_extension_handlers)

        # 段階 2: QGIS 完全初期化時（後発プラグインや遅延 import プラグイン）。
        # テスト環境や非 GUI QGIS では iface が None のことがあるのでガードする。
        if self.iface is None:
            return
        try:
            init_completed = getattr(self.iface, "initializationCompleted", None)
            if init_completed is None:
                return
            # 既に初期化完了後にロードされるケース（プラグインマネージャから後追加）
            # でも確実に走らせるため、connect 直後に手動で 1 回呼ぶ必要はない。
            # singleShot(0) が QGIS のイベントループに乗った時点でまず 1 回走る。
            init_completed.connect(self._discover_extension_handlers)
        except (AttributeError, TypeError):  # pragma: no cover - 環境差の保険
            logger.debug("initializationCompleted signal unavailable", exc_info=True)

    def _discover_extension_handlers(self) -> None:
        """他の QGIS プラグインから puppeteer_api.build_handlers を pull して登録する。

        冪等：同一ハンドラ再登録は Worker 側が `HandlerAlreadyRegisteredError`
        で握りつぶすので、何度呼んでも安全。
        """
        if self._worker is None:
            return
        try:
            from qgis.utils import (  # type: ignore[import-not-found]
                plugins as qgis_plugins,
            )
        except ImportError:
            logger.debug("qgis.utils.plugins unavailable; skipping extension discovery")
            return

        registered = discover_extensions(
            plugin_names=list(qgis_plugins.keys()),
            iface=self.iface,
            register=self._worker.register_handler,
        )
        if registered:
            logger.info(
                "Discovered %d extension handler(s): %s",
                len(registered),
                [f"{cmd} ({plug})" for plug, cmd in registered],
            )

    def unload(self) -> None:
        """QGIS がプラグインをアンロードする直前に呼ばれる：明示切断。

        `aboutToQuit` での graceful close と両方走ることがあるが、
        `Worker.disconnect_from_hub` は冪等に動く（closing フラグで保護）。
        """
        # Dialog handler の polling timer を停止して registry をクリア。
        # 再 load 時に古い handler が残らないように。
        self._stop_dialog_handler_polling()
        self._close_worker()

    # ------------------------------------------------------------
    # 内部：Dialog handler polling
    # ------------------------------------------------------------

    def _start_dialog_handler_polling(self) -> None:
        """ADR-0002 Roadmap "Dialog handler": polling timer を開始。

        Worker の親に紐付けて life cycle を管理（unload で stop + clear）。
        Tier 1 ハンドラ ``qgis_register_dialog_handler`` が登録した predicate
        にマッチするモーダルが現れたら、tick が action を発火する。
        """
        try:
            from qgis_puppeteer.qgis_tools import (  # type: ignore[import-not-found]
                dialog_handler_tools,
            )
        except ImportError:
            logger.debug("dialog_handler_tools not available; polling disabled")
            return

        self._dialog_timer = QTimer()
        # 250ms = ADR-0002 §8.3 の auto-wait poll 間隔と同じ周期。Hub に余計な
        # 負荷を掛けず、人間が「処理されてない」と感じない程度のレスポンス。
        self._dialog_timer.setInterval(250)
        self._dialog_timer.timeout.connect(dialog_handler_tools.tick)
        self._dialog_timer.start()
        logger.debug("Dialog handler polling started (interval=250ms)")

    def _stop_dialog_handler_polling(self) -> None:
        """Polling timer を止め、registry をクリア。"""
        timer = getattr(self, "_dialog_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001
                logger.debug("Failed to stop dialog timer", exc_info=True)
            self._dialog_timer = None
        try:
            from qgis_puppeteer.qgis_tools import (  # type: ignore[import-not-found]
                dialog_handler_tools,
            )

            dialog_handler_tools.get_registry().clear()
        except ImportError:
            pass

    # ------------------------------------------------------------
    # 内部：初期化ヘルパ
    # ------------------------------------------------------------

    def _resolve_lock_path(self, port: int) -> Path:
        """Hub auto-spawn 用のロックファイルパスを決定する。

        環境変数優先、なければ OS の一時ディレクトリ配下。
        プロセスをまたいで **同一 port なら同じパス** であることが重要
        （別パスだと同じ port に複数 Hub を起動してしまう）。
        port サフィックスにより、xdist 並列や外部所有者モードで別 port の
        Hub を立てても lock ファイルが衝突しない（ADR-0001 §4）。
        """
        override = os.environ.get(ENV_HUB_LOCK_PATH)
        if override:
            return Path(override)
        filename = DEFAULT_LOCK_FILENAME_TEMPLATE.format(port=port)
        return Path(tempfile.gettempdir()) / filename

    def _current_project_path(self) -> str | None:
        """現在開いている QGIS プロジェクトのファイルパスを取得する。"""
        try:
            from qgis.core import (  # type: ignore[import-not-found]
                QgsProject,
            )
        except ImportError:
            return None
        path = QgsProject.instance().fileName()
        return path if path else None

    def _hook_about_to_quit(self) -> None:
        """`QgsApplication.aboutToQuit` に graceful close を接続する。"""
        if self._about_to_quit_hooked:
            return
        try:
            from qgis.core import (  # type: ignore[import-not-found]
                QgsApplication,
            )

            app = QgsApplication.instance()
            if app is None:
                return
            app.aboutToQuit.connect(self._graceful_close)
            self._about_to_quit_hooked = True
        except Exception:  # pragma: no cover - ランタイム環境差
            logger.debug("Could not hook aboutToQuit", exc_info=True)

    # ------------------------------------------------------------
    # 内部：close 処理
    # ------------------------------------------------------------

    def _close_worker(self) -> None:
        """Worker を明示切断してインスタンス参照を解放する。"""
        if self._worker is None:
            return
        try:
            self._worker.disconnect_from_hub(send_bye=True)
        except Exception:  # pragma: no cover - 防御的
            logger.debug("Error during disconnect_from_hub", exc_info=True)
        self._worker = None

    def _graceful_close(self) -> None:
        """QGIS 終了時：bye を送り、close 完了を短時間だけ待つ。

        ADR-0001 §4 の graceful close 手順に倣う：
        `unregistered` シグナル（= close 完了）か 500ms タイムアウトのいずれかで抜ける。
        """
        if self._worker is None:
            return
        worker = self._worker  # loop.quit 接続用に参照を保持
        loop = QEventLoop()

        def _quit_on_unregistered() -> None:
            if loop.isRunning():
                loop.quit()

        worker.unregistered.connect(_quit_on_unregistered)
        try:
            worker.disconnect_from_hub(send_bye=True)
            QTimer.singleShot(500, loop.quit)
            loop.exec_()
        finally:
            # pragma: no cover — 既に切断済み
            with contextlib.suppress(TypeError, RuntimeError):
                worker.unregistered.disconnect(_quit_on_unregistered)
            self._worker = None

    # ------------------------------------------------------------
    # 内部：Qt シグナルハンドラ（ログ用）
    # ------------------------------------------------------------

    def _on_registered(self, instance_id: str) -> None:
        self._instance_id = instance_id
        logger.info("QGIS Puppet registered as %s", instance_id)
        self._show_message(f"QGIS Puppet: {instance_id}")

    def _on_unregistered(self) -> None:
        logger.info("QGIS Puppet disconnected from Hub")

    def _on_register_failed(self, reason: str) -> None:
        logger.warning("QGIS Puppet register failed: %s", reason)
        self._show_message(f"QGIS Puppet: register failed ({reason})", warning=True)

    def _on_hub_startup_failed(self, detail: str) -> None:
        logger.warning("QGIS Puppeteer Hub startup failed: %s", detail)
        self._show_message(f"QGIS Puppet: Hub startup failed ({detail})", warning=True)

    def _show_message(self, text: str, *, warning: bool = False) -> None:
        """QGIS のメッセージバーに通知する（iface が利用可能な場合のみ）。"""
        try:
            from qgis.core import Qgis  # type: ignore[import-not-found]

            level = Qgis.Warning if warning else Qgis.Info
            mbar = getattr(self.iface, "messageBar", None)
            if mbar is None:
                return
            mbar().pushMessage("QGIS Puppet", text, level=level, duration=5)
        except Exception:  # pragma: no cover - ランタイム環境差
            logger.debug("Could not show message bar", exc_info=True)
