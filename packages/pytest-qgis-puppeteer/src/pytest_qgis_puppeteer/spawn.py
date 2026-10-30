"""Inline QGIS spawn helper（ADR-0004 Phase 1）。

「QGIS 起動 option 自体がテスト対象」のシナリオで、テストコード上に option を
直接書ける context manager を提供する。fixture override や ``qgis_env`` marker と
違い、option がテスト関数の中に出るので intent が明確になる:

    with spawn_qgis(qgis_bin=QGIS_BIN, args=["--clean-canvas"], automation_client=client):
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

from qgis_puppeteer.client import RegistrationRejectedError, find_rejection
from qgis_puppeteer.hub_state import LABEL_MAX_LENGTH, validate_label
from qgis_puppeteer.launcher import (
    ENV_LAUNCH_TOKEN,
    ENV_WORKER_LABEL,
    generate_launch_token,
    inject_launch_token,
)
from qgis_puppeteer.protocol import RejectedRegistration

if TYPE_CHECKING:
    from pytest_qgis_puppeteer.automation_client import E2EAutomationClient

logger = logging.getLogger("pytest_qgis_puppeteer.spawn")

# dev mode 判定は plugin.py の定義を真として再利用する
_ENV_DEV_MODE = "QPUPPETEER_E2E_USE_RUNNING_QGIS"

# QGIS subprocess に渡す env 変数
_ENV_HUB_PREFIX = "QPUPPETEER_HUB_"
_ENV_HUB_FILE = "QPUPPETEER_HUB_FILE"
_ENV_TRUSTED_MODE = "QPUPPETEER_TRUSTED_MODE"
_ENV_ALLOW_TEST_HANDLERS = "QPUPPETEER_ALLOW_TEST_HANDLERS"


class WorkerRegisterTimeout(TimeoutError):
    """spawn した QGIS が ``register_timeout_s`` 内に Hub に register しなかった。

    ``TimeoutError`` を継承しているので既存の ``except TimeoutError`` でも拾えるが、
    `pytest_qgis_puppeteer` 由来の問題であることを明示するための独自型。
    """


class WorkerRegisterRejected(WorkerRegisterTimeout):
    """spawn した QGIS の登録を Hub が断った（#13）。

    断られた Worker はつなぎ直さないので、``register_timeout_s`` を待たずに出す。
    ``WorkerRegisterTimeout`` の下位型なので、「QGIS が登録されなかった」として
    既存の ``except`` でも拾える。``rejection`` に Hub の記録（理由と QGIS の
    pid）が入る。出す前に、その QGIS は止めてある。
    """

    def __init__(self, message: str, *, rejection: RejectedRegistration) -> None:
        super().__init__(message)
        self.rejection = rejection


class WorkerLaunchFailed(WorkerRegisterTimeout):
    """QGIS が登録する前に、起動のコマンドが 0 以外の終了コードで終わった。

    ランチャー（bat など）が構成の誤りで止まると、QGIS は来ないので、
    ``register_timeout_s`` を待たずに出す。0 で終わったときは待ち続ける
    （``start`` で QGIS を起こしてすぐ 0 で終わるランチャーがあるため）。
    ``WorkerRegisterTimeout`` の下位型なので、「QGIS が登録されなかった」として
    既存の ``except`` でも拾える。``returncode`` に終了コード、``stderr_tail`` に
    stderr の最後の数行（空行を除く）が入り、メッセージにも載る。
    """

    def __init__(self, message: str, *, returncode: int, stderr_tail: list[str]) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


# 失敗のメッセージに載せる stderr の行数
_STDERR_TAIL_LINES = 10


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
        automation_client: 既存 session の ``automation_client`` fixture を渡す。
            register 検出（pid 一致 polling）に使う。QGIS はこの client と同じ
            Hub ファイル（``automation_client.hub_file``）の Hub につなぐ。
        qgis_bin: QGIS 実行ファイルパス。``command`` 指定時は不要。
        args: ``qgis_bin`` 用の追加引数。
        command: 起動コマンド全体（host 独自 launcher 用）。``qgis_bin``/``args``
            と排他。
        env: 追加環境変数。``QPUPPETEER_HUB_FILE`` は helper による値が常に勝つ
            （別の Hub ファイルを渡すと、この session の Hub に register 出来ないため）。
        register_timeout_s: Worker が Hub に register するまでの待機（秒）。
        graceful_shutdown_timeout_s: terminate → kill のタイムアウト（秒）。
        label: ユーザー指定ラベル。未指定時は ``worker-{pid}`` で自動採番。

    Yields:
        ``SpawnedWorker``: instance_id / pid / label を持つ識別オブジェクト。

    Raises:
        ValueError: 引数の排他違反（``qgis_bin``/``args`` と ``command`` 併用、
            または両方未指定）。
        WorkerRegisterTimeout: ``register_timeout_s`` 内に Worker が register
            しなかった。起動のコマンドの stderr があれば、その最後の数行が
            メッセージに載る。
        WorkerLaunchFailed: Worker が register する前に、起動のコマンドが 0 以外の
            終了コードで終わった（``register_timeout_s`` を待たない）。
        WorkerRegisterRejected: Worker の登録を Hub が断った。
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
    spawn_env = _build_env(
        base=os.environ,
        hub_file=automation_client.hub_file,
        user_env=env,
    )
    spawn_env = inject_launch_token(spawn_env, launch_token, label=label)
    # Hub が断るラベルなら、QGIS を起こす前に知らせる（#13）。起こしてから
    # 断られると、待ったあとに「登録されなかった」としか分からない
    _check_worker_label(spawn_env.get(ENV_WORKER_LABEL))

    logger.info(
        "spawn_qgis: launching %s (launch_token=%s)",
        " ".join(str(c) for c in cmd),
        launch_token,
    )
    # 起動した時刻（#18）。待ちが切れたとき、これより後に作られた子孫だけを
    # 自分の起こした QGIS とみなす。時計の粒度の分だけ少し前に取る
    launched_at = time.time() - 1.0
    proc = subprocess.Popen(
        cmd,
        env=spawn_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # posix: ランチャーから起きた QGIS も、プロセスグループごと止められるように
        start_new_session=sys.platform != "win32",
    )

    captured_stdout: list[str] = []
    captured_stderr: list[str] = []
    drain_threads = _start_drain_threads(proc, captured_stdout, captured_stderr)

    try:
        instance_id = _wait_for_worker_by_token(
            automation_client,
            launch_token=launch_token,
            timeout_s=register_timeout_s,
            launch=_Launch(
                proc=proc,
                command=cmd,
                stderr=captured_stderr,
                drain_threads=drain_threads,
            ),
        )
    except WorkerRegisterRejected as e:
        # 断られた QGIS は登録されていないので、閉じるよう頼めない。pid で止める
        _kill_rejected_worker(e.rejection)
        _terminate_worker(proc, graceful_timeout_s=graceful_shutdown_timeout_s)
        _join_drain_threads(drain_threads, timeout_s=2.0)
        raise
    except WorkerRegisterTimeout:
        # register 失敗時はサブプロセスを片付けてから raise（dangling 防止）。
        # 起動のコマンドが失敗して終わった（WorkerLaunchFailed）ときもここを通る。
        # まず Hub に「もう待たない」と知らせ、これから登録してくる QGIS には
        # 自分で閉じてもらう。Hub に QGIS がもう見えていれば、そちらはここで
        # 止める（#16）。ランチャー経由だと proc はもう居ない
        _abandon_launch(automation_client, launch_token)
        if not _stop_late_worker(automation_client, launch_token=launch_token):
            # Hub に何も届いていない QGIS（登録の要求を送ってこない）は、
            # 環境変数の launch_token で見つけて止める（#18）
            _kill_unregistered_qgis(
                proc, launch_token=launch_token, launched_at=launched_at, gave_up_at=time.time()
            )
        _terminate_worker(proc, graceful_timeout_s=graceful_shutdown_timeout_s)
        _join_drain_threads(drain_threads, timeout_s=2.0)
        raise

    # QGIS 本体の pid（#43）。一覧から外れた後の後片付けで使う
    known_qgis = _KnownQgis(
        pid=_registered_pid(automation_client, instance_id),
        launch_token=launch_token,
        launched_at=launched_at,
        registered_at=time.time(),
        group=proc.pid,
    )

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
            known_qgis=known_qgis,
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
    hub_file: str | os.PathLike[str],
    user_env: Mapping[str, str] | None,
) -> dict[str, str]:
    """spawn 用 env を組み立てる。

    ``base``（ふつうは pytest の環境変数）から ``QPUPPETEER_HUB_*`` を全部消し、
    ユーザー指定 env を上に merge する。``QPUPPETEER_HUB_FILE`` は helper が
    常に勝つ（ユーザーが違う Hub ファイルを渡すと、この session の Hub に
    register 出来ないため）。Hub ファイルは ``automation_client`` が挨拶に
    使うものと同じ。
    """
    # 開発者の環境に残った Hub の設定を、起こす QGIS に持ち込まない
    env = {k: v for k, v in base.items() if not k.upper().startswith(_ENV_HUB_PREFIX)}
    if user_env:
        for k, v in user_env.items():
            env[k] = v
    # helper による上書き（ユーザー指定よりも後に書くことで helper を勝たせる）
    env[_ENV_HUB_FILE] = os.fspath(hub_file)
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


@dataclass(frozen=True)
class _Launch:
    """登録を待つ間に見る、起動のコマンドのプロセス。"""

    proc: subprocess.Popen[bytes]
    command: Sequence[str]
    stderr: list[str]
    drain_threads: list[threading.Thread]

    def failed_returncode(self) -> int | None:
        """0 以外の終了コードで終わっていれば、その終了コード。まだ動いているか 0 なら None。"""
        returncode = self.proc.poll()
        return None if returncode in (None, 0) else returncode

    def stderr_tail(self, *, settle_s: float = 0.0) -> list[str]:
        """stderr の最後の数行（空行を除く）。

        ``settle_s`` を渡すと、drain thread が終わりまで読むのをその秒数まで待つ
        （終わった直後は、最後の行がまだ積まれていないことがある）。ランチャーが
        起こした QGIS が pipe を引き継いでいると drain thread は終わらないので、
        待つのは ``settle_s`` まで。
        """
        if settle_s > 0:
            _join_drain_threads(self.drain_threads, timeout_s=settle_s)
        lines = "".join(list(self.stderr)).splitlines()
        return [line for line in lines if line.strip()][-_STDERR_TAIL_LINES:]


def _format_stderr_tail(tail: list[str]) -> str:
    """失敗のメッセージに足す stderr の部分。無ければ空文字。"""
    if not tail:
        return ""
    body = "\n".join(f"  {line}" for line in tail)
    return f"\nstderr of the launch command (last {len(tail)} lines):\n{body}"


def _launch_failed(launch_token: str, launch: _Launch, returncode: int) -> WorkerLaunchFailed:
    tail = launch.stderr_tail(settle_s=0.5)
    command = " ".join(str(c) for c in launch.command)
    detail = _format_stderr_tail(tail) or "\nThe launch command wrote nothing to stderr."
    return WorkerLaunchFailed(
        f"The launch command exited with code {returncode} before QGIS registered to "
        f"the Hub (launch_token={launch_token}, command: {command})" + detail,
        returncode=returncode,
        stderr_tail=tail,
    )


def _wait_for_worker_by_token(
    automation_client: E2EAutomationClient,
    *,
    launch_token: str,
    timeout_s: float,
    poll_interval_s: float = 0.25,
    launch: _Launch | None = None,
) -> str:
    """自分の launch_token の Worker が **使える** ようになるまで待つ。

    ADR-0005 D6: pid diff heuristic を廃止し、helper が注入した
    ``launch_token`` で **決定的** に相関する。pid 再利用や xdist 並列 spawn の
    race に左右されない（U9）。

    待つ条件は `E2EAutomationClient.wait_for_ready` に委譲する。``list_instances``
    は heartbeat が途絶えた instance も ``state="unresponsive"`` で返すように
    なったので、register されたことだけを見ると「登録はできたが GUI スレッドが
    塞がっていて何も処理できない」個体を掴んでしまう。

    ``launch`` を渡すと、待つ間に起動のコマンドが 0 以外の終了コードで終わったら、
    待ちを打ち切って `WorkerLaunchFailed` を上げる。0 で終わったら待ち続ける
    （QGIS を起こしてすぐ 0 で終わるランチャーがあるため）。Worker が使える状態に
    なっていれば、終了コードは問わない。
    """

    def stop_if_launch_failed() -> None:
        assert launch is not None
        returncode = launch.failed_returncode()
        if returncode is not None:
            raise _launch_failed(launch_token, launch, returncode)

    try:
        return automation_client.wait_for_ready(
            launch_token,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            on_poll=stop_if_launch_failed if launch is not None else None,
        )
    except WorkerRegisterTimeout:
        raise  # on_poll が上げた WorkerLaunchFailed
    except RegistrationRejectedError as e:
        raise WorkerRegisterRejected(
            f"The Hub rejected the QGIS started with launch_token={launch_token} "
            f"(pid={e.rejection.pid}, label={e.rejection.label!r}): "
            f"{e.rejection.code}: {e.rejection.message}",
            rejection=e.rejection,
        ) from e
    except TimeoutError as e:
        if launch is None:
            tail: list[str] = []
        else:
            # 待ちが切れる間際に終わったなら、そちらが原因
            returncode = launch.failed_returncode()
            if returncode is not None:
                raise _launch_failed(launch_token, launch, returncode) from e
            tail = launch.stderr_tail()
        raise WorkerRegisterTimeout(
            f"QGIS subprocess (launch_token={launch_token}) did not register "
            f"to Hub and answer a command within {timeout_s}s" + _format_stderr_tail(tail)
        ) from e


def _check_worker_label(label: str | None) -> None:
    """Hub が断るラベルなら ``ValueError``（#13）。None はラベル無し（Hub が自動で付ける）。"""
    if label is None:
        return
    result = validate_label(label)
    if not result.ok:
        raise ValueError(
            f"spawn_qgis: the Hub would reject the worker label {label!r} "
            f"({result.reason}). A label is 1-{LABEL_MAX_LENGTH} characters, "
            "not digits only, and does not start with '@'."
        )


