"""ADR-0005 D3/D6: selector 解決順と sticky 安定キーの単体テスト。

`gateways.mcp._resolve_selector` / `_stable_sticky_selector` と
`client._match_selector` が Hub `resolve_instance` と同じ semantics
（launch_token > @label > label > instance_id、pid 廃止）であることを担保する。
"""

from __future__ import annotations

from qgis_puppeteer.client import _match_selector
from qgis_puppeteer.gateways.mcp import _resolve_selector, _stable_sticky_selector
from qgis_puppeteer.protocol import InstanceInfo


def _info(
    instance_id: str,
    label: str,
    pid: int = 1,
    *,
    launch_token: str | None = None,
    project: str | None = None,
    label_explicit: bool = True,
) -> InstanceInfo:
    return InstanceInfo(
        instance_id=instance_id,
        label=label,
        pid=pid,
        project=project,
        launch_token=launch_token,
        label_explicit=label_explicit,
    )


class TestResolveSelectorOrder:
    def test_launch_token_top_precedence(self) -> None:
        # label と launch_token が別 instance を指す → token が勝つ
        a = _info("w-1", "shared", launch_token="lt-aaa")
        b = _info("w-2", "lt-aaa")  # label がたまたま token 文字列
        out = _resolve_selector("lt-aaa", [a, b])
        assert out is a

    def test_label_then_instance_id(self) -> None:
        a = _info("w-1", "A")
        b = _info("w-2", "B")
        assert _resolve_selector("B", [a, b]) is b
        assert _resolve_selector("w-1", [a, b]) is a

    def test_at_label(self) -> None:
        a = _info("w-1", "A")
        assert _resolve_selector("@A", [a]) is a

    def test_pid_no_longer_resolves(self) -> None:
        a = _info("w-1", "A", pid=4321)
        assert _resolve_selector("4321", [a]) is None

    def test_ambiguous_returns_none(self) -> None:
        a = _info("w-1", "dup")
        b = _info("w-2", "dup")
        assert _resolve_selector("dup", [a, b]) is None


class TestStableStickySelector:
    def test_prefers_launch_token(self) -> None:
        info = _info("w-1", "A", launch_token="lt-zzz")
        assert _stable_sticky_selector(info) == "lt-zzz"

    def test_falls_back_to_explicit_label(self) -> None:
        info = _info("w-1", "role-a", label_explicit=True)
        assert _stable_sticky_selector(info) == "role-a"

    def test_auto_label_not_used_for_sticky(self) -> None:
        # ADR-0005 D3: auto-label は再起動で変わるので sticky には使わない
        info = _info("w-1", "a-1234", label_explicit=False)
        assert _stable_sticky_selector(info) == "w-1"

    def test_instance_id_last_resort(self) -> None:
        info = InstanceInfo(instance_id="w-9", label="", pid=1)
        assert _stable_sticky_selector(info) == "w-9"


class TestClientMatchSelectorParity:
    """client._match_selector は Hub / gateway と同じ解決順であること。"""

    def test_token_then_label_then_id(self) -> None:
        a = _info("w-1", "A", launch_token="lt-1")
        b = _info("w-2", "B")
        assert _match_selector("lt-1", [a, b]) is a
        assert _match_selector("B", [a, b]) is b
        assert _match_selector("w-1", [a, b]) is a
        assert _match_selector("@A", [a, b]) is a

    def test_ambiguous_and_missing_none(self) -> None:
        a = _info("w-1", "dup")
        b = _info("w-2", "dup")
        assert _match_selector("dup", [a, b]) is None
        assert _match_selector("nope", [a, b]) is None
