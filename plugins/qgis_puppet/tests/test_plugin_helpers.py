"""plugin_helpers.py の単体テスト。

PyQt5 / QGIS / qgis_puppeteer は不要（helper が PyQt5 非依存な点が売り）。
`monkeypatch` で環境変数・`sys.platform` / `sys.executable` を差し替え、
`tmp_path` で実ファイルを作って Path 判定をリアルに回す。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from qgis_puppet import plugin_helpers as ph

# ==============================================================
# _is_python_executable
# ==============================================================


class TestIsPythonExecutable:
    """basename stem の分類。パスが存在するかは見ない（純粋関数）。"""

    @pytest.mark.parametrize(
        "name",
        [
            "python",
            "python.exe",
            "python3",
            "python3.exe",
            "pythonw",
            "pythonw.exe",
            "python3w",
            # 注: `python3.11` は Path.stem で `python3` になる（`.11` が拡張子扱い）
            # ので _PYTHON_STEMS 側でヒットする。startswith 側は `python3.12.exe`
            # のように suffix=.exe のケースで効く。
            "python3.11",
            "python3.12.exe",
            "PYTHON",  # 大文字小文字は区別しない
        ],
    )
    def test_python_variants_are_recognized(self, name: str) -> None:
        assert ph._is_python_executable(Path(f"/tmp/{name}")) is True

    @pytest.mark.parametrize(
        "name",
        [
            "qgis-bin",
            "qgis-ltr-bin",
            "qgis-ltr-bin.exe",
            "QGIS",
            "qgis.exe",
            "node",
            "something",
        ],
    )
    def test_non_python_binaries_are_rejected(self, name: str) -> None:
        assert ph._is_python_executable(Path(f"/tmp/{name}")) is False


# ==============================================================
# _find_python_in_dir
# ==============================================================


class TestFindPythonInDir:
    def test_returns_none_when_dir_does_not_exist(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        assert ph._find_python_in_dir(missing) is None

    def test_returns_none_when_no_candidate_found(self, tmp_path: Path) -> None:
        # 空ディレクトリ
        assert ph._find_python_in_dir(tmp_path) is None

    def test_returns_first_matching_candidate(self, tmp_path: Path) -> None:
        # 候補順は ("python3.exe", "python.exe", "python3", "python")
        # python.exe しかない場合はそれを返す
        exe = tmp_path / "python.exe"
        exe.write_text("")
        result = ph._find_python_in_dir(tmp_path)
        assert result == exe

    def test_prefers_python3_exe_over_python_exe(self, tmp_path: Path) -> None:
        (tmp_path / "python.exe").write_text("")
        py3 = tmp_path / "python3.exe"
        py3.write_text("")
        result = ph._find_python_in_dir(tmp_path)
        assert result == py3


# ==============================================================
# _resolve_hub_python
#
# 注: `python-qgis-ltr.bat` の探索ロジック自体は OSS 公開 API
# `qgis_puppeteer.find_qgis_python_launcher()` に集約されており、その単体
# テストは packages/qgis-puppeteer/tests/test_qgis_env.py に移動。ここでは
# プラグイン固有のフォールバック層（sys.executable 兄弟検索 etc.）の挙動と、
# OSS 関数を呼び出す合成挙動を検証する。
# ==============================================================

# .bat ファイル名は OSS 側と揃えてテスト内で再利用する
_QGIS_LAUNCHER_BAT = "python-qgis-ltr.bat"


class TestResolveHubPython:
    """4 層の優先順が正しく機能するかを個別に確認する。

    各テストで `monkeypatch.delenv(..., raising=False)` を先に行い、
    親プロセスの環境変数の影響を切る。
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ph.ENV_HUB_PYTHON, raising=False)
        monkeypatch.delenv("OSGEO4W_ROOT", raising=False)

    def test_1_env_var_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        explicit = tmp_path / "my-python.exe"
        explicit.write_text("")
        monkeypatch.setenv(ph.ENV_HUB_PYTHON, str(explicit))

        # 他の候補が見つかってもオーバーライドが勝つよう .bat も配置
        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        (osgeo / "bin" / _QGIS_LAUNCHER_BAT).write_text("")
        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))

        assert ph._resolve_hub_python() == explicit

    def test_1_env_var_override_missing_file_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ph.ENV_HUB_PYTHON, str(tmp_path / "does-not-exist"))

        # python-qgis-ltr.bat を用意して 2 段目が拾うようにする
        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        bat = osgeo / "bin" / _QGIS_LAUNCHER_BAT
        bat.write_text("")
        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))

        assert ph._resolve_hub_python() == bat

    def test_2_qgis_launcher_bat_wins_over_sys_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # sys.executable も python 系だが、.bat 優先
        fake_sys_python = tmp_path / "system-python.exe"
        fake_sys_python.write_text("")
        monkeypatch.setattr("sys.executable", str(fake_sys_python))

        osgeo = tmp_path / "QGIS"
        (osgeo / "bin").mkdir(parents=True)
        bat = osgeo / "bin" / _QGIS_LAUNCHER_BAT
        bat.write_text("")
        monkeypatch.setenv("OSGEO4W_ROOT", str(osgeo))

        assert ph._resolve_hub_python() == bat

    def test_3_sys_executable_used_when_python_like(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # .bat が見つからない（OSGEO4W_ROOT 未設定・兄弟にも .bat なし）
        fake_python = tmp_path / "python3.exe"
        fake_python.write_text("")
        monkeypatch.setattr("sys.executable", str(fake_python))

        assert ph._resolve_hub_python() == fake_python

    def test_4_sibling_python_when_sys_executable_is_qgis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # QGIS プラグイン実行を模擬：sys.executable=qgis-ltr-bin.exe、
        # しかし .bat が兄弟にも OSGEO4W_ROOT にもない（変則配置）
        qgis_dir = tmp_path / "osgeo" / "bin"
        qgis_dir.mkdir(parents=True)
        qgis_bin = qgis_dir / "qgis-ltr-bin.exe"
        qgis_bin.write_text("")
        sibling = qgis_dir / "python3.exe"
        sibling.write_text("")

        monkeypatch.setattr("sys.executable", str(qgis_bin))

        assert ph._resolve_hub_python() == sibling

    def test_returns_none_when_nothing_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 全フォールバック空振り
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        qgis_bin = empty_dir / "qgis.exe"
        qgis_bin.write_text("")
        monkeypatch.setattr("sys.executable", str(qgis_bin))

        assert ph._resolve_hub_python() is None


# ==============================================================
# _resolve_qgis_puppeteer_root
# ==============================================================


class TestResolveQgisPuppeteerRoot:
    """import 解決と OSS workspace fallback の両経路を検証する。"""

    def test_returns_package_dir_when_import_succeeds(self) -> None:
        # 通常実行時（uv workspace で qgis_puppeteer が import 可能）。
        # `__init__.py` の親ディレクトリ = パッケージ実体ディレクトリを返す。
        import qgis_puppeteer

        resolved = ph._resolve_qgis_puppeteer_root()
        assert resolved is not None
        assert resolved == Path(qgis_puppeteer.__file__).resolve().parent

    def test_falls_back_to_workspace_when_import_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # import を強制的に失敗させる。OSS workspace に実体ディレクトリが
        # ある前提（このテストは qgis-puppeteer リポ内で走る）。
        import builtins

        real_import = builtins.__import__

        def _fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "qgis_puppeteer" or name.startswith("qgis_puppeteer."):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

        resolved = ph._resolve_qgis_puppeteer_root()
        # OSS workspace 配下の `packages/qgis-puppeteer/src/qgis_puppeteer/`
        assert resolved is not None
        expected = (
            ph._REPO_ROOT / "packages" / "qgis-puppeteer" / "src" / "qgis_puppeteer"
        ).resolve()
        assert resolved == expected

    def test_returns_none_when_import_fails_and_no_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # import 失敗 + fallback も存在しない条件。`_REPO_ROOT` をテンポラリに
        # 差し替えて fallback を「存在しないパス」へ向ける。
        import builtins

        real_import = builtins.__import__

        def _fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "qgis_puppeteer" or name.startswith("qgis_puppeteer."):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        monkeypatch.setattr(ph, "_REPO_ROOT", tmp_path)

        assert ph._resolve_qgis_puppeteer_root() is None


# ==============================================================
# _build_hub_spawn_command
# ==============================================================


class TestBuildHubSpawnCommand:
    def test_uses_bootstrap_script(self) -> None:
        # `-m qgis_puppeteer.hub` ではなく bootstrap の絶対パスを渡す。
        # python-qgis-ltr.bat 経由だと PYTHONPATH が wipe されるため。
        python = Path("/usr/bin/python3")
        cmd = ph._build_hub_spawn_command(python, 9876)
        assert cmd[0] == str(python)
        assert cmd[1] == str(ph._HUB_BOOTSTRAP)
        assert cmd[2:] == ["--port", "9876"]

    def test_bootstrap_path_exists(self) -> None:
        # 実ファイルとして存在していないと subprocess が即死する
        assert ph._HUB_BOOTSTRAP is not None
        assert ph._HUB_BOOTSTRAP.is_file()

    def test_bootstrap_lives_inside_qgis_puppeteer_package(self) -> None:
        # `_hub_bootstrap.py` は qgis_puppeteer パッケージ内に同梱される
        assert ph._QGIS_PUPPETEER_ROOT is not None
        assert ph._HUB_BOOTSTRAP == ph._QGIS_PUPPETEER_ROOT / "_hub_bootstrap.py"

    def test_port_is_converted_to_string(self) -> None:
        cmd = ph._build_hub_spawn_command(Path("py"), 12345)
        assert "12345" in cmd
        assert all(isinstance(x, str) for x in cmd)

    def test_raises_when_bootstrap_unresolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # qgis_puppeteer が import できず fallback も無い極端ケース。
        # 静かに壊れた command を返さず、明示的に RuntimeError を上げる。
        monkeypatch.setattr(ph, "_HUB_BOOTSTRAP", None)
        with pytest.raises(RuntimeError, match="qgis_puppeteer is not importable"):
            ph._build_hub_spawn_command(Path("py"), 9876)


# ==============================================================
# _build_hub_spawn_env
# ==============================================================


class TestBuildHubSpawnEnv:
    def test_extra_syspath_carries_current_sys_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 親プロセスの sys.path をそのまま env に詰めるのが新仕様。
        # PYTHONPATH は QGIS の python-qgis-ltr.bat が消去するため、
        # 独自 env QPUPPETEER_HUB_EXTRA_SYSPATH 経由で伝達する。
        sentinel = "/__sentinel_path__/qgis_puppeteer_test"
        monkeypatch.syspath_prepend(sentinel)
        env = ph._build_hub_spawn_env()

        extra = env[ph.ENV_HUB_EXTRA_SYSPATH]
        parts = extra.split(os.pathsep)
        assert sentinel in parts

    def test_extra_syspath_is_set_even_when_pythonpath_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
        env = ph._build_hub_spawn_env()
        # 値は sys.path 由来なので空にはならない
        assert env[ph.ENV_HUB_EXTRA_SYSPATH]

    def test_inherits_other_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_CUSTOM_VAR", "keep-me")
        env = ph._build_hub_spawn_env()
        assert env.get("MY_CUSTOM_VAR") == "keep-me"

    def test_returns_a_fresh_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 親プロセス側の os.environ を壊さないこと
        env = ph._build_hub_spawn_env()
        env[ph.ENV_HUB_EXTRA_SYSPATH] = "modified"
        assert os.environ.get(ph.ENV_HUB_EXTRA_SYSPATH) != "modified"


# ==============================================================
# _extract_port
# ==============================================================


class TestExtractPort:
    def test_extracts_explicit_port(self) -> None:
        assert ph._extract_port("ws://127.0.0.1:9876/path") == 9876

    def test_returns_default_when_port_missing(self) -> None:
        assert ph._extract_port("ws://127.0.0.1/path") == ph.DEFAULT_HUB_PORT

    def test_returns_custom_default(self) -> None:
        assert ph._extract_port("ws://127.0.0.1/", default=1234) == 1234

    def test_returns_default_for_invalid_url(self) -> None:
        # urlparse は緩いのでほぼ何でも受ける。それでも port が取れなければ default
        assert ph._extract_port("not a url") == ph.DEFAULT_HUB_PORT

    def test_supports_wss_and_custom_ports(self) -> None:
        assert ph._extract_port("wss://example.com:443/register") == 443
        assert ph._extract_port("ws://[::1]:8765/") == 8765


# ==============================================================
# _resolve_hub_log_file
# ==============================================================


class TestResolveHubLogFile:
    def test_env_var_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        explicit = tmp_path / "my.log"
        monkeypatch.setenv(ph.ENV_HUB_LOG_FILE, str(explicit))

        assert ph._resolve_hub_log_file() == explicit

    def test_default_is_temp_dir_with_known_filename(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ph.ENV_HUB_LOG_FILE, raising=False)
        result = ph._resolve_hub_log_file()
        assert result.name == ph.DEFAULT_SPAWN_LOG_FILENAME
        # 一時ディレクトリ配下にあること
        import tempfile

        assert str(result.parent) == tempfile.gettempdir()


# ==============================================================
# discover_extensions（ADR-0001 §12.3）
# ==============================================================


class _FakeModule:
    """importlib.import_module が返すモックモジュール。"""

    def __init__(self, build_handlers=None):
        if build_handlers is not None:
            self.build_handlers = build_handlers


class TestDiscoverExtensions:
    """`puppeteer_api.py` convention discovery の検証。

    `importlib.import_module` を monkeypatch で差し替えて、実際のプラグイン
    パッケージを用意せずに各分岐を網羅する。
    """

    @pytest.fixture
    def mock_importer(self, monkeypatch: pytest.MonkeyPatch):
        """モジュール名 → モック module の dict を差し替えるヘルパ。"""
        modules: dict[str, object] = {}

        def fake_import(name: str):
            if name in modules:
                return modules[name]
            raise ImportError(name)

        monkeypatch.setattr(ph.importlib, "import_module", fake_import)
        return modules

    def test_skips_self_plugin(self, mock_importer) -> None:
        """skip パラメータに一致するプラグインは import しない。"""
        called: list[str] = []

        def build_handlers(iface):
            called.append("built")
            return {"sample.x": lambda p: None}

        mock_importer["qgis_puppet.puppeteer_api"] = _FakeModule(build_handlers)

        register_calls: list[tuple[str, object]] = []
        result = ph.discover_extensions(
            plugin_names=["qgis_puppet"],
            iface=object(),
            register=lambda cmd, h: register_calls.append((cmd, h)),
        )
        assert called == []
        assert register_calls == []
        assert result == []

    def test_registers_handlers_from_multiple_plugins(self, mock_importer) -> None:
        iface = object()

        def build_sample(iface_arg):
            assert iface_arg is iface
            return {"sample.a": lambda p: "a", "sample.b": lambda p: "b"}

        def build_region(iface_arg):
            return {"extensions.region.c": lambda p: "c"}

        mock_importer["sample_plugin.puppeteer_api"] = _FakeModule(build_sample)
        mock_importer["region_plugin.puppeteer_api"] = _FakeModule(build_region)

        register_calls: list[tuple[str, object]] = []
        result = ph.discover_extensions(
            plugin_names=["sample_plugin", "region_plugin"],
            iface=iface,
            register=lambda cmd, h: register_calls.append((cmd, h)),
        )
        registered_commands = {cmd for cmd, _ in register_calls}
        assert registered_commands == {"sample.a", "sample.b", "extensions.region.c"}
        assert len(result) == 3

    def test_plugin_without_puppeteer_api_is_skipped(self, mock_importer) -> None:
        """該当モジュール無し（ImportError）は正常系として無視。"""
        register_calls: list = []
        result = ph.discover_extensions(
            plugin_names=["no_extension_plugin"],
            iface=object(),
            register=lambda cmd, h: register_calls.append((cmd, h)),
        )
        assert register_calls == []
        assert result == []

    def test_missing_build_handlers_is_skipped(self, mock_importer) -> None:
        """puppeteer_api.py はあるが build_handlers が無い → warning skip。"""
        mock_importer["bad_plugin.puppeteer_api"] = _FakeModule(build_handlers=None)
        register_calls: list = []
        result = ph.discover_extensions(
            plugin_names=["bad_plugin"],
            iface=object(),
            register=lambda cmd, h: register_calls.append((cmd, h)),
        )
        assert register_calls == []
        assert result == []

    def test_build_handlers_exception_skips_plugin(self, mock_importer) -> None:
        def boom(iface):
            raise RuntimeError("build failed")

        mock_importer["raising_plugin.puppeteer_api"] = _FakeModule(boom)
        register_calls: list = []
        result = ph.discover_extensions(
            plugin_names=["raising_plugin"],
            iface=object(),
            register=lambda cmd, h: register_calls.append((cmd, h)),
        )
        assert register_calls == []
        assert result == []

    def test_non_dict_return_is_skipped(self, mock_importer) -> None:
        mock_importer["bogus.puppeteer_api"] = _FakeModule(lambda iface: ["not a dict"])
        register_calls: list = []
        result = ph.discover_extensions(
            plugin_names=["bogus"],
            iface=object(),
            register=lambda cmd, h: register_calls.append((cmd, h)),
        )
        assert register_calls == []
        assert result == []

    def test_non_callable_handler_value_is_skipped(self, mock_importer) -> None:
        mock_importer["mixed.puppeteer_api"] = _FakeModule(
            lambda iface: {"sample.ok": lambda p: 1, "sample.bad": "not callable"}
        )
        register_calls: list = []
        result = ph.discover_extensions(
            plugin_names=["mixed"],
            iface=object(),
            register=lambda cmd, h: register_calls.append((cmd, h)),
        )
        assert [cmd for cmd, _ in register_calls] == ["sample.ok"]
        assert result == [("mixed", "sample.ok")]

    def test_register_failure_isolates_single_handler(self, mock_importer) -> None:
        """register が失敗した handler は skip、他は続行。"""
        mock_importer["multi.puppeteer_api"] = _FakeModule(
            lambda iface: {
                "sample.ok1": lambda p: 1,
                "sample.bad": lambda p: 2,
                "sample.ok2": lambda p: 3,
            }
        )

        def register(cmd, handler):
            if cmd == "sample.bad":
                raise ValueError("simulated registration failure")

        result = ph.discover_extensions(
            plugin_names=["multi"],
            iface=object(),
            register=register,
        )
        assert sorted(cmd for _, cmd in result) == ["sample.ok1", "sample.ok2"]

    def test_returned_list_preserves_plugin_and_command_names(self, mock_importer) -> None:
        mock_importer["sample_plugin.puppeteer_api"] = _FakeModule(
            lambda iface: {"sample.cmd1": lambda p: None}
        )
        mock_importer["test_helpers.puppeteer_api"] = _FakeModule(
            lambda iface: {"test.cmd2": lambda p: None}
        )
        result = ph.discover_extensions(
            plugin_names=["sample_plugin", "test_helpers"],
            iface=object(),
            register=lambda cmd, h: None,
        )
        assert ("sample_plugin", "sample.cmd1") in result
        assert ("test_helpers", "test.cmd2") in result


# ==============================================================
# _resolve_hub_url
# ==============================================================


class TestResolveHubUrl:
    """ENV の優先順（URL > HOST/PORT > default）。

    `monkeypatch.delenv(..., raising=False)` で 3 キーを空に戻してから
    個別のシナリオを作る。
    """

    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (ph.ENV_HUB_URL, ph.ENV_HUB_HOST, ph.ENV_HUB_PORT):
            monkeypatch.delenv(key, raising=False)

    def test_returns_default_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        assert ph._resolve_hub_url() == "ws://127.0.0.1:9876"

    def test_hub_url_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv(ph.ENV_HUB_URL, "ws://example.com:8080/path")
        # HOST/PORT を併設しても HUB_URL が優先される
        monkeypatch.setenv(ph.ENV_HUB_HOST, "10.0.0.1")
        monkeypatch.setenv(ph.ENV_HUB_PORT, "1234")
        assert ph._resolve_hub_url() == "ws://example.com:8080/path"

    def test_host_and_port_are_composed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv(ph.ENV_HUB_HOST, "10.0.0.5")
        monkeypatch.setenv(ph.ENV_HUB_PORT, "9999")
        assert ph._resolve_hub_url() == "ws://10.0.0.5:9999"

    def test_only_host_keeps_default_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv(ph.ENV_HUB_HOST, "192.168.1.10")
        assert ph._resolve_hub_url() == "ws://192.168.1.10:9876"

    def test_only_port_keeps_default_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv(ph.ENV_HUB_PORT, "4242")
        assert ph._resolve_hub_url() == "ws://127.0.0.1:4242"
