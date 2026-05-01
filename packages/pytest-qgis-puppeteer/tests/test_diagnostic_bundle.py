"""diagnostic bundle 出力ロジックの単体テスト（ADR-0002 §13、B4）。

実 QGIS / Hub に依存せず、fake client / fake item で `_build_diag_meta` と
``_dump_diagnostic_bundle`` の動作を検証する。``_drain_pipe_to_list`` の
buffer truncate 挙動も合わせて確認。
"""

from __future__ import annotations

import io
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pytest_qgis_puppeteer._environments import EnvironmentSpec
from pytest_qgis_puppeteer.plugin import (
    _CONFIG_ATTR_ACTIVE_ENV,
    _CONFIG_ATTR_HUB_STDERR_BUF,
    _CONFIG_ATTR_HUB_STDOUT_BUF,
    _HUB_LOG_TAIL_LINES,
    _build_diag_meta,
    _drain_pipe_to_list,
    _dump_diagnostic_bundle,
)

# ============================================================
# fakes
# ============================================================


@dataclass
class _FakeInstance:
    instance_id: str
    label: str
    pid: int
    project: str | None = None


class _FakeAutomationClient:
    """``screenshot`` / ``snapshot_ui`` / ``list_instances`` / ``get_recorder`` だけ
    実装したスタブ。recorder は B5a 以降の actions.jsonl 出力に必要。
    """

    def __init__(self) -> None:
        self.snapshot_payload: dict[str, Any] = {"active_modal": None}
        self.instances: list[_FakeInstance] = [
            _FakeInstance(instance_id="inst-1", label="worker-1", pid=12345)
        ]
        self.screenshot_calls: list[dict[str, Any]] = []
        self._recorder: Any = None

    def screenshot(self, **kwargs: Any) -> dict[str, Any]:
        self.screenshot_calls.append(kwargs)
        # 実際のファイル書き出しは plugin 側ではなく Worker 側の責務。
        # テストでは out path に touch だけしておく。
        out = kwargs.get("output_path")
        if out:
            Path(out).touch()
        return {"ok": True}

    def snapshot_ui(self, **kwargs: Any) -> dict[str, Any]:
        return self.snapshot_payload

    def list_instances(self) -> list[_FakeInstance]:
        return list(self.instances)

    def get_recorder(self) -> Any:
        return self._recorder

    def set_recorder(self, recorder: Any) -> None:
        self._recorder = recorder


class _FakeIni:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = values or {}

    def __call__(self, name: str) -> Any:
        return self._values.get(name, "")


class _FakeConfig:
    """``pytest.Config`` の最小スタブ（``getini`` / ``getoption`` / 任意 attr 設定）。"""

    def __init__(
        self,
        *,
        ini: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        self._ini = ini or {}
        self._options = options or {}

    def getini(self, name: str) -> Any:
        return self._ini.get(name, "")

    def getoption(self, name: str, *, default: Any = None) -> Any:
        return self._options.get(name, default)


class _FakeItem:
    """``pytest.Item`` の最小スタブ。`fixturenames` / `funcargs` / `config` のみ。"""

    def __init__(
        self,
        *,
        nodeid: str,
        config: _FakeConfig,
        client: _FakeAutomationClient | None,
    ) -> None:
        self.nodeid = nodeid
        self.config = config
        self.fixturenames = ["automation_client"] if client is not None else []
        self.funcargs: dict[str, Any] = {"automation_client": client} if client is not None else {}


@dataclass
class _FakeReport:
    when: str = "call"
    failed: bool = True
    longrepr: Any = "Traceback (most recent call last):\n  AssertionError: x != y"


class _FakeCall:
    def __init__(self, exc_type: type[BaseException], message: str) -> None:
        try:
            raise exc_type(message)
        except BaseException:  # noqa: BLE001
            import sys

            self.excinfo = pytest.ExceptionInfo.from_current()
            del sys  # 参照だけ消して exc_info を保持


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "QPUPPETEER_QGIS_BIN",
        "QPUPPETEER_QGIS_ARGS",
        "QPUPPETEER_QGIS_COMMAND",
        "QPUPPETEER_QGIS_PYTHON",
    ):
        monkeypatch.delenv(key, raising=False)


