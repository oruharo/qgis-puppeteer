"""Inline QGIS spawn helper（ADR-0004 Phase 1）。

「QGIS 起動 option 自体がテスト対象」のシナリオで、テストコード上に option を
直接書ける context manager を提供する。fixture override や ``qgis_env`` marker と
違い、option がテスト関数の中に出るので intent が明確になる:

    with spawn_qgis(qgis_bin=QGIS_BIN, args=["--clean-canvas"], hub_port=hub_port):
        client.wait_for_worker(timeout_s=60)
        # execute_python は _result の値を返す。コード例外時は WorkerCodeError。
        assert client.execute_python("_result = len(...)") == 0

session-scoped な ``hub_process`` / ``automation_client`` fixture は plugin 側を
そのまま使い、helper は **Worker のみ** spawn する。Hub は session を通じて 1 つ。

設計詳細は ADR-0004（``docs/architecture/0004-inline-qgis-spawn-helper.md``）を参照。
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis_puppeteer.launcher import generate_launch_token, inject_launch_token

if TYPE_CHECKING:
    from pytest_qgis_puppeteer.automation_client import E2EAutomationClient

logger = logging.getLogger("pytest_qgis_puppeteer.spawn")

# dev mode 判定は plugin.py の定義を真として再利用する
_ENV_DEV_MODE = "QPUPPETEER_E2E_USE_RUNNING_QGIS"

# QGIS subprocess に渡す env 変数（plugin._qgis_spawn_env と同じセマンティクス）
_ENV_HUB_PORT = "QPUPPETEER_HUB_PORT"
_ENV_HUB_HOST = "QPUPPETEER_HUB_HOST"
_ENV_TRUSTED_MODE = "QPUPPETEER_TRUSTED_MODE"
_ENV_ALLOW_TEST_HANDLERS = "QPUPPETEER_ALLOW_TEST_HANDLERS"


class WorkerRegisterTimeout(TimeoutError):
    """spawn した QGIS が ``register_timeout_s`` 内に Hub に register しなかった。

    ``TimeoutError`` を継承しているので既存の ``except TimeoutError`` でも拾えるが、
    `pytest_qgis_puppeteer` 由来の問題であることを明示するための独自型。
    """


@dataclass(frozen=True)
class SpawnedWorker:
    """``spawn_qgis`` が起動した Worker の識別情報。

    Attributes:
        instance_id: Hub が割り当てた識別子。``automation_client.use_instance(...)``
            で target を切り替える時に使う。
        pid: OS pid。``list_instances()`` の戻り値と pid 一致で自分の Worker を pin
            するのに使う。
        label: ユーザー指定 or ``worker-{pid}`` 形式の自動採番ラベル。
    """

    instance_id: str
    pid: int
    label: str
    # capture した stdout/stderr の生テキスト（drain thread が積む）。
    # `_captured_stdout` / `_captured_stderr` という命名は ADR-0004 §"subprocess の
    # stdout/stderr" に揃える。frozen dataclass なので list を field default に。
    _captured_stdout: list[str] = field(default_factory=list, compare=False)
    _captured_stderr: list[str] = field(default_factory=list, compare=False)

    def captured_stdout(self) -> list[str]:
        """drain thread が積んだ subprocess の stdout 行 snapshot を返す（ADR-0004 P2）。

        snapshot は Python list のコピーで、worker がまだ alive でも安全に呼べる
        （GIL 下で list iteration は atomic）。失敗テストの diagnostic bundle に
        ``spawn_stdout.log`` として書き出す用途。
        """
        return list(self._captured_stdout)

    def captured_stderr(self) -> list[str]:
        """drain thread が積んだ subprocess の stderr 行 snapshot を返す（ADR-0004 P2）。

        QGIS は GDAL/PROJ の警告等を大量に stderr に出すので、register timeout や
        モジュール loading エラーの調査では本 channel が一次情報になる。
        """
        return list(self._captured_stderr)


@contextmanager
def spawn_qgis(
    *,
    hub_port: int,
    automation_client: E2EAutomationClient,
    qgis_bin: str | Path | None = None,
    args: Sequence[str] | None = None,
    command: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    register_timeout_s: float = 60.0,
    graceful_shutdown_timeout_s: float = 15.0,
    label: str | None = None,
) -> Iterator[SpawnedWorker]:
    """Test 内で直接 QGIS を起動・停止する context manager。

    起動 option がテスト対象になるシナリオ向け（ADR-0004）。``qgis_bin`` /
    ``command`` は排他で、両方指定すると ``ValueError``。

    Args:
        hub_port: 既存 session の ``hub_port`` fixture を渡す。
        automation_client: 既存 session の ``automation_client`` fixture を渡す。
            register 検出（pid 一致 polling）に使う。
        qgis_bin: QGIS 実行ファイルパス。``command`` 指定時は不要。
        args: ``qgis_bin`` 用の追加引数。
        command: 起動コマンド全体（host 独自 launcher 用）。``qgis_bin``/``args``
            と排他。
        env: 追加環境変数。``QPUPPETEER_HUB_PORT`` 等は内部で merge され、helper
            による値が常に勝つ（別 port を渡すと register 出来ないため）。
        register_timeout_s: Worker が Hub に register するまでの待機（秒）。
        graceful_shutdown_timeout_s: terminate → kill のタイムアウト（秒）。
        label: ユーザー指定ラベル。未指定時は ``worker-{pid}`` で自動採番。

    Yields:
        ``SpawnedWorker``: instance_id / pid / label を持つ識別オブジェクト。

    Raises:
        ValueError: 引数の排他違反（``qgis_bin``/``args`` と ``command`` 併用、
            または両方未指定）。
        WorkerRegisterTimeout: ``register_timeout_s`` 内に Worker が register
            しなかった。
        pytest.skip.Exception: dev mode（``QPUPPETEER_E2E_USE_RUNNING_QGIS=1``）下で
            呼ばれた場合、テストを skip する。
    """
    # dev mode は spawn の前提と矛盾する。silently no-op にすると assert が既存
    # Worker に流れて誤検知になるため、明示 skip する（ADR-0004 §"dev mode"）。
    if os.environ.get(_ENV_DEV_MODE) == "1":
        import pytest

        pytest.skip(
            "spawn_qgis() cannot be used in dev mode "
            f"({_ENV_DEV_MODE}=1). The test relies on spawning a fresh QGIS "
            "with specific startup options, which conflicts with reusing an "
            "already-running QGIS."
        )

    cmd = _build_command(qgis_bin=qgis_bin, args=args, command=command)
    # ADR-0005 D6: launch_token を発番・注入して決定的に相関する（pid diff の
    # heuristic を廃止。pid 再利用 / 並列 spawn race に強い）。
    launch_token = generate_launch_token()
    spawn_env = _build_env(base=os.environ, hub_port=hub_port, user_env=env)
    spawn_env = inject_launch_token(spawn_env, launch_token, label=label)

    logger.info(
        "spawn_qgis: launching %s (launch_token=%s)",
        " ".join(str(c) for c in cmd),
        launch_token,
    )
    proc = subprocess.Popen(
        cmd,
        env=spawn_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    captured_stdout: list[str] = []
    captured_stderr: list[str] = []
    drain_threads = _start_drain_threads(proc, captured_stdout, captured_stderr)

    try:
        instance_id = _wait_for_worker_by_token(
            automation_client,
            launch_token=launch_token,
            timeout_s=register_timeout_s,
        )
    except WorkerRegisterTimeout:
        # register 失敗時はサブプロセスを片付けてから raise（dangling 防止）
        _terminate_worker(proc, graceful_timeout_s=graceful_shutdown_timeout_s)
        _join_drain_threads(drain_threads, timeout_s=2.0)
        raise

    effective_label = label or f"worker-{proc.pid}"
    worker = SpawnedWorker(
        instance_id=instance_id,
        pid=proc.pid,
        label=effective_label,
        _captured_stdout=captured_stdout,
        _captured_stderr=captured_stderr,
    )
    logger.info(
        "spawn_qgis: registered instance_id=%s pid=%d label=%s",
        instance_id,
        proc.pid,
        effective_label,
    )

    try:
        yield worker
    finally:
        _shutdown_worker_best_effort(
            proc,
            automation_client=automation_client,
            instance_id=instance_id,
            graceful_timeout_s=graceful_shutdown_timeout_s,
        )
        _join_drain_threads(drain_threads, timeout_s=2.0)


# ============================================================
# 内部ヘルパ
# ============================================================


def _build_command(
    *,
    qgis_bin: str | Path | None,
    args: Sequence[str] | None,
    command: Sequence[str] | None,
) -> list[str]:
    """``qgis_bin`` / ``args`` と ``command`` の排他チェック + コマンド組み立て。

    plugin.py の ``_resolve_qgis_command`` / ``_resolve_qgis_args`` の排他規則と
    整合させる（ADR-0003 / ADR-0004）。
    """
    if command is not None:
        if qgis_bin is not None or args is not None:
            raise ValueError("spawn_qgis: 'command' is mutually exclusive with 'qgis_bin' / 'args'")
        cmd_list = [str(c) for c in command]
        if not cmd_list:
            raise ValueError("spawn_qgis: 'command' must not be empty")
        return cmd_list

    if qgis_bin is None:
        raise ValueError("spawn_qgis: either 'command' or 'qgis_bin' must be specified")
    extra = list(args) if args else []
    return [str(qgis_bin), *extra]


def _build_env(
    *,
    base: Mapping[str, str],
    hub_port: int,
    user_env: Mapping[str, str] | None,
) -> dict[str, str]:
    """spawn 用 env を組み立てる。

    plugin._qgis_spawn_env と同じ意味の自動注入をしつつ、ユーザー指定 env を
    上に merge する。``QPUPPETEER_HUB_PORT`` だけは helper が常に勝つ（ユーザーが
    間違えた port を渡すと register 出来ないため）。
    """
    env = dict(base)
    if user_env:
        # ユーザー指定が QPUPPETEER_HUB_PORT を含んでいても上書きしない
        for k, v in user_env.items():
            env[k] = v
    # helper による上書き（ユーザー指定よりも後に書くことで helper を勝たせる）
    env[_ENV_HUB_PORT] = str(hub_port)
    env[_ENV_HUB_HOST] = "127.0.0.1"
    env.setdefault(_ENV_TRUSTED_MODE, "1")
    env.setdefault(_ENV_ALLOW_TEST_HANDLERS, "1")
    return env


def _start_drain_threads(
    proc: subprocess.Popen[bytes],
    stdout_buf: list[str],
    stderr_buf: list[str],
) -> list[threading.Thread]:
    """stdout / stderr を drain して buffer 飽和による hang を防ぐ。

    QGIS は GDAL/PROJ ログを大量に stderr に吐くので、PIPE の buffer
    （OS によって 4KB〜64KB）が埋まると writev が block して QGIS 全体が
    hang する。daemon thread で延々と read して list に積むだけ（test runner
    終了時に道連れで止まればよい設計）。
    """
    threads: list[threading.Thread] = []
    if proc.stdout is not None:
        t_out = threading.Thread(
            target=_drain_pipe,
            args=(proc.stdout, stdout_buf, "stdout"),
            daemon=True,
            name=f"spawn_qgis-stdout-{proc.pid}",
        )
        t_out.start()
        threads.append(t_out)
    if proc.stderr is not None:
        t_err = threading.Thread(
            target=_drain_pipe,
            args=(proc.stderr, stderr_buf, "stderr"),
            daemon=True,
            name=f"spawn_qgis-stderr-{proc.pid}",
        )
        t_err.start()
        threads.append(t_err)
    return threads


def _drain_pipe(stream, buf: list[str], stream_name: str) -> None:
    """PIPE を line ごとに read して decode & buffer に append。

    EOF（プロセス終了 → close）で抜ける。decode 失敗は ``replace`` で潰す
    （diagnostic 用途で text として残せれば OK）。
    """
    try:
        for raw in iter(stream.readline, b""):
            # drain thread は何があっても落とさない
            with contextlib.suppress(Exception):  # noqa: BLE001
                buf.append(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 同上
        logger.debug("drain thread %s exited with exception", stream_name, exc_info=True)
    finally:
        with contextlib.suppress(Exception):  # noqa: BLE001
            stream.close()


def _join_drain_threads(threads: list[threading.Thread], *, timeout_s: float) -> None:
    """drain thread の終了を待つ（best-effort、daemon なので無理に止めない）。"""
    for t in threads:
        t.join(timeout=timeout_s)


def _wait_for_worker_by_token(
    automation_client: E2EAutomationClient,
    *,
    launch_token: str,
    timeout_s: float,
    poll_interval_s: float = 0.25,
) -> str:
    """自分の launch_token の Worker が **使える** ようになるまで待つ。

    ADR-0005 D6: pid diff heuristic を廃止し、helper が注入した
    ``launch_token`` で **決定的** に相関する。pid 再利用や xdist 並列 spawn の
    race に左右されない（U9）。

    待つ条件は `E2EAutomationClient.wait_for_ready` に委譲する。``list_instances``
    は heartbeat が途絶えた instance も ``state="unresponsive"`` で返すように
    なったので、register されたことだけを見ると「登録はできたが GUI スレッドが
    塞がっていて何も処理できない」個体を掴んでしまう。
    """
    try:
        return automation_client.wait_for_ready(
            launch_token, timeout_s=timeout_s, poll_interval_s=poll_interval_s
        )
    except TimeoutError as e:
        raise WorkerRegisterTimeout(
            f"QGIS subprocess (launch_token={launch_token}) did not register "
            f"to Hub and answer a command within {timeout_s}s"
        ) from e


def _shutdown_worker_best_effort(
    proc: subprocess.Popen[bytes],
    *,
    automation_client: E2EAutomationClient,
    instance_id: str,
    graceful_timeout_s: float,
) -> None:
    """spawn した QGIS と、起動に使ったプロセスを片付ける。

    止める相手は ``proc`` ではなく、Hub に登録されている Worker の pid（QGIS
    本体）。``command`` がランチャーで、QGIS を起こしてすぐ終わる場合、``proc``
    はもう居ないので、``proc`` だけを見ると QGIS が残る（#9）。

    1. Worker がまだ一覧に居れば `stop_registered_worker` で閉じる（閉じるよう
       頼み、消えなければその pid だけを強制終了）
    2. ``proc`` が残っていれば従来どおり止める。``qgis_bin`` を直接起こした場合は
       1 で同じプロセスが終わっているので何もしない。Worker が一覧に居ない
       （登録が外れた、Hub に聞けない）場合はここが唯一の片付けになる
    3. 失敗は warning ログだけ出す（次のテストに進む方を優先）
    """
    worker_pid: int | None = None
    try:
        for info in automation_client.list_instances():
            if info.instance_id == instance_id:
                worker_pid = info.pid
                break
    except Exception:  # noqa: BLE001 - teardown は best-effort
        logger.warning(
            "list_instances() failed during spawn_qgis teardown (instance_id=%s)",
            instance_id,
            exc_info=True,
        )

    if worker_pid is not None:
        stop_registered_worker(
            automation_client,
            instance_id=instance_id,
            pid=worker_pid,
            graceful_timeout_s=graceful_timeout_s,
        )

    if proc.poll() is None:
        _terminate_worker(proc, graceful_timeout_s=graceful_timeout_s)


# QGIS に自分で閉じてもらうコード。プロジェクトの変更は捨てる（保存の確認
# ダイアログで止まらないように）。``QTimer`` で 1 拍遅らせるのは、この
# execute_python の応答を返してから閉じるため。
#
# ``QgsApplication.exitQgis()`` は使わない。あれは終了処理の最後に呼ぶ後片付け
# （プロバイダの破棄など）で、GUI が動いている最中に呼ぶと QGIS が落ちる。
_CLOSE_QGIS_CODE = """\
from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtCore import QTimer
from qgis.utils import iface

