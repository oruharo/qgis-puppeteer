# ADR-0004: Inline QGIS spawn helper — option-under-test シナリオ向け薄い API

- **Status**: Accepted
- **Date**: 2026-04-28
- **Deciders**: qgis-puppeteer maintainers
- **Related**:
  - ADR-0002 §10.4 `@pytest.mark.fresh_qgis` — テスト粒度の QGIS 再起動
  - ADR-0003 Test Environments — partition モデル（session 単位構成切替）

## Context

### 解決したい問題

QGIS の **起動 option 自体がテスト対象**になるケース。例：

- `--clean-canvas` を付けると state が本当にクリアされるか
- `--profile=offline` で読み込まれるプラグインセットが期待通りか
- `--code` で起動時 Python が確実に実行されるか
- ホスト独自 launcher の引数解釈が壊れていないか

これらは「機能テスト」ではなく「起動 option バリエーションのテスト」。
**起動 option がテストの主題**なので、option はテストコード上に明示的に書きたい。

### 既存手段の不足

| 手段 | 起動 option の置き場所 | 問題 |
|---|---|---|
| `qgis_process` fixture override | `conftest.py` | session 単位、test ごとに変えられない |
| `qgis_env` marker（ADR-0003） | `environments.toml` | option がテストの外、test 読んでも何の option を試してるか分からない |
| `fresh_qgis` marker（ADR-0002） | marker 引数 | テストファイルにはあるが、env の args に append される複雑な合成ルール |
| 完全手動（`subprocess.Popen`） | テストコード | 望む形だが boilerplate（Hub port 注入 / kill / register 待ち）が毎回必要 |

「**option はテストコードに書きたい、でも boilerplate は書きたくない**」という gap が
あり、これを埋めるのが本 ADR。

### Playwright / Testcontainers との対比

- Playwright: `browser.launch(args=[...])` を test 内で直接呼べる
- Testcontainers: `with PostgresContainer(...)` のように起動 option をインラインで渡す
- どちらも「起動構成 = テスト対象」のシナリオで標準的な API

QGIS にも同じ感覚で書ける薄い helper を提供する。

## Decision

### 1. `spawn_qgis()` context manager を提供する

```python
from pytest_qgis_puppeteer import spawn_qgis

def test_clean_canvas_clears_layers(hub_port, automation_client):
    with spawn_qgis(qgis_bin=QGIS_BIN, args=["--clean-canvas"], hub_port=hub_port):
        instance = automation_client.wait_for_worker(timeout_s=60)
        assert automation_client.execute_python(
            "len(QgsProject.instance().mapLayers())"
        ) == 0
```

- **薄さ**: subprocess.Popen + QPUPPETEER_* env 注入 + register 待機 + graceful kill だけ
- **戻り値**: Worker 識別情報（instance_id / pid）。複数 Worker 同時運用に備える
- **起動 option はインライン**: `args=` / `command=` で test 内に書ける

### 2. session 共有資源（Hub）は再利用する

- `hub_process` / `automation_client` は plugin の session-scoped fixture をそのまま使う
- helper は **Worker のみ** spawn する。Hub は session を通じて 1 つ
- これで helper の起動コストが軽くなる（Hub 起動 ~3-5 秒のオーバーヘッドを各 test で払わない）

### 3. `qgis_process` fixture とは独立して使える

- helper を使うテストは `qgis_process` fixture に依存しない
- 同じ session 内で `qgis_process` が起動した QGIS と helper で起動した QGIS が **共存**できる（multi-instance、ADR-0002 §5）
- `qgis_process` を抑止したいテストは fixture を要求しなければ良い（pytest の lazy resolution）

## Detailed Design

### API

```python
@contextmanager
def spawn_qgis(
    *,
    hub_port: int,
    qgis_bin: str | Path | None = None,
    args: Sequence[str] | None = None,
    command: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    register_timeout_s: float = 60.0,
    graceful_shutdown_timeout_s: float = 15.0,
    label: str | None = None,
) -> Iterator[SpawnedWorker]:
    """Test 内で直接 QGIS を起動・停止する context manager。

    起動オプションがテスト対象になるシナリオ向け。fixture override や
    environments.toml と違い、option を test コードに書ける。

    Args:
        hub_port: 既存 session の hub_port fixture を渡す
        qgis_bin: QGIS 実行ファイルパス。command 指定時は不要
        args: qgis_bin 用の追加引数
        command: 起動コマンド全体（host 独自 launcher 用）。qgis_bin/args と排他
        env: 追加環境変数。QPUPPETEER_HUB_PORT 等は内部で merge される
        register_timeout_s: Worker が Hub に register するまでの待機
        graceful_shutdown_timeout_s: terminate → kill のタイムアウト
        label: list_instances() で識別しやすくするラベル

    Yields:
        SpawnedWorker: instance_id / pid / label を持つ識別オブジェクト
    """
```