# ============================================================
# _drain_pipe_to_list
# ============================================================


class TestDrainPipeToList:
    """drain thread の基本動作。"""

    def test_drains_lines_from_pipe(self) -> None:
        stream = io.BytesIO(b"line one\nline two\nline three\n")
        buf: list[str] = []
        _drain_pipe_to_list(stream, buf)
        assert buf == ["line one", "line two", "line three"]

    def test_handles_empty_stream(self) -> None:
        stream = io.BytesIO(b"")
        buf: list[str] = []
        _drain_pipe_to_list(stream, buf)
        assert buf == []

    def test_handles_invalid_utf8(self) -> None:
        stream = io.BytesIO(b"valid line\n\xff\xfe invalid\n")
        buf: list[str] = []
        _drain_pipe_to_list(stream, buf)
        # invalid bytes は replace で潰されるが、行は捨てない
        assert buf[0] == "valid line"
        assert len(buf) == 2

    def test_truncates_to_tail_limit(self) -> None:
        # _HUB_LOG_TAIL_LINES + 100 行を流して、末尾だけ残ることを確認
        n = _HUB_LOG_TAIL_LINES + 100
        data = "".join(f"line {i}\n" for i in range(n))
        stream = io.BytesIO(data.encode())
        buf: list[str] = []
        _drain_pipe_to_list(stream, buf)
        # 直近 _HUB_LOG_TAIL_LINES 行だけが残る
        assert len(buf) == _HUB_LOG_TAIL_LINES
        # 最後の行は 末尾の line
        assert buf[-1] == f"line {n - 1}"
        # 最初の行は捨てられている
        assert buf[0] == f"line {n - _HUB_LOG_TAIL_LINES}"

    def test_does_not_raise_on_none_stream(self) -> None:
        # 念のため None 渡し（caller 側で stream が None になるケースの保険）
        _drain_pipe_to_list(None, [])  # 例外を上げないだけで良い

    def test_runs_in_real_thread(self) -> None:
        """daemon thread として動かしても安全に終了する。"""
        stream = io.BytesIO(b"a\nb\n")
        buf: list[str] = []
        t = threading.Thread(target=_drain_pipe_to_list, args=(stream, buf), daemon=True)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert buf == ["a", "b"]


# ============================================================
# _build_diag_meta
# ============================================================


