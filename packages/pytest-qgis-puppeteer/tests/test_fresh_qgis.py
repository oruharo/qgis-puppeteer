"""``@pytest.mark.fresh_qgis`` の単体・統合テスト。

ADR-0002 §10.4 / ADR-0003 §fresh_qgis 合成ルール の実装を検証する:

- ``_resolve_fresh_qgis_spawn``: env spec と marker 引数の合成規則
- pytester 統合: marker が collection を通り、fixture が active env と組み合わさる

実 QGIS は使わない（spawn_qgis は subprocess を mock する設計）。
"""

from __future__ import annotations

import textwrap
from typing import Any

import pytest
from pytest_qgis_puppeteer._environments import EnvironmentSpec
from pytest_qgis_puppeteer.plugin import _resolve_fresh_qgis_spawn

pytest_plugins = ["pytester"]


# ============================================================
# fakes for unit tests
# ============================================================


class _FakeConfig:
    """``pytest.Config`` の最低限互換 stub（plugin.py の解決ヘルパが使う）。"""

    def __init__(
        self,
        *,
        options: dict[str, Any] | None = None,
        ini: dict[str, Any] | None = None,
    ) -> None:
        self._options = options or {}
        self._ini = ini or {}

    def getoption(self, name: str, *, default: Any = None) -> Any:
        return self._options.get(name, default)

    def getini(self, name: str) -> Any:
        return self._ini.get(name, "")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """親プロセス env が漏れ込まないよう毎テスト delete。"""
    for key in (
        "QPUPPETEER_QGIS_BIN",
        "QPUPPETEER_QGIS_PYTHON",
        "QPUPPETEER_QGIS_ARGS",
        "QPUPPETEER_QGIS_COMMAND",
    ):
        monkeypatch.delenv(key, raising=False)


# ============================================================
# _resolve_fresh_qgis_spawn: 合成ルール
# ============================================================


class TestResolveFreshQgisSpawnWithoutEnv:
    """active_env=None（plain ini 設定のみ）の合成。"""

    def test_qgis_bin_only_appends_extra_args(self) -> None:
        config = _FakeConfig(ini={"qgis_bin": "qgis-bin.exe"})
        cmd, qgis_bin, args, env_vars = _resolve_fresh_qgis_spawn(
            config,  # type: ignore[arg-type]
            None,
            extra_args=["--clean-canvas"],
            extra_env={},
        )
        assert cmd is None
        assert qgis_bin == "qgis-bin.exe"
        assert args == ["--clean-canvas"]
        assert env_vars == {}

    def test_qgis_bin_with_existing_args_appends(self) -> None:
        config = _FakeConfig(
            ini={
                "qgis_bin": "qgis-bin.exe",
                "qgis_args": ["--profile=test"],
            }
        )
        cmd, qgis_bin, args, env_vars = _resolve_fresh_qgis_spawn(
            config,  # type: ignore[arg-type]
            None,
            extra_args=["--clean-canvas"],
            extra_env={},
        )
        assert cmd is None
        assert qgis_bin == "qgis-bin.exe"
        # 既存 args + extra_args の append
        assert args == ["--profile=test", "--clean-canvas"]
        assert env_vars == {}

    def test_qgis_command_appends_extra_args_at_end(self) -> None:
        config = _FakeConfig(ini={"qgis_command": ["launcher.bat", "--config", "e2e"]})
        cmd, qgis_bin, args, env_vars = _resolve_fresh_qgis_spawn(
            config,  # type: ignore[arg-type]
            None,
            extra_args=["--feature", "x"],
            extra_env={},
        )
        # qgis_command 指定下では fresh_qgis args は command 末尾に append
        assert cmd == ["launcher.bat", "--config", "e2e", "--feature", "x"]
        assert qgis_bin is None
        assert args == []
        assert env_vars == {}

    def test_no_args_yields_empty_args(self) -> None:
        config = _FakeConfig(ini={"qgis_bin": "qgis-bin.exe"})
        cmd, qgis_bin, args, env_vars = _resolve_fresh_qgis_spawn(
            config,  # type: ignore[arg-type]
            None,
            extra_args=[],
            extra_env={},
        )
        assert cmd is None
        assert qgis_bin == "qgis-bin.exe"
        assert args == []
        assert env_vars == {}

    def test_extra_env_passes_through(self) -> None:
        config = _FakeConfig(ini={"qgis_bin": "qgis-bin.exe"})
        _, _, _, env_vars = _resolve_fresh_qgis_spawn(
            config,  # type: ignore[arg-type]
            None,
            extra_args=[],
            extra_env={"MYAPP_DEBUG": "1"},
        )
        assert env_vars == {"MYAPP_DEBUG": "1"}

    def test_no_qgis_bin_raises_usage_error(self) -> None:
        config = _FakeConfig()
        with pytest.raises(pytest.UsageError, match="qgis_command nor qgis_bin"):
            _resolve_fresh_qgis_spawn(
                config,  # type: ignore[arg-type]
                None,
                extra_args=[],
                extra_env={},
            )


