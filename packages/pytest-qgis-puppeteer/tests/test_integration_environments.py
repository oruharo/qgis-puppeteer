"""Integration tests for ADR-0003 environments using pytest's ``pytester`` fixture.

実 QGIS は要らない。``pytester`` が temp dir に環境を作り、その上で pytest を
サブプロセスで動かして、collection / marker / artifacts の振る舞いを検証する。

検証する観点:

- ``--env=<name>`` で marker 一致テストだけが selected され、それ以外が deselected
- ``environments.toml`` が無いプロジェクトでは ``--env`` を渡すと UsageError
- ``strategy="fail"`` で marker 無しテストがあると UsageError
- ``--env`` 未指定時は ``default_name`` の env が active になる
- ``qgis_env`` marker が登録されていて ``--strict-markers`` でも通る

QGIS spawn が要るテスト（``qgis_process`` fixture を解決するもの）は対象外。
ここでは ``--collect-only`` モードや「fixture を要求しないテスト」だけで挙動を見る。
"""

from __future__ import annotations

import textwrap

import pytest

# 各テストで `pytester` を使う。pyproject.toml で plugin が auto-load されているため、
# pytester も親 plugin（自身）を引き継いで子 invocation で使える。
pytest_plugins = ["pytester"]


def _write_basic_envs_toml(pytester: pytest.Pytester) -> None:
    """共通の environments.toml を temp dir に書く。"""
    pytester.makefile(
        ".toml",
        environments=textwrap.dedent(
            """
            [[environments]]
            name = "smoke"
            description = "smoke tests"

            [[environments]]
            name = "auth"
            description = "authentication tests"

            [default_environment]
            strategy = "named"
            default_name = "smoke"
            """
        ).strip(),
    )


# ============================================================
# marker フィルタの基本動作
# ============================================================


class TestEnvFilterBasic:
    """``--env=<name>`` で marker 一致テストだけが selected される。"""

    def test_env_matches_filter(self, pytester: pytest.Pytester) -> None:
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_envs=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_smoke_only():
                    pass

                @pytest.mark.qgis_env("auth")
                def test_auth_only():
                    pass

                @pytest.mark.qgis_env("smoke", "auth")
                def test_both():
                    pass
                """
            )
        )
        result = pytester.runpytest("--env=smoke", "--collect-only", "-q")
        # smoke と both が selected、auth_only は deselected
        result.stdout.fnmatch_lines(
            [
                "*test_smoke_only*",
                "*test_both*",
            ]
        )
        # auth_only は出ないはず
        assert "test_auth_only" not in result.stdout.str()

    def test_unknown_env_raises_usage_error(self, pytester: pytest.Pytester) -> None:
        _write_basic_envs_toml(pytester)
        pytester.makepyfile("def test_dummy(): pass")
        result = pytester.runpytest("--env=nonexistent")
        assert result.ret != 0
        result.stderr.fnmatch_lines(["*does not match any environment*"])


# ============================================================
# environments.toml 不在時の振る舞い（後方互換）
# ============================================================


class TestNoEnvironmentsToml:
    """``environments.toml`` 不在時は ``--env`` 指定を error、未指定なら no-op。"""

    def test_env_flag_without_toml_fails(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("def test_dummy(): pass")
        result = pytester.runpytest("--env=anything")
        assert result.ret != 0
        result.stderr.fnmatch_lines(["*environments.toml was not found*"])

    def test_no_toml_no_env_flag_runs_normally(self, pytester: pytest.Pytester) -> None:
        """toml 無し + ``--env`` 未指定 → 既存 ini 経路で動く（後方互換）。"""
        pytester.makepyfile(
            test_normal=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_marked():
                    pass

                def test_unmarked():
                    pass
                """
            )
        )
        # collection だけ確認（実際の qgis_process fixture は要求していない）
        result = pytester.runpytest("--collect-only", "-q")
        assert result.ret == 0
        # 両方 collect される（filter 無効）
        result.stdout.fnmatch_lines(["*test_marked*", "*test_unmarked*"])