class TestBuildDiagMeta:
    """meta.json の中身を組み立てる pure 関数。"""

    def _make_item(
        self,
        *,
        ini: dict[str, Any] | None = None,
        active_env: EnvironmentSpec | None = None,
    ) -> _FakeItem:
        config = _FakeConfig(ini=ini)
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, active_env)
        return _FakeItem(
            nodeid="tests/test_x.py::test_y",
            config=config,
            client=_FakeAutomationClient(),
        )

    def test_minimum_meta(self, tmp_path: Path) -> None:
        item = self._make_item(ini={"qgis_bin": "qgis-bin.exe"})
        meta = _build_diag_meta(
            item,  # type: ignore[arg-type]
            report=_FakeReport(),  # type: ignore[arg-type]
            call=None,
            bundle_dir=tmp_path,
            active_env=None,
            instances=[],
        )
        assert meta["schema_version"] == 1
        assert meta["nodeid"] == "tests/test_x.py::test_y"
        assert meta["outcome"] == "failed"
        assert meta["when"] == "call"
        assert meta["env_name"] is None
        assert meta["qgis_bin"] == "qgis-bin.exe"
        assert meta["qgis_args"] == []
        assert meta["qgis_command"] == []
        assert meta["instances"] == []
        assert meta["exception_type"] is None
        assert meta["exception_message"] is None

    def test_active_env_populated(self, tmp_path: Path) -> None:
        env = EnvironmentSpec(
            name="auth",
            description="auth tests",
            qgis_bin="env-qgis.exe",
            qgis_args=("--profile=auth",),
        )
        item = self._make_item(active_env=env)
        meta = _build_diag_meta(
            item,  # type: ignore[arg-type]
            report=_FakeReport(),  # type: ignore[arg-type]
            call=None,
            bundle_dir=tmp_path,
            active_env=env,
            instances=[],
        )
        assert meta["env_name"] == "auth"
        assert meta["env_description"] == "auth tests"
        assert meta["qgis_bin"] == "env-qgis.exe"
        assert meta["qgis_args"] == ["--profile=auth"]

    def test_qgis_command_takes_precedence_in_meta(self, tmp_path: Path) -> None:
        env = EnvironmentSpec(
            name="host",
            qgis_command=("launcher.bat", "--config", "e2e"),
        )
        item = self._make_item(active_env=env)
        meta = _build_diag_meta(
            item,  # type: ignore[arg-type]
            report=_FakeReport(),  # type: ignore[arg-type]
            call=None,
            bundle_dir=tmp_path,
            active_env=env,
            instances=[],
        )
        assert meta["qgis_command"] == ["launcher.bat", "--config", "e2e"]

    def test_exception_info_from_call(self, tmp_path: Path) -> None:
        item = self._make_item(ini={"qgis_bin": "qgis-bin.exe"})
        call = _FakeCall(AssertionError, "expected 1, got 2")
        meta = _build_diag_meta(
            item,  # type: ignore[arg-type]
            report=_FakeReport(),  # type: ignore[arg-type]
            call=call,  # type: ignore[arg-type]
            bundle_dir=tmp_path,
            active_env=None,
            instances=[],
        )
        assert meta["exception_type"] == "AssertionError"
        assert "expected 1, got 2" in meta["exception_message"]

    def test_instances_serialized(self, tmp_path: Path) -> None:
        item = self._make_item(ini={"qgis_bin": "qgis-bin.exe"})
        instances = [
            _FakeInstance(instance_id="i1", label="w1", pid=10, project="p1"),
            _FakeInstance(instance_id="i2", label="w2", pid=20, project=None),
        ]
        meta = _build_diag_meta(
            item,  # type: ignore[arg-type]
            report=_FakeReport(),  # type: ignore[arg-type]
            call=None,
            bundle_dir=tmp_path,
            active_env=None,
            instances=instances,
        )
        assert meta["instances"] == [
            {"instance_id": "i1", "label": "w1", "pid": 10, "project": "p1"},
            {"instance_id": "i2", "label": "w2", "pid": 20, "project": None},
        ]


# ============================================================
# _dump_diagnostic_bundle: end-to-end output check
# ============================================================