### `SpawnedWorker` 戻り値

```python
@dataclass(frozen=True)
class SpawnedWorker:
    instance_id: str    # Hub 上の identifier
    pid: int            # OS pid
    label: str | None
```

`automation_client.use_instance(worker.instance_id)` で target を切り替えられる。
複数 Worker を同時に動かす test ではこの戻り値で識別する。

#### `instance_id` の解決方法

`subprocess.Popen` で起動した QGIS は内部で `qgis_puppet` プラグインを load し、
Hub に WebSocket で register する（ADR-0001 §6）。helper は以下の手順で起動した
Worker の `instance_id` を pin する:

1. spawn 直前に `automation_client.list_instances()` を取って **既存 instance の集合**を記録
2. `subprocess.Popen` で QGIS を起動（pid を取得）
3. `register_timeout_s` の間 `list_instances()` をポーリング
4. **新規追加 instance** のうち pid が一致するものを `instance_id` として pin
5. 一致が見つからなければ `WorkerRegisterTimeout` を raise

pid 一致が必要なのは、xdist 並列・session 内 multi-instance で同時刻に複数 Worker が
register される race を分離するため。`automation_client.wait_for_worker(timeout_s=)`
は現在 `str` を返す（plugin.py L515）が、この helper では pid フィルタ付きの
内部メソッド `_wait_for_worker_by_pid(pid, timeout_s)` を新設して使う。

### Worker isolation（複数 Worker 同居）

`spawn_qgis` で起動した Worker と、`qgis_process` fixture で起動した Worker、
さらに別の `spawn_qgis` で起動した Worker が同じ Hub に register される可能性がある。

**target 切替ルール**:

- `automation_client.use_instance(instance_id)` を明示的に呼ぶ間、target はその Worker
- 切替なしで `automation_client.execute_python(...)` を呼ぶと、Hub の **default target**
  に行く（ADR-0001 の挙動）
- helper の戻り値 `worker.instance_id` を使って明示切替するのが推奨パターン

```python
def test_compare_clean_vs_dirty_canvas(hub_port, automation_client):
    with spawn_qgis(args=["--clean-canvas"], hub_port=hub_port) as clean:
        with spawn_qgis(args=[], hub_port=hub_port) as dirty:
            automation_client.use_instance(clean.instance_id)
            clean_count = automation_client.execute_python("len(...)")
            automation_client.use_instance(dirty.instance_id)
            dirty_count = automation_client.execute_python("len(...)")
            assert clean_count != dirty_count
```

### 環境変数の合成

helper が自動注入する env（fixture と同じ）:

- `QPUPPETEER_HUB_PORT`
- `QPUPPETEER_HUB_HOST`（127.0.0.1）
- `QPUPPETEER_TRUSTED_MODE=1`
- `QPUPPETEER_ALLOW_TEST_HANDLERS=1`

ユーザー指定 `env=` は **これらの上に追加 merge**（同名キーはユーザー優先、ただし
`QPUPPETEER_HUB_PORT` だけは helper が常に勝つ：別 port を渡すと register 出来ない）。

### 引数の排他性

`qgis_bin` / `args` と `command` は **排他**。両方指定された場合は **`ValueError`** を
raise する（プロパティとしてではなく、context manager `__enter__` 直前）。

```python
def spawn_qgis(*, qgis_bin=None, args=None, command=None, ...):
    if command is not None and (qgis_bin is not None or args is not None):
        raise ValueError(
            "spawn_qgis: 'command' is mutually exclusive with 'qgis_bin'/'args'"
        )
    if command is None and qgis_bin is None:
        raise ValueError("spawn_qgis: either 'command' or 'qgis_bin' must be specified")
```

これは plugin.py の `_resolve_qgis_command` / `_resolve_qgis_args` での排他規則と整合する。

