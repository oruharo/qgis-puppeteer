"""qgis_puppeteer.launcher の単体テスト（ADR-0005 D6）。

純粋関数（token 発番 / env 注入）と CLI 引数パースを Qt/QGIS 非依存で検証する。
実際の subprocess 起動は伴わない（`--print-token` 経路と引数分解のみ）。
"""

from __future__ import annotations

from qgis_puppeteer.launcher import (
    ENV_LAUNCH_TOKEN,
    ENV_WORKER_LABEL,
    _parse_args,
    generate_launch_token,
    inject_launch_token,
    main,
)


class TestGenerateLaunchToken:
    def test_prefixed_and_unique(self) -> None:
        a = generate_launch_token()
        b = generate_launch_token()
        assert a.startswith("lt-")
        assert a != b
        # instance_id（w-）と prefix で区別できる
        assert not a.startswith("w-")

    def test_reasonable_entropy(self) -> None:
        # lt- + 20 hex chars (80bit)
        tok = generate_launch_token()
        assert len(tok) == len("lt-") + 20


class TestInjectLaunchToken:
    def test_does_not_mutate_source(self) -> None:
        src = {"PATH": "/bin"}
        out = inject_launch_token(src, "lt-x")
        assert out[ENV_LAUNCH_TOKEN] == "lt-x"
        assert ENV_LAUNCH_TOKEN not in src  # 元 dict は不変
        assert out["PATH"] == "/bin"

    def test_label_optional(self) -> None:
        out_no_label = inject_launch_token({}, "lt-x")
        assert ENV_WORKER_LABEL not in out_no_label
        out_label = inject_launch_token({}, "lt-x", label="role-a")
        assert out_label[ENV_WORKER_LABEL] == "role-a"


class TestParseArgs:
    def test_separates_command_after_double_dash(self) -> None:
        ns, cmd = _parse_args(["--label", "A", "--", "qgis", "--clean-canvas"])
        assert ns.label == "A"
        assert ns.print_token is False
        assert cmd == ["qgis", "--clean-canvas"]

    def test_print_token_flag(self) -> None:
        ns, cmd = _parse_args(["--print-token"])
        assert ns.print_token is True
        assert cmd == []


class TestMainPrintToken:
    def test_print_token_only_emits_and_returns_zero(self, capsys) -> None:
        rc = main(["--print-token"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out.startswith("launch_token=lt-")

    def test_no_command_is_usage_error(self, capsys) -> None:
        rc = main([])
        assert rc == 2
        captured = capsys.readouterr()
        # token は常に先に出す（呼び出し側が拾えるように）
        assert "launch_token=lt-" in captured.out
        assert "no launch command" in captured.err