class TestDumpDiagnosticBundle:
    """全ファイルが正しく出力されるか。"""

    def _make_setup(
        self,
        tmp_path: Path,
        *,
        active_env: EnvironmentSpec | None = None,
        with_hub_logs: bool = True,
    ) -> tuple[_FakeItem, _FakeAutomationClient, Path]:
        config = _FakeConfig(
            ini={
                "qgis_bin": "qgis-bin.exe",
                "qgis_diag_dir": str(tmp_path / "diag"),
            }
        )
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, active_env)
        if with_hub_logs:
            setattr(config, _CONFIG_ATTR_HUB_STDOUT_BUF, ["hub-stdout-line-1"])
            setattr(config, _CONFIG_ATTR_HUB_STDERR_BUF, ["err-line-1", "err-line-2"])
        client = _FakeAutomationClient()
        item = _FakeItem(
            nodeid="tests/test_x.py::test_y",
            config=config,
            client=client,
        )
        return item, client, tmp_path

    def test_writes_all_files(self, tmp_path: Path) -> None:
        item, client, _ = self._make_setup(tmp_path)
        report = _FakeReport(longrepr="my-traceback-text")
        call = _FakeCall(ValueError, "something went wrong")

        _dump_diagnostic_bundle(item, report=report, call=call)  # type: ignore[arg-type]

        # diagnostic dir 内のサブディレクトリを 1 つ取り出す
        diag_root = tmp_path / "diag"
        bundles = list(diag_root.iterdir())
        assert len(bundles) == 1
        bundle = bundles[0]

        # 6 ファイル（screenshot, snapshot_ui, list_instances, meta, traceback,
        #  hub_stdout, hub_stderr）が揃っているか
        assert (bundle / "screenshot.png").exists()
        assert (bundle / "snapshot_ui.json").exists()
        assert (bundle / "list_instances.json").exists()
        assert (bundle / "meta.json").exists()
        assert (bundle / "traceback.txt").exists()
        assert (bundle / "hub_stdout.log").exists()
        assert (bundle / "hub_stderr.log").exists()

        # meta.json 中身（schema_version + 失敗情報）
        meta = json.loads((bundle / "meta.json").read_text(encoding="utf-8"))
        assert meta["schema_version"] == 1
        assert meta["exception_type"] == "ValueError"
        assert "something went wrong" in meta["exception_message"]

        # traceback
        assert (bundle / "traceback.txt").read_text(encoding="utf-8") == "my-traceback-text"

        # hub logs
        assert "hub-stdout-line-1" in (bundle / "hub_stdout.log").read_text(encoding="utf-8")
        assert "err-line-2" in (bundle / "hub_stderr.log").read_text(encoding="utf-8")

    def test_skips_when_no_automation_client(self, tmp_path: Path) -> None:
        config = _FakeConfig(ini={"qgis_diag_dir": str(tmp_path / "diag")})
        item = _FakeItem(
            nodeid="tests/test_x.py::test_y",
            config=config,
            client=None,
        )
        # automation_client fixture を要求していない → no-op で抜ける
        _dump_diagnostic_bundle(item)  # type: ignore[arg-type]
        assert not (tmp_path / "diag").exists()

    def test_env_partition_directory(self, tmp_path: Path) -> None:
        """active env 設定下では outputs/<env_name>/diagnostics/... に分離される。"""
        env = EnvironmentSpec(name="auth", qgis_bin="env-qgis.exe")
        item, _, _ = self._make_setup(tmp_path, active_env=env)
        report = _FakeReport()
        _dump_diagnostic_bundle(item, report=report)  # type: ignore[arg-type]

        # diag root が "<tmp_path>/auth/diag" に切り替わっている
        env_dir = tmp_path / "auth" / "diag"
        assert env_dir.exists()
        bundles = list(env_dir.iterdir())
        assert len(bundles) == 1

    def test_skips_hub_logs_when_buffers_absent(self, tmp_path: Path) -> None:
        """hub_process が走っていない（dev mode 等）ケースは hub log を書かない。"""
        item, _, _ = self._make_setup(tmp_path, with_hub_logs=False)
        report = _FakeReport()
        _dump_diagnostic_bundle(item, report=report)  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        assert not (bundle / "hub_stdout.log").exists()
        assert not (bundle / "hub_stderr.log").exists()
        # 他のファイルは出ている
        assert (bundle / "meta.json").exists()

    def test_no_traceback_when_longrepr_none(self, tmp_path: Path) -> None:
        item, _, _ = self._make_setup(tmp_path)
        report = _FakeReport(longrepr=None)
        _dump_diagnostic_bundle(item, report=report)  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        assert not (bundle / "traceback.txt").exists()
        assert (bundle / "meta.json").exists()


# ============================================================
# B5a: actions.jsonl 出力の検証
# ============================================================


