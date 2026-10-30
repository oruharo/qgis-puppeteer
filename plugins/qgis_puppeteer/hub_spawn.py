"""Hub が無ければ起こす（Qt 非依存）。

ADR-0001 §4 "Hub の起動競合回避（親主導の spawn 確認モデル）" を実装する。
「Hub がいるか」は Hub ファイルで決める（``qgis_puppeteer.hub_probe.find_live_hub``：
ファイルがある・挨拶が通る）。

1. 生きた Hub がいれば何もしない
2. いなければ、Hub ファイルの隣の lock を取って、もう一度確かめてから Hub を起こし、
   Hub ファイルが現れて挨拶が通るまで待つ
3. lock を別のプロセスが持っていれば（そちらが起こしている）、生きた Hub が現れるまで待つ

Hub は port 0（OS が選ぶ）で待ち受けるので、ポートはぶつからない。同じ Hub ファイルを
使う Hub が 2 つ起きないようにするのは lock と、Hub 自身の起動時の確かめ
（生きた Hub がいれば ``HUB_EXIT_ALREADY_RUNNING`` で降りる）。

## 公開 API

- ``ensure_hub(...)`` — 生きた Hub を返す。いなければ起こす
- ``try_spawn_hub(...)`` — lock → 確かめ → 起こす → 待つ の 1 回
- ``default_hub_command(hub_file=..., python=...)`` — ``python -m qgis_puppeteer.hub``
- ``hub_lock_path`` / ``hub_log_path`` / ``hub_spawn_log_path`` — Hub ファイルの隣のパス
- ``HubStartupError`` — 最後まで生きた Hub が見つからなかった

## 設計原則

- Qt に依存しない（pytest から直接呼べる）
- 副作用（Popen・Hub の確かめ・sleep・子を止める）は注入できる（ユニットテスト用）
- Windows / POSIX 両対応（``msvcrt.locking`` / ``fcntl.flock``）
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qgis_puppeteer.hub_file import HubFileInfo
from qgis_puppeteer.hub_probe import find_live_hub

logger = logging.getLogger("qgis_puppeteer.hub_spawn")

# ============================================================
# 定数
# ============================================================

# Hub の終了コード（``python -m qgis_puppeteer.hub``）
HUB_EXIT_OK: int = 0
HUB_EXIT_BIND_FAILED: int = 1
HUB_EXIT_HUB_FILE_FAILED: int = 2
# 同じ Hub ファイルで、生きた Hub がもういる（上書きせずに降りた）
HUB_EXIT_ALREADY_RUNNING: int = 3

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
# Hub の確かめ 1 回（接続・挨拶）にかける時間
DEFAULT_PROBE_TIMEOUT: float = 1.0
# 起こしてから生きた Hub になるまで待つ時間（QGIS の Python の起動を含む）
DEFAULT_SPAWN_DEADLINE: float = 15.0
DEFAULT_POLL_INTERVAL: float = 0.1


# ============================================================
# 例外・Enum
# ============================================================


class HubStartupError(Exception):
    """Hub を起こせなかった、または生きた Hub が現れなかった。"""


class SpawnOutcome(Enum):
    """try_spawn_hub の結果。"""

    SPAWNED = "spawned"
    """この呼び出しで Hub を起こし、生きた Hub になった。"""

    ALREADY_RUNNING = "already_running"
    """lock を取ってから確かめると、もう生きた Hub がいた。"""

    ANOTHER_PROCESS_OWNS_LOCK = "another_process_owns_lock"
    """別のプロセスが lock を持っていた（何もしていない）。"""


class EnsureOutcome(Enum):
    """ensure_hub の結果。"""

    ALREADY_RUNNING = "already_running"
    """最初の確かめで、もう生きた Hub がいた。"""

    SPAWNED_BY_US = "spawned_by_us"
    """このプロセスが Hub を起こした（lock の後で生きた Hub が見つかった場合も含む）。"""

    SPAWNED_BY_OTHER = "spawned_by_other"
    """別のプロセスが起こしている間待って、生きた Hub が現れた。"""


@dataclass(frozen=True)
class HubReady:
    """ensure_hub の結果：どうやって見つけたかと、その Hub の Hub ファイルの中身。"""

    outcome: EnsureOutcome
    info: HubFileInfo


# Hub を確かめる関数（``find_live_hub`` と同じ形）
FindHub = Callable[..., "HubFileInfo | None"]
# Popen factory の型（subprocess.Popen 互換）
PopenFactory = Callable[..., "subprocess.Popen[bytes]"]


# ============================================================
# Hub ファイルの隣のパス
# ============================================================


def hub_lock_path(hub_file: str | os.PathLike[str]) -> Path:
    """Hub を起こすときの lock（``hub.json`` なら ``hub.json.lock``）。

    Hub ファイルごとに 1 つなので、ユーザーごと・pytest の session ごとに分かれる。
    """
    path = Path(hub_file)
    return path.with_name(path.name + ".lock")


def hub_log_path(hub_file: str | os.PathLike[str]) -> Path:
    """Hub のログ（``hub.json`` なら ``hub.log``）。Hub ごとに分かれる。"""
    return Path(hub_file).with_suffix(".log")


def hub_spawn_log_path(hub_file: str | os.PathLike[str]) -> Path:
    """起こした Hub の標準出力・標準エラーの受け皿（``hub.spawn.log``）。

    Hub のログ（``hub_log_path``）が回り始める前の失敗（import の失敗など）を拾う。
    """
    return Path(hub_file).with_suffix(".spawn.log")


# ============================================================
# 排他ロック（Windows / POSIX 両対応）
# ============================================================


@contextmanager
def _exclusive_nonblocking_lock(path: Path) -> Iterator[bool]:
    """ファイルに対する非ブロッキング排他ロック。

    取得できれば True、他プロセスが保持中なら False を yield する。
    スコープ退出時に、取得していた場合のみロックを解放する。
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
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