QgsProject.instance().setDirty(False)
if iface is not None and iface.mainWindow() is not None:
    QTimer.singleShot(0, iface.mainWindow().close)
else:
    QTimer.singleShot(0, QgsApplication.instance().quit)
"""


def stop_registered_worker(
    automation_client: E2EAutomationClient,
    *,
    instance_id: str,
    pid: int,
    graceful_timeout_s: float,
) -> None:
    """Hub に登録されている QGIS を閉じる（best-effort、例外は出さない）。

    1. QGIS の中でメインウィンドウを閉じるよう頼む（`_CLOSE_QGIS_CODE`）
    2. ``pid`` が消えるのを ``graceful_timeout_s`` まで待つ
    3. 消えなければ ``pid`` だけを強制終了する

    3 で子プロセスごと（Windows の ``taskkill /T``）止めないのは、子に先に合図が
    届いて、本体が止まらないまま固まった例があるため。外部所有者モードの QGIS
    は Hub を起こさないので、止めるべき子は無い。
    """
    if not _pid_alive(pid):
        return
    try:
        automation_client.execute_python(_CLOSE_QGIS_CODE, instance=instance_id)
    except Exception:  # noqa: BLE001 - 応答しない QGIS は 3 で止める
        logger.warning(
            "Could not ask QGIS to close (instance_id=%s pid=%d); killing it",
            instance_id,
            pid,
            exc_info=True,
        )
    else:
        if _wait_pid_dead(pid, timeout_s=graceful_timeout_s):
            logger.info("QGIS closed (instance_id=%s pid=%d)", instance_id, pid)
            return
        logger.warning(
            "QGIS did not close within %.1fs (instance_id=%s pid=%d); killing it",
            graceful_timeout_s,
            instance_id,
            pid,
        )
    _force_kill_pid(pid)
    if not _wait_pid_dead(pid, timeout_s=2.0):
        logger.warning("QGIS still alive after force kill (pid=%d)", pid)


def _force_kill_pid(pid: int) -> None:
    """``pid`` だけを強制終了する（子プロセスは巻き込まない）。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            check=False,
            capture_output=True,
        )
        return
    import signal as _signal

    try:
        os.kill(pid, _signal.SIGKILL)
    except OSError:
        logger.debug("SIGKILL(%d) raised", pid, exc_info=True)