def _abandon_launch(automation_client: E2EAutomationClient, launch_token: str) -> None:
    """この token の QGIS をもう待たない、と Hub に知らせる（#16、best-effort）。"""
    abandon = getattr(automation_client, "abandon_launch", None)
    if abandon is None:
        return
    try:
        abandon(launch_token)
    except Exception:  # noqa: BLE001 - 片付けは best-effort
        logger.warning("Could not tell the Hub to abandon %s", launch_token, exc_info=True)


def _stop_late_worker(
    automation_client: E2EAutomationClient,
    *,
    launch_token: str,
) -> bool:
    """待ち時間が切れた後、Hub に見えている自分の QGIS を止める（#16、best-effort）。

    Hub に自分の QGIS が見えていれば True（止められたかどうかは問わない）。

    待ちが切れても、QGIS が Hub に何も届けていないとは限らない:

    - 登録はされたが、まだ使える状態でない（プロジェクトの読み込みで GUI が
      塞がっている、待ち時間ぎりぎりで登録した）→ 一覧にいる
    - 登録を断られた（#13 の記録）→ 断られた登録にいる

    どちらでも pid が分かるので、pid で止める。閉じるよう頼まないのは、使える
    状態にならなかった QGIS は GUI が塞がっていることが多く、頼むと応答の上限
    （30 秒）まで待つことになるため。起動の途中の QGIS に守るべき作業は無い。
    待ちが切れた後に登録してくる QGIS には効かない（#16 の案 2）。
    """
    pid: int | None = None
    try:
        for info in automation_client.list_instances():
            if getattr(info, "launch_token", None) == launch_token:
                pid = info.pid
                break
    except Exception:  # noqa: BLE001 - 片付けは best-effort
        logger.warning("list_instances() failed after the register timeout", exc_info=True)
    if pid is None:
        list_rejected = getattr(automation_client, "list_rejected", None)
        if list_rejected is not None:
            try:
                rejection = find_rejection(launch_token, list_rejected())
            except Exception:  # noqa: BLE001
                logger.warning("list_rejected() failed after the register timeout", exc_info=True)
                rejection = None
            if rejection is not None:
                pid = rejection.pid
    if pid is None:
        return False
    if not _pid_alive(pid):
        return True
    logger.warning(
        "QGIS (launch_token=%s pid=%d) did not become usable in time; killing it",
        launch_token,
        pid,
    )
    _force_kill_pid(pid)
    if not _wait_pid_dead(pid, timeout_s=2.0):
        logger.warning("QGIS still alive after force kill (pid=%d)", pid)
    return True


