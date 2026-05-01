"""``pytest_qgis_puppeteer._environments`` の単体テスト。

TOML loader / 優先順位解決 / marker フィルタをそれぞれ独立して検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_qgis_puppeteer._environments import (
    DefaultEnvironmentConfig,
    EnvironmentsConfig,
    EnvironmentSpec,
    aggregate_exit_codes,
    filter_items_by_env_marker,
    find_environments_toml,
    find_unmarked_items,
    inject_maxfail,
    is_hard_exit_code,
    load_environments,
    parse_env_arg,
    resolve_active_env,
    resolve_qgis_setting,
    strip_env_args,
    strip_maxfail_args,
)

# ============================================================
# load_environments
# ============================================================


class TestLoadEnvironments:
    """``environments.toml`` のパース。"""

    def _write(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "environments.toml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_minimal_single_env(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "smoke"
qgis_args = ["--profile=test"]

[default_environment]
strategy = "named"
default_name = "smoke"
""",
        )
        config = load_environments(p)
        assert len(config.environments) == 1
        assert config.environments[0].name == "smoke"
        assert config.environments[0].qgis_args == ("--profile=test",)
        assert config.default.strategy == "named"
        assert config.default.default_name == "smoke"

    def test_full_features(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "smoke"
description = "minimal"
qgis_bin = "qgis-bin.exe"
qgis_args = ["--profile=test"]
qgis_python = "python-qgis.bat"
env = { MYAPP_FEATURE = "auth" }

[[environments]]
name = "host_launcher"
qgis_command = ["launcher.bat", "--config", "e2e"]

[default_environment]
strategy = "named"
default_name = "smoke"
""",
        )
        config = load_environments(p)
        assert len(config.environments) == 2
        smoke = config.by_name("smoke")
        assert smoke is not None
        assert smoke.qgis_bin == "qgis-bin.exe"
        assert smoke.qgis_args == ("--profile=test",)
        assert smoke.qgis_python == "python-qgis.bat"
        assert smoke.env == {"MYAPP_FEATURE": "auth"}
        assert smoke.description == "minimal"

        host = config.by_name("host_launcher")
        assert host is not None
        assert host.qgis_command == ("launcher.bat", "--config", "e2e")

    def test_default_environment_optional(self, tmp_path: Path) -> None:
        """``[default_environment]`` 未指定の場合は strategy='named' / default_name=None。"""
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "smoke"
""",
        )
        # strategy='named' で default_name=None は load_environments で error
        with pytest.raises(ValueError, match="requires 'default_name'"):
            load_environments(p)

    def test_default_strategy_fail_does_not_require_default_name(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "smoke"

[default_environment]
strategy = "fail"
""",
        )
        config = load_environments(p)
        assert config.default.strategy == "fail"
        assert config.default.default_name is None

    def test_default_name_must_match_an_env(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "smoke"

[default_environment]
strategy = "named"
default_name = "nonexistent"
""",
        )
        with pytest.raises(ValueError, match="does not match any environment"):
            load_environments(p)

    def test_duplicate_env_names_error(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "smoke"

[[environments]]
name = "smoke"

[default_environment]
strategy = "named"
default_name = "smoke"
""",
        )
        with pytest.raises(ValueError, match="duplicate environment name"):
            load_environments(p)

    def test_empty_environments_error(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, "[default_environment]\nstrategy = 'fail'\n")
        with pytest.raises(ValueError, match="missing or empty"):
            load_environments(p)

    def test_invalid_strategy_error(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "smoke"

[default_environment]
strategy = "first"
""",
        )
        with pytest.raises(ValueError, match="must be 'named' or 'fail'"):
            load_environments(p)

    def test_qgis_args_must_be_list_of_strings(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "smoke"
qgis_args = "--profile=test"

[default_environment]
strategy = "named"
default_name = "smoke"
""",
        )
        with pytest.raises(ValueError, match="must be a list"):
            load_environments(p)

    def test_env_table_must_be_strings(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "smoke"
env = { PORT = 9876 }

[default_environment]
strategy = "named"
default_name = "smoke"
""",
        )
        with pytest.raises(ValueError, match="env keys/values must be strings"):
            load_environments(p)


# ============================================================
# find_environments_toml
# ============================================================


class TestFindEnvironmentsToml:
    def test_finds_at_root(self, tmp_path: Path) -> None:
        (tmp_path / "environments.toml").write_text("# stub\n", encoding="utf-8")
        result = find_environments_toml(tmp_path)
        assert result == tmp_path / "environments.toml"

    def test_finds_under_test_e2e(self, tmp_path: Path) -> None:
        (tmp_path / "test_e2e").mkdir()
        target = tmp_path / "test_e2e" / "environments.toml"
        target.write_text("# stub\n", encoding="utf-8")
        result = find_environments_toml(tmp_path)
        assert result == target

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert find_environments_toml(tmp_path) is None