def _pid_alive(pid: int) -> bool:
    """pid のプロセスがまだ生きているかを cross-platform に判定する（psutil 不使用）。

    posix: ``os.kill(pid, 0)`` が ``ProcessLookupError`` なら死亡。
    Windows: ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` + ``GetExitCodeProcess``
    で exit code が ``STILL_ACTIVE`` (259) なら生存とみなす。
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
    """pid のプロセスが消えるまで poll する。timeout 内に消えれば True を返す。"""
    deadline = time.monotonic() + timeout_s
    while True:
        if not _pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return not _pid_alive(pid)
        time.sleep(poll_interval_s)


def _terminate_worker(proc: subprocess.Popen[bytes], *, graceful_timeout_s: float) -> None:
    """proc を graceful terminate → timeout 後 force kill する。

    Phase 1 は plugin._terminate_pid_tree と同等のロジック（固定 sleep 含む）。
    Phase 4 で ``proc.wait(timeout=)`` ベースに切替予定（ADR-0004 §"Windows での
    graceful kill"）。
    """
    if proc.poll() is not None:
        return  # すでに死んでいる

    if sys.platform != "win32":
        import signal as _signal

        try:
            os.kill(proc.pid, _signal.SIGTERM)
        except OSError:
            logger.debug("SIGTERM raised (pid=%d)", proc.pid, exc_info=True)
        try:
            proc.wait(timeout=graceful_timeout_s)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.kill(proc.pid, _signal.SIGKILL)
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("force kill failed (pid=%d)", proc.pid, exc_info=True)
        return

    # Windows: taskkill /T で子プロセスごと
    logger.info("Terminating QGIS pid=%d (graceful)", proc.pid)
    subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/T"],
        check=False,
        capture_output=True,
    )
    try:
        proc.wait(timeout=graceful_timeout_s)
        return
    except subprocess.TimeoutExpired:
        pass
    result = subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        logger.info("Force-killed QGIS pid=%d", proc.pid)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        logger.warning("Process still alive after force kill (pid=%d)", proc.pid)


def spawn_qgis_fixture(
    *,
    scope: str = "module",
    name: str | None = None,
    yield_client: bool = False,
    qgis_bin: str | Path | None = None,
    args: Sequence[str] | None = None,
    command: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    register_timeout_s: float = 60.0,
    graceful_shutdown_timeout_s: float = 15.0,
    label: str | None = None,
):
    """``spawn_qgis()`` を任意 scope の pytest fixture として生やす factory（ADR-0004 P3）。

    `with spawn_qgis(...)` を毎テストで書くより、module / class / session 単位で
    1 つの fresh QGIS を共有したいシナリオで便利。

    Args:
        scope: 生成 fixture の scope（``"function"`` / ``"class"`` / ``"module"`` /
            ``"session"``）。``"module"`` がよく使う想定。
        name: fixture 名。``None`` の場合は呼び出し側で代入した変数名がそのまま
            fixture 名になる（pytest の通常の挙動）。
        yield_client: ``True`` なら ``automation_client`` を yield する（fixture 内で
            ``use_instance(worker.instance_id)`` 自動セット、teardown で復元）。
            既定 ``False`` で ``SpawnedWorker`` を yield（呼び出し側が ``use_instance``
            を呼ぶ）。
        qgis_bin / args / command / env / register_timeout_s /
            graceful_shutdown_timeout_s / label: ``spawn_qgis()`` と同じ。

    Returns:
        ``pytest.fixture`` でラップされた fixture 関数。``conftest.py`` で代入する。

    Examples:
        >>> # conftest.py
        >>> module_qgis = spawn_qgis_fixture(
        ...     scope="module",
        ...     args=["--profile", "ci-test"],
        ... )
        >>>
        >>> # test_x.py
        >>> def test_x(module_qgis, automation_client):
        ...     automation_client.use_instance(module_qgis.instance_id)
        ...     ...

        >>> # yield_client=True 版
        >>> module_qgis_client = spawn_qgis_fixture(
        ...     scope="module", yield_client=True
        ... )
        >>> def test_y(module_qgis_client):
        ...     # 既に use_instance 済みなのでそのまま使える
        ...     module_qgis_client.execute_python("...")
    """
    import pytest

    @pytest.fixture(scope=scope, name=name)
    def _fixture(
        hub_port: int,
        automation_client: E2EAutomationClient,
    ) -> Iterator[Any]:
        spawn_kwargs: dict[str, Any] = {
            "hub_port": hub_port,
            "automation_client": automation_client,
            "env": env,
            "register_timeout_s": register_timeout_s,
            "graceful_shutdown_timeout_s": graceful_shutdown_timeout_s,
            "label": label,
        }
        if command is not None:
            spawn_kwargs["command"] = command
        else:
            spawn_kwargs["qgis_bin"] = qgis_bin
            spawn_kwargs["args"] = args

        with spawn_qgis(**spawn_kwargs) as worker:
            if yield_client:
                saved = automation_client.get_default_instance()
                automation_client.use_instance(worker.instance_id)
                try:
                    yield automation_client
                finally:
                    automation_client.use_instance(saved)
            else:
                yield worker

    return _fixture


__all__ = [
    "SpawnedWorker",
    "WorkerRegisterTimeout",
    "spawn_qgis",
    "spawn_qgis_fixture",
]