class TestResolveFreshQgisSpawnWithEnv:
    """active_env が set されている場合の合成（ADR-0003 §fresh_qgis ルール）。"""

    def test_env_args_plus_marker_args_appends(self) -> None:
        env = EnvironmentSpec(
            name="auth",
            qgis_bin="env-qgis.exe",
            qgis_args=("--profile=auth",),
        )
        config = _FakeConfig()
        cmd, qgis_bin, args, env_vars = _resolve_fresh_qgis_spawn(
            config,  # type: ignore[arg-type]
            env,
            extra_args=["--clean-canvas"],
            extra_env={},
        )
        assert cmd is None
        assert qgis_bin == "env-qgis.exe"
        # ADR-0003: env.qgis_args + extra_args を append
        assert args == ["--profile=auth", "--clean-canvas"]

    def test_env_table_merged_with_extra_env(self) -> None:
        """``env`` table の shallow merge: 同一キーは fresh_qgis(env=) 優先。"""
        env = EnvironmentSpec(
            name="auth",
            qgis_bin="env-qgis.exe",
            env={"MYAPP_AUTH": "1", "MYAPP_DEBUG": "0"},
        )
        config = _FakeConfig()
        _, _, _, env_vars = _resolve_fresh_qgis_spawn(
            config,  # type: ignore[arg-type]
            env,
            extra_args=[],
            extra_env={"MYAPP_DEBUG": "1", "MYAPP_EXTRA": "x"},
        )
        # MYAPP_DEBUG は fresh_qgis 側が勝つ
        assert env_vars == {
            "MYAPP_AUTH": "1",
            "MYAPP_DEBUG": "1",
            "MYAPP_EXTRA": "x",
        }

    def test_env_qgis_command_appends_extra_args(self) -> None:
        """qgis_command 指定 env では fresh_qgis args は command 末尾に append。"""
        env = EnvironmentSpec(
            name="host",
            qgis_command=("launcher.bat", "--config", "e2e"),
        )
        config = _FakeConfig()
        cmd, qgis_bin, args, env_vars = _resolve_fresh_qgis_spawn(
            config,  # type: ignore[arg-type]
            env,
            extra_args=["--clean-canvas"],
            extra_env={},
        )
        assert cmd == ["launcher.bat", "--config", "e2e", "--clean-canvas"]
        assert qgis_bin is None
        assert args == []

    def test_cli_overrides_env_for_qgis_bin(self) -> None:
        """CLI > env_spec > ini の優先順位は fresh_qgis 経路でも維持。"""
        env = EnvironmentSpec(name="auth", qgis_bin="env-qgis.exe")
        config = _FakeConfig(options={"qgis_bin": "cli-qgis.exe"})
        _, qgis_bin, _, _ = _resolve_fresh_qgis_spawn(
            config,  # type: ignore[arg-type]
            env,
            extra_args=[],
            extra_env={},
        )
        assert qgis_bin == "cli-qgis.exe"


# ============================================================
# Integration: marker が pytester で正しく collect される
# ============================================================


class TestFreshQgisMarkerRegistration:
    """``--strict-markers`` 下でも ``fresh_qgis`` が unknown と判定されない。"""

    def test_marker_registered(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile(
            test_marker=textwrap.dedent(
                """
                import pytest

                @pytest.mark.fresh_qgis(args=["--clean-canvas"])
                def test_x():
                    pass
                """
            )
        )
        result = pytester.runpytest("--strict-markers", "--collect-only")
        assert result.ret == 0