# ============================================================
# resolve_active_env
# ============================================================


class TestResolveActiveEnv:
    """``--env=<name>`` の解決。"""

    def _config(
        self,
        *,
        envs: tuple[EnvironmentSpec, ...],
        strategy: str = "named",
        default_name: str | None = None,
    ) -> EnvironmentsConfig:
        return EnvironmentsConfig(
            environments=envs,
            default=DefaultEnvironmentConfig(strategy=strategy, default_name=default_name),
        )

    def test_cli_match(self) -> None:
        config = self._config(
            envs=(EnvironmentSpec(name="smoke"), EnvironmentSpec(name="auth")),
            default_name="smoke",
        )
        result = resolve_active_env(config, cli_env_name="auth")
        assert result is not None
        assert result.name == "auth"

    def test_cli_unknown_raises_usage_error(self) -> None:
        config = self._config(
            envs=(EnvironmentSpec(name="smoke"),),
            default_name="smoke",
        )
        with pytest.raises(pytest.UsageError, match="does not match any environment"):
            resolve_active_env(config, cli_env_name="unknown")

    def test_cli_unset_uses_default_named(self) -> None:
        config = self._config(
            envs=(EnvironmentSpec(name="smoke"), EnvironmentSpec(name="auth")),
            default_name="smoke",
        )
        result = resolve_active_env(config, cli_env_name=None)
        assert result is not None
        assert result.name == "smoke"

    def test_cli_unset_with_strategy_fail_returns_none(self) -> None:
        config = self._config(
            envs=(EnvironmentSpec(name="smoke"),),
            strategy="fail",
        )
        result = resolve_active_env(config, cli_env_name=None)
        assert result is None


# ============================================================
# filter_items_by_env_marker
# ============================================================


class _FakeMarker:
    def __init__(self, *args: str) -> None:
        self.args = args


class _FakeItem:
    """``pytest.Item`` の最小スタブ。``iter_markers(name=...)`` のみ実装。"""

    def __init__(self, *, name: str, env_markers: list[_FakeMarker] | None = None) -> None:
        self.name = name
        self._env_markers = env_markers or []

    def iter_markers(self, name: str) -> list[_FakeMarker]:
        if name == "qgis_env":
            return list(self._env_markers)
        return []


class TestFilterItemsByEnvMarker:
    def test_marker_match_selected(self) -> None:
        item_a = _FakeItem(name="a", env_markers=[_FakeMarker("smoke")])
        item_b = _FakeItem(name="b", env_markers=[_FakeMarker("auth")])
        active = EnvironmentSpec(name="smoke")
        selected, deselected = filter_items_by_env_marker(
            [item_a, item_b],  # type: ignore[list-item]
            active_env=active,
            strategy="named",
        )
        assert selected == [item_a]
        assert deselected == [item_b]

    def test_marker_with_multiple_env_listing(self) -> None:
        """opt-in cartesian: ``qgis_env("smoke", "auth")`` で両方所属。"""
        item = _FakeItem(name="a", env_markers=[_FakeMarker("smoke", "auth")])
        for active_name in ("smoke", "auth"):
            active = EnvironmentSpec(name=active_name)
            selected, _ = filter_items_by_env_marker(
                [item],  # type: ignore[list-item]
                active_env=active,
                strategy="named",
            )
            assert selected == [item], f"failed for active={active_name}"

    def test_unmarked_with_strategy_named_runs_in_active(self) -> None:
        """marker 無しテストは active env が default なら selected。"""
        item = _FakeItem(name="a")
        active = EnvironmentSpec(name="smoke")
        selected, _ = filter_items_by_env_marker(
            [item],  # type: ignore[list-item]
            active_env=active,
            strategy="named",
        )
        assert selected == [item]

    def test_unmarked_with_strategy_fail_deselected(self) -> None:
        """marker 無し + strategy="fail" は deselected（呼び出し側で error 化）。"""
        item = _FakeItem(name="a")
        active = EnvironmentSpec(name="smoke")
        selected, deselected = filter_items_by_env_marker(
            [item],  # type: ignore[list-item]
            active_env=active,
            strategy="fail",
        )
        assert selected == []
        assert deselected == [item]

    def test_active_env_none_deselects_all(self) -> None:
        """active_env が None（strategy=fail で marker 無し）は全部 deselected。"""
        item = _FakeItem(name="a", env_markers=[_FakeMarker("smoke")])
        selected, deselected = filter_items_by_env_marker(
            [item],  # type: ignore[list-item]
            active_env=None,
            strategy="fail",
        )
        assert selected == []
        assert deselected == [item]


