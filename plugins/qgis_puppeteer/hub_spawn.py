"""Hub プロセスの起動競合回避ロジック（Qt 非依存）。

ADR-0001 §4 "Hub の起動競合回避（親主導の spawn 確認モデル）" を実装する。
Windows は `pass_fds=` が使えないため、親が排他ロックを保持したまま Hub を
`subprocess.Popen` で DETACHED 起動 → TCP probe で listen 成功を確認 →
ロック解放、というモデルに従う。

## 公開 API

- `tcp_probe(host, port, timeout)` — listen しているかだけ確認
- `ensure_hub_reachable(...)` — probe 失敗時はロック獲得して spawn、
  他プロセスが spawn 中ならバックオフで待つ
- `try_spawn_hub(...)` — ロック → Popen → probe → 解放の 1 サイクル
- `default_hub_command(port, python)` — `python -m qgis_puppeteer.hub`
- `HubStartupError` — 最終的に到達できなかった場合に送出
- `SpawnOutcome` / `EnsureOutcome` — 結果分類 Enum

## 設計原則

- Qt に依存しない（pytest から直接呼べる）
- 副作用（Popen 呼び出し・sleep）は注入可能（ユニットテストでモック可）
- Windows / POSIX 両対応（`msvcrt.locking` / `fcntl.flock`）
- 同一プロセスでの再帰ロック挙動は OS 依存なので、排他確認は別プロセス経由で検証
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from enum import Enum
from pathlib import Path

logger = logging.getLogger("qgis_puppeteer.hub_spawn")

# ============================================================
# 定数
# ============================================================

DEFAULT_HUB_HOST: str = "127.0.0.1"
DEFAULT_HUB_PORT: int = 9876

DEFAULT_CONNECT_BACKOFF: tuple[float, ...] = (
    0.3,
    0.6,
    1.0,
    1.5,
    2.0,
    3.0,
    4.0,
    6.0,
    8.0,
    10.0,
)
DEFAULT_PROBE_TIMEOUT: float = 0.2
DEFAULT_SPAWN_DEADLINE: float = 5.0
DEFAULT_POLL_INTERVAL: float = 0.1


# ============================================================
# 例外・Enum
# ============================================================


class HubStartupError(Exception):
    """Hub の起動または到達に最終的に失敗した。"""


class SpawnOutcome(Enum):
    """try_spawn_hub の結果。"""

    SPAWNED = "spawned"
    """この呼び出しで Hub を起動し、listen を確認した。"""

    ALREADY_LISTENING = "already_listening"
    """ロック獲得後の再 probe で、既に Hub が listen していた。"""

    ANOTHER_PROCESS_OWNS_LOCK = "another_process_owns_lock"
    """別プロセスがロックを保持していたため、何もせず戻った。"""


class EnsureOutcome(Enum):
    """ensure_hub_reachable の結果。"""

    ALREADY_LISTENING = "already_listening"
    """最初の probe で既に Hub が応答した。"""

    SPAWNED_BY_US = "spawned_by_us"
    """このプロセスが Hub を起動して listen させた。"""

    SPAWNED_BY_OTHER = "spawned_by_other"
    """別プロセスが起動中だったので待機した結果、到達できるようになった。"""


# ============================================================
# TCP probe
# ============================================================


def tcp_probe(host: str, port: int, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> bool:
    """指定アドレスに TCP connect が通れば True。

    WebSocket ハンドシェイクは確認しない（ADR-0001 §4 の補足参照：
    `QWebSocketServer.listen()` 成功後の accept は可能なので、
    一瞬のハンドシェイク未準備窓は Worker の接続バックオフで吸収する前提）。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


# ============================================================
# 排他ロック（Windows / POSIX 両対応）
# ============================================================