def default_hub_command(
    *, hub_file: str | os.PathLike[str], python: str | None = None
) -> list[str]:
    """``python -m qgis_puppeteer.hub --hub-file <hub_file>`` を起こすコマンド。"""
    return [
        python or sys.executable,
        "-m",
        "qgis_puppeteer.hub",
        "--hub-file",
        os.fspath(hub_file),
    ]


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
        # 自分のプロセスグループで起こす（時間切れのとき、グループごと止める）
        kwargs["start_new_session"] = True
    return kwargs


def kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """起こした Hub をプロセスの木ごと止める。

    Windows の既定の起こし方は ``python-qgis-ltr.bat`` なので、``proc`` は
    ``cmd.exe`` で、Hub の python はその子（孫）になる。``proc.kill()`` では
    ``cmd.exe`` しか止まらない。

    - Windows：``taskkill /T /F`` で木ごと
    - POSIX：Hub は自分のプロセスグループで起こしてあるので、グループごと
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("taskkill failed (pid=%s)", proc.pid, exc_info=True)
    else:
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with suppress(OSError):
        proc.kill()


# ============================================================
# try_spawn_hub
# ============================================================


def try_spawn_hub(
    *,
    hub_file: str | os.PathLike[str],
    command: Sequence[str] | None = None,
    log_file: Path | None = None,
    env: dict[str, str] | None = None,
    startup_deadline: float = DEFAULT_SPAWN_DEADLINE,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    popen: PopenFactory | None = None,
    find_hub: FindHub = find_live_hub,
    kill_tree: Callable[[subprocess.Popen[bytes]], None] = kill_process_tree,
) -> tuple[SpawnOutcome, HubFileInfo | None]:
    """lock → 確かめ → Hub を起こす → 生きた Hub になるまで待つ の 1 回。

    - lock を別のプロセスが持っていれば ``(ANOTHER_PROCESS_OWNS_LOCK, None)``。
    - lock を取ってから確かめて生きた Hub がいれば ``(ALREADY_RUNNING, info)``。
    - 起こして ``startup_deadline`` 秒以内に生きた Hub になれば ``(SPAWNED, info)``。
    - 子が生きた Hub にならずに終わったら ``HubStartupError``。
    - 期限までに生きた Hub にならなければ、子を木ごと止めて ``HubStartupError``。

    `env` を指定すると subprocess に渡す（QGIS プラグインから sys.path 等を
    注入するために使う）。`None` なら親プロセスの環境を継承。
    """
    path = Path(hub_file)
    cmd = list(command) if command is not None else default_hub_command(hub_file=path)
    popen_fn = popen if popen is not None else subprocess.Popen
    lock_path = hub_lock_path(path)

    with _exclusive_nonblocking_lock(lock_path) as locked:
        if not locked:
            logger.debug("Hub spawn: lock %s held by another process; skipping", lock_path)
            return SpawnOutcome.ANOTHER_PROCESS_OWNS_LOCK, None

        info = find_hub(path, timeout=probe_timeout)
        if info is not None:
            logger.info("Hub spawn: a live Hub appeared at %s after taking the lock", path)
            return SpawnOutcome.ALREADY_RUNNING, info

        popen_kwargs = _build_popen_kwargs(log_file, env)
        logger.info("Hub spawn: launching %s", " ".join(cmd))
        proc = popen_fn(cmd, **popen_kwargs)
        # 親側で OS fd を閉じる（子は独自に継承済み）。ResourceWarning 回避。
        _close_inherited_log_handle(popen_kwargs)
        # GC で Popen.__del__ が running 警告を出さないよう参照を保持。
        _retain_child(proc)

        deadline = time.monotonic() + startup_deadline
        while True:
            info = find_hub(path, timeout=probe_timeout)
            if info is not None:
                logger.info("Hub is running: %s", ", ".join(info.addresses))
                return SpawnOutcome.SPAWNED, info
            code = proc.poll()
            if code is not None:
                # 終わる直前に Hub ファイルを書き終えたかもしれない。もう一度だけ確かめる
                info = find_hub(path, timeout=probe_timeout)
                if info is not None:
                    return SpawnOutcome.SPAWNED, info
                raise HubStartupError(
                    f"Hub exited with code {code} before it was running "
                    f"(see {log_file or hub_log_path(path)})"
                )
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval)

        kill_tree(proc)
        raise HubStartupError(
            f"Hub did not start within {startup_deadline}s (no live Hub at {path}; "
            f"see {log_file or hub_log_path(path)})"
        )


# ============================================================
# ensure_hub
# ============================================================


def ensure_hub(
    *,
    hub_file: str | os.PathLike[str],
    command: Sequence[str] | None = None,
    log_file: Path | None = None,
    env: dict[str, str] | None = None,
    connect_backoff: Sequence[float] = DEFAULT_CONNECT_BACKOFF,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    startup_deadline: float = DEFAULT_SPAWN_DEADLINE,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    popen: PopenFactory | None = None,
    find_hub: FindHub = find_live_hub,
    sleep: Callable[[float], None] = time.sleep,
    kill_tree: Callable[[subprocess.Popen[bytes]], None] = kill_process_tree,
) -> HubReady:
    """``hub_file`` の生きた Hub を返す。いなければ起こす。

    1. 確かめて生きた Hub がいれば ``ALREADY_RUNNING``。
    2. いなければ ``try_spawn_hub``。
       - SPAWNED / ALREADY_RUNNING → ``SPAWNED_BY_US``
       - ANOTHER_PROCESS_OWNS_LOCK → 間を空けて確かめ直す
    3. 待つ間に生きた Hub が現れれば ``SPAWNED_BY_OTHER``。
    4. 待ちきっても現れなければ ``HubStartupError``。
    """
    path = Path(hub_file)
    info = find_hub(path, timeout=probe_timeout)
    if info is not None:
        return HubReady(EnsureOutcome.ALREADY_RUNNING, info)

    outcome, info = try_spawn_hub(
        hub_file=path,
        command=command,
        log_file=log_file,
        env=env,
        startup_deadline=startup_deadline,
        probe_timeout=probe_timeout,
        poll_interval=poll_interval,
        popen=popen,
        find_hub=find_hub,
        kill_tree=kill_tree,
    )
    if info is not None:
        return HubReady(EnsureOutcome.SPAWNED_BY_US, info)

    # ANOTHER_PROCESS_OWNS_LOCK: 他プロセスが起こしているので待つ
    del outcome
    for delay in connect_backoff:
        sleep(delay)
        info = find_hub(path, timeout=probe_timeout)
        if info is not None:
            return HubReady(EnsureOutcome.SPAWNED_BY_OTHER, info)
    raise HubStartupError(
        f"No live Hub appeared at {path} after {len(connect_backoff)} attempts "
        "(another process was starting it)"
    )