# ============================================================
# find_unmarked_items
# ============================================================


class TestFindUnmarkedItems:
    def test_returns_only_items_without_marker(self) -> None:
        a = _FakeItem(name="a", env_markers=[_FakeMarker("smoke")])
        b = _FakeItem(name="b")
        c = _FakeItem(name="c", env_markers=[_FakeMarker("auth", "smoke")])
        result = find_unmarked_items([a, b, c])  # type: ignore[list-item]
        assert result == [b]


# ============================================================
# resolve_qgis_setting
# ============================================================


class TestResolveQgisSetting:
    """4 段優先順位 (CLI > env > env_spec > ini) の解決。"""

    def test_cli_wins_all(self) -> None:
        result = resolve_qgis_setting(
            cli_value="from-cli",
            env_value="from-env",
            env_spec_value="from-spec",
            ini_value="from-ini",
        )
        assert result == "from-cli"

    def test_env_wins_when_cli_unset(self) -> None:
        result = resolve_qgis_setting(
            cli_value=None,
            env_value="from-env",
            env_spec_value="from-spec",
            ini_value="from-ini",
        )
        assert result == "from-env"

    def test_env_spec_wins_when_cli_and_env_unset(self) -> None:
        result = resolve_qgis_setting(
            cli_value=None,
            env_value=None,
            env_spec_value="from-spec",
            ini_value="from-ini",
        )
        assert result == "from-spec"

    def test_ini_used_when_others_empty(self) -> None:
        result = resolve_qgis_setting(
            cli_value=None,
            env_value=None,
            env_spec_value=None,
            ini_value="from-ini",
        )
        assert result == "from-ini"

    def test_returns_none_when_all_empty(self) -> None:
        result = resolve_qgis_setting(
            cli_value=None,
            env_value=None,
            env_spec_value=None,
            ini_value=None,
        )
        assert result is None

    def test_empty_string_treated_as_unset(self) -> None:
        result = resolve_qgis_setting(
            cli_value="",
            env_value="from-env",
            env_spec_value=None,
            ini_value=None,
        )
        assert result == "from-env"

    def test_empty_list_treated_as_unset(self) -> None:
        result = resolve_qgis_setting(
            cli_value=None,
            env_value=None,
            env_spec_value=(),
            ini_value=["from-ini"],
        )
        assert result == ["from-ini"]


# ============================================================
# Phase 2: parse_env_arg / aggregate_exit_codes / strip_env_args
# ============================================================


class TestParseEnvArg:
    """``--env`` の値を (mode, names) に正規化する。"""

    def test_none_when_not_specified(self) -> None:
        assert parse_env_arg(None) == ("none", ())

    def test_single_name(self) -> None:
        assert parse_env_arg("smoke") == ("single", ("smoke",))

    def test_all_keyword(self) -> None:
        assert parse_env_arg("all") == ("all", ())

    def test_multi_comma_separated(self) -> None:
        assert parse_env_arg("smoke,auth") == ("multi", ("smoke", "auth"))

    def test_multi_with_whitespace_trimmed(self) -> None:
        assert parse_env_arg("smoke, auth ,offline") == (
            "multi",
            ("smoke", "auth", "offline"),
        )

    def test_trailing_comma_collapses_to_single(self) -> None:
        # 末尾 comma で 1 件に縮退するケースは single 扱い
        assert parse_env_arg("smoke,") == ("single", ("smoke",))

    def test_empty_value_raises(self) -> None:
        with pytest.raises(ValueError, match="empty value"):
            parse_env_arg("")

    def test_only_commas_raises(self) -> None:
        with pytest.raises(ValueError, match="empty value"):
            parse_env_arg(",,")

    def test_all_with_other_names_treated_as_multi(self) -> None:
        # "all" は厳密に単独指定のみ keyword 扱い。"all,smoke" は 2 件の multi
        # （env 名 "all" を実際に持つかは plugin 側で検証する）
        assert parse_env_arg("all,smoke") == ("multi", ("all", "smoke"))