# ============================================================
# default_environment strategy
# ============================================================


class TestDefaultEnvironment:
    """``default_environment`` の挙動。"""

    def test_default_named_runs_default_when_no_env_flag(self, pytester: pytest.Pytester) -> None:
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_default=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_smoke():
                    pass

                @pytest.mark.qgis_env("auth")
                def test_auth():
                    pass
                """
            )
        )
        result = pytester.runpytest("--collect-only", "-q")
        # default_name="smoke" なので smoke だけ selected
        result.stdout.fnmatch_lines(["*test_smoke*"])
        assert "test_auth" not in result.stdout.str()

    def test_strategy_fail_with_unmarked_raises(self, pytester: pytest.Pytester) -> None:
        pytester.makefile(
            ".toml",
            environments=textwrap.dedent(
                """
                [[environments]]
                name = "smoke"

                [default_environment]
                strategy = "fail"
                """
            ).strip(),
        )
        pytester.makepyfile(
            test_strict=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_marked():
                    pass

                def test_unmarked():
                    pass
                """
            )
        )
        result = pytester.runpytest("--env=smoke")
        assert result.ret != 0
        result.stderr.fnmatch_lines(["*strategy='fail'*no @pytest.mark.qgis_env marker*"])

    def test_strategy_named_unmarked_run_in_default(self, pytester: pytest.Pytester) -> None:
        """``strategy="named"`` で marker 無し test は default env で run される。"""
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_named=textwrap.dedent(
                """
                def test_unmarked():
                    pass
                """
            )
        )
        result = pytester.runpytest("--collect-only", "-q")
        assert result.ret == 0
        result.stdout.fnmatch_lines(["*test_unmarked*"])


# ============================================================
# strict-markers との整合
# ============================================================


class TestStrictMarkersCompat:
    """``--strict-markers`` で ``qgis_env`` が unknown と判定されないこと。"""

    def test_qgis_env_marker_registered(self, pytester: pytest.Pytester) -> None:
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_strict=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_marked():
                    pass
                """
            )
        )
        result = pytester.runpytest("--strict-markers", "--collect-only")
        # `qgis_env` が registered marker なので strict でも通る
        assert result.ret == 0


# ============================================================
# 複数 env marker（opt-in cartesian）
# ============================================================


class TestOptInCartesian:
    """``qgis_env("a", "b")`` で複数 env に所属するテストの挙動。"""

    def test_test_runs_in_either_env(self, pytester: pytest.Pytester) -> None:
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_both=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke", "auth")
                def test_both():
                    pass
                """
            )
        )
        # smoke で run できる
        r1 = pytester.runpytest("--env=smoke", "--collect-only", "-q")
        r1.stdout.fnmatch_lines(["*test_both*"])
        # auth でも run できる
        r2 = pytester.runpytest("--env=auth", "--collect-only", "-q")
        r2.stdout.fnmatch_lines(["*test_both*"])


# ============================================================
# Phase 2: --env=all / --env=a,b の meta-parent モード
# ============================================================


