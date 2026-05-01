# ADR-0003: Test Environments — partition モデルの宣言的 QGIS 起動構成切替

- **Status**: Accepted
- **Date**: 2026-04-26 (Accepted: 2026-04-28)
- **Deciders**: qgis-puppeteer maintainers
- **Supersedes**: [ADR-0002 §14 "Test Environments"](0002-e2e-test-architecture.md#14-test-environmentspartition-モデル) — 旧 Playwright `projects` 準拠の cartesian モデル
- **Related**:
  - ADR-0002 §10.4 `@pytest.mark.fresh_qgis` — 個別テスト単位の QGIS 再起動
  - ADR-0002 §13 失敗時診断バンドル — env 別の artifacts 分離

## Context

### 解決したい問題

QGIS の E2E テストでは、テスト対象によって QGIS の起動構成を変えたいケースが
多い。代表的な分岐軸:

| 分岐軸 | 例 |
|---|---|
| **QGIS バージョン** | LTR (`3.34`) と最新版 (`3.40`) の両方でリグレッション確認 |
| **QGIS プロファイル** | `--profile=test_clean` / `--profile=with_extras` など、有効プラグインや QSettings が異なる |
| **プラグインセット** | 検査対象プラグインだけ有効 / 全プラグイン有効、を切り替え |
| **認証・外部接続のモード** | 認証サブシステム有効・無効、ネットワーク有無 |
| **データセット規模** | 軽量データ（CI 用）/ 大規模データ（性能テスト用） |
| **Locale / 表示言語** | UI テキストが日本語 / 英語 |
| **ホストアプリ独自 launcher の引数** | ホストが提供する `launcher.bat` の起動オプションを切り替え |

これらは「同じテストを異なる構成で走らせる」のではなく、**「異なるテスト群を
異なる構成で走らせる」** のが普通。例えば「認証フローのテスト」は認証有効構成で
しか意味がなく、認証無効構成で走らせるとほぼ全部 red になる。

現状の OSS が提供する手段:

1. **session-scoped fixture override**: `conftest.py` ごとに別 launcher で QGIS を起動（複数ディレクトリ運用）
2. **`qgis_command` env / ini 設定**: pytest 起動時に外から構成を渡す（CI matrix で切替）
3. **`@pytest.mark.fresh_qgis`**（ADR-0002 §10.4、未実装）: 個別テストで QGIS 再起動

これらは「複数 env を 1 pytest コマンドで回す」ニーズに対しては手動運用（複数 conftest + bash loop / CI matrix）が必要で、宣言性が低い。

### Playwright `projects` を直輸入した旧案の問題

ADR-0002 §14 の旧案は Playwright の `projects` を踏襲した:

- **モデル**: テスト × project の **cartesian product**
- **既定挙動**: 全テストが全 project で実行される
- **想定**: 「同じテストをブラウザ違いで走らせて互換性検証」

これは Playwright のユースケース（ブラウザ間の同等性確認）には合うが、**QGIS では合わない**:

- QGIS の「状態違い」は実装互換性ではなくドメイン固有性
- 「認証フローのテスト」は認証有効構成の DB / プラグイン / 設定を前提にしている
- 全テスト × 全 env で走らせると、ほとんどが意味のない red になる
- 各テストに「この env でだけ走らせる」「この env では skip する」を表現する複雑な exclusion 機構が必要になり、結果として「素直に書いた test が cartesian で走って壊れる」逆向きの設計になる

QGIS は **partition モデル**（テストは特定 env に所属する）の方が自然。

## Decision

### 1. Test Environments を **partition モデル** で実装する

「環境定義」と「テストの env 所属」を分けて宣言し、CLI で env を選択して走らせる。

- 環境 = QGIS 起動構成（command / args / python / env）の集合
- テスト → 環境の関係は **partition**（cartesian ではない）
- 既定: 各テストは marker で所属 env を指定。明示しないテストはデフォルト env で走る

### 2. Playwright `projects` モデルは採用しない

「同じテストを多 env で」というユースケースが必要になった少数のテストには、複数 env を marker に列挙する **opt-in cartesian** を許す。これにより:

- 大多数の env 固有テストは marker 1 個で簡潔
- 少数の cross-env テスト（プロトコル互換性確認等）は明示的に opt-in
- 「全テストが意味なく cartesian で走る」事故が起きない

### 3. 名前は `environments` を採用

「project」は Playwright 連想を強く引きずるため避け、QGIS の現実に合った中立名 `environments` を使う。CLI / marker / 設定ファイルすべてで統一。

## Detailed Design

### 環境定義（`environments.toml`）

プロジェクトルートまたは `test_e2e/` 配下に配置。`pyproject.toml` 内インライン記法も検討可。

```toml
# test_e2e/environments.toml

[[environments]]
name = "smoke"
qgis_args = ["--profile=test"]
description = "最小プロファイルのスモークテスト"

[[environments]]
name = "with_authentication"
qgis_args = ["--profile=auth_test"]
description = "認証サブシステム有効。ログイン flow のテストはここに所属"
env = { MYAPP_AUTH_ENABLED = "1" }

[[environments]]
name = "offline_mode"
qgis_args = ["--profile=offline"]
description = "ネットワーク無効。WMTS / リモート DB 不在時の挙動を検証"
env = { MYAPP_OFFLINE = "1" }

[[environments]]
name = "qgis_lts"
qgis_bin = "C:/Program Files/QGIS 3.34/bin/qgis-bin.exe"
qgis_args = ["--profile=regression"]
description = "QGIS 3.34 LTR でのリグレッション確認（最新版と挙動が分岐するテスト）"

[[environments]]
name = "host_launcher"
qgis_command = ["scripts/launcher.bat", "--config", "e2e", "--feature", "x"]
description = "ホストアプリが提供する launcher 経由（独自 env / DB 接続情報を立てる）"
```

各 environment は既存の `pytest-qgis-puppeteer` 設定キーと同じセマンティクスで上書き可能:

| キー | 用途 | 既存設定との関係 |
|---|---|---|
| `name` | 識別名（必須） | — |
| `description` | 説明（任意、レポート用） | — |
| `qgis_command` | 起動コマンド全体 | `qgis_command` ini と同じ |
| `qgis_bin` | QGIS 実行ファイル | `qgis_bin` ini と同じ |
| `qgis_args` | 起動引数 | `qgis_args` ini と同じ |
| `qgis_python` | Hub spawn 用 Python launcher | `qgis_python` ini と同じ |
| `env` (table) | 追加環境変数 | spawn 時に既存 env に merge |

排他ルール: `qgis_command` を指定すると `qgis_bin` / `qgis_args` は無視される（既存と同じ）。

### Marker による env 所属

```python
# tests/auth/conftest.py — ディレクトリ単位
pytestmark = pytest.mark.qgis_env("with_authentication")

# tests/test_smoke.py — ファイル単位
pytestmark = pytest.mark.qgis_env("smoke")

# 個別テスト
@pytest.mark.qgis_env("with_authentication")
def test_login_flow(qgis):
    ...

@pytest.mark.qgis_env("offline_mode")
def test_loads_cached_layer_when_network_down(qgis):
    ...

# 複数 env で走らせたい少数のテスト（opt-in cartesian）
@pytest.mark.qgis_env("smoke", "qgis_lts")
def test_basic_open_project(qgis):
    """基本機能が現行版と LTR 両方で動くことを確認するスモーク。"""
    ...

# marker 無しテスト → デフォルト env で走る（既定動作は §"未指定 marker の扱い" 参照）
def test_unmarked(qgis):
    ...
```

`qgis_env` marker は `pytest_qgis_puppeteer` plugin 側で `pytest_configure` 内に登録する。

**marker の処理経路**: `qgis_env` は pytest 標準の `-m` セレクタとは **独立経路**で
処理する。理由:

- `-m "qgis_env(with_authentication)"` のような expression 評価では env table の
  存在チェック（typo 検出）や `--env` CLI との突合せができない
- 独立経路なら、`--env` を受けて該当 marker の test だけを残す処理を
  `pytest_collection_modifyitems` で完結できる
- ユーザーが `-m "qgis_env"` で「env marker が付いてる test 全部」を選びたい場合は、
  pytest 標準経路でも従来通り動く（marker は registered なので衝突しない）

つまり `qgis_env` marker は **二重に解釈される**:
- `--env=<name>` 経路: env name 一致をフィルタ条件にする（独自実装）
- `-m "<expr>"` 経路: 単純な marker 存在チェック（pytest 標準）

両者は直交し、`pytest --env=with_authentication -m "smoke"` のような併用も自然に動く。

### CLI

```bash
# デフォルト env のみ実行
pytest

# 特定 env のテストのみ実行
pytest --env=with_authentication

# 複数 env を順次実行
pytest --env=with_authentication,offline_mode

# 全 env を順次実行
pytest --env=all

# env と pytest 標準フィルタの併用
pytest --env=with_authentication -k "login"
pytest --env=all tests/test_smoke.py
```

### 実行モデル

#### モード判定

pytest 起動引数を見て **2 つのモード**のいずれかに入る。判定は
`pytest_configure` の冒頭で 1 回だけ行い、以降のフックの挙動を切り替える:

| 起動形 | モード | 親 invocation の挙動 |
|---|---|---|
| `pytest`（`--env` 未指定） | **single-env** | 通常 collection あり。`default_environment` 設定に従い env 1 つを使ってテスト実行 |
| `pytest --env=<name>` | **single-env** | 通常 collection あり。指定 env の marker を持つ test だけにフィルタして実行 |
| `pytest --env=a,b` または `--env=all` | **meta-parent** | 親側は collection 0 件で素通し。`pytest_sessionfinish` で env ごとに子 `pytest.main()` を順次起動。集約 exit code を `session.exitstatus` に設定 |

子 invocation（meta-parent から起動された）は常に **single-env** モードで動作する。
`QPUPPETEER_QGIS_ENV` 等の env 変数を子に渡して env 名を伝える。

#### single-env モード

`--env=<name>` 指定時:

1. environments.toml をロードして対象 env を確定
2. `pytest_collection_modifyitems` で該当 env の marker を持つ test だけ残す
3. session fixture が env の構成で QGIS を spawn
4. tests を実行
5. teardown
6. artifacts を `outputs/<env_name>/` に保存

#### meta-parent モード

`--env=all` または `--env=a,b` 指定時:

各 env について上記 1〜6 を順次実行。pytest の collection を env ごとに再構築するために **複数 session を内部実行する**:

```
for env in environments:
    1. その env を marker に持つ test を collect
       （0 件なら次の env へ）
    2. QGIS を env の構成で spawn
    3. tests 実行
    4. teardown
    5. artifacts を outputs/<env_name>/ に保存
6. 集約レポートを outputs/summary.json に出力
exit code: 全 env で 0 なら 0、いずれか fail なら非 0
```

実装は §"Implementation approaches" を参照。

### 未指定 marker の扱い

`qgis_env` marker が無いテストの扱いは設定で選べる:

```toml
# environments.toml
[default_environment]
strategy = "named"           # 指定名を使う（既定。default_name 必須）
default_name = "smoke"
# strategy = "fail"          # marker 必須、無ければエラー
```

既定を `strategy="named"` にしたのは、TOML の宣言順依存（`strategy="first"`）が
fragile なため。`environments.toml` の上から 1 番目が意図通りの env である保証は
無く、env を追加・並べ替えしただけでデフォルトが変わると事故が起きやすい。

`default_name` を必須にすることで「明示宣言された env が default」という不変が保たれる。
env が増えて全テストに marker を付ける運用に移行したら `strategy="fail"` に切り替える。

### 既定 env

`environments.toml` が無い、または `--env` 未指定の場合:

- `environments.toml` 無し: 従来通り `qgis_command` / `qgis_bin` ini を使う（後方互換）
- `environments.toml` 有り + `--env` 未指定: `default_environment.strategy` に従う

### env 設定 と 既存 ini / env / CLI の優先順位

`environments.toml` 有りの状態で env 解決後にも、既存の CLI / 環境変数 / ini が
評価される。優先順位は **CLI > 環境変数 > environments.toml の env 定義 > 既存 ini**:

| ソース | 例 | 優先度 |
|---|---|---|
| CLI 引数 | `--qgis-command "x.bat"` | 1（最優先） |
| 環境変数 | `QPUPPETEER_QGIS_COMMAND=x.bat` | 2 |
| `environments.toml` の env table | `[[environments]] qgis_command=...` | 3 |
| `pyproject.toml` の `[tool.pytest.ini_options]` | `qgis_command = [...]` | 4（最下位） |

意図: env 定義は「テスト群ごとの既定構成」、CLI / 環境変数は「その場限りの上書き」。
CI で `--env=with_authentication` を指定しつつローカル debug で
`QPUPPETEER_QGIS_BIN` を一時的に差し替える、といった運用が成立する。

排他ルール（既存と同一）: いずれか 1 つでも `qgis_command` が解決されたら、
`qgis_bin` / `qgis_args` は無視される。

### CLI と pytest 標準オプションの併用

| 組み合わせ | 挙動 |
|---|---|
| `pytest --env=all -k "smoke"` | 全 env で `-k` フィルタを適用 |
| `pytest --env=with_authentication tests/test_login.py` | 指定 env で指定ファイルのみ |
| `pytest --env=all -x` | env 内で fail-fast。次 env には進む |
| `pytest --env=all --maxfail=3` | 全 env 累計で 3 回 fail で停止 |

`--env=all` 内の各 env は独立 session として扱うため、fixture state は env を跨いで持ち越されない。

**`--maxfail` 累計の実装方針**: Approach (b) では子 `pytest.main()` は親の累計 fail 数を
知らない。親側で以下を実装する:

1. 各子 invocation 終了後、その env の JUnit XML をパースして fail 数を取得
2. 親プロセスで `cumulative_fails` を加算
3. 次 env を呼ぶ前に `cumulative_fails >= maxfail` なら break（残 env は skip 扱いで `summary.json` に記録）
4. 子 invocation には `--maxfail=<remaining>` に **書き換えた値**を渡す（残許容数を渡すことで子内 fail-fast も連動）

JUnit を途中段で読むのが嫌なら、子に小さな report-plugin を inject して exit 時に
fail 数を `os.environ` 経由で親に返す方式でも可。Phase 2 着手時に判断。

### artifacts のレイアウト

```
outputs/
├── smoke/
│   ├── junit.xml
│   ├── pytest.log
│   └── diagnostics/
│       └── <test_name>_<timestamp>/
│           ├── screenshot.png
│           ├── snapshot_ui.json
│           └── ...
├── with_authentication/
│   └── ...
├── offline_mode/
│   └── ...
├── qgis_lts/
│   └── ...
└── summary.json   # 全 env の pass/fail/skip カウント、durations、失敗 test ID
```

`summary.json` は ADR-0002 §21 のサマリレポート設計と同じ schema を踏襲し、env 軸を追加する。

### artifacts / JUnit の衝突回避

opt-in cartesian（marker に複数 env 列挙）または `--env=all` で同一テストが複数 env で
走る場合の衝突回避ルール:

- **diagnostic bundle**: `outputs/<env_name>/diagnostics/<test_name>_<timestamp>/` で
  env ディレクトリ分離により衝突しない
- **JUnit XML**: env ごとに別ファイル（`outputs/<env_name>/junit.xml`）。
  CI の集約ツールで全 env を 1 ファイルに merge する場合は、`testcase.classname` に
  env 名を prefix（`<env_name>::test_module::test_func`）する処理を Phase 3 で
  `summary.json` 生成と合わせて実装。
- **pytest-cov の coverage data**: env ごとに `.coverage.<env_name>` を生成し、
  最終段で `coverage combine` する想定。`COVERAGE_FILE` env 変数を子 invocation で
  切り替えることで実現。

### `--env=all` の exit code 集約ルール

pytest の標準 exit code（`0` pass / `1` fail / `2` interrupt / `3` internal error /
`4` usage / `5` no tests collected）に対する集約:

| 状況 | 親 invocation の exit code |
|---|---|
| 全 env が 0 | 0 |
| いずれかが 1 (fail) | 1 |
| いずれかが 2 (interrupt) | 2（即時 abort、後続 env は走らせない） |
| いずれかが 3 / 4 | そのまま伝播（即時 abort） |
| ある env が 5 (no tests collected) | **吸収して 0 扱い**（その env に該当テストが無いだけ。`summary.json` には記録） |
| 5 と他の組み合わせ | 5 を無視し他のルールを適用 |
| **全 env が 5** | **5 を返す**（CI が green と誤検知するのを防ぐガード。1 件もテストが走らない実行は明示的にエラーにする） |

`--env=all` の "no tests collected" 全 env 共通は希なので、5 の吸収を既定とする。
明示的にエラーにしたいユーザー向けに `--env-strict` フラグを将来追加検討（Phase 4）。

### `@pytest.mark.fresh_qgis` との関係

env はベース構成、`fresh_qgis` は個別テストで「env 構成 + 追加 args で再起動」の上書き:

```python
@pytest.mark.qgis_env("with_authentication")
@pytest.mark.fresh_qgis(args=["--clean-canvas"], env={"FOO": "1"})
def test_pristine_state(qgis):
    """with_authentication env の構成で、このテストだけ canvas クリアで再起動。"""
    ...
```

合成ルール（env を base、`fresh_qgis` を overlay として明文化）:

| キー | 合成方式 |
|---|---|
| `qgis_args` | env の args に `fresh_qgis(args=...)` を **append** |
| `env` (table) | env の env table に `fresh_qgis(env=...)` を **shallow merge**（同一キーは fresh_qgis 優先） |
| `qgis_bin` | env の値を使用（`fresh_qgis` での上書き不可） |
| `qgis_python` | env の値を使用（`fresh_qgis` での上書き不可） |
| `qgis_command` | **env が `qgis_command` 指定の場合、`fresh_qgis(args=...)` は command 末尾に append**。host 独自 launcher は append された args の意味を解釈する責任を持つ。append したくない場合はテスト側で `fresh_qgis(args=...)` を使わない |

`qgis_command` 指定 env で `fresh_qgis(args=...)` を使う場合、host launcher が
未知 arg を素直に QGIS に転送しないとテストが壊れる。Host launcher の責務として
ドキュメント化する。

### pytest-xdist との相互作用

`hub_process` / `qgis_process` は session-scoped で port / QGIS プロセスを握っているため、
xdist worker ごとに独立した Hub + QGIS が必要。Phase 1 時点での方針:

- **`--env=<single>` × `-n N`（xdist あり）**: xdist worker ごとに `_free_port()` で
  別 port を取得し、env 構成で QGIS を spawn。env 設定はすべての xdist worker で共通。
- **`--env=all` × `-n N`**: env は **順次実行**（envごとに別 pytest invocation）、
  各 invocation 内で xdist が test 並列。env 並列は Phase 1 では非対応。
  理由: Hub port / artifacts ディレクトリの分離は env 内で完結している方が単純。
  env 並列が必要になったら Phase 4 以降で `pytest-xdist` の dist mode 拡張として検討。
- **xdist worker × env の N×M セットアップ**: 単純に Hub × N×M を spawn するため
  リソース消費が大きい。`-n auto` のような自動指定では env 数を考慮しない点に注意。

### Multi-instance（ADR-0002 §5）との関係

複数 QGIS インスタンスを 1 session 内で同時操作するシナリオは、env レベルでは表現しない。env は「session 単位の構成」であり、multi-instance は session 内で複数 Worker を register する別軸。

将来必要なら env 内で `[[environments.instances]]` のサブテーブルを持たせる拡張余地はあるが、本 ADR では scope 外。

## Comparison: Playwright projects vs QGIS environments

| 観点 | Playwright projects | QGIS environments (本 ADR) |
|---|---|---|
| モデル | test × project の cartesian | test → env の partition |
| 既定挙動 | 全テストが全 project で走る | marker 指定 env のみで走る |
| 同じテストを多環境で | 主用途 | opt-in（marker に複数列挙） |
| 想定スケール | 「実装互換性確認」 | 「環境固有テスト群の独立管理」 |
| 実行時間 | テスト数 × project 数 | テスト総数（重複なし、または marker での minor 重複） |
| テスト exclusion ロジック | parametrize_projects 等の複雑な指定 | 「marker に書いてある env だけ」のシンプルなルール |
| デフォルト env | なし（全 project が等価） | あり（first / named） |

## Implementation approaches

### Approach (a): pytest 内で展開（`pytest_collection_modifyitems` 拡張）

各 test を marker に応じてフィルタし、`--env=all` の場合は内部で env 数だけ collection を再構築・順次実行する。

```python
# pytest_qgis_puppeteer/_environments.py
def pytest_addoption(parser):
    parser.addoption("--env", default=None)

def pytest_collection_modifyitems(config, items):
    selected = _selected_envs(config)  # 1 env or list
    items[:] = _filter_by_env_markers(items, selected)

def pytest_sessionstart(session):
    if session.config.getoption("--env") == "all":
        # ここで全 env を内部 invocation で順次実行する仕組みが必要
        ...
```

**メリット**: ユーザーが `pytest --env=all` で自然に動かせる。`-k` などの標準フィルタが効く。
**デメリット**: pytest の collection / session lifecycle に深く介入する必要があり、内部 invocation の実装が複雑。

### Approach (b): pytest を env ごとに subprocess で再呼び出し

`--env=all` を受けたら pytest plugin が `pytest.main()` を env 数分呼び出す。各 invocation は単一 env で完結。

```python
def pytest_configure(config):
    env_arg = config.getoption("--env")
    if env_arg == "all":
        envs = _load_environments(config)
        results = []
        for env in envs:
            os.environ["QPUPPETEER_QGIS_ENV"] = env.name
            rc = pytest.main(_remaining_argv(config))
            results.append((env.name, rc))
        _print_summary(results)
        sys.exit(any(rc != 0 for _, rc in results))
```

**メリット**: 単純。各 env が独立した session として綺麗に分離される。
**デメリット**: pytest の collection 結果が env ごとに繰り返し生成されるオーバーヘッド（初回のみ顕著）。`pytest.main()` 内で `pytest.main()` を呼ぶ再帰の安全性に注意。

### Approach (c): 専用 CLI ランナー

`pytest-qgis-puppeteer-run` のような外部 CLI を提供し、内部で pytest を env 数だけ呼ぶ。pytest 自体は単純化（1 invocation = 1 env）。

```bash
pytest-qgis-puppeteer-run --envs=all tests/
```

**メリット**: pytest plugin 内部に複雑なロジックを持ち込まない。
**デメリット**: ユーザーが `pytest` ではなく専用コマンドを使う必要があり、pytest の他 plugin（pytest-cov 等）との連携が分かりにくい。

### 推奨: (b) を選択

ユーザー体験（`pytest --env=all` 1 つで動く）と実装の素直さの両立を狙う。
(a) は `pytest_sessionstart` 以降で `pytest.main()` を呼んで親 session を抜けるのが
pytest lifecycle 上不自然（親側で collection / plugin manager / capture / logging が
既に立ち上がっている）で、collection を捨てて再構築するコードパスは罠が多い。
(b) は `pytest_configure` の早期段階で `--env=all` を検出し、env を子 invocation に
分配したうえで親をそのまま `sys.exit` で終了させる。各 env が独立 session として
分離され、(a) の lifecycle 課題を回避できる。

具体的な実装ポイント:

- `pytest_configure(config)` の冒頭で `--env=all` または comma 列挙を検出
- 検出した場合は env ごとに `pytest.main(_argv_for_env(env))` を順次呼び出し
  （`--env=<name>` を 1 つずつ渡す形に書き換えた argv）
- 親 invocation の collection / fixture は使わない。`pytest_configure` で早期分岐し、
  全 env 終了後に `sys.exit(_aggregate_exit_codes(results))` する
- 単一 env の `pytest.main()` 内では Approach (a) 的に
  `pytest_collection_modifyitems` で marker フィルタするだけ
- artifacts ディレクトリは子 invocation の `pytest_configure` で env 別パスに切り替える
- exit code 集約は §"`--env=all` の exit code 集約ルール" に従う

`pytest.main()` の再帰呼び出しは pytest 公式にサポートされている（plugin manager は
独立に立ち上がる）。pytest-cov / pytest-xdist など子側で必要な plugin は子 argv に
そのまま渡るため、追加配慮は不要。

**親 invocation の finalize 注意点**: `pytest_configure` で `sys.exit` してしまうと
親側の `pytest_unconfigure` フックがスキップされ、cov / cacheprovider などの finalize
（coverage の書き出し、cache の永続化）が漏れる。回避策:

1. `pytest_configure` 内で `--env=all` を検出したら即 `sys.exit` せず、フラグを
   `config._qgis_puppeteer_meta_envs` 等で立てるだけにする
2. `pytest_collection` フック（または `pytest_collection_modifyitems`）で「親はテスト
   を 1 件も collect しない」状態を作る
3. 親の `pytest_sessionfinish` で子 invocation を順次起動し、全 env 完了後に
   集約 exit code を `session.exitstatus` に設定
4. これにより親の `pytest_unconfigure` が正常に呼ばれ、親側 plugin の finalize は通る

子 invocation 側の `pytest_unconfigure` は各々の `pytest.main()` 内で正常に呼ばれる
ため、子側の cov / cacheprovider は影響なし。

**hookimpl の優先順位指定**: meta-parent の `pytest_sessionfinish` は **`tryfirst=True`**
で登録する必要がある:

```python
@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    if not getattr(session.config, "_qgis_puppeteer_meta_envs", None):
        return
    # 子 invocation を順次起動 → exitstatus を session に設定
    session.exitstatus = _run_envs_and_aggregate(session.config)
```

理由: `pytest_sessionfinish` は LIFO（後から登録した plugin が先）だが、`pytest-cov`
等の他 plugin は自分の finish 処理（coverage 書き出し）を `pytest_sessionfinish` で
行う。meta-parent が **先に**子 invocation を全部回し終えてから親の finalize 群が
動かないと、coverage 書き出しタイミングが狂う。`tryfirst=True` で他 plugin より
優先実行を保証する。

逆に **`trylast=True` 派の選択肢**もある（meta-parent の処理を最後にする）。どちらが
正しいかは pytest-cov / pytest-xdist の `pytest_sessionfinish` 実装に依存するため、
実装時に統合テストで検証する（Phase 2 の TDD ステップに含める）。

## Consequences

### Positive

- **CI 1 コマンド** で全 env を回せる（CI matrix 設定の代替）
- テスト → env の関係が **marker 1 行で宣言的**
- 既定で意味のない cartesian が起きない（QGIS の現実に整合）
- artifact が env 別に分離され、失敗解析が容易
- `@pytest.mark.fresh_qgis` と直交し、相補的に使える
- `qgis_command` / `qgis_bin` 等の既存設定キーをそのまま再利用（学習コスト低）

### Negative

- 実装規模 ~250〜400 行 + テスト 100 行
- pytest 内部 invocation を伴うため、pytest 他 plugin（特に xdist / cov）との相互作用に注意が必要
- ユーザーが env を意識する必要があり、シンプルな単一構成 E2E では overhead
- env と既存設定（`qgis_command` ini）の優先順位ルールを明文化する必要あり

### Neutral

- ADR-0002 §14 の Playwright モデルを廃案にする（cartesian を望むユーザーには `qgis_env` marker の複数列挙を案内）
- `environments.toml` を新規ファイルとして導入（または `pyproject.toml` インライン）

## Open Questions

1. **設定ファイルの形式**
   - 単独 `environments.toml` vs `pyproject.toml` 内 `[tool.qgis_puppeteer.environments]`
   - 推奨: 単独ファイル（多 env の見通しが良い）+ `pyproject.toml` 内記述もサポート（小規模時）

2. **`--env` の形式**
   - `--env=a,b` の comma 区切り vs `--env a --env b` の繰り返し
   - 推奨: 両方サポート（pytest 標準は繰り返し、Playwright は comma）

3. **デフォルト env の暗黙適用範囲**
   - marker 無しテストは `default_environment.default_name` で実行されるが、`--env=all` 時にデフォルト env のみ実行される（重複なし）か
   - 推奨: `--env=all` 時はデフォルト env が含まれる（marker 無しテストは default_env でのみ実行）

4. **Marker 名の衝突回避**
   - `qgis_env` は他 plugin との衝突リスクが完全には無い名前ではない
   - 候補: `qgis_env`（短い、現案）/ `qgis_puppeteer_env`（衝突回避優先、長い）
   - 推奨: `qgis_env` のまま開始し、衝突報告が来たら `qgis_puppeteer_env` を alias で追加

5. **`extends` による env 継承**（Phase 4 以降）
   - `[[environments]] name="qgis_lts_offline" extends="qgis_lts" ...` で差分定義
   - env が 5+ になると重複が辛くなるが、Phase 1 では YAGNI

### 解決済み（Decision に格上げ）

以下は当初 Open Question だったが、Critical issue を踏まえて Decision に組み込んだ:

- **`environments.toml` 不在時の挙動** → §"既定 env" で「既存 ini に透過動作」と明記
- **未指定 marker の既定値** → §"未指定 marker の扱い" で `strategy="named"` + `default_name` 必須に決定
- **xdist 並列との組み合わせ** → §"pytest-xdist との相互作用" で Phase 1 方針を明記
- **CLI / env / ini と environments.toml の優先順位** → §"env 設定 と 既存 ini / env / CLI の優先順位" で表として明記
- **マーカーの処理経路（独立 vs `-m` syntactic sugar）** → §"Marker による env 所属" で「独立経路。`-m` 経路とは直交、両者併用可」と明記

## Roadmap

実装順:

1. **Phase 1**（ベース、✅ 実装済）: `--env=<single_name>` のみサポート + `qgis_env`
   marker フィルタ + artifacts 分離 + 優先順位ルール（CLI/env/ini > env table）
2. **Phase 2**（✅ 実装済）: `--env=all` / `--env=a,b` の内部 invocation 実装
   （Approach (b)、`pytest_sessionfinish(tryfirst=True)`）+ exit code 集約
   （`aggregate_exit_codes`）+ `pytest_unconfigure` finalize 対応。
   `--maxfail` 累計は **Phase 3 へ繰り延べ**（実装時に JUnit / report-plugin
   どちらの方式で実装するか比較する）
3. **Phase 3**（✅ 実装済）: `summary.json` 集約レポート + JUnit env prefix +
   `--maxfail` 累計実装。child 側で `_env_stats.json`（schema_version=1、counts +
   failed_tests + duration_s）を `outputs/<env_name>/` に書き、parent 側で全
   `_env_stats.json` を読んで `outputs/summary.json` に集約。`--maxfail` は
   `inject_maxfail(argv, remaining)` で各 child に「残許容数」を渡し、累計が
   `--maxfail` に達したら break して残 env を `skipped_reason` 付きで記録。
   JUnit XML は child 終了時に `_postprocess_junit_for_env_prefix` が
   `<testcase classname="X.Y">` を `<env_name>::X.Y` に書き換え（idempotent、
   stdlib `xml.etree.ElementTree` で実装し追加依存なし）
4. **Phase 4**（一部 ✅ 実装済）: `--env-strict` フラグ ✅、`extends` 継承 ✅、
   `--list-envs` ✅、env 並列実行（**未実装**、xdist 実機検証と併せて Phase 5 相当
   に繰り延べ）。実装メモ:
   - `extends`: TOML の各 env が `extends = "<name>"` で base を指定可能。
     forward reference / self-reference / 不明な base は ValueError。継承規則は
     `qgis_args` のみ concat（base 先行）、`env` table は shallow merge（child 優先）、
     scalar フィールド（`qgis_bin` / `qgis_python` / `qgis_command`）は明示があれば
     上書き、無ければ継承
   - `aggregate_exit_codes(results, strict=True)`: `--env-strict` が立っていると
     「ある env だけ 5 (no tests collected)」を吸収せず 1 (fail) に格上げする
   - `_print_envs_and_exit(envs_config)`: `--list-envs` で TSV 形式
     （`name<TAB>description`）を stdout に出して `pytest.exit(0)`

各フェーズで非破壊的に追加できる設計（後方互換は常に保持）。

### Phase 1 実装メモ

- `_environments.py`: TOML loader、優先順位解決、marker フィルタ
- `plugin.py`: `pytest_configure` で marker 登録 + toml ロード、
  `pytest_collection_modifyitems` で env フィルタ、`qgis_process` で env spec
  の `qgis_command` / `qgis_bin` / `qgis_args` / `qgis_python` / `env` を反映、
  diagnostic dir を `outputs/<env_name>/diagnostics/` に切替
- `fresh_qgis` 合成ルールは設計のみ（marker 自体は ADR-0002 §10.4 で別途実装）

### Phase 2 実装メモ

- `_environments.py`:
  - `parse_env_arg(value) -> (mode, names)`: `"none" | "single" | "multi" | "all"` に正規化
  - `aggregate_exit_codes(results)`: ADR-0003 §exit code 表に従って 1 つの code に集約
  - `is_hard_exit_code(code)`: 2 / 3 / 4 を「hard error」として break 判定
  - `strip_env_args(argv)`: 子 invocation 用に親 argv から `--env=...` / `--env <val>` を除去
- `plugin.py`:
  - `pytest_configure`: 値を `parse_env_arg` で分類。`multi` / `all` を検出したら env 名を
    `_qgis_puppeteer_meta_envs` に保存し、active env は None にする
  - `pytest_collection_modifyitems`: meta-parent モードなら `pytest_deselected` で全 item を
    deselect し、items を空に
  - `pytest_sessionfinish(tryfirst=True)`: meta-parent なら env ごとに
    `_build_child_argv` で子 argv を組み立てて `pytest.main()` を順次起動。
    各子の exit code を `aggregate_exit_codes` で集約し `session.exitstatus` に設定。
    hard error 時は break。`QPUPPETEER_META_CHILD=1` env を子に渡して signal
- 既存 single-env 経路は `_qgis_puppeteer_meta_envs is None` の通常パスとして残る
- 集約 exit code が `5` を返すケース（全 env で no tests collected）も pytest 標準と整合

## References

- ADR-0002 §14（Playwright モデル、本 ADR で廃案）
- ADR-0002 §10.4 `@pytest.mark.fresh_qgis`
- ADR-0002 §13 失敗時診断バンドル
- ADR-0002 §21 サマリレポート
- Playwright Test Configuration: <https://playwright.dev/docs/test-configuration>
- Playwright Projects: <https://playwright.dev/docs/test-projects>