### subprocess の stdout/stderr

QGIS の stdout/stderr を **PIPE で capture** する（fixture と異なる方針）:

- `subprocess.Popen(..., stdout=PIPE, stderr=PIPE)` で起動
- バックグラウンド drain thread を立てて pipe buffer 飽和を防ぐ（QGIS は通常 stderr に
  GDAL/PROJ ログを大量に吐くので buffer が埋まると hang する）
- capture 内容は `SpawnedWorker._captured_stdout` / `_captured_stderr` に蓄積
- `__exit__` 時に diagnostic bundle が有効なら `outputs/diagnostics/.../qgis_stdout.log` /
  `qgis_stderr.log` として書き出す（Phase 2）

理由: helper を使うテストは pytest の capture 設定（`-s` / `--capture=no`）と独立して
動くべき。継承だと `--capture=no` 時に QGIS のログが pytest の output に大量混入し、
test 結果の判読が悪化する。

### Teardown 失敗時の挙動

- `__exit__` で graceful terminate → timeout 後 force kill
- kill が失敗（プロセスがまだ生きている）した場合は `RuntimeError` を raise せず
  `logging.warning` に留める（次のテストの汚染を防ぐ目的なので、ログだけで先に進む）
- Worker が Hub から自動 deregister されない場合に備え、`__exit__` 内で
  `automation_client.list_instances()` を呼んで pid 一致の Worker が残っていれば
  追加で kill する best-effort 処理を入れる（**dangling worker last-resort**）

#### dangling worker last-resort（Phase 1 必須）

multi-instance test では Worker の register / deregister タイミングが重なるため、
`__exit__` の terminate / kill が完了しても Hub 側で instance が残ることがある。
Phase 1 の段階から以下の last-resort を実装する:

1. `__exit__` の最後に `automation_client.list_instances()` を呼ぶ
2. 戻り値の中に **自分の pid と一致する instance** があれば、それを target に
   `client.use_instance(...).execute_python("QgsApplication.exitQgis()")` で graceful
   shutdown を試みる（subprocess kill だけでは Hub から deregister されないケース）
3. それでも残るなら taskkill / SIGKILL を再発行
4. 最終的に残った場合は warning ログだけ出して進む

これは ADR-0002 §10.4（`fresh_qgis`）でも同じ問題が出るため、共通ヘルパとして
`_dispose_worker_best_effort(pid, instance_id)` を切り出して両者で使う。

### Windows での graceful kill（Phase 4 で改善）

既存の `_terminate_pid_tree`（plugin.py L281-307）は graceful terminate 後に
`time.sleep(graceful_timeout_s)` で固定待機している。これは helper を多用する test
では合計時間に効くため、Phase 4 のリファクタで `proc.wait(timeout=...)` ベースに
切り替える（既存 fixture の `qgis_process` も同時に切替）。Phase 1 では既存ロジックを
そのまま流用。

### `qgis_env` marker（ADR-0003）との合成

| 組み合わせ | 挙動 |
|---|---|
| `qgis_env` のみ | partition で env 構成の QGIS が `qgis_process` 経由で起動 |
| `spawn_qgis` のみ | テスト内で個別 spawn。`qgis_process` fixture を要求しなければ無視される |
| `qgis_env` + `spawn_qgis` 併用 | env で起動した QGIS と、`spawn_qgis` で起動した QGIS が共存。multi-instance test として動く |

併用は **禁止しない**。env で起動した baseline と option 違いの instance を比較する
テストが書ける（例：env=lts と spawn=current の両方で動作確認）。

### `fresh_qgis` marker（ADR-0002 §10.4）との関係

`fresh_qgis` は env 構成のまま再起動、`spawn_qgis` は完全別構成での起動。

- **テスト 1 件だけ env と同じ構成で再起動したい** → `fresh_qgis`
- **テスト 1 件だけ env と異なる option で起動したい** → `spawn_qgis`
- **option バリエーション数件をパラメタライズ** → `pytest.mark.parametrize` + `spawn_qgis`

```python
@pytest.mark.parametrize("profile", ["offline", "online", "minimal"])
def test_profile_loads_expected_plugins(hub_port, automation_client, profile):
    with spawn_qgis(args=[f"--profile={profile}"], hub_port=hub_port):
        plugins = automation_client.execute_python(
            "list(QgsApplication.pluginManager().pluginNames())"
        )
        assert _expected_plugins_for(profile).issubset(set(plugins))
```