def _kill_unregistered_qgis(
    proc: subprocess.Popen[bytes],
    *,
    launch_token: str,
    launched_at: float,
    gave_up_at: float,
) -> None:
    """登録の要求を一度も送ってこない QGIS を見つけて止める（#18）。

    ランチャーが QGIS を起こしてすぐ終わると、``proc`` を止めても QGIS は残り、
    Hub にも見えないので pid が分からない。

    - Windows: 名前が ``qgis`` で始まる実行ファイルのうち、``launched_at`` から
      ``gave_up_at`` の間に作られ、環境変数 ``QPUPPETEER_LAUNCH_TOKEN`` が自分の
      ``launch_token`` と一致するものだけを止める。token は環境変数でランチャーの
      途中（cmd、Python、``start`` など）を通って QGIS まで届くので、途中の
      プロセスが終わっていても見つけられる。親子関係はたどらない（途中が終わると、
      生きているプロセスの一覧から鎖が切れるため）。人が起こした QGIS は token を
      持たないので巻き込まない。調べるのも止めるのも同じハンドルで行う（pid で
      開き直すと、その間に pid が使い回されたとき別のプロセスを止めうる）。
      調べられなかった QGIS（管理者で動いている、途中で終わった、など）は止めずに
      飛ばし、その数を warning に出す
    - posix: QGIS を起こしたコマンドは自分のプロセスグループで動いているので、
      グループごと止める
    """
    if sys.platform != "win32":
        import signal as _signal

        with contextlib.suppress(OSError):
            os.killpg(proc.pid, _signal.SIGKILL)
        return
    try:
        killed, unreadable = _kill_qgis_with_token(
            launch_token, since=launched_at, until=gave_up_at
        )
    except Exception:  # noqa: BLE001 - 片付けは best-effort
        logger.warning("Could not look for the QGIS of %s", launch_token, exc_info=True)
        return
    for pid in killed:
        logger.warning(
            "QGIS (pid=%d, launch_token=%s) never registered; killed it", pid, launch_token
        )
    if unreadable:
        logger.warning(
            "Could not inspect %d QGIS process(es) started while waiting for %s "
            "(no access, or they exited); if one of them was ours, it is still running",
            unreadable,
            launch_token,
        )