@contextmanager
def _exclusive_nonblocking_lock(path: Path) -> Iterator[bool]:
    """ファイルに対する非ブロッキング排他ロック。

    取得できれば True、他プロセスが保持中なら False を yield する。
    スコープ退出時に、取得していた場合のみロックを解放する。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    locked = _try_acquire(fd)
    try:
        yield locked
    finally:
        if locked:
            try:
                _release(fd)
            except OSError:
                logger.debug("Failed to release lock on %s", path, exc_info=True)
        with suppress(OSError):
            os.close(fd)


def _try_acquire(fd: int) -> bool:
    """非ブロッキングで fd の排他ロックを取る。取れたら True。"""
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            return False


def _release(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


# ============================================================
# spawn コマンド
# ============================================================


def default_hub_command(*, port: int = DEFAULT_HUB_PORT, python: str | None = None) -> list[str]:
    """`python -m qgis_puppeteer.hub --port <port>` を起動するコマンド。"""
    return [
        python or sys.executable,
        "-m",
        "qgis_puppeteer.hub",
        "--port",
        str(port),
    ]


# Popen factory の型（subprocess.Popen 互換）
PopenFactory = Callable[..., "subprocess.Popen[bytes]"]


# 親プロセス終了まで Popen への参照を保持するストア。
#
# 背景：Hub subprocess は DETACHED で起動され親プロセスから独立して動くため、
# 親側で `proc.wait()` などをする必要はない。だが、ローカル変数として放置すると
# GC 時に `Popen.__del__` が "subprocess NNNN is still running" という
# `ResourceWarning` を出す（subprocess モジュールの実装上の挙動）。
#
# 親が生きている間は参照を保持しておけば `__del__` が走らないので警告も出ない。
# リストが無限に伸びるが、Hub spawn は QGIS プラグインのライフサイクルで一度
# （〜まれに再試行）しか呼ばれないので実用上問題にならない。
_SPAWNED_CHILDREN: list[object] = []


def _retain_child(proc: object) -> None:
    """Popen オブジェクトへの参照を親プロセスが終了するまで保持する。"""
    _SPAWNED_CHILDREN.append(proc)


def _close_inherited_log_handle(popen_kwargs: dict[str, object]) -> None:
    """`_build_popen_kwargs` が開いた log file を親側で閉じる。

    Popen に渡したファイル記述子は OS レベルで子に継承されているので、
    親側では即座に close して OK（子は書き続けられる）。親が close し忘れると
    GC で `ResourceWarning: unclosed file` が出る。

    `subprocess.DEVNULL` のような int 定数は無視する（close 不要）。
    """
    stream = popen_kwargs.get("stdout")
    if hasattr(stream, "close") and callable(getattr(stream, "close", None)):
        try:
            stream.close()  # type: ignore[union-attr]
        except OSError:
            logger.debug("Failed to close inherited log handle", exc_info=True)


def _build_popen_kwargs(
    log_file: Path | None, env: dict[str, str] | None = None
) -> dict[str, object]:
    """プラットフォーム別の Popen kwargs を構築する。

    DETACHED で起動し、ログは指定ファイル（なければ DEVNULL）に流す。
    `env` が指定されていれば Popen に渡す（`None` なら親環境を継承）。
    """
    kwargs: dict[str, object] = {"stdin": subprocess.DEVNULL}
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        f = log_file.open("ab")
        kwargs["stdout"] = f
        kwargs["stderr"] = subprocess.STDOUT
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

    if env is not None:
        kwargs["env"] = env

    if sys.platform == "win32":
        # CREATE_NO_WINDOW: 新しいコンソールウィンドウを開かない。
        #   `python.exe` は Console subsystem 実行ファイルなので、単に
        #   `DETACHED_PROCESS` を指定すると Windows が自動で新しいコンソールを
        #   割り当ててしまい、QGIS 起動時に黒い cmd ウィンドウが表示される。
        #   `CREATE_NO_WINDOW` は「親コンソールから切り離す」+「新規コンソール
        #   も作らない」挙動で、GUI アプリから子プロセスを静かに起動するときの
        #   定石（MS docs: Process Creation Flags）。
        #   親（QGIS）終了後も子は残る（DETACHED_PROCESS と同じく独立プロセス）。
        # CREATE_NEW_PROCESS_GROUP: Ctrl+C が親経由で飛ばないように分離。
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


# ============================================================
# try_spawn_hub
# ============================================================


def try_spawn_hub(
    *,
    lock_path: Path,
    host: str = DEFAULT_HUB_HOST,
    port: int = DEFAULT_HUB_PORT,
    command: Sequence[str] | None = None,
    log_file: Path | None = None,
    env: dict[str, str] | None = None,
    startup_deadline: float = DEFAULT_SPAWN_DEADLINE,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    popen: PopenFactory | None = None,
) -> SpawnOutcome:
    """ロック獲得 → Hub spawn → listen 確認 → ロック解放の 1 サイクル。

    - ロックが他プロセスに取られていれば `ANOTHER_PROCESS_OWNS_LOCK` を返して即 return。
    - ロック獲得後に再 probe して既に listen していれば `ALREADY_LISTENING`。
    - spawn 後 `startup_deadline` 秒以内に listen が始まれば `SPAWNED`。
    - 子プロセスが即死したら `HubStartupError`。
    - 期限までに listen しなければ子を kill して `HubStartupError`。

    `env` を指定すると subprocess に渡す（QGIS プラグインから PYTHONPATH 等を
    注入するために使う）。`None` なら親プロセスの環境を継承。
    """
    cmd = list(command) if command is not None else default_hub_command(port=port)
    popen_fn = popen if popen is not None else subprocess.Popen

    with _exclusive_nonblocking_lock(lock_path) as locked:
        if not locked:
            logger.debug("Hub spawn: lock %s held by another process; skipping", lock_path)
            return SpawnOutcome.ANOTHER_PROCESS_OWNS_LOCK

        if tcp_probe(host, port, timeout=probe_timeout):
            logger.info(
                "Hub spawn: %s:%d is already listening after lock acquisition",
                host,
                port,
            )
            return SpawnOutcome.ALREADY_LISTENING

        popen_kwargs = _build_popen_kwargs(log_file, env)
        logger.info("Hub spawn: launching %s", " ".join(cmd))
        proc = popen_fn(cmd, **popen_kwargs)
        # 親側で OS fd を閉じる（子は独自に継承済み）。ResourceWarning 回避。
        _close_inherited_log_handle(popen_kwargs)

        deadline = time.monotonic() + startup_deadline
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise HubStartupError(f"Hub exited immediately with code {proc.returncode}")
            if tcp_probe(host, port, timeout=probe_timeout):
                logger.info("Hub is listening on %s:%d", host, port)
                # GC で Popen.__del__ が running 警告を出さないよう参照を保持。
                _retain_child(proc)
                return SpawnOutcome.SPAWNED
            time.sleep(poll_interval)

        try:
            proc.kill()
        except OSError:
            logger.debug("Failed to kill hub child after startup timeout", exc_info=True)
        # kill 後も wait されるまでは __del__ 警告が出る可能性があるので保持。
        _retain_child(proc)
        raise HubStartupError(f"Hub did not start listening within {startup_deadline}s")


# ============================================================
# ensure_hub_reachable
# ============================================================


def ensure_hub_reachable(
    *,
    lock_path: Path,
    host: str = DEFAULT_HUB_HOST,
    port: int = DEFAULT_HUB_PORT,
    command: Sequence[str] | None = None,
    log_file: Path | None = None,
    env: dict[str, str] | None = None,
    connect_backoff: Sequence[float] = DEFAULT_CONNECT_BACKOFF,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    startup_deadline: float = DEFAULT_SPAWN_DEADLINE,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    popen: PopenFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> EnsureOutcome:
    """Hub に TCP connect できる状態を保証する。

    1. 先に probe。通れば `ALREADY_LISTENING`。
    2. probe 失敗なら `try_spawn_hub`。
       - SPAWNED / ALREADY_LISTENING → `SPAWNED_BY_US`
       - ANOTHER_PROCESS_OWNS_LOCK → バックオフで再 probe
    3. バックオフ中に listen 開始を検知すれば `SPAWNED_BY_OTHER`。
    4. 全バックオフ消化で繋がらなければ `HubStartupError`。

    `env` を指定すると `try_spawn_hub` 経由で subprocess に渡す。QGIS
    プラグインから PYTHONPATH を伝えるのに使う。
    """
    if tcp_probe(host, port, timeout=probe_timeout):
        return EnsureOutcome.ALREADY_LISTENING

    outcome = try_spawn_hub(
        lock_path=lock_path,
        host=host,
        port=port,
        command=command,
        log_file=log_file,
        env=env,
        startup_deadline=startup_deadline,
        probe_timeout=probe_timeout,
        poll_interval=poll_interval,
        popen=popen,
    )
    if outcome in (SpawnOutcome.SPAWNED, SpawnOutcome.ALREADY_LISTENING):
        return EnsureOutcome.SPAWNED_BY_US

    # ANOTHER_PROCESS_OWNS_LOCK: 他プロセスが spawn 中なのでバックオフで待つ
    for delay in connect_backoff:
        sleep(delay)
        if tcp_probe(host, port, timeout=probe_timeout):
            return EnsureOutcome.SPAWNED_BY_OTHER
    raise HubStartupError(
        f"Hub did not become reachable at {host}:{port} after {len(connect_backoff)} attempts"
    )