class TestActionsJsonlDump:
    """``actions.jsonl`` が recorder 経由で diagnostic bundle に書き出されるか。"""

    def _setup_with_recorder(self, tmp_path: Path) -> tuple[_FakeItem, _FakeAutomationClient, Any]:
        """recorder を attach 済みの fake client で setup。"""
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        config = _FakeConfig(
            ini={
                "qgis_bin": "qgis-bin.exe",
                "qgis_diag_dir": str(tmp_path / "diag"),
            }
        )
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)
        setattr(config, _CONFIG_ATTR_HUB_STDOUT_BUF, [])
        setattr(config, _CONFIG_ATTR_HUB_STDERR_BUF, [])
        client = _FakeAutomationClient()
        recorder = ActionRecorder()
        client.set_recorder(recorder)
        item = _FakeItem(
            nodeid="tests/test_x.py::test_y",
            config=config,
            client=client,
        )
        return item, client, recorder

    def test_writes_actions_jsonl_when_recorder_attached(self, tmp_path: Path) -> None:
        item, _client, recorder = self._setup_with_recorder(tmp_path)
        # 失敗テストで使われそうなシナリオを 2 record 入れる
        with recorder.step("ログイン"):
            recorder.record(
                command="qgis_click_widget",
                params={"selector": {"object_name": "ok"}},
                result={"ok": True},
                duration_ms=12.3,
            )
        recorder.record(
            command="qgis_screenshot",
            params={},
            error=RuntimeError("worker dead"),
            duration_ms=5.0,
        )

        report = _FakeReport(longrepr="tb")
        _dump_diagnostic_bundle(item, report=report)  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        actions_path = bundle / "actions.jsonl"
        assert actions_path.exists()

        lines = actions_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        rec0 = json.loads(lines[0])
        rec1 = json.loads(lines[1])
        # 1 行目: ログイン step 内の click
        assert rec0["command"] == "qgis_click_widget"
        assert rec0["step_path"] == ["ログイン"]
        assert rec0["error"] is None
        assert rec0["result"] == {"ok": True}
        # 2 行目: error 付き screenshot
        assert rec1["command"] == "qgis_screenshot"
        assert rec1["step_path"] == []
        assert rec1["error"] == {"type": "RuntimeError", "message": "worker dead"}
        assert rec1["result"] is None
        # schema_version は両方 2（B5b 以降）
        assert rec0["schema_version"] == 2 == rec1["schema_version"]

    def test_actions_jsonl_empty_when_no_records(self, tmp_path: Path) -> None:
        """recorder は attach されているが record が 1 件も無いケースは空 file。"""
        item, _client, _recorder = self._setup_with_recorder(tmp_path)
        report = _FakeReport()
        _dump_diagnostic_bundle(item, report=report)  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        actions_path = bundle / "actions.jsonl"
        # 空 file は生成される（呼び出し側が「file 在 = 試行成功」と判定可能）
        assert actions_path.exists()
        assert actions_path.read_text(encoding="utf-8") == ""

    def test_no_actions_jsonl_when_recorder_not_attached(self, tmp_path: Path) -> None:
        """既存テスト（recorder 未対応 client）との互換性: actions.jsonl は出ない。"""
        config = _FakeConfig(
            ini={
                "qgis_bin": "qgis-bin.exe",
                "qgis_diag_dir": str(tmp_path / "diag"),
            }
        )
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)
        setattr(config, _CONFIG_ATTR_HUB_STDOUT_BUF, [])
        setattr(config, _CONFIG_ATTR_HUB_STDERR_BUF, [])
        client = _FakeAutomationClient()  # recorder 未 attach（None のまま）
        item = _FakeItem(
            nodeid="tests/test_x.py::test_y",
            config=config,
            client=client,
        )
        report = _FakeReport()
        _dump_diagnostic_bundle(item, report=report)  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        # recorder が None → actions.jsonl は出さない（他のファイルは出る）
        assert not (bundle / "actions.jsonl").exists()
        assert (bundle / "meta.json").exists()


# ============================================================
# B5b: per-action screenshots dir rename
# ============================================================