def _kill_qgis_with_token(
    launch_token: str, *, since: float, until: float
) -> tuple[list[int], int]:
    """token が ``launch_token`` の QGIS を止める（Windows）。

    名前（``qgis*.exe``）で絞ったものを 1 つずつ開き、そのハンドルで作られた時刻
    （``since``〜``until``、エポック秒）、環境変数の token、の順に確かめ、一致すれば
    同じハンドルで止める。

    Returns:
        (止めた pid, 調べられなかった数)。範囲外の時刻のものは数えない。
    """
    killed: list[int] = []
    unreadable = 0
    for pid, _ppid, name in _list_processes():
        lower = name.lower()
        if not (lower.startswith("qgis") and lower.endswith(".exe")):
            continue
        process = _open_process(pid)
        if process is None:
            unreadable += 1
            continue
        try:
            ours = _is_our_qgis(process, launch_token, since=since, until=until)
            if ours is None:
                unreadable += 1
                continue
            if not ours:
                continue
            if process.terminate():
                killed.append(pid)
            else:
                logger.warning("Could not kill QGIS (pid=%d)", pid)
        finally:
            process.close()
    return killed, unreadable


def _is_our_qgis(
    process: _WindowsProcess, launch_token: str, *, since: float, until: float
) -> bool | None:
    """開いたプロセスが、``launch_token`` で起こした QGIS か（Windows）。

    作られた時刻が ``since``〜``until``（エポック秒）で、環境変数の token が
    ``launch_token`` なら True。時刻や環境変数を読めなければ None。
    """
    created = process.created_at()
    if created is None:
        return None
    if not since <= created <= until:
        return False
    environ = process.environ()
    if environ is None:
        return None
    token = next((v for k, v in environ.items() if k.upper() == ENV_LAUNCH_TOKEN), None)
    return token == launch_token