### pytest-xdist 並列との関係

`hub_port` fixture は session-scoped で `_free_port()` を使うため、xdist worker ごとに
**独立した Hub port** が割り当てられる（plugin.py L329-336）。`spawn_qgis` は
`hub_port` を引数で受け取る設計なので：

- xdist worker A で起動した Worker は worker A の Hub にしか register されない
- xdist worker B の `spawn_qgis` が worker A の `instance_id` を見ることは無い
- `list_instances()` の pid フィルタも xdist worker 内で完結

つまり xdist 並列下でも `spawn_qgis` の動作は単一 worker 時と同じ。port 競合や
register race は発生しない。ユーザーは xdist の存在を意識せず使える。

### dev mode（`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`）との関係

dev mode は「既存 QGIS / Hub に接続して spawn を全 skip する」設計（plugin.py）。

- `spawn_qgis` を dev mode で呼んだら **`pytest.skip()`** する（warning と共に）
- 理由: dev mode の前提（既存 QGIS が register 済み）と spawn のセマンティクスが矛盾。
  silently no-op にすると test の assert が既存 Worker に流れて誤検知が起きる
- skip メッセージで「dev mode では `spawn_qgis` を含むテストは検証できない」と明示

### 実装場所

`pytest_qgis_puppeteer.spawn` モジュール（新規）:

```
packages/pytest-qgis-puppeteer/src/pytest_qgis_puppeteer/
├── plugin.py          # 既存。fixture 群
├── automation_client.py
├── locator.py
├── spawn.py           # ★ 新規。spawn_qgis context manager
└── __init__.py        # spawn_qgis を re-export
```

実装は `plugin.py` の `qgis_process` fixture 内ロジック（subprocess + env 注入 +
graceful kill）を抽出して helper 化したものになる。重複コードを避けるため、
`plugin.py` の `qgis_process` も内部で `spawn_qgis` を使うようリファクタすると綺麗。

## Consequences

### Positive

- **起動 option バリエーションテストの意図が明確**になる（option がテストコードに書ける）
- 既存の fixture / marker と直交し、相補的
- Hub は session で共有なので helper の起動コストが軽い
- multi-instance test の自然な書き方を提供
- `plugin.py` の spawn ロジックを helper として抽出することでコード重複削減

### Negative

- 新規 API 追加で学習コストが増える（fixture / marker / helper の 3 系統）
- `qgis_process` を要求しない test と要求する test が同居すると、テストファイル全体の
  fixture 依存が読みにくくなる可能性
- 複数 Worker 同居時の target 切替ミスは debug が辛い（明示的な `use_instance` 呼出を
  推奨ドキュメントで強調する）

### Neutral

- ADR-0003 とは独立。ADR-0003 の Phase に関係なく着手可能
- `fresh_qgis` 実装より先に出せる（こちらの方が単純）

### `label` の自動採番

未指定時は `worker-{pid}` の形式で自動採番する。`worker-{index}` のような連番にすると
xdist 並列・multi-instance test で複数 helper が同時に動いた時に衝突する可能性がある。
pid なら OS が unique を保証する。

```python
def spawn_qgis(*, label=None, ...):
    effective_label = label or f"worker-{proc.pid}"
```

ユーザーが明示的に意味のあるラベル（例: `"clean_canvas"` / `"offline_profile"`）を
指定すればそれを尊重する。

## Open Questions

1. **session-scoped fixture を helper にラップする糖衣**
   - `pytest.fixture(scope="module")` で `spawn_qgis` を呼ぶ書き方が頻出するなら
     `module_qgis(args=..., command=...)` のような fixture factory を別途提供するか
   - 推奨: Phase 2 以降で需要が見えたら追加。Phase 1 は context manager だけ

2. **timeout 失敗時の例外型**
   - `register_timeout_s` 超過時に何を raise するか
   - 推奨: `pytest_qgis_puppeteer.WorkerRegisterTimeout`（独自型）。pytest 標準の
     `pytest.fail` より test runner 統合が綺麗

3. **session 終了時の Hub 側 dangling 検出**
   - 個々の `__exit__` の last-resort（§Teardown 失敗時の挙動）に加えて、session
     teardown でも `list_instances()` を見て孤児 Worker が居れば warning + kill
   - 推奨: Phase 2 で plugin 側の session-scoped finalize に追加