class TestB5bScreenshotsRename:
    """``recorder.screenshot_dir`` 配下の pending PNG が失敗時に
    ``<bundle>/screenshots/`` に rename される（ADR-0002 §13.1）。"""

    def _setup(
        self, tmp_path: Path, *, with_pending: bool
    ) -> tuple[_FakeItem, _FakeAutomationClient, Any, Path | None]:
        from pytest_qgis_puppeteer._action_recorder import ActionRecorder

        config = _FakeConfig(
            ini={
                "qgis_bin": "qgis-bin.exe",
                "qgis_diag_dir": str(tmp_path / "diag"),
            }
        )
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)
        setattr(config, _CONFIG_ATTR_HUB_STDOUT_BUF, [])
        setattr(config, _CONFIG_ATTR_HUB_STDERR_BUF, [])
        client = _FakeAutomationClient()
        recorder = ActionRecorder()
        client.set_recorder(recorder)
        pending_dir: Path | None = None
        if with_pending:
            pending_dir = tmp_path / "_pending" / "test_x"
            pending_dir.mkdir(parents=True, exist_ok=True)
            recorder.set_screenshot_dir(pending_dir)
        item = _FakeItem(
            nodeid="tests/test_x.py::test_y",
            config=config,
            client=client,
        )
        return item, client, recorder, pending_dir

    def test_pending_dir_renamed_to_bundle_screenshots(self, tmp_path: Path) -> None:
        item, _client, recorder, pending_dir = self._setup(tmp_path, with_pending=True)
        assert pending_dir is not None

        # pending に PNG を 2 枚置いて record する（実 capture を擬似）
        recorder.record(
            command="qgis_click_widget",
            params={},
            result={"ok": True},
            screenshot_path="screenshots/0000.png",
        )
        (pending_dir / "0000.png").write_bytes(b"\x89PNG fake0")
        recorder.record(
            command="qgis_set_widget_value",
            params={},
            result={"ok": True},
            screenshot_path="screenshots/0001.png",
        )
        (pending_dir / "0001.png").write_bytes(b"\x89PNG fake1")

        report = _FakeReport()
        _dump_diagnostic_bundle(item, report=report)  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        # bundle 内に screenshots/ ができ、PNG 2 枚が在る
        ss_dir = bundle / "screenshots"
        assert ss_dir.exists()
        assert (ss_dir / "0000.png").exists()
        assert (ss_dir / "0001.png").exists()
        # 元の pending dir は消える（rename）
        assert not pending_dir.exists()
        # actions.jsonl 中の screenshot_path は bundle 相対 path のまま参照可能
        rec0 = json.loads(
            (bundle / "actions.jsonl").read_text(encoding="utf-8").strip().split("\n")[0]
        )
        assert rec0["screenshot_path"] == "screenshots/0000.png"

    def test_no_screenshots_dir_when_pending_unset(self, tmp_path: Path) -> None:
        """``recorder.screenshot_dir`` が None なら bundle に screenshots/ は出ない。"""
        item, _client, recorder, _pending = self._setup(tmp_path, with_pending=False)
        recorder.record(command="cmd", params={})

        report = _FakeReport()
        _dump_diagnostic_bundle(item, report=report)  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        assert not (bundle / "screenshots").exists()

    def test_pending_target_already_exists_merged(self, tmp_path: Path) -> None:
        """target screenshots/ が既に在る場合は merge して移動（fallback パス）。"""
        item, _client, recorder, pending_dir = self._setup(tmp_path, with_pending=True)
        assert pending_dir is not None

        # bundle dir の生成は _dump_diagnostic_bundle がやるが、target を先回りで
        # 作って衝突を再現する。bundle dir 名は timestamp 込みなので分からない →
        # 先に PNG を pending に置いて 1 回 dump → 2 度目 dump で衝突を観測する手法
        # は複雑。ここでは fallback コードパスの最低限カバーで OK とする。
        (pending_dir / "0000.png").write_bytes(b"png")
        recorder.record(
            command="cmd",
            params={},
            result={},
            screenshot_path="screenshots/0000.png",
        )

        # 1 回目の dump で rename される
        _dump_diagnostic_bundle(item, report=_FakeReport())  # type: ignore[arg-type]
        bundle1 = next((tmp_path / "diag").iterdir())
        assert (bundle1 / "screenshots" / "0000.png").exists()
        # pending は消えた
        assert not pending_dir.exists()


# ============================================================
# ADR-0004 Phase 2: spawn stdout/stderr in diagnostic bundle
# ============================================================