class _WindowsProcess:
    """1 つのプロセスのハンドル。調べるのも止めるのも、これ 1 つで行う（#18）。"""

    # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_TERMINATE | SYNCHRONIZE
    _ACCESS = 0x0400 | 0x0010 | 0x0001 | 0x00100000

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def open(cls, pid: int) -> _WindowsProcess | None:
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(cls._ACCESS, False, pid)
        return cls(handle) if handle else None

    def close(self) -> None:
        if self._handle:
            _kernel32().CloseHandle(self._handle)
            self._handle = 0

    def created_at(self) -> float | None:
        """作られた時刻（エポック秒）。"""
        import ctypes
        from ctypes import wintypes

        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _kernel32().GetProcessTimes(
            self._handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime  # 1601 年からの 100ns
        return (ticks - 116_444_736_000_000_000) / 10_000_000

    def environ(self) -> dict[str, str] | None:
        """環境変数。読めなければ None（64 ビットの Windows 8 以降）。

        PEB → ``RTL_USER_PROCESS_PARAMETERS`` → 環境変数の塊、の順に
        ``ReadProcessMemory`` で読む（PEB の ProcessParameters が 0x20、
        Environment が 0x80、EnvironmentSize が 0x3F0）。pytest の Python が
        32 ビットなら読まない。
        """
        import ctypes

        if ctypes.sizeof(ctypes.c_void_p) != 8:
            return None

        class _BasicInformation(ctypes.Structure):
            _fields_ = [
                ("ExitStatus", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("AffinityMask", ctypes.c_void_p),
                ("BasePriority", ctypes.c_void_p),
                ("UniqueProcessId", ctypes.c_void_p),
                ("InheritedFromUniqueProcessId", ctypes.c_void_p),
            ]

        info = _BasicInformation()
        if _ntdll().NtQueryInformationProcess(
            self._handle, 0, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            return None
        if not info.PebBaseAddress:
            return None
        params = self._read_pointer(info.PebBaseAddress + 0x20)
        if not params:
            return None
        address = self._read_pointer(params + 0x80)
        size = self._read_pointer(params + 0x3F0)
        if not address or not size or size > 1 << 22:
            return None
        block = self._read(address, size)
        if block is None:
            return None
        environ: dict[str, str] = {}
        for item in block.decode("utf-16-le", errors="replace").split("\0"):
            if not item:
                break
            key, sep, value = item.partition("=")
            if sep and key:  # "=C:=C:\..." のような隠しの項目は key が空になる
                environ[key] = value
        return environ

    def wait(self, timeout_s: float) -> bool:
        """終わるまで最大 ``timeout_s`` 秒待つ。終わったら True。"""
        return _kernel32().WaitForSingleObject(self._handle, int(timeout_s * 1000)) == 0

    def terminate(self) -> bool:
        """強制終了し、終わるまで最大 2 秒待つ。終わったら True。"""
        kernel32 = _kernel32()
        if not kernel32.TerminateProcess(self._handle, 1):
            return False
        return kernel32.WaitForSingleObject(self._handle, 2000) == 0  # WAIT_OBJECT_0

    def _read(self, address: int, size: int) -> bytes | None:
        import ctypes

        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        if not _kernel32().ReadProcessMemory(
            self._handle, ctypes.c_void_p(address), buf, size, ctypes.byref(got)
        ):
            return None
        return buf.raw[: got.value]

    def _read_pointer(self, address: int) -> int | None:
        raw = self._read(address, 8)
        return None if raw is None or len(raw) < 8 else int.from_bytes(raw, "little")


def _open_process(pid: int) -> _WindowsProcess | None:
    """``pid`` を調べて止められるように開く（Windows）。開けなければ None。"""
    return _WindowsProcess.open(pid)


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    return kernel32


def _ntdll() -> Any:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    return ntdll


def _list_processes() -> list[tuple[int, int, str]]:
    """(pid, 親の pid, 実行ファイル名) の一覧（Windows、Toolhelp32 で取る）。"""
    import ctypes
    from ctypes import wintypes

    class _Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        found: list[tuple[int, int, str]] = []
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append(
                (int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile)
            )
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return found
    finally:
        kernel32.CloseHandle(snapshot)


def _kill_rejected_worker(rejection: RejectedRegistration) -> None:
    """登録を断られた QGIS を止める（best-effort）。"""
    pid = rejection.pid
    if pid is None or not _pid_alive(pid):
        return
    logger.warning("Killing the QGIS whose registration was rejected (pid=%d)", pid)
    _force_kill_pid(pid)
    if not _wait_pid_dead(pid, timeout_s=2.0):
        logger.warning("Rejected QGIS still alive after force kill (pid=%d)", pid)


@dataclass(frozen=True)
class _KnownQgis:
    """``spawn_qgis`` が起こして登録された QGIS 本体（#43）。

    Attributes:
        pid: 登録したときの一覧の pid。引けなかったら None。
        launch_token: 起こすときに渡した token。
        launched_at: 起こした時刻（エポック秒、時計の粒度の分だけ前）。
        registered_at: 登録を確かめた時刻（エポック秒）。QGIS はこの間に作られている。
        group: posix で QGIS を起こしたコマンドのプロセスグループ（起動した
            プロセスの pid）。
    """

    pid: int | None
    launch_token: str
    launched_at: float
    registered_at: float
    group: int


def _registered_pid(automation_client: E2EAutomationClient, instance_id: str) -> int | None:
    """登録された QGIS の pid を一覧から引く（#43）。引けなければ None。"""
    try:
        for info in automation_client.list_instances():
            if info.instance_id == instance_id:
                return info.pid
    except Exception:  # noqa: BLE001 - 引けなくても起動は成功している
        logger.warning(
            "list_instances() failed after QGIS registered (instance_id=%s)",
            instance_id,
            exc_info=True,
        )
    return None


def _shutdown_worker_best_effort(
    proc: subprocess.Popen[bytes],
    *,
    automation_client: E2EAutomationClient,
    instance_id: str,
    graceful_timeout_s: float,
    known_qgis: _KnownQgis | None = None,
) -> None:
    """spawn した QGIS と、起動に使ったプロセスを片付ける。

    止める相手は ``proc`` ではなく、Hub に登録されている Worker の pid（QGIS
    本体）。``command`` がランチャーで、QGIS を起こしてすぐ終わる場合、``proc``
    はもう居ないので、``proc`` だけを見ると QGIS が残る（#9）。

    1. Worker がまだ一覧に居れば `stop_registered_worker` で閉じる（閉じるよう
       頼み、消えなければその pid だけを強制終了）
    2. 一覧に居なければ（固まって登録が外れた、Hub に聞けない）、登録したときに
       覚えた ``known_qgis`` を `_stop_unlisted_qgis` で止める（#43）。ランチャー
       経由だと ``proc`` を止めても QGIS 本体は残るため
    3. ``proc`` が残っていれば従来どおり止める。``qgis_bin`` を直接起こした場合は
       ``proc`` が QGIS 本体なので、2 ではなくここで止める
    4. 失敗は warning ログだけ出す（次のテストに進む方を優先）
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
    elif known_qgis is not None and known_qgis.pid is not None:
        if known_qgis.pid != proc.pid:
            _stop_unlisted_qgis(known_qgis, graceful_timeout_s=graceful_timeout_s)
        elif proc.poll() is None:
            logger.warning(
                "QGIS (pid=%d, launch_token=%s) is no longer registered with the Hub "
                "(it may have hung); stopping it",
                proc.pid,
                known_qgis.launch_token,
            )

    if proc.poll() is None:
        _terminate_worker(proc, graceful_timeout_s=graceful_timeout_s)


def _stop_unlisted_qgis(known: _KnownQgis, *, graceful_timeout_s: float) -> None:
    """Hub の一覧から外れた QGIS 本体を止める（#43、best-effort）。

    固まって heartbeat が切れた QGIS は、Hub が接続を切り、猶予の後に一覧から
    外す。閉じるよう頼めないので、外から止める。

    - Windows: pid を開き、作られた時刻（起こしてから登録を確かめるまで）と
      環境変数の launch_token で同じ QGIS かを確かめる（pid の使い回しで無関係の
      プロセスを止めないため）。``taskkill``（``/F`` なし、窓に閉じるよう伝える）
      で穏やかに止め、``graceful_timeout_s`` 待っても残れば、同じハンドルで
      強制終了する
    - posix: QGIS を起こしたコマンドのプロセスグループに居れば、SIGTERM、
      残れば SIGKILL
    - 止めるときは warning を出す（固まった QGIS があったことを知らせる）
    """
    pid = known.pid
    if pid is None:
        return
    if sys.platform != "win32":
        _stop_unlisted_qgis_posix(known, pid, graceful_timeout_s=graceful_timeout_s)
        return
    process = _open_process(pid)
    if process is None:
        if _pid_alive(pid):
            logger.warning(
                "Could not open QGIS (pid=%d) that is no longer registered with the Hub; "
                "it may still be running",
                pid,
            )
        return
    try:
        ours = _is_our_qgis(
            process, known.launch_token, since=known.launched_at, until=known.registered_at
        )
        if ours is None:
            logger.warning(
                "Could not inspect QGIS (pid=%d) that is no longer registered with the Hub; "
                "if it is ours, it is still running",
                pid,
            )
            return
        if not ours or process.wait(0):
            return  # 別のプロセス（pid が使い回された）か、もう終わっている
        logger.warning(
            "QGIS (pid=%d, launch_token=%s) is no longer registered with the Hub "
            "(it may have hung); stopping it",
            pid,
            known.launch_token,
        )
        subprocess.run(["taskkill", "/PID", str(pid)], check=False, capture_output=True)
        if process.wait(graceful_timeout_s):
            return
        logger.warning(
            "QGIS (pid=%d) did not close within %.1fs; killing it", pid, graceful_timeout_s
        )
        if not process.terminate():
            logger.warning("QGIS still alive after force kill (pid=%d)", pid)
    finally:
        process.close()


def _stop_unlisted_qgis_posix(known: _KnownQgis, pid: int, *, graceful_timeout_s: float) -> None:
    """`_stop_unlisted_qgis` の posix 版。プロセスグループで自分の QGIS か確かめる。"""
    import signal as _signal

    try:
        if os.getpgid(pid) != known.group:
            return
    except OSError:
        return  # もう居ない
    logger.warning(
        "QGIS (pid=%d, launch_token=%s) is no longer registered with the Hub "
        "(it may have hung); stopping it",
        pid,
        known.launch_token,
    )
    try:
        os.kill(pid, _signal.SIGTERM)
    except OSError:
        return
    if _wait_pid_dead(pid, timeout_s=graceful_timeout_s):
        return
    logger.warning("QGIS (pid=%d) did not close within %.1fs; killing it", pid, graceful_timeout_s)
    _force_kill_pid(pid)
    if not _wait_pid_dead(pid, timeout_s=2.0):
        logger.warning("QGIS still alive after force kill (pid=%d)", pid)


# QGIS に自分で閉じてもらうコード。手順は QGIS 側の
# ``qgis_puppeteer.qgis_tools.app_tools.close_qgis``：変更を捨て（保存の確認を
# 出させない）、開いているモーダルを上から reject してから、メインウィンドウを
# 閉じる。モーダルが開いていると、メインウィンドウを閉じるだけでは閉じない。
# 手順は QTimer で進むので、この execute_python の応答はすぐ返る。
#
# それを持たない古いプラグインの QGIS には、前の閉じ方（変更を捨てて
# メインウィンドウを閉じるだけ）で頼む。
#
# ``QgsApplication.exitQgis()`` は使わない。あれは終了処理の最後に呼ぶ後片付け
# （プロバイダの破棄など）で、GUI が動いている最中に呼ぶと QGIS が落ちる。
_CLOSE_QGIS_CODE = """from qgis.utils import iface

try:
    from qgis_puppeteer.qgis_tools.app_tools import close_qgis
except ImportError:
    from qgis.core import QgsApplication, QgsProject
    from qgis.PyQt.QtCore import QTimer

    QgsProject.instance().setDirty(False)
    if iface is not None and iface.mainWindow() is not None:
        QTimer.singleShot(0, iface.mainWindow().close)
    else:
        QTimer.singleShot(0, QgsApplication.instance().quit)
else:
    close_qgis(iface)
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
    # 閉じる依頼はテストの操作ではないので、操作記録に入れない
    unrecorded = getattr(automation_client, "unrecorded", contextlib.nullcontext)
    try:
        with unrecorded():
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
        automation_client: E2EAutomationClient,
    ) -> Iterator[Any]:
        spawn_kwargs: dict[str, Any] = {
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
    "WorkerLaunchFailed",
    "WorkerRegisterRejected",
    "WorkerRegisterTimeout",
    "spawn_qgis",
    "spawn_qgis_fixture",
]
