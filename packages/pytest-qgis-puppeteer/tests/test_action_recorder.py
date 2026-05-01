"""``pytest_qgis_puppeteer._action_recorder`` の単体テスト。

ActionRecorder は B5a 追加の純粋データ構造（network や fixture を要さない）。
record / step / dump_jsonl / clear / max_actions / serialise 失敗 / thread 安全
を独立して検証する。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from pytest_qgis_puppeteer._action_recorder import (
    DEFAULT_MAX_ACTIONS,
    ActionRecord,
    ActionRecorder,
    _ensure_recorder_attached,
    _format_error,
    _safe_jsonify,
)

# ============================================================
# record の基本動作
# ============================================================


class TestRecordBasic:
    def test_records_minimal(self) -> None:
        r = ActionRecorder()
        r.record(command="qgis_click_widget", params={"selector": {"object_name": "ok"}})
        actions = r.actions()
        assert len(actions) == 1
        assert actions[0].command == "qgis_click_widget"
        assert actions[0].params == {"selector": {"object_name": "ok"}}
        assert actions[0].result is None
        assert actions[0].error is None
        assert actions[0].step_path == ()

    def test_records_with_result(self) -> None:
        r = ActionRecorder()
        r.record(
            command="qgis_list_layers",
            params={},
            result={"layers": ["a", "b"], "count": 2},
            duration_ms=12.5,
        )
        rec = r.actions()[0]
        assert rec.result == {"layers": ["a", "b"], "count": 2}
        assert rec.duration_ms == 12.5

    def test_records_with_error(self) -> None:
        r = ActionRecorder()
        try:
            raise ValueError("boom")
        except ValueError as exc:
            r.record(command="qgis_screenshot", params={}, error=exc)
        rec = r.actions()[0]
        assert rec.error == {"type": "ValueError", "message": "boom"}
        # error 時 result は None（重複情報を出さない）
        assert rec.result is None

    def test_negative_duration_clamped_to_zero(self) -> None:
        r = ActionRecorder()
        r.record(command="x", params={}, duration_ms=-5.0)
        assert r.actions()[0].duration_ms == 0.0

    def test_instance_field(self) -> None:
        r = ActionRecorder()
        r.record(command="x", params={}, instance="A")
        assert r.actions()[0].instance == "A"


# ============================================================
# step() context manager
# ============================================================


class TestStep:
    def test_step_path_recorded(self) -> None:
        r = ActionRecorder()
        with r.step("ログイン"):
            r.record(command="x", params={})
        assert r.actions()[0].step_path == ("ログイン",)

    def test_nested_steps(self) -> None:
        r = ActionRecorder()
        with r.step("外側"), r.step("内側"):
            r.record(command="x", params={})
        assert r.actions()[0].step_path == ("外側", "内側")

    def test_step_pops_on_exit(self) -> None:
        r = ActionRecorder()
        with r.step("a"):
            r.record(command="x", params={})
        # context exit 後の record は path 空
        r.record(command="y", params={})
        assert r.actions()[0].step_path == ("a",)
        assert r.actions()[1].step_path == ()

    def test_step_pops_on_exception(self) -> None:
        r = ActionRecorder()
        with pytest.raises(RuntimeError), r.step("blow_up"):
            raise RuntimeError("test")
        # 例外で抜けても stack が空に戻っていることを次の record で確認
        r.record(command="x", params={})
        assert r.actions()[0].step_path == ()

    def test_current_step_path_inside(self) -> None:
        r = ActionRecorder()
        assert r.current_step_path() == ()
        with r.step("a"):
            assert r.current_step_path() == ("a",)
            with r.step("b"):
                assert r.current_step_path() == ("a", "b")
            assert r.current_step_path() == ("a",)
        assert r.current_step_path() == ()

    def test_inconsistent_stack_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """``reset_step_stack`` で stack を空にした後 contextmanager exit すると
        warn だけ出して例外は出さない（best-effort 復旧）。
        """
        r = ActionRecorder()
        cm = r.step("a")
        cm.__enter__()
        r.reset_step_stack()  # stack を強制クリア → context exit で不整合
        with caplog.at_level("WARNING", logger="pytest_qgis_puppeteer._action_recorder"):
            cm.__exit__(None, None, None)
        # warn が出たか
        assert any("inconsistency" in rec.message for rec in caplog.records)


# ============================================================
# clear / reset_step_stack
# ============================================================


class TestClearAndReset:
    def test_clear_empties_actions(self) -> None:
        r = ActionRecorder()
        r.record(command="x", params={})
        r.clear()
        assert r.actions() == ()

    def test_clear_does_not_reset_step_stack(self) -> None:
        r = ActionRecorder()
        cm = r.step("a")
        cm.__enter__()
        try:
            r.clear()
            # clear は actions だけ。stack は維持
            assert r.current_step_path() == ("a",)
        finally:
            cm.__exit__(None, None, None)

    def test_reset_step_stack_clears_stack(self) -> None:
        r = ActionRecorder()
        cm = r.step("a")
        cm.__enter__()
        r.reset_step_stack()
        assert r.current_step_path() == ()
        # context exit は best-effort で warn のみ
        cm.__exit__(None, None, None)


# ============================================================
# max_actions による drop-oldest
# ============================================================


class TestMaxActions:
    def test_drops_oldest_when_full(self) -> None:
        r = ActionRecorder(max_actions=3)
        for i in range(5):
            r.record(command=f"cmd_{i}", params={})
        actions = r.actions()
        assert len(actions) == 3
        # 古い 2 件（cmd_0 / cmd_1）が落ちて新しい 3 件が残る
        assert [a.command for a in actions] == ["cmd_2", "cmd_3", "cmd_4"]

    def test_max_actions_below_one_clamped(self) -> None:
        r = ActionRecorder(max_actions=0)
        r.record(command="x", params={})
        # 0 だと何も入らないので、本実装は最低 1 にクランプ
        assert len(r.actions()) == 1

    def test_default_max_is_high(self) -> None:
        # ADR-0002 §13 の想定（数十〜数百 actions / test）に対し、5000 はゆとり
        assert DEFAULT_MAX_ACTIONS >= 1000


# ============================================================
# dump_jsonl
# ============================================================


class TestDumpJsonl:
    def test_writes_one_line_per_action(self, tmp_path: Path) -> None:
        r = ActionRecorder()
        with r.step("step_a"):
            r.record(command="cmd1", params={"k": 1}, result={"ok": True})
            r.record(command="cmd2", params={"k": 2}, result={"ok": False})
        out = tmp_path / "actions.jsonl"
        r.dump_jsonl(out)
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        rec0 = json.loads(lines[0])
        rec1 = json.loads(lines[1])
        assert rec0["command"] == "cmd1"
        assert rec0["step_path"] == ["step_a"]
        assert rec1["command"] == "cmd2"
        assert rec0["schema_version"] == 2  # B5b: bumped to 2

    def test_empty_actions_creates_empty_file(self, tmp_path: Path) -> None:
        r = ActionRecorder()
        out = tmp_path / "actions.jsonl"
        r.dump_jsonl(out)
        # ファイルは存在し、空（呼び出し側が「ファイル存在 = dump 試行成功」を判定可）
        assert out.exists()
        assert out.read_text(encoding="utf-8") == ""

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        r = ActionRecorder()
        r.record(command="x", params={})
        out = tmp_path / "deeply" / "nested" / "actions.jsonl"
        r.dump_jsonl(out)
        assert out.exists()

    def test_japanese_step_names_not_escaped(self, tmp_path: Path) -> None:
        """``ensure_ascii=False`` で日本語 step がそのまま読める。"""
        r = ActionRecorder()
        with r.step("ログイン"):
            r.record(command="x", params={"key": "値"})
        out = tmp_path / "actions.jsonl"
        r.dump_jsonl(out)
        content = out.read_text(encoding="utf-8")
        assert "ログイン" in content
        assert "値" in content


# ============================================================
# serialise の安全性
# ============================================================


class TestSafeJsonify:
    def test_passes_through_basic_types(self) -> None:
        for v in [None, 1, "x", [1, 2], {"a": 1}, True]:
            assert _safe_jsonify(v) == v

    def test_path_becomes_string(self) -> None:
        # Path は default=str で文字列に
        out = _safe_jsonify({"path": Path("/tmp/x")})
        assert isinstance(out, dict)
        assert isinstance(out["path"], str)

    def test_circular_returns_unrepresentable(self) -> None:
        d: dict[str, object] = {"k": "v"}
        d["self"] = d  # 循環
        assert _safe_jsonify(d) == "<unrepresentable>"

    def test_bytes_handled(self) -> None:
        # bytes は default=str で repr 化される（unrepresentable にはならない）
        out = _safe_jsonify({"b": b"xyz"})
        assert isinstance(out, dict)
        assert isinstance(out["b"], str)


class TestFormatError:
    def test_basic_exception(self) -> None:
        try:
            raise ValueError("nope")
        except ValueError as exc:
            d = _format_error(exc)
        assert d == {"type": "ValueError", "message": "nope"}

    def test_broken_str(self) -> None:
        class BadStr(Exception):
            def __str__(self) -> str:
                raise RuntimeError("__str__ broken")

        d = _format_error(BadStr())
        assert d["type"] == "BadStr"
        # message は何らかの非空 str（"<unrepresentable>" 等）
        assert isinstance(d["message"], str)


# ============================================================
# _ensure_recorder_attached helper
# ============================================================


class TestEnsureRecorderAttached:
    def test_calls_set_recorder(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.attached: ActionRecorder | None = None

            def set_recorder(self, recorder: ActionRecorder | None) -> None:
                self.attached = recorder

        client = FakeClient()
        rec = ActionRecorder()
        _ensure_recorder_attached(client, rec)
        assert client.attached is rec

    def test_no_setter_is_noop(self) -> None:
        class OldClient:
            pass

        rec = ActionRecorder()
        # 例外を出さない（後方互換）
        _ensure_recorder_attached(OldClient(), rec)


# ============================================================
# B5b: screenshot_dir / next_screenshot_path / record(screenshot_path=...)
# ============================================================


class TestScreenshotDir:
    def test_default_is_none(self) -> None:
        r = ActionRecorder()
        assert r.screenshot_dir is None

    def test_set_and_get(self, tmp_path: Path) -> None:
        r = ActionRecorder()
        r.set_screenshot_dir(tmp_path / "ss")
        assert r.screenshot_dir == tmp_path / "ss"

    def test_set_none_disables(self, tmp_path: Path) -> None:
        r = ActionRecorder()
        r.set_screenshot_dir(tmp_path / "ss")
        r.set_screenshot_dir(None)
        assert r.screenshot_dir is None

    def test_next_screenshot_path_raises_without_dir(self) -> None:
        r = ActionRecorder()
        with pytest.raises(RuntimeError, match="screenshot_dir is not set"):
            r.next_screenshot_path()

    def test_next_screenshot_path_uses_seq(self, tmp_path: Path) -> None:
        """seq 番号は次に append される record の index（= 現 actions 件数）。"""
        r = ActionRecorder()
        r.set_screenshot_dir(tmp_path / "ss")
        # 0 件目
        assert r.next_screenshot_path() == tmp_path / "ss" / "0000.png"
        r.record(command="cmd", params={})
        # 1 件目
        assert r.next_screenshot_path() == tmp_path / "ss" / "0001.png"
        r.record(command="cmd", params={})
        assert r.next_screenshot_path() == tmp_path / "ss" / "0002.png"

    def test_seq_zero_padded_to_four_digits(self, tmp_path: Path) -> None:
        r = ActionRecorder()
        r.set_screenshot_dir(tmp_path / "ss")
        assert r.next_screenshot_path().name == "0000.png"


class TestRecordWithScreenshotPath:
    def test_default_is_none(self) -> None:
        r = ActionRecorder()
        r.record(command="cmd", params={})
        assert r.actions()[0].screenshot_path is None

    def test_records_supplied_path(self) -> None:
        r = ActionRecorder()
        r.record(command="cmd", params={}, screenshot_path="screenshots/0042.png")
        assert r.actions()[0].screenshot_path == "screenshots/0042.png"

    def test_dump_jsonl_includes_screenshot_path(self, tmp_path: Path) -> None:
        r = ActionRecorder()
        r.record(command="a", params={}, screenshot_path="screenshots/0001.png")
        r.record(command="b", params={})
        out = tmp_path / "actions.jsonl"
        r.dump_jsonl(out)
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        rec0 = json.loads(lines[0])
        rec1 = json.loads(lines[1])
        assert rec0["screenshot_path"] == "screenshots/0001.png"
        assert rec1["screenshot_path"] is None


# ============================================================
# thread 安全性（軽い smoke）
# ============================================================


class TestThreadSafety:
    def test_concurrent_records_do_not_corrupt(self) -> None:
        r = ActionRecorder()
        n_per_thread = 100
        n_threads = 4

        def worker(tid: int) -> None:
            for i in range(n_per_thread):
                r.record(command=f"t{tid}_{i}", params={"i": i})

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 全 record が混ざらず格納されている（lock が効いている）
        assert len(r.actions()) == n_per_thread * n_threads

    def test_thread_local_step_stack(self) -> None:
        """別 thread の step は混ざらない（thread-local の挙動）。"""
        r = ActionRecorder()
        results: list[tuple[int, tuple[str, ...]]] = []
        results_lock = threading.Lock()

        def worker(tid: int) -> None:
            with r.step(f"t{tid}"), results_lock:
                # その thread から見た step path
                results.append((tid, r.current_step_path()))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 各 thread は自分の "t<tid>" しか見えない
        for tid, path in results:
            assert path == (f"t{tid}",)


# ============================================================
# ActionRecord.to_dict
# ============================================================


class TestActionRecordToDict:
    def test_round_trip(self) -> None:
        rec = ActionRecord(
            ts="2026-04-30T10:00:00.000000",
            duration_ms=1.5,
            command="cmd",
            params={"a": 1},
            result={"ok": True},
            error=None,
            instance="A",
            step_path=("s1", "s2"),
        )
        d = rec.to_dict()
        # JSON で round-trip できる
        s = json.dumps(d, ensure_ascii=False)
        d2 = json.loads(s)
        assert d2["command"] == "cmd"
        assert d2["step_path"] == ["s1", "s2"]
        assert d2["instance"] == "A"
        assert d2["schema_version"] == 2  # B5b: bumped
        # B5b: screenshot_path は default None で常に含まれる
        assert d2["screenshot_path"] is None

    def test_with_screenshot_path(self) -> None:
        """B5b: ``screenshot_path`` が non-None なら to_dict に含まれる。"""
        rec = ActionRecord(
            ts="2026-05-01T10:00:00.000000",
            duration_ms=0.0,
            command="cmd",
            params={},
            result=None,
            error=None,
            instance=None,
            step_path=(),
            screenshot_path="screenshots/0007.png",
        )
        d = rec.to_dict()
        assert d["screenshot_path"] == "screenshots/0007.png"