class TestAggregateExitCodes:
    """ADR-0003 §exit code 集約ルール。"""

    def test_empty_results_returns_5(self) -> None:
        assert aggregate_exit_codes([]) == 5

    def test_all_zero_returns_zero(self) -> None:
        assert aggregate_exit_codes([("a", 0), ("b", 0)]) == 0

    def test_any_one_returns_one(self) -> None:
        assert aggregate_exit_codes([("a", 0), ("b", 1), ("c", 0)]) == 1

    def test_all_five_returns_five(self) -> None:
        # 全 env で no tests collected → CI 誤検知防止のため 5 を返す
        assert aggregate_exit_codes([("a", 5), ("b", 5)]) == 5

    def test_five_with_zero_absorbed(self) -> None:
        # 5 と 0 の混在は 5 を吸収して 0
        assert aggregate_exit_codes([("a", 0), ("b", 5)]) == 0

    def test_five_with_one_returns_one(self) -> None:
        # 5 と 1 の混在は fail を優先
        assert aggregate_exit_codes([("a", 1), ("b", 5)]) == 1

    def test_interrupt_propagates(self) -> None:
        # 2 (interrupt) は他より優先で伝播
        assert aggregate_exit_codes([("a", 0), ("b", 2), ("c", 1)]) == 2

    def test_internal_error_propagates(self) -> None:
        assert aggregate_exit_codes([("a", 0), ("b", 3)]) == 3

    def test_usage_error_propagates(self) -> None:
        assert aggregate_exit_codes([("a", 4)]) == 4

    def test_hard_error_takes_precedence_over_one(self) -> None:
        # 2/3/4 は 1 より優先
        assert aggregate_exit_codes([("a", 1), ("b", 3)]) == 3


class TestIsHardExitCode:
    @pytest.mark.parametrize("code", [2, 3, 4])
    def test_hard_codes(self, code: int) -> None:
        assert is_hard_exit_code(code) is True

    @pytest.mark.parametrize("code", [0, 1, 5])
    def test_soft_codes(self, code: int) -> None:
        assert is_hard_exit_code(code) is False


class TestStripEnvArgs:
    """子 invocation 用 argv 構築のため ``--env=...`` を除去する。"""

    def test_no_env_flag_unchanged(self) -> None:
        argv = ["tests/", "-q", "-x"]
        assert strip_env_args(argv) == argv

    def test_strips_equals_form(self) -> None:
        assert strip_env_args(["-q", "--env=smoke", "tests/"]) == ["-q", "tests/"]

    def test_strips_space_form(self) -> None:
        assert strip_env_args(["-q", "--env", "smoke", "tests/"]) == [
            "-q",
            "tests/",
        ]

    def test_strips_multiple_env_args(self) -> None:
        # 複数 --env=... を全部消す
        assert strip_env_args(["--env=a", "--env=b", "tests/"]) == ["tests/"]

    def test_does_not_strip_partial_match(self) -> None:
        # `--envoy` のような prefix 衝突しそうな別オプションは残す
        assert strip_env_args(["--envoy=x"]) == ["--envoy=x"]

    def test_empty_argv(self) -> None:
        assert strip_env_args([]) == []


# ============================================================
# Phase 3: --maxfail 累計用 argv 操作
# ============================================================


class TestStripMaxfailArgs:
    def test_no_flag_unchanged(self) -> None:
        assert strip_maxfail_args(["pytest", "tests/"]) == ["pytest", "tests/"]

    def test_strips_equals_form(self) -> None:
        assert strip_maxfail_args(["a", "--maxfail=3", "b"]) == ["a", "b"]

    def test_strips_space_form(self) -> None:
        assert strip_maxfail_args(["a", "--maxfail", "3", "b"]) == ["a", "b"]

    def test_strips_multiple_forms(self) -> None:
        assert strip_maxfail_args(["--maxfail=2", "a", "--maxfail", "5"]) == ["a"]

    def test_does_not_strip_partial_match(self) -> None:
        # `--maxfail-something` はプロジェクト独自オプションとして残す
        assert strip_maxfail_args(["--maxfail-extra=1"]) == ["--maxfail-extra=1"]

    def test_empty_argv(self) -> None:
        assert strip_maxfail_args([]) == []


class TestInjectMaxfail:
    def test_appends_when_absent(self) -> None:
        assert inject_maxfail(["a", "b"], 5) == ["a", "b", "--maxfail=5"]

    def test_replaces_existing_equals(self) -> None:
        assert inject_maxfail(["--maxfail=3", "tests/"], 7) == ["tests/", "--maxfail=7"]

    def test_replaces_existing_space(self) -> None:
        assert inject_maxfail(["--maxfail", "3", "tests/"], 7) == ["tests/", "--maxfail=7"]

    def test_clamps_zero_to_one(self) -> None:
        # remaining=0 を child に渡すと「即 fail-fast 不可能」で混乱するので 1 に強制
        assert inject_maxfail(["a"], 0) == ["a", "--maxfail=1"]

    def test_clamps_negative_to_one(self) -> None:
        assert inject_maxfail(["a"], -3) == ["a", "--maxfail=1"]

    def test_preserves_order_of_other_args(self) -> None:
        argv = ["pytest", "-x", "--env=smoke", "tests/test_x.py", "--maxfail=2"]
        result = inject_maxfail(argv, 4)
        assert result == ["pytest", "-x", "--env=smoke", "tests/test_x.py", "--maxfail=4"]


