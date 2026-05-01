"""Test Environments（partition モデル）の実装（ADR-0003 Phase 1 / 2）。

`environments.toml` をロードし、``--env=<name>`` CLI 指定 + ``@pytest.mark.qgis_env``
marker で「テスト群を env に partition する」仕組みを提供する。

Phase 1 のスコープ:

- ``environments.toml`` の TOML loader（``EnvironmentSpec`` への deserialize）
- ``--env=<single_name>`` CLI option の登録
- ``qgis_env`` marker の登録
- ``pytest_collection_modifyitems`` での marker フィルタ
- env 設定と既存 ``qgis_command`` / ``qgis_bin`` ini / CLI / 環境変数の優先順位解決
- artifacts 出力先の env 別切替（``outputs/<env_name>/``）

Phase 2 のスコープ（本モジュールに追加された pure 関数）:

- ``parse_env_arg``: ``--env`` の値を ``("none"|"single"|"multi"|"all", names)`` に正規化
- ``aggregate_exit_codes``: env ごとの pytest exit code を ADR-0003 §exit code 集約ルールに
  従って 1 つに丸める
- ``strip_env_args``: 子 invocation 用に argv から ``--env=...`` を削除

実際の dispatch（``pytest.main()`` の再帰呼び出し）は plugin.py 側で実装する。

詳細は ADR-0003（``docs/architecture/0003-test-environments.md``）参照。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[unresolved-import]
else:
    import tomli as tomllib  # type: ignore[no-redef]

import pytest

logger = logging.getLogger("pytest_qgis_puppeteer._environments")

# `environments.toml` ファイル名のデフォルト探索順
DEFAULT_FILENAMES: tuple[str, ...] = (
    "environments.toml",
    "test_e2e/environments.toml",
)


# ============================================================
# データクラス
# ============================================================


@dataclass(frozen=True)
class EnvironmentSpec:
    """1 つの environment の起動構成。

    既存 plugin.py の ``_resolve_qgis_command`` / ``_resolve_qgis_args`` が解決する
    キーと semantic を揃える（ADR-0003）。
    """

    name: str
    description: str = ""
    qgis_bin: str | None = None
    qgis_args: tuple[str, ...] = ()
    qgis_command: tuple[str, ...] = ()
    qgis_python: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DefaultEnvironmentConfig:
    """``[default_environment]`` table の解析結果。

    Phase 1 の strategy:

    - ``"named"``: ``default_name`` 必須。marker 無しテストはこの env で走る。
    - ``"fail"``: marker 必須。marker 無しテストは error。
    """

    strategy: str = "named"
    default_name: str | None = None


@dataclass(frozen=True)
class EnvironmentsConfig:
    """``environments.toml`` 全体の解析結果。"""

    environments: tuple[EnvironmentSpec, ...]
    default: DefaultEnvironmentConfig

    def by_name(self, name: str) -> EnvironmentSpec | None:
        for env in self.environments:
            if env.name == name:
                return env
        return None


# ============================================================
# TOML ローダ
# ============================================================


def find_environments_toml(rootpath: Path) -> Path | None:
    """``rootpath`` 配下から ``environments.toml`` を探す（既定の探索順）。

    Phase 1 では単独ファイルのみサポート（``pyproject.toml`` インライン記法は
    Phase 2 以降）。
    """
    for relpath in DEFAULT_FILENAMES:
        candidate = rootpath / relpath
        if candidate.is_file():
            return candidate
    return None


def load_environments(toml_path: Path) -> EnvironmentsConfig:
    """``environments.toml`` をパースして ``EnvironmentsConfig`` に整形。

    形式は ADR-0003 §"環境定義（environments.toml）" を参照。

    Raises:
        ValueError: TOML の構造が不正、env 名重複、``strategy="named"`` で
            ``default_name`` 未指定 等。
    """
    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    raw_envs = raw.get("environments")
    if not isinstance(raw_envs, list) or not raw_envs:
        raise ValueError(f"{toml_path}: '[[environments]]' table is missing or empty")

    parsed: list[EnvironmentSpec] = []
    seen_names: dict[str, EnvironmentSpec] = {}
    for i, item in enumerate(raw_envs):
        if not isinstance(item, dict):
            raise ValueError(f"{toml_path}: environments[{i}] must be a table")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{toml_path}: environments[{i}].name must be a non-empty string")
        if name in seen_names:
            raise ValueError(f"{toml_path}: duplicate environment name {name!r}")

        # ADR-0003 Phase 4 §"`extends` による env 継承": forward reference 禁止
        # （topological order を強制する）。base が同 toml の前方に出ているはず。
        extends = item.get("extends")
        base_spec: EnvironmentSpec | None = None
        if extends is not None:
            if not isinstance(extends, str) or not extends:
                raise ValueError(
                    f"{toml_path}: environments[{i}].extends must be a non-empty string"
                )
            if extends == name:
                raise ValueError(f"{toml_path}: environments[{i}] cannot extend itself ({name!r})")
            if extends not in seen_names:
                raise ValueError(
                    f"{toml_path}: environments[{i}].extends={extends!r} must reference "
                    f"an environment defined earlier in the file"
                )
            base_spec = seen_names[extends]

        spec = _parse_environment_spec(item, toml_path=toml_path, index=i, base=base_spec)
        seen_names[name] = spec
        parsed.append(spec)

    raw_default = raw.get("default_environment")
    default = _parse_default_environment(raw_default, toml_path=toml_path)

    if default.strategy == "named":
        if default.default_name is None:
            raise ValueError(
                f"{toml_path}: [default_environment] strategy='named' requires 'default_name'"
            )
        if not any(env.name == default.default_name for env in parsed):
            raise ValueError(
                f"{toml_path}: [default_environment].default_name "
                f"{default.default_name!r} does not match any environment"
            )

    return EnvironmentsConfig(environments=tuple(parsed), default=default)


def _parse_environment_spec(
    item: dict[str, Any],
    *,
    toml_path: Path,
    index: int,
    base: EnvironmentSpec | None = None,
) -> EnvironmentSpec:
    """1 つの ``[[environments]]`` table を ``EnvironmentSpec`` に変換。

    `qgis_command` と `qgis_bin`/`qgis_args` の排他は plugin 側 fixture 解決時
    （`_resolve_qgis_command` 等）で評価されるので、ここでは型 / 必須チェックのみ。

    ADR-0003 Phase 4 §"`extends` による env 継承": ``base`` が与えられた場合は
    base の値を出発点にしつつ、本 item の値で上書き / 拡張する:

    - ``description``: 本 item に明示があればそれ。省略時は base からは継承しない
      （description は env 個別の説明なので継承不要）
    - ``qgis_bin`` / ``qgis_python``: 明示があれば上書き、省略時は base を継承
    - ``qgis_command``: 明示があれば上書き、省略時は base を継承（list なので
      ``[]`` を明示すると "clear" を意味する）
    - ``qgis_args``: base + child（concat、base 先行）。child 単独で完結させたい場合は
      ``extends`` を使わない選択を推奨
    - ``env``: shallow merge、child のキーが勝つ
    """
    name = item["name"]
    description = item.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"{toml_path}: environments[{index}].description must be string")

    if "qgis_bin" in item:
        qgis_bin = item.get("qgis_bin")
        if qgis_bin is not None and not isinstance(qgis_bin, str):
            raise ValueError(f"{toml_path}: environments[{index}].qgis_bin must be string")
    else:
        qgis_bin = base.qgis_bin if base is not None else None

    if "qgis_python" in item:
        qgis_python = item.get("qgis_python")
        if qgis_python is not None and not isinstance(qgis_python, str):
            raise ValueError(f"{toml_path}: environments[{index}].qgis_python must be string")
    else:
        qgis_python = base.qgis_python if base is not None else None

    if "qgis_args" in item:
        own_args = _parse_string_list(
            item["qgis_args"],
            field_name=f"environments[{index}].qgis_args",
            toml_path=toml_path,
        )
        qgis_args = list(base.qgis_args) + own_args if base is not None else own_args
    else:
        qgis_args = list(base.qgis_args) if base is not None else []

    if "qgis_command" in item:
        qgis_command = _parse_string_list(
            item["qgis_command"],
            field_name=f"environments[{index}].qgis_command",
            toml_path=toml_path,
        )
    else:
        qgis_command = list(base.qgis_command) if base is not None else []

    if "env" in item:
        raw_env = item["env"]
        if not isinstance(raw_env, dict):
            raise ValueError(f"{toml_path}: environments[{index}].env must be a table")
        own_env = {}
        for k, v in raw_env.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError(
                    f"{toml_path}: environments[{index}].env keys/values must be strings"
                )
            own_env[k] = v
        # base + own (shallow merge, child wins)
        env = dict(base.env) if base is not None else {}
        env.update(own_env)
    else:
        env = dict(base.env) if base is not None else {}

    return EnvironmentSpec(
        name=name,
        description=description,
        qgis_bin=qgis_bin,
        qgis_args=tuple(qgis_args),
        qgis_command=tuple(qgis_command),
        qgis_python=qgis_python,
        env=env,
    )


def _parse_string_list(raw: Any, *, field_name: str, toml_path: Path) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"{toml_path}: {field_name} must be a list")
    out: list[str] = []
    for j, v in enumerate(raw):
        if not isinstance(v, str):
            raise ValueError(f"{toml_path}: {field_name}[{j}] must be string")
        out.append(v)
    return out


def _parse_default_environment(raw: Any, *, toml_path: Path) -> DefaultEnvironmentConfig:
    if raw is None:
        return DefaultEnvironmentConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"{toml_path}: [default_environment] must be a table")
    strategy = raw.get("strategy", "named")
    if strategy not in ("named", "fail"):
        raise ValueError(
            f"{toml_path}: [default_environment].strategy must be 'named' or "
            f"'fail' (got {strategy!r})"
        )
    default_name = raw.get("default_name")
    if default_name is not None and not isinstance(default_name, str):
        raise ValueError(f"{toml_path}: [default_environment].default_name must be string")
    return DefaultEnvironmentConfig(strategy=strategy, default_name=default_name)


# ============================================================
# env 解決ロジック（CLI > env > toml > ini の優先順位）
# ============================================================


def resolve_active_env(
    config: EnvironmentsConfig, *, cli_env_name: str | None
) -> EnvironmentSpec | None:
    """``--env=<name>`` の解決。

    CLI で env 名が指定されていればそれを返す（存在チェック）。
    指定無しの場合は ``default_environment.strategy`` に従う:

    - ``"named"``: ``default_name`` の env を返す
    - ``"fail"``: None を返す（呼び出し側で「marker 無しテストは error」を実装）
    """
    if cli_env_name is not None:
        env = config.by_name(cli_env_name)
        if env is None:
            available = ", ".join(e.name for e in config.environments)
            raise pytest.UsageError(
                f"--env={cli_env_name!r} does not match any environment (available: {available})"
            )
        return env

    if config.default.strategy == "named":
        assert config.default.default_name is not None  # validated in load
        return config.by_name(config.default.default_name)

    # strategy == "fail"
    return None


def filter_items_by_env_marker(
    items: Sequence[pytest.Item],
    *,
    active_env: EnvironmentSpec | None,
    strategy: str,
) -> tuple[list[pytest.Item], list[pytest.Item]]:
    """marker に基づいて item をフィルタする。

    Returns:
        (selected, deselected) — pytest_collection_modifyitems の規約。

    Rules:
        - marker `qgis_env(env_a, env_b, ...)` を持つテストは、引数のいずれかが
          ``active_env.name`` と一致すれば selected
        - marker 無しテストの扱いは ``strategy`` に従う:
            - ``"named"``: active_env と同じ default なら selected、違えば deselected
            - ``"fail"``: 呼び出し側で error にすべき（ここでは deselected として扱う）
        - ``active_env`` が None（``strategy="fail"`` で marker 無し）は呼び出し側で error
    """
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    active_name = active_env.name if active_env is not None else None

    for item in items:
        marker_envs = _qgis_env_marker_names(item)

        if marker_envs:
            if active_name is not None and active_name in marker_envs:
                selected.append(item)
            else:
                deselected.append(item)
            continue

        # marker 無し
        if strategy == "named" and active_name is not None:
            selected.append(item)
        else:
            # strategy="fail" の場合は deselected（呼び出し側で error 化）
            deselected.append(item)

    return selected, deselected


def _qgis_env_marker_names(item: pytest.Item) -> tuple[str, ...]:
    """``@pytest.mark.qgis_env("a", "b")`` の引数を tuple で返す。

    複数 marker（テスト・class・module 単位の重複付与）は和集合として扱う。
    """
    names: list[str] = []
    for marker in item.iter_markers(name="qgis_env"):
        for arg in marker.args:
            if isinstance(arg, str):
                names.append(arg)
    return tuple(names)


def find_unmarked_items(items: Sequence[pytest.Item]) -> list[pytest.Item]:
    """marker `qgis_env` を持たない item の list（``strategy="fail"`` での error 表示用）。"""
    return [item for item in items if not _qgis_env_marker_names(item)]


# ============================================================
# env と既存 ini / env / CLI の優先順位解決（ADR-0003）
# ============================================================


def resolve_qgis_setting(
    *,
    cli_value: Any,
    env_value: str | None,
    env_spec_value: Any,
    ini_value: Any,
) -> Any:
    """単一の設定項目について **CLI > env > toml(env_spec) > ini** で解決。

    plugin.py の ``_resolve_setting`` と同じ semantics を `environments.toml` の
    値と統合する。空文字列 / 空 list / None は「未設定」扱い。
    """
    if _is_set(cli_value):
        return cli_value
    if _is_set(env_value):
        return env_value
    if _is_set(env_spec_value):
        return env_spec_value
    if _is_set(ini_value):
        return ini_value
    return None


def _is_set(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value == "":
        return False
    return not (isinstance(value, (list, tuple)) and len(value) == 0)


# ============================================================
# Phase 2: --env=all / --env=a,b の引数解析と exit code 集約
# ============================================================


# `--env` の解釈モード。Phase 1 は "none" / "single" のみ、Phase 2 で "multi" / "all" を追加。
EnvArgMode = str  # Literal["none", "single", "multi", "all"]


def parse_env_arg(cli_value: str | None) -> tuple[EnvArgMode, tuple[str, ...]]:
    """``--env`` の値をモードと name 列に正規化する（pure 関数）。

    返り値の tuple:

    - ``("none", ())``: ``--env`` 未指定
    - ``("single", (name,))``: 単一 env（``--env=foo``）
    - ``("multi", (name, name, ...))``: 2 つ以上の env（``--env=a,b``）
    - ``("all", ())``: 全 env（``--env=all``）

    空白は trim する。``--env=foo,`` のように末尾 comma で 1 件になるケースは
    ``"single"`` として扱う（実用上の親切ルール）。
    """
    if cli_value is None:
        return ("none", ())
    if cli_value == "all":
        return ("all", ())
    if "," in cli_value:
        parts = tuple(p.strip() for p in cli_value.split(",") if p.strip())
        if len(parts) == 0:
            raise ValueError(f"--env={cli_value!r}: empty value after splitting on comma")
        if len(parts) == 1:
            return ("single", parts)
        return ("multi", parts)
    name = cli_value.strip()
    if not name:
        raise ValueError(f"--env={cli_value!r}: empty value")
    return ("single", (name,))


# pytest 標準 exit code（参考用、ADR-0003 §exit code 集約ルール）
_PYTEST_EXIT_OK = 0
_PYTEST_EXIT_TESTSFAILED = 1
_PYTEST_EXIT_INTERRUPTED = 2
_PYTEST_EXIT_INTERNALERROR = 3
_PYTEST_EXIT_USAGEERROR = 4
_PYTEST_EXIT_NOTESTSCOLLECTED = 5

_HARD_EXIT_CODES: tuple[int, ...] = (
    _PYTEST_EXIT_INTERRUPTED,
    _PYTEST_EXIT_INTERNALERROR,
    _PYTEST_EXIT_USAGEERROR,
)


def aggregate_exit_codes(
    results: Sequence[tuple[str, int]],
    *,
    strict: bool = False,
) -> int:
    """env ごとの exit code を ADR-0003 §exit code 集約ルールで丸める。

    Rules（ADR-0003 §"`--env=all` の exit code 集約ルール"）:

    - 全 env が 0 → 0
    - いずれかが 2 / 3 / 4（hard error）→ そのまま伝播
    - いずれかが 1 → 1
    - ある env だけが 5（no tests collected）→ 吸収して 0 扱い
    - **全 env** が 5 → 5（CI green 誤検知を防ぐガード）
    - results が空（env 0 件で呼ばれた）→ 5

    ADR-0003 Phase 4 §"`--env-strict`": ``strict=True`` の時は「ある env が 5 →
    吸収」ルールを止め、5 を 1（fail）に格上げする。CI で「指定した env で実際に
    テストが走ったか」を保証したいユースケース向け。

    呼び出し側は hard error code を見たら break すべき（後続 env を走らせない）。
    """
    if not results:
        return _PYTEST_EXIT_NOTESTSCOLLECTED
    codes = [int(rc) for _, rc in results]
    # hard error は最初に伝播
    for c in codes:
        if c in _HARD_EXIT_CODES:
            return c
    # 全 5
    if all(c == _PYTEST_EXIT_NOTESTSCOLLECTED for c in codes):
        return _PYTEST_EXIT_NOTESTSCOLLECTED
    # いずれか fail
    if any(c == _PYTEST_EXIT_TESTSFAILED for c in codes):
        return _PYTEST_EXIT_TESTSFAILED
    # strict mode: 部分的に 5 が混じっていれば fail に格上げ
    if strict and any(c == _PYTEST_EXIT_NOTESTSCOLLECTED for c in codes):
        return _PYTEST_EXIT_TESTSFAILED
    # 0 と 5 の混在 → 0（既定）
    return _PYTEST_EXIT_OK


def is_hard_exit_code(code: int) -> bool:
    """``code`` が ADR-0003 で「hard error」扱いなら True（呼び出し側で break するため）。"""
    return int(code) in _HARD_EXIT_CODES


def strip_env_args(argv: Sequence[str]) -> list[str]:
    """argv から ``--env=<value>`` および ``--env <value>`` を取り除く（pure 関数）。

    子 invocation には親が決めた single env name を 1 つだけ渡したいので、親の argv に
    含まれる ``--env=...`` 系を除去してから ``--env=<name>`` を append する。

    対応形式:
    - ``--env=value`` → 削除
    - ``--env value`` → ``--env`` とその次の引数を削除
    - ``-env`` のような短縮形は対象外（pytest plugin として ``--env`` のみ登録）
    """
    out: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--env":
            skip_next = True
            continue
        if arg.startswith("--env="):
            continue
        out.append(arg)
    return out


def strip_maxfail_args(argv: Sequence[str]) -> list[str]:
    """argv から ``--maxfail=<N>`` および ``--maxfail <N>`` を除去（pure 関数）。

    ADR-0003 Phase 3: meta-parent が子 invocation に「残許容数」を再注入する前段。
    """
    out: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--maxfail":
            skip_next = True
            continue
        if arg.startswith("--maxfail="):
            continue
        out.append(arg)
    return out


def inject_maxfail(argv: Sequence[str], remaining: int) -> list[str]:
    """``--maxfail`` を ``remaining`` で書き換え（or 追加）した argv を返す（pure 関数）。

    ADR-0003 Phase 3 §"`--maxfail` 累計": meta-parent は env 横断で fail を累計し、
    各子 invocation には「残許容数」を渡す。``remaining <= 0`` は呼び出し側で break
    することが期待される（本関数は呼ばれたら 1 を最小値として強制する＝ 0 だと pytest
    が fail-fast 0 件で即 exit してしまう挙動を避ける）。
    """
    base = strip_maxfail_args(argv)
    safe = max(int(remaining), 1)
    return [*base, f"--maxfail={safe}"]


__all__ = [
    "DefaultEnvironmentConfig",
    "EnvironmentSpec",
    "EnvironmentsConfig",
    "aggregate_exit_codes",
    "filter_items_by_env_marker",
    "find_environments_toml",
    "find_unmarked_items",
    "inject_maxfail",
    "is_hard_exit_code",
    "load_environments",
    "parse_env_arg",
    "resolve_active_env",
    "resolve_qgis_setting",
    "strip_env_args",
    "strip_maxfail_args",
]
