"""MCP gateway の操作記録（#8）の単体テスト。

gateway に組み込んだ通しの確認は ``test_mcp_gateway.py`` の
``TestMcpGatewayRecording``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from qgis_puppeteer.gateways import recording as rec


def _lines(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


class TestRecorder:
    def test_nothing_is_written_until_started(self, tmp_path: Path) -> None:
        r = rec.McpRecorder(tmp_path)
        r.record(
            command="qgis_x", params={}, result="{}", is_error=False, instance=None, duration_ms=1
        )
        assert not r.active
        assert list(tmp_path.iterdir()) == []

    def test_start_step_stop(self, tmp_path: Path) -> None:
        r = rec.McpRecorder(tmp_path)
        started = r.start("explore parcels")
        path = Path(started["path"])
        assert path.parent == tmp_path
        assert path.name.endswith("_explore_parcels.jsonl")

        r.record(
            command="qgis_list_layers",
            params={"instance": None},
            result='{"layers": []}',
            is_error=False,
            instance="A",
            duration_ms=1.25,
        )
        assert r.step("レイヤを開く") == {"recording": True, "step": "レイヤを開く"}
        r.record(
            command="qgis_get_layer_info",
            params={"layer_name": "L"},
            result='{"x": 1}',
            is_error=False,
            instance=None,
            duration_ms=2,
        )
        stopped = r.stop()
        assert stopped == {"recording": False, "path": str(path), "actions": 2}

        first, second = _lines(path)
        assert first["command"] == "qgis_list_layers"
        assert first["params"] == {}  # None は落とす
        assert first["result"] == {"layers": []}
        assert first["instance"] == "A"
        assert first["source"] == "mcp"
        assert first["step_path"] == []
        assert second["step_path"] == ["レイヤを開く"]

    def test_stop_without_start(self, tmp_path: Path) -> None:
        assert rec.McpRecorder(tmp_path).stop() == {"recording": False}

    def test_starting_again_closes_the_previous_one(self, tmp_path: Path) -> None:
        r = rec.McpRecorder(tmp_path)
        first = r.start("a")
        second = r.start("a")
        assert second["previous"] == {"path": first["path"], "actions": 0}
        assert second["path"] != first["path"]
        r.stop()

    def test_name_cannot_escape_the_directory(self, tmp_path: Path) -> None:
        r = rec.McpRecorder(tmp_path)
        path = Path(r.start("../../etc/passwd")["path"])
        r.stop()
        assert path.parent == tmp_path
        assert ".." not in path.name and "/" not in path.name

    def test_directory_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rec.ENV_RECORD_DIR, str(tmp_path / "rec"))
        r = rec.McpRecorder()
        path = Path(r.start("x")["path"])
        r.stop()
        assert path.parent == tmp_path / "rec"

    def test_tool_error_keeps_the_whole_body(self, tmp_path: Path) -> None:
        class _Content:
            type = "text"
            text = '{"error": {"code": "hub_unreachable"}}' + "x" * 5000

        class _Result:
            content = [_Content()]
            is_error = True

        r = rec.McpRecorder(tmp_path)
        path = r.start("e")["path"]
        r.record(
            command="qgis_list_layers",
            params={},
            result=_Result(),
            is_error=True,
            instance=None,
            duration_ms=0,
        )
        r.stop()
        [line] = _lines(path)
        assert line["result"] is None
        assert line["error"]["type"] == "tool_error"
        assert len(line["error"]["message"]) > 5000

    def test_value_put_into_a_password_field_is_redacted(self, tmp_path: Path) -> None:
        """#10: 結果が masked なら、記録の value は "***" になる。"""

        class _Content:
            type = "text"
            text = json.dumps({"success": True, "masked": True, "prior_value": None})

        class _Result:
            content = [_Content()]
            is_error = False

        r = rec.McpRecorder(tmp_path)
        path = r.start("pw")["path"]
        r.record(
            command="qgis_set_widget_value",
            params={"selector": {"object_name": "password"}, "value": "s3cret"},
            result=_Result(),
            is_error=False,
            instance=None,
            duration_ms=0,
        )
        r.stop()
        [line] = _lines(path)
        assert line["params"]["value"] == "***"
        assert "s3cret" not in Path(path).read_text(encoding="utf-8")


class TestSummarizeResult:
    def test_json_text_is_parsed(self) -> None:
        assert rec.summarize_result('{"a": 1}') == {"a": 1}

    def test_plain_text_stays_text(self) -> None:
        assert rec.summarize_result("hello") == "hello"

    def test_long_results_are_truncated(self) -> None:
        text = json.dumps({"tree": "x" * 10_000})
        out = rec.summarize_result(text)
        assert out["truncated"] is True
        assert out["chars"] == len(text)
        assert len(out["head"]) == rec.MAX_RESULT_CHARS

    def test_images_are_described_not_embedded(self) -> None:
        class _Text:
            type = "text"
            text = '{"path": "a.png"}'

        class _Image:
            type = "image"
            data = "QUJD" * 100
            mime_type = "image/png"

        class _Result:
            content = [_Text(), _Image()]

        out = rec.summarize_result(_Result())
        assert out == {"body": {"path": "a.png"}, "images": [{"image": "image/png", "bytes": 300}]}


def test_env_switch_records_the_whole_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QPUPPETEER_MCP_RECORD=1 なら gateway の起動から終了まで記録する。"""
    pytest.importorskip("mcp")
    import asyncio

    from qgis_puppeteer.gateways.mcp import _default_lifespan

    monkeypatch.setenv(rec.ENV_RECORD, "1")
    monkeypatch.setenv(rec.ENV_RECORD_DIR, str(tmp_path))

    async def run() -> bool:
        async with _default_lifespan(None) as ctx:  # type: ignore[arg-type]
            return ctx.recorder.active

    assert asyncio.run(run()) is True
    [path] = list(tmp_path.iterdir())
    assert path.name.endswith("_session.jsonl")


def test_unwritable_record_dir_does_not_stop_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from qgis_puppeteer.gateways.mcp import _default_lifespan

    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    monkeypatch.setenv(rec.ENV_RECORD, "1")
    monkeypatch.setenv(rec.ENV_RECORD_DIR, str(not_a_dir / "sub"))

    async def run() -> bool:
        async with _default_lifespan(None) as ctx:  # type: ignore[arg-type]
            return ctx.recorder.active

    assert asyncio.run(run()) is False