# ============================================================
# Phase 4: extends 継承
# ============================================================


class TestExtendsInheritance:
    def _write(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "environments.toml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_basic_inheritance(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "base"
qgis_bin = "qgis-base.exe"
qgis_args = ["--profile=base"]
env = { COMMON = "1" }

[[environments]]
name = "child"
extends = "base"
qgis_args = ["--noversioncheck"]
env = { CHILD = "1" }

[default_environment]
strategy = "named"
default_name = "base"
""",
        )
        config = load_environments(p)
        child = config.by_name("child")
        assert child is not None
        # qgis_bin: base から継承
        assert child.qgis_bin == "qgis-base.exe"
        # qgis_args: base + child（concat）
        assert child.qgis_args == ("--profile=base", "--noversioncheck")
        # env: base + child（shallow merge）
        assert child.env == {"COMMON": "1", "CHILD": "1"}

    def test_child_overrides_qgis_bin(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "base"
qgis_bin = "qgis-base.exe"

[[environments]]
name = "child"
extends = "base"
qgis_bin = "qgis-child.exe"

[default_environment]
strategy = "named"
default_name = "base"
""",
        )
        config = load_environments(p)
        assert config.by_name("child").qgis_bin == "qgis-child.exe"

    def test_child_env_overrides_base_env_per_key(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "base"
env = { K = "base", L = "base" }

[[environments]]
name = "child"
extends = "base"
env = { K = "child" }

[default_environment]
strategy = "named"
default_name = "base"
""",
        )
        config = load_environments(p)
        # K は child override、L は base 継承
        assert config.by_name("child").env == {"K": "child", "L": "base"}

    def test_forward_reference_rejected(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "child"
extends = "base"

[[environments]]
name = "base"

[default_environment]
strategy = "named"
default_name = "base"
""",
        )
        with pytest.raises(ValueError, match="must reference an environment defined earlier"):
            load_environments(p)

    def test_self_extends_rejected(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "x"
extends = "x"

[default_environment]
strategy = "named"
default_name = "x"
""",
        )
        with pytest.raises(ValueError, match="cannot extend itself"):
            load_environments(p)

    def test_chained_extends(self, tmp_path: Path) -> None:
        # a -> b -> c で 3 段 chain。 c は a + b + 自分の args 全部繋がる。
        p = self._write(
            tmp_path,
            """
[[environments]]
name = "a"
qgis_args = ["--a"]

[[environments]]
name = "b"
extends = "a"
qgis_args = ["--b"]

[[environments]]
name = "c"
extends = "b"
qgis_args = ["--c"]

[default_environment]
strategy = "named"
default_name = "a"
""",
        )
        config = load_environments(p)
        assert config.by_name("b").qgis_args == ("--a", "--b")
        assert config.by_name("c").qgis_args == ("--a", "--b", "--c")


# ============================================================
# Phase 4: aggregate_exit_codes(strict=True)
# ============================================================


class TestAggregateExitCodesStrict:
    def test_partial_5_absorbed_by_default(self) -> None:
        # 既定では 0 と 5 の混在は 0
        assert aggregate_exit_codes([("a", 0), ("b", 5)]) == 0

    def test_partial_5_promoted_to_1_in_strict(self) -> None:
        # strict では 5 を 1 に格上げ
        assert aggregate_exit_codes([("a", 0), ("b", 5)], strict=True) == 1

    def test_all_5_remains_5_even_in_strict(self) -> None:
        # 全 5 はガードとして既存通り 5（CI green 誤検知防止）
        assert aggregate_exit_codes([("a", 5), ("b", 5)], strict=True) == 5

    def test_hard_error_propagates_in_strict(self) -> None:
        # strict でも hard error は最優先
        assert aggregate_exit_codes([("a", 5), ("b", 2)], strict=True) == 2

    def test_one_propagates_over_5_in_strict(self) -> None:
        # 1 と 5 と 0 混在 → 1（5 promotion より先に 1 が決まる）
        assert aggregate_exit_codes([("a", 1), ("b", 5), ("c", 0)], strict=True) == 1