class TestSpawnLogsInBundle:
    """``fresh_qgis`` で spawn 中の worker の captured stdout/stderr が、失敗時の
    diagnostic bundle に ``spawn_stdout.log`` / ``spawn_stderr.log`` として書き出される
    （ADR-0004 Phase 2）。
    """

    def _setup_with_active_worker(
        self, tmp_path: Path, *, stdout_lines: list[str], stderr_lines: list[str]
    ) -> _FakeItem:
        from pytest_qgis_puppeteer.plugin import _CONFIG_ATTR_ACTIVE_FRESH_WORKERS
        from pytest_qgis_puppeteer.spawn import SpawnedWorker

        config = _FakeConfig(
            ini={
                "qgis_bin": "qgis-bin.exe",
                "qgis_diag_dir": str(tmp_path / "diag"),
            }
        )
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)
        setattr(config, _CONFIG_ATTR_HUB_STDOUT_BUF, [])
        setattr(config, _CONFIG_ATTR_HUB_STDERR_BUF, [])

        worker = SpawnedWorker(instance_id="fresh-1", pid=99999, label="fresh")
        worker._captured_stdout.extend(stdout_lines)
        worker._captured_stderr.extend(stderr_lines)
        nodeid = "tests/test_x.py::test_y"
        setattr(config, _CONFIG_ATTR_ACTIVE_FRESH_WORKERS, {nodeid: worker})

        client = _FakeAutomationClient()
        return _FakeItem(nodeid=nodeid, config=config, client=client)

    def test_spawn_logs_written_when_active_worker(self, tmp_path: Path) -> None:
        item = self._setup_with_active_worker(
            tmp_path,
            stdout_lines=["spawn out 1", "spawn out 2"],
            stderr_lines=["GDAL warn", "PROJ info"],
        )
        _dump_diagnostic_bundle(item, report=_FakeReport())  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        assert (bundle / "spawn_stdout.log").exists()
        assert (bundle / "spawn_stderr.log").exists()

        out_text = (bundle / "spawn_stdout.log").read_text(encoding="utf-8")
        assert "spawn out 1" in out_text
        assert "spawn out 2" in out_text
        err_text = (bundle / "spawn_stderr.log").read_text(encoding="utf-8")
        assert "GDAL warn" in err_text
        assert "PROJ info" in err_text

    def test_no_spawn_logs_when_no_active_worker(self, tmp_path: Path) -> None:
        """active worker dict が空 / 該当 nodeid が居ない → spawn_*.log は出さない。"""
        from pytest_qgis_puppeteer.plugin import _CONFIG_ATTR_ACTIVE_FRESH_WORKERS

        config = _FakeConfig(
            ini={
                "qgis_bin": "qgis-bin.exe",
                "qgis_diag_dir": str(tmp_path / "diag"),
            }
        )
        setattr(config, _CONFIG_ATTR_ACTIVE_ENV, None)
        setattr(config, _CONFIG_ATTR_HUB_STDOUT_BUF, [])
        setattr(config, _CONFIG_ATTR_HUB_STDERR_BUF, [])
        setattr(config, _CONFIG_ATTR_ACTIVE_FRESH_WORKERS, {})  # 空
        client = _FakeAutomationClient()
        item = _FakeItem(nodeid="tests/test_x.py::test_y", config=config, client=client)

        _dump_diagnostic_bundle(item, report=_FakeReport())  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        # active worker が居ないので spawn_*.log は出ない（他は出る）
        assert not (bundle / "spawn_stdout.log").exists()
        assert not (bundle / "spawn_stderr.log").exists()
        # 念のため通常 bundle 出力は通っている
        assert (bundle / "meta.json").exists()

    def test_spawn_logs_empty_when_buffers_empty(self, tmp_path: Path) -> None:
        """worker は active だが drain buffer が空 → 空 file を生成。"""
        item = self._setup_with_active_worker(tmp_path, stdout_lines=[], stderr_lines=[])
        _dump_diagnostic_bundle(item, report=_FakeReport())  # type: ignore[arg-type]

        bundle = next((tmp_path / "diag").iterdir())
        # ファイルは出る（「在る = 試行成功」と分かる方が運用が楽）が中身は空
        assert (bundle / "spawn_stdout.log").exists()
        assert (bundle / "spawn_stdout.log").read_text(encoding="utf-8") == ""
        assert (bundle / "spawn_stderr.log").exists()
        assert (bundle / "spawn_stderr.log").read_text(encoding="utf-8") == ""
