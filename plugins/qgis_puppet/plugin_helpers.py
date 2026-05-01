"""plugin.py の PyQt5 非依存ヘルパ群。

QGIS / PyQt5 を import せずに単体テストできるよう、`plugin.py` から分離した。
Python interpreter の解決、Hub subprocess 用 command/env の構築、URL parse などを
ここに集約する。

## 分離の理由

`plugin.py` は `from qgis_puppeteer.worker import Worker` を top-level で
import するため、`PyQt5` を必要とする。テストを
`plugins/qgis_puppet/tests/` 側で動かすとき、毎回 PyQt5 を準備
するのは手間なので、ロジックだけこのモジュールに切り出している。
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from qgis_puppeteer.qgis_env import find_qgis_python_launcher

logger = logging.getLogger("qgis_puppet.plugin_helpers")

# ==============================================================
# パス定数（plugin.py と共有）
# ==============================================================

_HELPERS_FILE = Path(__file__).resolve()
_PLUGIN_DIR = _HELPERS_FILE.parent
# plugins/qgis_puppet → plugins → repo root（OSS workspace fallback でのみ使用）
_REPO_ROOT = _PLUGIN_DIR.parent.parent


def _resolve_qgis_puppeteer_root() -> Path | None:
    """`qgis_puppeteer` パッケージのディレクトリを解決する。

    返り値は `qgis_puppeteer` パッケージの実体ディレクトリ（`__init__.py` が
    入っているディレクトリ）。`_hub_bootstrap.py` の探索基点として使う。

    ## 解決順

    1. `import qgis_puppeteer` を試行 → 成功なら `Path(__file__).parent`
       を返す。pip install 済み / ホストアプリが事前に sys.path を整えて
       いる / OSS workspace 開発で uv sync 済み 等に該当。
    2. OSS workspace 直接開発時のフォールバック:
       `<repo>/packages/qgis-puppeteer/src/qgis_puppeteer/`
       - 例: qgis-puppeteer リポを直接 cd して plugin_helpers.py を import
         した時点でまだ `qgis_puppeteer` が sys.path にないケース
    3. いずれも該当しなければ `None`。auto-spawn は無効化される。

    ## 中立性

    物理パスを決め打ちしない。ホストアプリ（例: 別リポジトリの vendor 配下）が
    plugin と Python ライブラリを別場所に配置しても、`import` が通れば
    site-packages 等から自動解決される。
    """
    try:
        import qgis_puppeteer
    except ImportError:
        pass
    else:
        file_attr = getattr(qgis_puppeteer, "__file__", None)
        if file_attr:
            return Path(file_attr).resolve().parent

    fallback = _REPO_ROOT / "packages" / "qgis-puppeteer" / "src" / "qgis_puppeteer"
    if fallback.is_dir():
        return fallback.resolve()
    return None


# 解決結果。None になり得る点に注意（その場合 auto-spawn は無効）。
_QGIS_PUPPETEER_ROOT: Path | None = _resolve_qgis_puppeteer_root()

# ==============================================================
# 環境変数キー・既定値
# ==============================================================

# Hub subprocess を起動する launcher（bat / exe どちらでも可）の上書き指定。
# 主にデバッグ・テスト用。実プラグイン運用では OSGEO4W_ROOT 経由で自動解決する。
ENV_HUB_PYTHON = "QPUPPETEER_HUB_PYTHON"
ENV_HUB_LOG_FILE = "QPUPPETEER_HUB_LOG_FILE"
# Hub 起動時に bootstrap が読む追加 sys.path（OS 区切りで連結）。
# `PYTHONPATH` は QGIS 同梱 `python-qgis-ltr.bat` 配下の `etc/ini/python3.bat`
# が `SET PYTHONPATH=` で消去するため、それを回避するための独自 env。
# `_hub_bootstrap.py` がこの env を読んで自身の sys.path に prepend する。
ENV_HUB_EXTRA_SYSPATH = "QPUPPETEER_HUB_EXTRA_SYSPATH"
DEFAULT_HUB_PORT = 9876

# Hub の接続先を決めるユーザ指定用 ENV。plugin.py の _is_external_owner_mode や
# _resolve_hub_url で参照される。ADR-0001 §6「外部所有者モード」の判定キーと
# 兼務しているため、ここに単一定義源として集約する（plugin.py は re-export）。
ENV_HUB_URL = "QPUPPETEER_HUB_URL"
ENV_HUB_HOST = "QPUPPETEER_HUB_HOST"
ENV_HUB_PORT = "QPUPPETEER_HUB_PORT"
DEFAULT_HUB_HOST = "127.0.0.1"

# subprocess の stdout/stderr を受ける既定ファイル名。
# `hub.py` 側の運用ログ（`%APPDATA%\qgis_puppeteer\hub.log`）とは別レイヤ：
# こちらは Python logger 以前の出力（import エラー、Qt の fatal、
# 未捕捉例外の traceback）を拾うためのもの。
DEFAULT_SPAWN_LOG_FILENAME = "qgis_puppet.spawn.log"

# QGIS 同梱の Python launcher (`python-qgis-ltr.bat`) 探索ロジックは
# `qgis_puppeteer.find_qgis_python_launcher()` に集約済み（OSS 公開 API）。
# `_resolve_hub_python` はそれを呼び、プラグイン固有のフォールバックを足す。

# Python interpreter 推測時に「これは python 系バイナリ」と判定する stem 一覧。
# `Path(sys.executable).stem.lower()` がここに含まれれば python と判断する。
_PYTHON_STEMS = frozenset({"python", "python3", "pythonw", "python3w", "pythonw3"})

# Python 実行ファイル兄弟検索時の候補名（`python-qgis-ltr.bat` が使えない
# 環境でのフォールバック用）。`pythonw.exe` は GUI subsystem 版でコンソールが
# 出ないため Windows では優先する。
_PYTHON_CANDIDATE_NAMES = (
    "pythonw.exe",
    "python3.exe",
    "python.exe",
    "pythonw",
    "python3",
    "python",
)


# ==============================================================
# Python interpreter 解決
# ==============================================================


def _is_python_executable(path: Path) -> bool:
    """basename の stem が python 系バイナリかどうか判定する。

    `python`, `python3`, `pythonw`, `python3.11` などを True に、
    `qgis-bin`, `qgis-ltr-bin` などを False にしたい。
    """
    stem = path.stem.lower()
    if stem in _PYTHON_STEMS:
        return True
    # `python3.11` などバージョン付き
    return stem.startswith("python3.") or stem.startswith("python2.")


def _find_python_in_dir(directory: Path) -> Path | None:
    """指定ディレクトリで python 実行ファイル候補を順に探す。"""
    if not directory.is_dir():
        return None
    for name in _PYTHON_CANDIDATE_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _resolve_hub_python() -> Path | None:
    """Hub subprocess を起動する launcher（`python-qgis-ltr.bat` か Python exe）を解決する。

    QGIS プラグイン内では `sys.executable = qgis-ltr-bin.exe` になるため、
    そのまま `subprocess.Popen` すると新しい QGIS プロセスが立ち上がってしまう
    （かつ引数 `9876` が "プロジェクトファイルパス" として解釈される）。
    明示的に launcher のパスを見つけて使う必要がある。

    QGIS 環境下では `python-qgis-ltr.bat` を優先する：これを使うと QGIS が
    `QT_PLUGIN_PATH` / `GDAL_DATA` / `PROJ_LIB` / `PYTHONPATH` 等を正しくセット
    した上で Python を起動してくれるので、Hub subprocess 側でも PyQt5 / GDAL /
    PROJ がそのまま使える。

    優先順：
        1. `python-qgis-ltr.bat` — `qgis_puppeteer.find_qgis_python_launcher()`
           に委譲。`QPUPPETEER_HUB_PYTHON` env 上書き → `OSGEO4W_ROOT/bin/` →
           `sys.executable` 兄弟、の順で探す（OSS 共通ロジック）
        2. `sys.executable` の basename が python 系なら → そのまま
           （QGIS 外で単体実行した場合、pytest 実行中なども該当。プラグイン固有
           のフォールバックなので OSS 側には置かない）
        3. `sys.executable` の親ディレクトリの `python.exe` / `python3.exe` 等
           — OSGeo4W の qgis-ltr-bin.exe と同じディレクトリに Python があるレイアウト

    見つからない場合は `None` を返す（auto-spawn は無効化する）。
    """
    # 1. QGIS 公式の Python launcher（OSS 共通の探索ロジックに委譲）。
    #    QPUPPETEER_HUB_PYTHON env による上書きはこの呼び出しの中で処理される。
    launcher = find_qgis_python_launcher(override_env=ENV_HUB_PYTHON)
    if launcher is not None:
        return launcher

    # 2. sys.executable が python 系ならそれ（プラグイン固有フォールバック）
    exe = Path(sys.executable)
    if _is_python_executable(exe) and exe.is_file():
        return exe

    # 3. sys.executable の兄弟（プラグイン固有フォールバック）
    sibling = _find_python_in_dir(exe.parent)
    if sibling is not None:
        return sibling

    return None


# ==============================================================
# Hub subprocess 用 command / env
# ==============================================================


# `_hub_bootstrap.py` の絶対パス。`_QGIS_PUPPETEER_ROOT` が None の場合は
# bootstrap も解決できない（spawn 不可）。
_HUB_BOOTSTRAP: Path | None = (
    _QGIS_PUPPETEER_ROOT / "_hub_bootstrap.py" if _QGIS_PUPPETEER_ROOT is not None else None
)


def _build_hub_spawn_command(python: Path, port: int) -> list[str]:
    """Hub subprocess 起動コマンドを構築する。

    `python -m qgis_puppeteer.hub` ではなく bootstrap スクリプトをフルパスで
    渡す：`python-qgis-ltr.bat` 配下の `etc/ini/python3.bat` が
    `SET PYTHONPATH=` で親プロセスの PYTHONPATH を消去するため、
    `-m` 方式では `ModuleNotFoundError: No module named 'qgis_puppeteer'` で
    即死する。bootstrap が自分自身の位置から sys.path を組み立て直し、
    さらに `QPUPPETEER_HUB_EXTRA_SYSPATH` env から追加パスを引き継ぐ。

    `_HUB_BOOTSTRAP` が解決できなかった場合（`qgis_puppeteer` が import できず
    OSS workspace fallback も使えない場合）は `RuntimeError` を上げる。
    """
    if _HUB_BOOTSTRAP is None:
        raise RuntimeError(
            "qgis_puppeteer is not importable and no OSS workspace fallback was "
            "found; cannot locate _hub_bootstrap.py. Install qgis-puppeteer "
            "(e.g. `pip install qgis-puppeteer`) or expose its location via "
            "sys.path before loading qgis_puppet."
        )
    return [str(python), str(_HUB_BOOTSTRAP), "--port", str(port)]


def _build_hub_spawn_env() -> dict[str, str]:
    """Hub subprocess に渡す環境変数を構築する。

    親プロセスの `sys.path` を独自 env `QPUPPETEER_HUB_EXTRA_SYSPATH` に
    OS 区切りで連結して詰める。bootstrap (`_hub_bootstrap.py`) がこの env を
    読んで自身の `sys.path` に prepend する。

    `PYTHONPATH` ではなく独自 env を使う理由:
        QGIS 同梱の `python-qgis-ltr.bat` 経由で起動すると、
        `etc/ini/python3.bat` が `SET PYTHONPATH=` で親の PYTHONPATH を
        完全消去するため、PYTHONPATH ベースで sys.path を渡す経路は
        Hub 子プロセスに届かない。

    `os.environ['PYTHONPATH']` ではなく `sys.path` を真実とする理由:
        QGIS の Python 初期化は `os.environ['PYTHONPATH']` を尊重しない
        ことがあり、`sys.path` のほうが「いま実際に import が解決される場所」
        を表している。`PYQGIS_STARTUP` 等で動的に追加されたパスもここに
        反映済み。ホストアプリ固有の path 名を qgis_puppeteer 側で知らずに
        転写できる。

    親の環境変数はそのまま継承する。
    """
    env = os.environ.copy()
    extra_paths = [p for p in sys.path if p]
    env[ENV_HUB_EXTRA_SYSPATH] = os.pathsep.join(extra_paths)
    return env


def _extract_port(hub_url: str, default: int = DEFAULT_HUB_PORT) -> int:
    """`ws://host:port/...` から port を取り出す。未指定ならデフォルト。"""
    try:
        parsed = urlparse(hub_url)
        if parsed.port is not None:
            return parsed.port
    except ValueError:
        pass
    return default


def _resolve_hub_url() -> str:
    """Hub の WS URL を決定する。

    優先順:
        1. `QPUPPETEER_HUB_URL` が明示指定されていればそれを使用
        2. `QPUPPETEER_HUB_HOST` / `QPUPPETEER_HUB_PORT` があれば URL を合成
        3. デフォルト `ws://127.0.0.1:9876`

    PyQt5 非依存なので plugin_helpers に置く（plugin.py の PyQt5 初期化より
    先に呼べる、pytest からも直接テストできる）。
    """
    override = os.environ.get(ENV_HUB_URL)
    if override:
        return override
    host = os.environ.get(ENV_HUB_HOST, DEFAULT_HUB_HOST)
    port = os.environ.get(ENV_HUB_PORT, str(DEFAULT_HUB_PORT))
    return f"ws://{host}:{port}"


# ==============================================================
# Hub subprocess の stdout/stderr ログ先
# ==============================================================


def _resolve_hub_log_file() -> Path:
    """Hub subprocess の stdout/stderr を受けるファイルパスを決定する。

    Hub 側には Python logger で `%APPDATA%\\qgis_puppeteer\\hub.log` に書く経路
    （`hub._setup_logging()`）があるが、これは Python が正常に起動して
    `logging.getLogger(...)` が動いた後にしか使えない。

    一方 subprocess の stdout/stderr は、
    - import エラー (PyQt5 が無い等)
    - Qt fatal ("QWebSocketServer: failed to listen" 等)
    - 未捕捉例外の traceback
    - 起動前の Python レベルのエラー
    を拾う唯一の手段なので、**デフォルトで必ずファイルに残す** 方針にする。
    `DEVNULL` にしてしまうと、起動失敗の原因が追えなくなる。

    優先順：
        1. `QPUPPETEER_HUB_LOG_FILE` 環境変数（ユーザ明示指定）
        2. `<TEMP>/qgis_puppet.spawn.log` （既定）
    """
    override = os.environ.get(ENV_HUB_LOG_FILE)
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / DEFAULT_SPAWN_LOG_FILENAME


# ==============================================================
# Extension discovery（ADR-0001 §12.3）
# ==============================================================

# puppeteer_api モジュールが公開すべき関数名。
EXTENSION_ENTRY_POINT = "build_handlers"
# discover 時に読み込むモジュールの suffix。
EXTENSION_MODULE_NAME = "puppeteer_api"

# Handler の型（Worker.register_handler の期待シグネチャに合わせる）。
# 実際の Handler 型は qgis_puppeteer.worker_state.Handler と同じだが、
# ここでは PyQt5 非依存を保つため Any で扱う。
DiscoveredHandler = Callable[[dict[str, Any]], Any]
RegisterFn = Callable[[str, DiscoveredHandler], None]


def discover_extensions(
    plugin_names: Iterable[str],
    iface: Any,
    register: RegisterFn,
    *,
    skip: str = "qgis_puppet",
) -> list[tuple[str, str]]:
    """各 QGIS プラグインの `puppeteer_api` モジュールを discover して登録する。

    ADR-0001 §12.3 "Worker 側 pull discovery" の実装本体。Qt 非依存で、
    plugin.py から呼ばれる際は `qgis.utils.plugins` dict を渡す。

    Args:
        plugin_names: 有効なプラグイン名の iterable（`qgis.utils.plugins` のキー）。
        iface: QGIS interface（各 extension の `build_handlers` に渡される）。
        register: `worker.register_handler` 相当の callable。
            `(command: str, handler: Callable) -> None` を期待する。
            例外を投げた場合は該当ハンドラのみ skip して discover を続行する。
        skip: 処理から除外するプラグイン名（通常は自分自身）。

    Returns:
        成功登録の `(plugin_name, command_name)` リスト。
    """
    registered: list[tuple[str, str]] = []
    for name in plugin_names:
        if name == skip:
            continue
        module_path = f"{name}.{EXTENSION_MODULE_NAME}"
        try:
            mod = importlib.import_module(module_path)
        except ImportError:
            # 該当プラグインは puppeteer 拡張を提供していない。正常系。
            continue
        except Exception:
            logger.exception("Failed to import %s", module_path)
            continue

        build = getattr(mod, EXTENSION_ENTRY_POINT, None)
        if not callable(build):
            logger.warning(
                "%s has no callable %r; skipping",
                module_path,
                EXTENSION_ENTRY_POINT,
            )
            continue

        try:
            handlers = build(iface)
        except Exception:
            logger.exception("%s.%s raised", module_path, EXTENSION_ENTRY_POINT)
            continue

        if not isinstance(handlers, dict):
            logger.warning(
                "%s.%s returned non-dict (%s); skipping",
                module_path,
                EXTENSION_ENTRY_POINT,
                type(handlers).__name__,
            )
            continue

        for command, handler in handlers.items():
            if not callable(handler):
                logger.warning(
                    "%s provided non-callable handler for %r; skipping",
                    module_path,
                    command,
                )
                continue
            try:
                register(command, handler)
            except Exception:
                # register 側の検証エラー（ReservedNamespaceError 等）は
                # 当該ハンドラのみ skip して他の登録を継続する
                logger.exception(
                    "Failed to register handler %r from %s",
                    command,
                    name,
                )
                continue
            registered.append((name, command))
    return registered