class TestMetaParentAll:
    """``--env=all`` で全 env を順次実行する meta-parent モード。"""

    def test_all_runs_each_env_in_child_invocation(self, pytester: pytest.Pytester) -> None:
        """各 env で対応する marker のテストが 1 回ずつ走る。"""
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_per_env=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_smoke_only():
                    pass

                @pytest.mark.qgis_env("auth")
                def test_auth_only():
                    pass
                """
            )
        )
        # subprocess モードで起動（in-process だと pytest.main 再帰の挙動が
        # pytester 内部状態と干渉する可能性があるため、確実な側に倒す）
        result = pytester.runpytest_subprocess("--env=all", "-v")
        # 親 invocation の summary は 0 件（全 deselect）だが、子 invocation の
        # summary が標準出力に流れる：smoke で test_smoke_only が PASS、
        # auth で test_auth_only が PASS
        out = result.stdout.str()
        assert "test_smoke_only" in out
        assert "test_auth_only" in out
        # 全 env で 0 → 集約 0
        assert result.ret == 0

    def test_multi_form_runs_specified_envs(self, pytester: pytest.Pytester) -> None:
        """``--env=a,b`` で 2 つの env を順次実行できる。"""
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_two=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_smoke():
                    pass

                @pytest.mark.qgis_env("auth")
                def test_auth():
                    pass
                """
            )
        )
        result = pytester.runpytest_subprocess("--env=smoke,auth", "-v")
        out = result.stdout.str()
        assert "test_smoke" in out
        assert "test_auth" in out
        assert result.ret == 0

    def test_unknown_env_in_multi_raises(self, pytester: pytest.Pytester) -> None:
        """未知 env 名を含む multi 指定は usage error。"""
        _write_basic_envs_toml(pytester)
        pytester.makepyfile("def test_dummy(): pass")
        result = pytester.runpytest_subprocess("--env=smoke,nonexistent")
        assert result.ret != 0
        result.stderr.fnmatch_lines(["*unknown environment*nonexistent*"])

    def test_meta_parent_aggregates_failure(self, pytester: pytest.Pytester) -> None:
        """ある env で fail があれば集約 exit code は 1。"""
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_mixed=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_smoke_pass():
                    pass

                @pytest.mark.qgis_env("auth")
                def test_auth_fail():
                    assert False, "intentional"
                """
            )
        )
        result = pytester.runpytest_subprocess("--env=all")
        # smoke=0, auth=1 → 集約 1
        assert result.ret == 1

    def test_meta_parent_no_tests_collected_returns_5(self, pytester: pytest.Pytester) -> None:
        """全 env で「該当 marker のテストが無い」ときは 5（ADR ガード）。"""
        _write_basic_envs_toml(pytester)
        # marker 一致しないテストを 1 つだけ作る
        pytester.makepyfile(
            test_no_match=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("offline")  # toml に存在しない env
                def test_other():
                    pass
                """
            )
        )
        result = pytester.runpytest_subprocess("--env=all")
        # smoke / auth どちらの child invocation も 0 件 (rc=5)
        # → aggregate も 5（CI 誤検知防止のガード）
        assert result.ret == 5

    def test_meta_parent_absorbs_partial_no_tests(self, pytester: pytest.Pytester) -> None:
        """ある env だけ「該当テスト 0 件」でも全体は green（rc=0）。"""
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_smoke_only=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_smoke():
                    pass
                """
            )
        )
        # smoke=0 (1 件 pass) + auth=5 (collect 0 件) → aggregate=0
        result = pytester.runpytest_subprocess("--env=all")
        assert result.ret == 0


class TestMetaParentParentDeselectsAll:
    """meta-parent モードでは親 invocation で全テスト deselect される。"""

    def test_parent_collects_zero(self, pytester: pytest.Pytester) -> None:
        """``--env=all --collect-only`` 時、親側 collection は 0 件 (deselected)。

        ``--collect-only`` 中は ``pytest_sessionfinish`` で子を起動しても
        ``--collect-only`` がそのまま伝播するので、親の summary 行で件数を確認する。
        """
        _write_basic_envs_toml(pytester)
        pytester.makepyfile(
            test_x=textwrap.dedent(
                """
                import pytest

                @pytest.mark.qgis_env("smoke")
                def test_smoke():
                    pass
                """
            )
        )
        # in-process でも親側の deselect は確定的なのでこれは inprocess で OK
        result = pytester.runpytest("--env=all", "--collect-only", "-q")
        # 親 invocation の deselect が起きていればこのフレーズが出る
        assert "deselected" in result.stdout.str().lower()