### 解決済み（本 ADR で Decision 化）

- **`label` の自動採番** → §"`label` の自動採番" で `worker-{pid}` 形式に決定
- **subprocess の stdout/stderr 取り回し** → §"subprocess の stdout/stderr" で PIPE + drain thread に決定
- **dangling worker last-resort** → §"Teardown 失敗時の挙動" で Phase 1 必須に格上げ

## Roadmap

1. **Phase 1**（コア、✅ 実装済）: `spawn_qgis` context manager + `SpawnedWorker`
   戻り値 + `instance_id` の pid フィルタ解決 + 引数排他チェック（`ValueError`）+
   stdout/stderr PIPE + drain thread + dangling worker last-resort + dev mode skip +
   基本テスト
2. **Phase 2**（部分的に ✅ 実装済）: diagnostic bundle 統合（stdout/stderr 書き出し）
   + session teardown で全 dangling worker check
   - **2a 実装済**: `SpawnedWorker.captured_stdout()` / `captured_stderr()` を public 化、
     `qgis` fixture（fresh_qgis 経路）で active worker を `pytestconfig` 経由で
     register、`_dump_diagnostic_bundle` が test 失敗時に `spawn_stdout.log` /
     `spawn_stderr.log` を bundle に書き出す。makereport(call) は fixture teardown
     より先なので、worker が alive な状態で snapshot を取る（GIL 下で list iteration は
     atomic、`captured_*()` は copy を返す）。
   - **2b 未実装**: session teardown で全 dangling worker を check して残骸プロセスを
     回収する仕組み（別タスクに分離）。
   - **2c 未実装**: setup phase 失敗時（`WorkerRegisterTimeout` 等）の captured logs を
     bundle に入れる。worker reference が無いので `spawn_qgis` 内の例外オブジェクトに
     captured を attach する設計を別途検討。
3. **Phase 3**（✅ 実装済）: fixture factory 糖衣 `spawn_qgis_fixture(*, scope, name,
   yield_client, qgis_bin/args/command, env, register_timeout_s, ...)`。
   `conftest.py` で `module_qgis = spawn_qgis_fixture(scope="module", args=[...])` の
   ように代入して fixture 化。`yield_client=True` で `automation_client` を yield
   （fixture が自動で `use_instance` してくれる）/ `False` で `SpawnedWorker` を yield。
   `pytest_qgis_puppeteer.spawn_qgis_fixture` から import 可能（package root export）。
4. **Phase 4**（✅ 実装済）: `plugin.py` の `qgis_process` fixture を `spawn_qgis`
   context manager に委譲（spawn / register 待機 / drain thread / graceful kill /
   dangling worker last-resort をすべて helper 側に集約）+ `_terminate_pid_tree` を
   `_wait_pid_dead` の poll ベースに切替（固定 `time.sleep(graceful_timeout_s)` を
   排除し、graceful shutdown が早く完了したら待ち時間を切り上げる）。`_pid_alive`
   helper は posix で `os.kill(pid, 0)`、Windows で `OpenProcess` +
   `GetExitCodeProcess` (ctypes) を使う psutil 不使用実装。重複していた
   `_qgis_spawn_env` は削除（`spawn._build_env` に一本化）

各フェーズで非破壊的に追加できる設計。

### Phase 1 実装メモ

- `spawn.py`: `spawn_qgis` context manager + `SpawnedWorker` dataclass +
  `WorkerRegisterTimeout` 例外 + ヘルパ群（`_build_command` / `_build_env` /
  `_wait_for_worker_by_pid` / `_shutdown_worker_best_effort` / `_terminate_worker`）
- xdist 並列下の動作テストは Phase 1 では unit test 経路のみ。実 xdist invocation
  での integration test は Phase 4 リファクタと併せて追加予定
- `__init__.py` に `spawn_qgis` / `SpawnedWorker` / `WorkerRegisterTimeout` を export

## References

- ADR-0001 §5 multi-instance（複数 Worker 同居）
- ADR-0002 §10.4 `@pytest.mark.fresh_qgis`
- ADR-0003 Test Environments
- Playwright `browser.launch()`: <https://playwright.dev/docs/api/class-browsertype#browser-type-launch>
- Testcontainers: <https://testcontainers.com/>
