"""`message_recorder`（QgsMessageLog のリングバッファ）の単体テスト（#5）。

購読（``install``）以外は QGIS に依存しないので venv で回す。本物の
QgsMessageLog / メッセージバーとの結線は ``test_message_recorder_integration.py``。
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Iterator

import pytest
from qgis_puppeteer.qgis_tools import message_recorder as mr


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    mr._reset_for_tests()
    yield
    mr._reset_for_tests()


class _Level(enum.IntEnum):
    """``Qgis.MessageLevel`` と同じ数値の並び。"""

    Info = 0
    Warning = 1
    Critical = 2
    Success = 3
    NoLevel = 4


class TestLevels:
    @pytest.mark.parametrize(
        ("level", "name"),
        [
            (_Level.Info, "info"),
            (_Level.Warning, "warning"),
            (_Level.Critical, "critical"),
            (_Level.Success, "success"),
            (_Level.NoLevel, "none"),
            (2, "critical"),
            ("Warning", "warning"),
            (99, "none"),
        ],
    )
    def test_level_name(self, level: object, name: str) -> None:
        assert mr.level_name(level) == name

    def test_success_is_not_heavier_than_critical(self) -> None:
        """数値では Success=3 > Critical=2。数値で比べると成功通知で落ちる。"""
        mr.record("done", "Processing", _Level.Success)
        mr.record("boom", "MyPlugin", _Level.Critical)
        assert [m["message"] for m in mr.get_recent(min_level="critical")] == ["boom"]
        assert [m["message"] for m in mr.get_recent(min_level="warning")] == ["boom"]
        assert len(mr.get_recent(min_level="info")) == 2

    def test_unknown_min_level_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_level"):
            mr.get_recent(min_level="error")


class TestFilters:
    def _fill(self) -> None:
        mr.record("a", "A", _Level.Info)
        mr.record("b", "B", _Level.Warning)
        mr.record("c", "A", _Level.Critical)
        mr.record("Title : body", "Messages", _Level.Warning)

    def test_records_in_order_with_fields(self) -> None:
        self._fill()
        got = mr.get_recent()
        assert [m["seq"] for m in got] == [1, 2, 3, 4]
        first = got[0]
        assert {k: first[k] for k in ("level", "tag", "message", "source")} == {
            "level": "info",
            "tag": "A",
            "message": "a",
            "source": "log",
        }
        assert first["ts"] and first["thread"]

    def test_tags_sources_after_seq_limit(self) -> None:
        self._fill()
        assert [m["message"] for m in mr.get_recent(tags=["A"])] == ["a", "c"]
        assert [m["message"] for m in mr.get_recent(sources=["message_bar"])] == ["Title : body"]
        assert [m["seq"] for m in mr.get_recent(after_seq=2)] == [3, 4]
        assert [m["seq"] for m in mr.get_recent(limit=1)] == [4]
        assert mr.get_recent(limit=0) == []

    def test_clear_keeps_the_sequence_going(self) -> None:
        """差分取得（after_seq）が clear をまたいでも壊れないこと。"""
        self._fill()
        mr.clear()
        assert mr.get_recent() == []
        assert mr.last_seq() == 4
        mr.record("d", "A", _Level.Info)
        assert [m["seq"] for m in mr.get_recent()] == [5]

    def test_returned_records_are_copies(self) -> None:
        mr.record("a", "A", _Level.Info)
        mr.get_recent()[0]["message"] = "changed"
        assert mr.get_recent()[0]["message"] == "a"


def test_ring_buffer_drops_the_oldest() -> None:
    for i in range(mr.size_limit() + 5):
        mr.record(str(i), "T", _Level.Info)
    got = mr.get_recent()
    assert len(got) == mr.size_limit()
    assert got[0]["message"] == "5"


def test_unprintable_message_does_not_raise() -> None:
    class Bad:
        def __str__(self) -> str:
            raise RuntimeError("no")

    mr.record(Bad(), "T", _Level.Critical)
    [entry] = mr.get_recent()
    assert "failed to format" in entry["message"]


def test_concurrent_records_get_unique_sequence_numbers() -> None:
    """logMessage は描画スレッド等からも呼ばれる。"""

    def worker() -> None:
        for _ in range(100):
            mr.record("x", "T", _Level.Info)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [m["seq"] for m in mr.get_recent()]
    assert len(seqs) == len(set(seqs)) == 800
    assert seqs == sorted(seqs)


def test_an_info_flood_does_not_push_out_an_earlier_error() -> None:
    """Processing の進捗などで info が大量に流れても、その前の critical は残る。"""
    mr.record("the real failure", "MyPlugin", _Level.Critical)
    for i in range(mr.size_limit() * 2):
        mr.record(f"progress {i}", "Processing", _Level.Info)
    assert [m["message"] for m in mr.get_recent(min_level="critical")] == ["the real failure"]
    assert [m["message"] for m in mr.get_recent(min_level="warning")] == ["the real failure"]
    # 全件の照会では押し出されている（バッファは 1000 件）
    assert all(m["level"] == "info" for m in mr.get_recent())
    mr.clear()
    assert mr.get_recent(min_level="critical") == []
