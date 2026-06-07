# qgis-puppeteer 利用ガイド

QGIS を Claude Desktop / pytest / Python スクリプトから WebSocket で
リモート操作するためのガイド。各ペルソナ向け quickstart と、
selector・Locator・dialog handler 等の機能リファレンスをまとめる。

---

## 目次

1. [はじめに](#はじめに)
2. [インストール](#インストール)
3. ペルソナ別 quickstart
   - [Claude Desktop ユーザー](#quickstart-claude-desktop)
   - [pytest E2E 作者](#quickstart-pytest-e2e)
     - 失敗時の診断バンドル / Test Environments / `fresh_qgis` / `spawn_qgis()`
   - [Python スクリプト作者](#quickstart-python-script)
4. [Selector リファレンス](#selector-リファレンス)
5. [Locator API](#locator-api)
6. [Dialog handler（想定外モーダルの自動応答）](#dialog-handler)
7. [Signal spy / wait_for_signal](#signal-spy--wait_for_signal)
8. [E2E カバレッジ計測](#e2e-カバレッジ計測)
9. [Uncaught Qt/Python 例外と test fail linkage](#uncaught-exceptions)
10. [複数 QGIS（multi-instance）](#multi-instance)
11. [Permissions / 信頼モード](#permissions)
12. [環境変数リファレンス](#環境変数リファレンス)
13. [Troubleshooting](#troubleshooting)
14. [Roadmap](#roadmap)

---

## はじめに

qgis-puppeteer は QGIS を **WebSocket 経由で外部から操作する** 基盤。

```
[Claude Desktop / pytest / your script]
                ↓ WebSocket
        [qgis-puppeteer Hub]   ← 単一ポート、ルーティング
                ↓ WebSocket (Worker dial out)
        [QGIS + qgis_puppet plugin]
```

3 つのパッケージで構成:

| パッケージ | 役割 | ライセンス |
|---|---|---|
| `qgis-puppeteer` | コア（Hub / Worker / AutomationClient / MCP gateway） | Apache-2.0 |
| `pytest-qgis-puppeteer` | pytest 用ヘルパ（Locator / fixture / 診断バンドル） | Apache-2.0 |
| `qgis_puppet` (QGIS plugin) | QGIS 内で動く Worker。プラグインとしてインストール | GPL-3.0-or-later |

ペルソナ別の入り口:

- **Claude に QGIS を触らせたい** → [Claude Desktop quickstart](#quickstart-claude-desktop)
- **QGIS プラグインの E2E テストを書きたい** → [pytest quickstart](#quickstart-pytest-e2e)
- **自動化スクリプトに組み込みたい** → [Python script quickstart](#quickstart-python-script)

---

## インストール

### 共通: QGIS plugin の配置

`qgis_puppet` プラグインを QGIS に組み込む（必須）:

1. `plugins/qgis_puppet/` 配下を QGIS のプラグインディレクトリにコピー
   （Windows 既定: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`）
2. QGIS を起動し、Plugin Manager で `QGIS Puppet` を有効化
3. QGIS の OSGeo4W Shell から:
   ```bat
   python-qgis-ltr.bat -m pip install qgis-puppeteer
   ```
   （`qgis_puppet` プラグインが import 時に `qgis_puppeteer` を要求するため）

### Claude Desktop ユーザー向け

Claude Desktop 起動側の Python に MCP gateway を入れる:

```bash
pip install qgis-puppeteer
```

### pytest E2E 作者向け

```bash
pip install pytest-qgis-puppeteer
# qgis-puppeteer は依存として自動的に入る
```

### Pre-release（dev ブランチ）から入れる場合

PyPI への公開前は GitHub から直接:

```bash
pip install "qgis-puppeteer @ git+https://github.com/oruharo/qgis-puppeteer.git@dev#subdirectory=packages/qgis-puppeteer"
pip install "pytest-qgis-puppeteer @ git+https://github.com/oruharo/qgis-puppeteer.git@dev#subdirectory=packages/pytest-qgis-puppeteer"
```

---

## Quickstart: Claude Desktop

Claude Desktop の MCP 機能を介して QGIS を制御する。

### 1. `claude_desktop_config.json` に MCP server を登録

Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "qgis-puppeteer": {
      "command": "python",
      "args": ["-m", "qgis_puppeteer.gateways.mcp"]
    }
  }
}
```

### 2. QGIS を起動

QGIS が起動すると `qgis_puppet` プラグインが自動的に Hub を spawn し、
Worker として登録される。Hub のポートは既定 `9876`（127.0.0.1 のみ）。

### 3. Claude Desktop を起動して試す

Claude に話しかける:

> 「今開いているレイヤを一覧して」

Claude 側で以下のような MCP ツール呼び出しが行われる:

- `mcp__qgis-puppeteer__qgis_list_layers`
- `mcp__qgis-puppeteer__qgis_select_features`
- `mcp__qgis-puppeteer__qgis_screenshot`
- `mcp__qgis-puppeteer__qgis_use_instance`（multi-instance 時）
- ... 他

### よくある初期トラブル

| 症状 | 原因 | 対処 |
|---|---|---|
| Claude から QGIS が見えない | QGIS が起動していない、Hub が未起動 | QGIS を起動してから Claude Desktop を再起動 |
| `instance_not_found` | Worker register 完了前に呼んだ | 数秒待って再試行 |
| 確認ダイアログで止まる | `execute_python` 系の confirm UI | [信頼モード](#permissions) を参照 |

詳しくは [Troubleshooting](#troubleshooting) も。

---

## Quickstart: pytest E2E

QGIS の E2E テスト（モーダル操作含む）を書く。

### 1. プロジェクトに依存追加

`pyproject.toml`:

```toml
[project.optional-dependencies]
e2e = [
  "pytest-qgis-puppeteer>=0.1.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
qgis_bin = "C:/Program Files/QGIS 3.34/bin/qgis-bin.exe"
qgis_args = ["--profile=test"]
qgis_startup_timeout = 60
# qgis_python は未指定なら OSGEO4W_ROOT から自動検出される
```

### 2. ホストアプリ独自の launcher を使いたい場合

例: ホスト固有の env / DB 接続情報を立ててから QGIS を起動する `.bat` を経由する場合。

```toml
[tool.pytest.ini_options]
qgis_command = ["scripts/launcher.bat", "--config", "e2e", "--feature", "auth"]
```

`qgis_command` は `qgis_bin` / `qgis_args` と排他。`QPUPPETEER_HUB_PORT` 等の
env は引き続き Worker に継承されるので、launcher 側でそれを QGIS に渡せばよい。

### 3. 最小テスト

```python
import pytest

@pytest.mark.asyncio
async def test_open_login_dialog(qgis):
    """qgis fixture は E2EAutomationClient のラッパ。pytest-qgis-puppeteer が自動提供。"""

    # Locator で対象を捕まえる
    username = qgis.locator({"object_name": "username", "scope": "modal"})
    password = qgis.locator({"object_name": "password", "scope": "modal"})
    ok_btn = qgis.locator({"object_name": "login_btn", "scope": "modal"})

    # auto-wait 付き操作（visible / enabled / not_covered になるまで待つ）
    username.fill("alice")
    password.fill("secret")
    ok_btn.click()

    # モーダルが閉じるまで待つ
    qgis.wait_for_modal_closed(timeout_s=5.0)

    # snapshot から状態を確認
    snap = qgis.snapshot_ui(include_main_window=True)
    assert snap.get("active_modal") is None
```

### 4. 失敗時の診断バンドル

テストが失敗すると `outputs/diagnostics/<test_name>_<timestamp>/`
（`environments.toml` 利用時は `outputs/<env_name>/diagnostics/...`）に
以下が自動保存される（`pytest_runtest_makereport` フック）:

| ファイル | 内容 |
|---|---|
| `screenshot.png` | QGIS のスクショ |
| `snapshot_ui.json` | UI ツリー（`include_main_window=True`、深さ 12） |
| `list_instances.json` | Hub に register されている Worker 一覧 |
| `meta.json` | `nodeid` / 解決済 `qgis_bin` / `qgis_args` / `qgis_command` / 例外 type+message / env name / instances サマリ |
| `traceback.txt` | pytest の `longrepr` テキスト |
| `uncaught_exceptions.json` | テスト実行中に Qt/Python から uncaught でキャッチされた例外（[Uncaught 例外](#uncaught-exceptions) 参照） |
| `hub_stdout.log` / `hub_stderr.log` | Hub プロセスの直近最大 500 行 |

`--env=all` / `--env=A,B` で meta-parent モードで動かした場合、parent 側で
`outputs/summary.json` も追加で書かれる（per-env stats + totals + 集約 exit code）。

CI 上で diagnostic バンドルだけ artifact として upload しておくと、
失敗時に full transcript を読まずに原因の当たりが付けやすくなる。

### 5. Test Environments（複数 QGIS 構成の宣言的切替）

QGIS の起動構成（プロファイル / 認証有無 / バージョン / launcher）が
テストごとに異なる場合、`environments.toml` で宣言し、`@pytest.mark.qgis_env`
でテストを env に partition できる（ADR-0003）。

`environments.toml`:

```toml
[[environments]]
name = "smoke"
qgis_args = ["--profile=test"]
description = "最小プロファイルのスモーク"

[[environments]]
name = "with_auth"
qgis_args = ["--profile=auth_test"]
env = { MYAPP_AUTH_ENABLED = "1" }

[[environments]]
name = "qgis_lts"
qgis_bin = "C:/Program Files/QGIS 3.34/bin/qgis-bin.exe"
qgis_args = ["--profile=regression"]

[default_environment]
strategy = "named"      # "fail" にすると marker 必須（推奨は段階移行）
default_name = "smoke"
```

#### env の継承（`extends`）

共通設定を base env にまとめ、差分だけ子で書く:

```toml
[[environments]]
name = "base"
qgis_args = ["--profile=test"]
env = { MYAPP_LOG_LEVEL = "DEBUG" }

[[environments]]
name = "with_auth"
extends = "base"                       # base の qgis_bin/qgis_python/qgis_command を継承
qgis_args = ["--feature=auth"]         # base の args の **後ろ** に append される
env = { MYAPP_AUTH_ENABLED = "1" }     # base の env と shallow merge（同キーは子優先）
```

ルール:

- `extends` は **既出の env name** だけ参照可（前方参照禁止 / 自己参照禁止）。違反は TOML load 時に `ValueError`。
- `qgis_bin` / `qgis_python` / `qgis_command` は scalar 継承（子で上書き可）。
- `qgis_args` は **append**（base の後ろに子）。
- `env` table は **shallow merge**（同 key は子優先）。
- 多段（`a → b → c`）は宣言順に解決されるので OK。

テスト側で env に所属を宣言:

```python
import pytest

# ファイル単位
pytestmark = pytest.mark.qgis_env("with_auth")

# 個別テスト
@pytest.mark.qgis_env("with_auth")
def test_login_flow(qgis): ...

@pytest.mark.qgis_env("qgis_lts")
def test_lts_only_regression(qgis): ...

# opt-in で複数 env に所属（少数の cross-env テスト向け）
@pytest.mark.qgis_env("smoke", "qgis_lts")
def test_basic_open_project(qgis): ...
```

CLI で env を選択:

```bash
pytest --env=smoke                # smoke env のテストだけ
pytest --env=with_auth -k login   # 標準 -k と併用可
pytest --env=smoke,with_auth      # 2 つの env を順次実行
pytest --env=all                  # 全 env を順次実行（meta-parent モード）
pytest --list-envs                # 利用可能な env を `name<TAB>description` 形式で出力して exit
pytest --env=all --env-strict     # ある env だけ exit 5 (no tests collected) を 1 に昇格
```

- `--list-envs` は `--env` 指定有無に関わらず使える（`pytest_configure` 早期で hook して `pytest.exit(0)`）。
- `--env-strict` は meta-parent モード専用。1 env でも「テストが拾えなかった」を fail 扱いにしたい CI 用途。**全 env が 5** のときは引き続き 5 のまま（CI が green と誤検知しないためのガード）。hard error (2/3/4) は strict 関係なく即時伝播。

`--env=all` / `--env=A,B` は meta-parent モード。親 invocation が env ごとに
子 `pytest.main()` を順次起動し、各 env の exit code を集約する:

| 状況 | 親の最終 exit code |
|---|---|
| 全 env が 0 | 0 |
| いずれかが 1（fail） | 1 |
| いずれかが 2 / 3 / 4（hard error） | 即時伝播、後続 env は走らない |
| ある env だけ 5（no tests collected） | 吸収して 0 |
| **全 env** が 5 | 5（CI green 誤検知防止） |

artifacts は `outputs/<env_name>/diagnostics/` に env 別に分離される。
詳細・優先順位（`CLI > env > toml > ini`）は ADR-0003 を参照。

#### JUnit XML の env prefix

`--junit-xml=...` 併用時、各子 invocation の `<testcase classname="...">` は
`<env_name>::<元 classname>` に書き換わる。CI 側で env を縦軸にした集計表が
そのまま作れる。stdlib の `xml.etree.ElementTree` で後処理しているので追加 dep 不要、
かつ idempotent（既に prefix されているものは触らない）。

#### `--maxfail` の env 横断累積

`--env=all --maxfail=N` のとき、parent 側で各 env の fail 数を `_env_stats.json`
経由で読み、累積 fail が N に達した時点で残り env を skip する。次の child を
spawn する直前に `--maxfail=<remaining>` を inject するので、child 単体での
fail-fast 挙動も活きる。skip された env は `summary.json` 内で
`skipped_reason="not_run_due_to_cumulative_maxfail"` として記録される。

### 6. テスト中だけ QGIS を再起動する: `@pytest.mark.fresh_qgis`

「このテストだけ別構成で QGIS を立ち上げ直したい」「session worker を汚さず
canvas クリアな状態で 1 件動かしたい」ケース向け（ADR-0002 §10.4 / ADR-0003 §fresh_qgis）。

```python
import pytest

@pytest.mark.fresh_qgis(args=["--clean-canvas"], env={"MYAPP_LOG_LEVEL": "DEBUG"})
def test_pristine_state(qgis):
    """この test だけ canvas クリアで再起動。fixture teardown で kill される。"""
    ...

@pytest.mark.qgis_env("with_auth")
@pytest.mark.fresh_qgis(args=["--reset-cache"])
def test_auth_with_reset_cache(qgis):
    """env=with_auth の構成 + 追加 args で fresh worker。"""
    ...
```

合成ルール（ADR-0003）:

| キー | 振る舞い |
|---|---|
| `args` | env の `qgis_args` に **append** |
| `env` | env の `env` table に **shallow merge**（marker 側優先） |
| `qgis_bin` / `qgis_python` | env / 既存設定から解決（marker 上書き不可） |
| `qgis_command` 指定 env | marker `args` は command 末尾に append（host launcher が QGIS へ転送する責務） |

routing は `qgis` fixture が自動でハンドル: spawn 後の Worker `instance_id` を
`automation_client.use_instance(...)` でセットし、test 終了時に元の default に戻す。

dev モード（`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`）下では `fresh_qgis` テストは
skip される（既存 QGIS を再起動できないため）。

### 7. 任意のタイミングで QGIS を立てる: `spawn_qgis()`

multi-instance テストや「テスト本体内で QGIS を 2 つ立てて比較したい」場合は
`spawn_qgis()` context manager（ADR-0004）を直接使う:

```python
from pytest_qgis_puppeteer.spawn import spawn_qgis

def test_two_workers_compare(automation_client, hub_port, qgis_bin):
    with spawn_qgis(
        hub_port=hub_port,
        automation_client=automation_client,
        qgis_bin=qgis_bin,
        args=["--profile=A"],
        label="worker-A",
    ) as worker_a, spawn_qgis(
        hub_port=hub_port,
        automation_client=automation_client,
        qgis_bin=qgis_bin,
        args=["--profile=B"],
        label="worker-B",
    ) as worker_b:
        # per-call の instance routing
        snap_a = automation_client.snapshot_ui(instance=worker_a.instance_id)
        snap_b = automation_client.snapshot_ui(instance=worker_b.instance_id)
        assert _equivalent(snap_a, snap_b)
```

`spawn_qgis()` は context exit で graceful → force kill する。
register タイムアウト（既定 60s）は `register_timeout_s=` で上書き可能。

### 8. dev モード（既存 QGIS に相乗り）

新しい QGIS を spawn せず、既に開いている QGIS でテストを走らせるモード。
書き始め・UI 操作系のデバッグ用途。

```bash
# 環境変数を立ててから pytest 実行
export QPUPPETEER_E2E_USE_RUNNING_QGIS=1
pytest test_e2e/
```

注意: `execute_python` を多用するテストは confirm UI で固まるので、
dev モードでは UI 操作系のみ走らせるのが安全（または対象 QGIS で
`QPUPPETEER_TRUSTED_MODE=1` を立てておく）。

### 9. 完全制御が必要な場合: fixture オーバーライド

`qgis_command` だけでは足りない（テスト session 内で複数 launcher を使い分け
たい等）場合は、ユーザー側 `conftest.py` で `qgis_process` fixture を override:

```python
import os, subprocess
import pytest

@pytest.fixture(scope="session")
def qgis_process(automation_client, hub_port):
    env = os.environ.copy()
    env["QPUPPETEER_HUB_PORT"] = str(hub_port)
    env["QPUPPETEER_HUB_HOST"] = "127.0.0.1"
    env["QPUPPETEER_TRUSTED_MODE"] = "1"
    env["QPUPPETEER_ALLOW_TEST_HANDLERS"] = "1"
    # ホスト固有 env を立てる
    env["MYAPP_CONFIG"] = "e2e"

    proc = subprocess.Popen(["scripts/launcher.bat", "--name", "e2e"], env=env)
    try:
        yield None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
```

`hub_process` / `automation_client` / `qgis` fixture はそのまま使える。

---

## Quickstart: Python script

Claude や pytest を介さず、自動化スクリプトから直接 QGIS を操作する。
`AutomationClient` は **async API**。

### 1. QGIS を起動

`qgis_puppet` プラグインを有効化した QGIS を起動しておく。Hub は自動 spawn。

### 2. スクリプト

```python
import asyncio
from qgis_puppeteer import AutomationClient

async def main():
    async with AutomationClient(url="ws://127.0.0.1:9876") as client:
        # 利用可能な instance（QGIS プロセス）を一覧
        instances = await client.list_instances()
        print(f"Found {len(instances)} QGIS instance(s)")

        # コマンド呼び出し
        layers = await client.call("qgis_list_layers")
        for layer in layers["layers"]:
            print(f"  - {layer['name']}")

        # 任意の Python コードを QGIS 内で実行（要 confirm or 信頼モード）
        result = await client.call(
            "qgis_execute_python",
            {"code": "print('hello from QGIS', QgsProject.instance().fileName())"},
        )

        # スクショを撮る
        await client.call(
            "qgis_screenshot",
            {"output_path": "C:/tmp/screenshot.png"},
        )

asyncio.run(main())
```

### 3. instance を切り替える

複数 QGIS が登録されている場合:

```python
# label or instance_id で指定
await client.call("qgis_use_instance", {"label": "QGIS-A"})
# 以降の call は QGIS-A を対象に
layers_a = await client.call("qgis_list_layers")
```

詳しくは [Multi-instance](#multi-instance) を参照。

---

## Selector リファレンス

`qgis_click_widget` / `qgis_set_widget_value` / Locator が受け取る
selector dict の全キー。複数キー指定は AND セマンティクス。

### マッチキー

| キー | 型 | 用途 |
|---|---|---|
| `object_name` | str | Qt `objectName()` 完全一致。**最も推奨**（一意性が高い） |
| `text` | str | ボタン / ラベル / QAction の `text()` 完全一致 |
| `class` | str | Python クラス名完全一致（`"QPushButton"` 等） |
| `title` | str | `windowTitle()` 完全一致（dialog 選択用） |
| `label` | str | `QLabel.buddy()` 経由で対応するラベル文字列。Playwright の `getByLabel` 相当 |
| `placeholder` | str | LineEdit/TextEdit の `placeholderText()` 完全一致 |

### スコープ・絞り込みキー

| キー | 型 | 用途 |
|---|---|---|
| `scope` | str | `"modal"`（既定） / `"active_window"` / `"any"` |
| `root_object_name` | str | トップレベル widget を `objectName()` で絞る（例: `"QgisApp"`） |
| `index` | int | 候補が複数ある場合に何番目を採用するか |

### Strict mode

`index` を指定**せず**に複数マッチした場合、`selector_ambiguous` エラーで
fail する（旧挙動の silent な先頭採用は廃止）。

**良くないパターン** (selector_ambiguous):

```python
# 同じ画面に QPushButton が複数ある → ambiguous
qgis.locator({"class": "QPushButton"}).click()
```

**良いパターン**:

```python
# 推奨: object_name で一意化
qgis.locator({"object_name": "ok_btn"}).click()

# 次善: scope で絞る
qgis.locator({"class": "QPushButton", "scope": "modal", "text": "OK"}).click()

# どうしても N 番目を取りたい場合: 明示的に index
qgis.locator({"class": "QPushButton", "index": 0}).click()
```

エラー時の diagnostics には match_count + 上位 10 件の候補（class /
object_name / text）が含まれるので、ログから即原因が分かる。

### `qgis_check_actionability`（low-level RPC）

Locator の auto-wait は内部でこの RPC を poll している。selector が当該
widget をどう判定したか単発で確認したい場合に直接呼べる:

```python
result = qgis.call("qgis_check_actionability", {
    "selector": {"object_name": "ok_btn"},
})
# {exists, visible, enabled, not_covered, editable, ...}
```

`expect()` の `to_be_visible()` / `to_be_enabled()` も同 RPC を再利用しており、
selector ambiguous は即時 abort される。

### `qgis_click_widget` / `qgis_set_widget_value` のエラーコード

これらの低レベル RPC は失敗時に **例外を投げず** `{"success": false, "error": "<code>"}`
を返す（呼び出し側で分岐する想定）。Locator (`click()` / `fill()` 等) を使う場合は
auto-wait で吸収されるか、対応する例外に変換される。

| `error` コード | 発生 RPC | 意味 | Locator での扱い |
|---|---|---|---|
| `widget_not_found` | 両方 | scope 内に該当 widget が無い | `_wait_actionable` が `exists=False` と見て poll を継続、timeout で `WidgetNotActionableError` |
| `selector_ambiguous` | 両方 | 複数マッチ + `index` 未指定（strict mode） | poll を続けず即 `SelectorAmbiguousError` |
| `widget_disabled` | 両方 | `widget.isEnabled() == False` | poll で待機。timeout 内に enabled になれば成功 |
| `widget_readonly` | set のみ | `isReadOnly() == True` の input への書き込み | `fill()` は `editable=True` を auto-wait |
| `widget_not_clickable` | click のみ | `QAbstractButton` / `QAction` / `click()` 持ちのいずれでもない | 例外なくそのまま返る |
| `combo_item_not_found` | set のみ | `QComboBox` に該当テキストが無い | 例外なくそのまま返る |
| `tab_not_found` | set のみ | `QTabWidget` に該当タブが無い | 例外なくそのまま返る |
| `list_item_not_found` | set のみ | `QListWidget` に該当アイテムが無い | 例外なくそのまま返る |
| `unsupported_widget_type` | set のみ | 値設定に未対応の widget クラス | 例外なくそのまま返る |
| `set_value_failed` | set のみ | setter 内部で例外（`message` フィールドに詳細） | 例外なくそのまま返る |
| `row_index_required` | set のみ | View 系に int 以外の値を渡した | 例外なくそのまま返る |
| `no_model` | set のみ | View に model が未設定 | 例外なくそのまま返る |

**重要**: `qgis_click_widget` を直接呼ぶとき disabled な widget は **即座に
`widget_disabled` で返り、`click()` は呼ばれない**。リトライしないので、
非同期で enabled に変わるのを待ちたい場合は Locator (`click()`) を使う。

### scope の挙動

- **`modal`**（既定）: `activeModalWidget()` 配下を検索。なければ
  `activeWindow()` にフォールバック（曖昧指定に寛容）
- **`active_window`**: `activeWindow()` 配下のみ
- **`any`**: 全 top-level widget。重い & strict mode と相性悪いので注意

### selector チートシート

```python
# OK ボタン（一意な object_name）
{"object_name": "ok_btn"}

# QToolBar 上の特定 QAction（main window scope）
{"class": "QAction", "text": "保存", "root_object_name": "QgisApp"}

# モーダル内の input（label 経由）
{"label": "Username", "scope": "modal"}

# Placeholder で input 特定
{"placeholder": "user@example.com"}

# 複数同名がある状況で 2 番目を選ぶ
{"object_name": "row_btn", "index": 1}
```

---

## Locator API

`pytest-qgis-puppeteer` が提供する Playwright 風の Locator。
`qgis.locator(selector)` で生成。state を持たないので使い回し OK。

### 操作系メソッド（auto-wait 内蔵）

| メソッド | 用途 | 待機条件 |
|---|---|---|
| `click()` | クリック | actionable |
| `fill(value)` | LineEdit / TextEdit / SpinBox 等への書き込み | actionable + **editable** |
| `select(value)` | ComboBox / Tab / List の選択 | actionable |
| `check()` | CheckBox / RadioButton を True に | actionable |
| `uncheck()` | 同 False に | actionable |

#### actionable とは（auto-wait チェック項目）

操作実行前に以下が満たされるまで poll（既定 timeout=5s、間隔 250ms）:

- **exists**: selector が widget を引き当てる
- **visible**: `widget.isVisible()` == True
- **enabled**: `widget.isEnabled()` == True
- **not_covered**: モーダルに被われていない

`fill()` のみ追加で `editable=True`（`isReadOnly()` == False）を要求。
読み取り専用フィールドが解除されるまで待つ用途。

timeout を超えると `WidgetNotActionableError`。strict mode で複数マッチ
した場合は poll を続けず即座に `SelectorAmbiguousError` を上げる。

### 即時状態チェック（wait なし）

| メソッド | 戻り値 |
|---|---|
| `exists()` | bool |
| `is_visible()` | bool |
| `is_enabled()` | bool |

### 値取得（snapshot ベース）

| メソッド | 戻り値 |
|---|---|
| `get_text()` | str or None — 主に QPushButton / QLabel / QLineEdit |
| `get_value()` | Any — widget 種別ごとの「現在値」を推定 |
| `snapshot()` | dict or None — selector に最初にマッチする node |

`get_value()` の優先順:

1. `value`（QSpinBox / QDoubleSpinBox）
2. `current_text`（QComboBox）
3. `checked`（QCheckBox / QRadioButton / 押下式）
4. `current_index`（QTabWidget）
5. `text`（その他）

### 例

```python
def test_login(qgis):
    # 入力
    qgis.locator({"object_name": "username"}).fill("alice")
    qgis.locator({"object_name": "password"}).fill("secret")

    # チェックボックスのトグル
    qgis.locator({"object_name": "remember_me"}).check()

    # クリック
    qgis.locator({"object_name": "login_btn"}).click()

    # 状態確認
    status = qgis.locator({"object_name": "status_label"})
    assert status.get_text() == "Logged in"
```

### Locator chain (`parent.locator(child)`)

Playwright 流の階層 selector。複雑な dialog で objectName を全部振らなくても、
親 widget 内に閉じた検索ができる。

```python
form = qgis.locator({"object_name": "login_form"})
# form 内の username input を捕まえる（main window 側の同名 widget は無視）
form.locator({"object_name": "username"}).fill("alice")
form.locator({"object_name": "password"}).fill("secret")
form.locator({"text": "Submit"}).click()
```

何段でもネスト可能:

```python
wizard = qgis.locator({"object_name": "wizard"})
step1 = wizard.locator({"object_name": "step1"})
step1.locator({"object_name": "name"}).fill("alice")
```

内部では Locator が `_scope_chain` キー付き selector を組み立てて Worker に
送り、Worker 側で「親を解決 → 子を subtree 内で検索」する。Locator 自体は
state を持たないので、同じ親から複数の子を作っても親は無傷:

```python
form = qgis.locator({"object_name": "form"})
field_a = form.locator({"object_name": "a"})  # form の中の a
field_b = form.locator({"object_name": "b"})  # form の中の b
# form 自身を click することも引き続き可能
```

### Web-first assertions（`expect`）

Playwright 流の retry 内蔵 assertion。`assert locator.is_visible()` は 1-shot で
flaky だが、`expect(locator).to_be_visible()` は内部で poll して条件を満たすまで
待つ（既定 5s × 250ms 間隔）。

```python
from pytest_qgis_puppeteer import expect

# 状態系
expect(qgis.locator({"object_name": "spinner"})).not_.to_be_visible()
expect(qgis.locator({"object_name": "submit"})).to_be_enabled()

# 値系
expect(qgis.locator({"object_name": "status"})).to_have_text("Logged in")
expect(qgis.locator({"object_name": "count"})).to_contain_text("件")
expect(qgis.locator({"object_name": "amount"})).to_have_value(123)
expect(qgis.locator({"object_name": "remember_me"})).to_be_checked()

# 個別 timeout
expect(loc).to_have_text("Done", timeout_s=10.0)
```

提供するマッチャ:

| メソッド | 用途 |
|---|---|
| `to_be_visible()` / `to_be_hidden()` | 表示状態 |
| `to_be_enabled()` / `to_be_disabled()` | 操作可能状態 |
| `to_have_text(s)` | `get_text()` 完全一致 |
| `to_contain_text(s)` | `get_text()` 部分一致 |
| `to_have_value(v)` | `get_value()` 一致 |
| `to_be_checked()` | `get_value()` が True |

`not_` プロパティで否定形（「~でなくなるまで待つ」）。selector 多重マッチ時は
`SelectorAmbiguousError` で即 abort（`Locator` の auto-wait と同じ挙動）。

---

## Dialog handler

「想定外モーダル（Bad Layers / 認証失敗 / 古いプロジェクト警告 等）」を
登録した条件で自動 dismiss する仕組み。Worker 側で 250ms 周期で
`activeModalWidget()` を polling し、predicate にマッチすれば action を実行。

### 提供 MCP ツール / RPC コマンド

| コマンド | 用途 |
|---|---|
| `qgis_register_dialog_handler` | 登録 |
| `qgis_unregister_dialog_handler` | 削除 |
| `qgis_list_dialog_handlers` | 一覧 |
| `qgis_clear_dialog_handlers` | 全削除（teardown 用） |

### 登録パラメータ

```json
{
  "name": "dismiss-bad-layers",
  "predicate": {"title": "利用できないレイヤを処理する"},
  "action": "accept",
  "once": false
}
```

- `name`: 識別名（既存 name は上書き）
- `predicate`: selector dict（`title` / `class` / `object_name` を主に使う）。
  空 `{}` は全 modal にマッチ（catch-all）
- `action`: `"accept"` / `"reject"` / `"close"` のいずれか
- `once` (optional): True で 1 度発火したら自動 unregister

### pytest からの利用例

```python
@pytest.fixture(scope="session", autouse=True)
def auto_dismiss_bad_layers(automation_client):
    automation_client.call("qgis_register_dialog_handler", {
        "name": "bad-layers",
        "predicate": {"title": "利用できないレイヤを処理する"},
        "action": "accept",
    })
    yield
    automation_client.call("qgis_clear_dialog_handlers", {})
```

### Claude Desktop からの利用例

ユーザー: 「Bad Layers が出るたび閉じて」
→ Claude が `qgis_register_dialog_handler` を呼ぶ
→ 以降は出るたび自動で `accept`

### Python script からの利用例

```python
await client.call("qgis_register_dialog_handler", {
    "name": "auto-close-msgbox",
    "predicate": {"class": "QMessageBox"},
    "action": "close",
    "once": True,  # 1 度だけ
})
```

### 注意点

- 同じ modal instance を 2 回処理しない（id ベースの状態管理）
- 複数 handler が match する場合は **登録順で先勝ち**
- modal が閉じたら状態リセット → 同じ条件の modal が再度開けば再度処理

---

## Signal spy / wait_for_signal

Qt signal の発火検証用ハンドラ。`test.*` namespace に置かれており、QGIS 側で
`QPUPPETEER_ALLOW_TEST_HANDLERS=1` が立っているときだけ登録される（pytest fixture
は subprocess に自動付与）。本番ユーザー環境では露出しないので、テストコードからのみ呼ぶ。

### 提供 RPC コマンド

| コマンド | 用途 |
|---|---|
| `test.signal_spy_start` | spy を開始（`{spy_id, matched, diagnostics}` を返す） |
| `test.signal_spy_get_emissions` | 発火履歴を取り出す（`since_index` で差分取得可） |
| `test.signal_spy_count` | 発火回数のみ |
| `test.signal_spy_stop` | spy 停止（`{ok}`） |
| `test.wait_for_signal` | local `QEventLoop` で blocking wait（`{fired, args, timed_out}`） |

### Spy で「複数回発火」を検証

```python
def test_combo_emits_currentIndexChanged(qgis):
    spy = qgis.call("test.signal_spy_start", {
        "selector": {"object_name": "myCombo"},
        "signal": "currentIndexChanged",
        "max_emissions": 100,        # 上限（任意）
    })
    spy_id = spy["spy_id"]

    qgis.locator({"object_name": "myCombo"}).select("Option B")
    qgis.locator({"object_name": "myCombo"}).select("Option C")

    result = qgis.call("test.signal_spy_get_emissions", {"spy_id": spy_id})
    assert result["count"] == 2
    # emissions: [{args: [...], ts: ...}, ...]

    qgis.call("test.signal_spy_stop", {"spy_id": spy_id})
```

### `wait_for_signal` で「特定の発火を待つ」

非同期処理完了の通知を待つケース。`timeout_ms` 既定 5000:

```python
def test_async_load(qgis):
    qgis.locator({"object_name": "load_btn"}).click()
    result = qgis.call("test.wait_for_signal", {
        "selector": {"object_name": "loader"},
        "signal": "loadFinished",
        "timeout_ms": 10000,
    })
    assert result["fired"] is True
    assert result["timed_out"] is False
```

selector が widget を引き当てられない場合、`{"fired": False, "matched": False,
"diagnostics": {...}}` が返る（例外にはしない）。signal 名が widget に存在しない
場合のみ `ValueError`。

---

## E2E カバレッジ計測

E2E テストは **2 プロセス**で動く:

- **pytest ランナー側**（テストコード＋ client ライブラリ）
- **QGIS 側**（実際の被テストコード = プラグイン/アプリ本体。WebSocket 越しに駆動される）

`pytest --cov=...` を付けても測れるのは**ランナー側だけ**で、本命の QGIS 内コードは
1 行も入らない。ここが E2E カバレッジの肝。

### (A) ランナー側

`pytest --cov=<runner側pkg>` で普通に取れる。env を分けて回す場合の combine 方針は
[ADR-0003](architecture/0003-test-environments.md) を参照（env ごとに
`.coverage.<env>` → 最終段で `coverage combine`、`COVERAGE_FILE` を切り替え）。

### (B) QGIS 側（本命）

`coverage.py` の **subprocess 計測**を使う。qgis-puppeteer は QGIS を spawn する際に
**親プロセスの `os.environ` をそのまま継承**する（`spawn.py` の
`_build_env(base=os.environ, …)`。fixture 経路も `spawn_qgis` 経由で同じ）。よって
**環境変数を立てるだけで QGIS 内 coverage を有効化**でき、本体コードの変更は不要。

手順（コピペ用テンプレ: [`examples/coverage/`](../examples/coverage/)）:

1. **QGIS の Python に coverage を入れる**（ランナーとは別インタプリタ）:
   `<qgis-python> -m pip install coverage`
2. **`.coveragerc`** を用意（`parallel=true` / `concurrency=thread` /
   `source=<対象pkg>`）。→ `examples/coverage/.coveragerc`
3. **起動フックを 1 つ入れる**（`COVERAGE_PROCESS_START` がある時だけ発火 ＝
   通常起動は no-op）:
   - 推奨: テスト用 profile の `python/startup.py` に
     `examples/coverage/startup.py` を置く（profile スコープ・site-packages 非汚染）
   - 代替: QGIS Python の `site-packages/` に
     `examples/coverage/coverage_subprocess.pth` を置く（interpreter init で発火 ＝
     最速・import-time も拾う）
4. **env を立てて実行**（QGIS に継承される）:
   ```bash
   export COVERAGE_PROCESS_START="$PWD/.coveragerc"
   export COVERAGE_FILE="$PWD/.coverage"   # cwd 非依存で同じ場所に集約
   pytest test_e2e/
   coverage combine    # ランナー＋各 QGIS プロセスの .coverage.* をマージ
   coverage report -m  # or coverage html / xml
   ```

### 注意点

- **graceful shutdown 必須**: coverage は `atexit` で書き出すので QGIS が正常終了
  すること。`qgis_process` / `spawn_qgis` / `fresh_qgis` は graceful kill するので
  基本 OK（強制 kill は欠落する）。
- **import-time フィデリティ**: profile/`.pth` フックはプラグイン load より**早い**ので
  モジュールトップレベル行も拾える。プラグイン load 時に起動する方式だとここが漏れる
  ため、この仕掛けは `qgis_puppet` プラグインには**あえて組み込んでいない**（静かな
  過小報告を避ける）。
- **`source` / `[paths]`**: 対象は自分の package を指定（qgis-puppeteer 自体ではなく）。
  QGIS 内とランナーでパスが違うなら `[paths]` で alias して combine をマージ。
- **外部 QGIS / dev モード**: 起動中 QGIS を再利用（`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`）
  したり独自 `qgis_command` wrapper を使う場合は env 継承が自前管理になる。その QGIS を
  `COVERAGE_PROCESS_START`（と `COVERAGE_FILE`）付きで起動するか、wrapper で forward する。

詳細手順とテンプレ本体は [`examples/coverage/README.md`](../examples/coverage/README.md)。

---

## Uncaught exceptions

Qt の slot で発生した Python 例外は、デフォルトで C++→Python 境界に飲まれて
stderr 出力されるだけになる。これだと「QGIS 内部が壊れているのにテストは pass」
する事故が起きる。pytest-qgis-puppeteer は ring buffer + pytest hook で
これを **テスト fail に昇格** させる（ADR-0002 §17）。

### 仕組み

- Worker 側 `qgis_tools.exception_recorder` が `sys.excepthook` を chain して、
  発生した例外を 200 件の deque に `{type, message, traceback, ts, thread}` で記録。
- pytest plugin が `pytest_runtest_call` の前で deque をクリア、
  `pytest_runtest_makereport` の後でクエリして例外があれば:
  - **pass → fail** に promote（`longrepr` に整形済 traceback を書く）
  - 既に fail なら `report.sections` に追記
  - diagnostic bundle に `uncaught_exceptions.json` を追加

### opt-out

特定プロジェクトで無効化したい場合は `pyproject.toml` で:

```toml
[tool.pytest.ini_options]
qgis_fail_on_uncaught_exception = false   # 既定: true
```

dev モード（`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`）下では自動 skip される
（既存の長期 QGIS が抱えている過去の例外を拾ってしまうため）。

### 手動 API

ハンドラを直接呼びたい場合:

```python
recent = await client.call("qgis_get_recent_exceptions", {"limit": 50})
# {exceptions: [{type, message, traceback, ts, thread}, ...]}

await client.call("qgis_clear_recent_exceptions", {})
```

`AutomationClient` には sync ラッパも:

```python
qgis.get_recent_exceptions(limit=50)
qgis.clear_recent_exceptions()
```

---

## Multi-instance

複数の QGIS プロセスを同時に Hub に接続させて、コマンドを切り替えながら
操作する機能。

### 起動

QGIS インスタンスごとに `QPUPPETEER_WORKER_LABEL` を立てて起動:

```bat
set QPUPPETEER_WORKER_LABEL=A
start qgis-bin.exe --profile=qgis-a

set QPUPPETEER_WORKER_LABEL=B
start qgis-bin.exe --profile=qgis-b
```

両者とも自動的に Hub に接続し、Worker として register される。

### Claude Desktop / RPC からの選択

```python
# 一覧
instances = await client.list_instances()
# [InstanceInfo(instance_id="w-7k3p9q2m4x8a", label="A", pid=1234, project="...",
#               launch_token=None, registered_seq=1),
#  InstanceInfo(instance_id="w-2f5e1c8b0d6a", label="B", pid=5678, project="...",
#               launch_token="lt-...", registered_seq=2)]
# instance_id は pid 非依存の不透明 nonce（ADR-0005）。再起動で変わるので
# ハードコードせず label / @label / launch_token で参照する。

# A に切り替え
await client.call("qgis_use_instance", {"label": "A"})
# 以降のコマンドは A 宛
await client.call("qgis_list_layers")  # ← A の layers
```

### pytest からの multi-instance テスト

`automation_client.use_instance()` で sticky な default instance を設定する
パターン（既に register 済みの複数 Worker がある前提）:

```python
@pytest.fixture
def qgis_a(automation_client):
    automation_client.use_instance("A")
    return automation_client

@pytest.fixture
def qgis_b(automation_client):
    automation_client.use_instance("B")
    return automation_client

def test_cross_instance(qgis_a, qgis_b):
    qgis_a.click_widget({"object_name": "open_btn"})
    qgis_b.click_widget({"object_name": "save_btn"})
```

`spawn_qgis()` で test 内から QGIS を立てる場合（ADR-0004、Phase 1 実装済）は
[Quickstart §7](#quickstart-pytest-e2e) を参照。`per-call` の `instance=` 指定 >
sticky `use_instance()` > Hub 既定、の優先順位で routing される。

### 同一 label での連続再起動（ADR-0005）

開発ループで「同じ label のまま QGIS を kill → 再起動」を繰り返すのは
**一級サポートされたフロー**。旧プロセスが Hub の grace 期間中でも、新プロセス
は衝突せず label を**継承（SUPERSEDE）**して登録される。

- `qgis_use_instance("A")` は一度呼べば再起動を跨いで有効。sticky は
  instance_id を凍結せず **label を保持**し、dispatch ごとに Hub が
  「現在 live な A」へ再解決する（再起動の谷間だけ `instance_not_found`）。
- `instance_id` は再起動で必ず変わる（不透明 nonce）。固定参照しないこと。
- selector 解決順: `launch_token` > `@label` > `label` > `instance_id` >
  project basename（`pid` 指定は廃止）。

### 公式 launch helper と `launch_token`（決定的な相関）

「自分が起動したインスタンスを確実に特定したい」自動化では、素の QGIS を
直接起動せず **公式 launch helper** を使う。helper が相関トークンを発番・
env 注入し、stdout に返す:

```bat
qgis-puppeteer-launch --label A -- qgis-bin.exe --profile=qgis-a
:: stdout: launch_token=lt-7k3p9q2m4x8a0b1c
```

返ってきた `lt-...` を selector に渡せば、pid 再利用や同時起動に左右されず
**決定的**にそのインスタンスへ到達できる:

```python
token = "lt-7k3p9q2m4x8a0b1c"  # helper の stdout から取得
info = await client.wait_for_instance(token, timeout_s=30)  # register 待ち
await client.call("qgis_list_layers", {"instance": token})
```

pytest の `spawn_qgis()` は内部でこの仕組みを使い、pid diff ではなく
`launch_token` で spawn 分を相関する（ADR-0005 D6）。

規約も token も無い手動起動では `client.current_max_seq()` → 起動依頼 →
`client.wait_for_new_instance(since_seq)` で best-effort に拾えるが、同時
起動が重なると取り違え得る（**racy**。helper 経由を推奨）。

### 同時同 label 衝突のポリシー（env で選択）

2 台が**同時に active** な同 label を登録した場合の挙動を起動時 env で選ぶ:

| env | モード | 挙動 |
|---|---|---|
| （無指定） | `reject`（既定） | `label_conflict` で 2 台目を弾く（事故を気づかせる） |
| `QPUPPETEER_WORKER_TAKEOVER=1` | `takeover` | 既存 active を強制 evict し新 worker が label を奪取（kill -9 直後の確実な継承 / 開発ループ向け） |
| `QPUPPETEER_WORKER_LABEL_SUFFIX=1` | `suffix` | `A (2)` 等へ自動 rename して登録（未登録放置を避ける CI 探索向け） |

連続再起動する開発では **label 明示 + `QPUPPETEER_WORKER_TAKEOVER=1`** を推奨。
takeover を既定にしないのは同時 multi-instance の安全性を守るため。

なお Hub は WS ping/pong で active の生存を監視し、kill -9 等で TCP が
half-open のまま残った旧 entry を ~20s で grace へ落とす（`reject` 既定でも
少し待てば SUPERSEDE で素直に継承できる）。

---

## Permissions

`qgis_execute_python` は QGIS 内で任意の Python コードを実行するため、
**3-tier 権限システム** で保護されている:

| Tier | 挙動 |
|---|---|
| **whitelist** | 既知の安全コード（`QgsProject.instance().fileName()` 等の参照系）→ 即実行 |
| **session** | テスト session 内で 1 度許可されたパターン → 即実行 |
| **confirm** | 上記以外 → QGIS 上に確認ダイアログを出してユーザーに承認を求める |

### 信頼モード（CI / E2E 向け）

QGIS 起動時に env を立てると confirm ステップが skip される:

```bat
set QPUPPETEER_TRUSTED_MODE=1
start qgis-bin.exe
```

pytest-qgis-puppeteer の fixture は subprocess 起動時に自動でこの env を
立てるので、E2E テストでは設定不要。

> **セキュリティ警告**: 信頼モードは「pytest が spawn した subprocess に
> 限る」想定。ユーザー / システムスコープで永続的に設定すると、Claude Desktop
> 経由の任意コード実行が confirm 無しで通ってしまう。

### 監査ログ

信頼モードでも実行内容は QGIS 側で `QgsMessageLog` に記録される
（`[trusted mode] exec: ...`）。失敗解析・セキュリティレビュー用。

### 既存の whitelist を確認・session を破棄

```python
# 一覧
result = await client.call("qgis_get_whitelist")

# session に追加された分をクリア
await client.call("qgis_clear_session_permissions")
```

---

## 環境変数リファレンス

### Hub 接続関連

| 変数 | 既定 | 用途 |
|---|---|---|
| `QPUPPETEER_HUB_HOST` | `127.0.0.1` | Hub の bind / connect ホスト |
| `QPUPPETEER_HUB_PORT` | `9876` | Hub の port |
| `QPUPPETEER_HUB_URL` | — | `ws://host:port` を直接指定（host/port を上書き） |
| `QPUPPETEER_HUB_ORIGIN` | `http://localhost` | register 時の Origin ヘッダ |

### Worker 関連

| 変数 | 既定 | 用途 |
|---|---|---|
| `QPUPPETEER_WORKER_LABEL` | — | self-reported label（安定ロール名）。空なら Hub が自動採番 |
| `QPUPPETEER_LAUNCH_TOKEN` | — | 公式 launch helper が注入する相関トークン（ADR-0005 D6）。通常は手で設定せず `qgis-puppeteer-launch` 経由 |
| `QPUPPETEER_WORKER_TAKEOVER` | unset | `1` で同時同 label 衝突時に既存 active を奪取（ADR-0005 D4。連続再起動の開発ループ向け） |
| `QPUPPETEER_WORKER_LABEL_SUFFIX` | unset | `1` で同時同 label 衝突時に `A (2)` 等へ自動 rename |
| `QPUPPETEER_HUB_PYTHON` | — | Hub spawn 用 Python launcher を明示指定（自動検出失敗時） |
| `QPUPPETEER_HUB_LOG_FILE` | `<TEMP>/qgis_puppet.spawn.log` | Hub subprocess の stdout/stderr 出力先 |
| `QPUPPETEER_HUB_LOCK_PATH` | OS 一時ディレクトリ | Hub auto-spawn 用ロックファイル（port 別） |
| `QPUPPETEER_HUB_EXTRA_SYSPATH` | — | Hub bootstrap が `sys.path` に prepend する追加 path（`PYTHONPATH` の代替、`:` / `;` 区切り） |

### 信頼 / テスト関連

| 変数 | 既定 | 用途 |
|---|---|---|
| `QPUPPETEER_TRUSTED_MODE` | unset | `1` で `execute_python` の confirm を skip |
| `QPUPPETEER_ALLOW_TEST_HANDLERS` | unset | `1` で `test.*` namespace への handler 登録を許可 |

### pytest-qgis-puppeteer 固有

| 変数 | 用途 |
|---|---|
| `QPUPPETEER_QGIS_BIN` | `qgis-bin.exe` パス（`qgis_command` 未指定時） |
| `QPUPPETEER_QGIS_COMMAND` | QGIS 起動コマンド全体（host 独自 launcher 用） |
| `QPUPPETEER_QGIS_PYTHON` | QGIS 同梱 Python launcher（未指定なら自動検出） |
| `QPUPPETEER_QGIS_ARGS` | 追加引数（shell-style） |
| `QPUPPETEER_E2E_USE_RUNNING_QGIS` | `1` で既存 QGIS に相乗り（dev モード） |
| `QPUPPETEER_E2E_HUB_URL` | dev モード時の既存 Hub URL（既定 `ws://127.0.0.1:9876`） |
| `QPUPPETEER_META_CHILD` | `--env=all` / `--env=A,B` の meta-parent から起動された子 invocation で `1` がセットされる（読み取り専用 signal） |

### qgis-puppeteer ロギング

| 変数 | 既定 | 用途 |
|---|---|---|
| `QPUPPETEER_LOG_LEVEL` | `INFO` | logger のレベル |

### coverage（標準 coverage.py 変数 — 参考）

qgis-puppeteer 独自ではないが、[E2E カバレッジ計測](#e2e-カバレッジ計測)で使う。
spawn 時に親 env が QGIS へ継承されるので、pytest 実行 env に立てれば QGIS 内
coverage が有効化される。

| 変数 | 用途 |
|---|---|
| `COVERAGE_PROCESS_START` | `.coveragerc` への絶対パス。QGIS 側の subprocess 計測を有効化 |
| `COVERAGE_FILE` | data file の出力先。cwd の違う QGIS とランナーで同じ場所に集約する用 |

---

## Troubleshooting

### `label_conflict`（同時同 label）

**症状**: 起動した QGIS が登録されず（メッセージバー `register failed
(label_conflict)`）、`list_instances` に出てこない。

**原因**: 既に **active** な同 label の Worker が居る状態で、別プロセスが
同じ `QPUPPETEER_WORKER_LABEL` で登録しようとした（ADR-0005 D4、既定
`reject`）。連続再起動で起きる場合は、旧プロセスが kill -9 等で TCP
half-open のまま active と誤認されている可能性。

**対処**:

1. 数十秒待つ → Hub の heartbeat sweep が旧 entry を grace へ落とし、
   再登録が **SUPERSEDE** で素通りする
2. 待てない開発ループは `QPUPPETEER_WORKER_TAKEOVER=1` で起動（旧 active を
   強制的に明け渡す）
3. 複数台を同時に動かしたい場合は label を別にする、または
   `QPUPPETEER_WORKER_LABEL_SUFFIX=1`（`A (2)` 等へ自動 rename）
4. 真に同時 2 台が同 label なのは設定ミス → どちらかの label を直す

### 自分が起動した QGIS が特定できない（attribution）

**症状**: 複数 QGIS がある中で「今 spawn した 1 台」を取り違える / 掴めない。

**原因**: 素の `qgis-bin.exe` を label 規約なしで起動した（非制御 launch）。
client は起動時点で identity を取得できず当て推量に頼っている。

**対処**:

1. **公式 launch helper を使う**: `qgis-puppeteer-launch --label A --
   qgis-bin.exe ...` → stdout の `launch_token=lt-...` を
   `client.wait_for_instance(token)` に渡せば決定的に特定できる
2. pytest なら `spawn_qgis()`（内部で launch_token 相関）を使う
3. helper を使えない場合は `current_max_seq()` →（起動）→
   `wait_for_new_instance(since_seq)`。ただし同時起動が重なると racy

### `selector_ambiguous` エラー

**症状**: `SelectorAmbiguousError: Selector ... matched N widgets.`

**原因**: 複数 widget にマッチする selector を `index` 未指定で渡した
（strict mode が既定有効）。

**対処**:

1. ログに上位 10 件の候補 (class / object_name) が出る → どれを選びたいか確認
2. `object_name` 等で一意になるよう絞る（推奨）
3. どうしても N 番目を取りたい場合のみ `index` を明示

### `widget_not_found`

**症状**: `error: "widget_not_found"` が返る

**原因**: scope 内に該当 widget が無い、または selector 仕様の typo。

**対処**:

1. `qgis_snapshot_ui` で UI ツリーをダンプして objectName / class を確認
2. `scope="modal"` で見つからない場合 `"active_window"` や `"any"` を試す
3. `root_object_name="QgisApp"` を付けて main window scope に切り替え

### `widget_disabled`

**症状**: `qgis_click_widget` / `qgis_set_widget_value` で
`error: "widget_disabled"`、または Locator が `WidgetNotActionableError`
（メッセージに `enabled=False`）。

**原因**: 対象 widget が `isEnabled() == False`。前段の入力が満たされていない、
非同期処理中で操作禁止になっている、UI 状態の更新待ち、等。

**対処**:

1. 低レベル RPC は **即返り・リトライ無し**。`Locator.click()` を使えば
   auto-wait で enabled になるのを待つ（既定 5s）
2. 待っても enabled にならない場合は **前提条件が欠けている**ので、UI フロー
   を見直す（必須項目の入力、別ウィジェットの先行操作 等）
3. `qgis_check_actionability` を 1 回呼んで `{enabled: bool, ...}` を確認すると
   切り分けが速い
4. snapshot の各ノードに `enabled` フィールドが入っているので
   `qgis_snapshot_ui` で全体を見渡すのも有効

### `widget_readonly`

**症状**: `set_widget_value` で `error: "widget_readonly"`

**原因**: readOnly な input に書き込もうとした。

**対処**:

1. UI で readOnly になる条件を確認（前段の checkbox 等）
2. `fill()` の前に readOnly を解除する操作を入れる
3. Locator の `fill()` は auto-wait で `editable=True` を待つので、解除が
   遅れる程度なら timeout 内に通る

### Hub spawn 失敗

**症状**: QGIS 起動時に「QGIS Puppet: Hub startup failed」のメッセージバー

**原因の切り分け**:

1. `<TEMP>/qgis_puppet.spawn.log` を確認（Python 起動前の fatal がここに出る）
2. `<TEMP>/qgis_puppeteer/hub.log` を確認（Python 側の logger 出力）
3. ポート衝突: 既に何かが `127.0.0.1:9876` を使ってないか
4. `QPUPPETEER_HUB_PYTHON` 未設定で `python-qgis-ltr.bat` が見つからない
   → 環境変数で明示指定するか OSGEO4W_ROOT を確認

### `qgis_python` auto-discovery 失敗（pytest）

**症状**: `qgis_python is not configured and could not be auto-discovered.`

**対処**: 以下のいずれか:

```toml
# pyproject.toml で明示
[tool.pytest.ini_options]
qgis_python = "C:/Program Files/QGIS 3.34/bin/python-qgis-ltr.bat"
```

```bash
# OSGEO4W_ROOT を立てれば auto-discovery される
set OSGEO4W_ROOT=C:\Program Files\QGIS 3.34
```

### `instance_not_found`

**症状**: `error: "instance_not_found"` が返る

**原因**: 指定した label / instance_id の Worker が Hub に register されて
いない。または register 完了前に呼んだ。

**対処**:

1. `qgis_list_instances` で現在の Worker 一覧を確認
2. pytest なら `wait_for_worker` を使って register 完了を待つ
3. multi-instance のラベル衝突 → `QPUPPETEER_WORKER_LABEL` を変える

### `widget_not_actionable`（timeout）

**症状**: 操作の auto-wait が 5 秒以内に actionable にならず timeout

**原因の切り分け**: 例外の `last_check` 属性に最後のチェック結果:

```python
try:
    qgis.locator({"object_name": "btn"}).click()
except WidgetNotActionableError as e:
    print(e.last_check)
    # exists / visible / enabled / not_covered / editable のどれが False かが分かる
```

- `exists=False` → selector が間違っている
- `visible=False` → 親 widget が hidden、または別タブにある
- `enabled=False` → 前段の入力で valid 状態にする必要あり
- `not_covered=False` → 別のモーダルが被っている（Dialog handler 検討）
- `editable=False`（fill 限定） → readOnly 状態

### confirm UI で固まる（dev モード）

**症状**: `execute_python` を呼ぶと QGIS で確認ダイアログが出てテストが進まない

**原因**: 信頼モードが OFF の QGIS で `execute_python` を呼んでいる

**対処**:

1. テストには `QPUPPETEER_TRUSTED_MODE=1` 付き subprocess を使う（pytest fixture
   既定）
2. dev モード（既存 QGIS 相乗り）で execute_python 多用テストを走らせない
3. または対象 QGIS を起動するときに `QPUPPETEER_TRUSTED_MODE=1` を立てておく

> pytest ラッパ `execute_python` は confirm ゲートを `ConfirmationRequiredError`
> として raise するので、固まらず即原因が分かる（dev モードで信頼モード未設定の
> サイン）。

### `execute_python` のコード内例外が見えない（後段が謎の timeout）

**症状**: `execute_python` で開いたダイアログ等の後段で `wait_for_*` /
Locator が timeout するが、失敗理由が「ウィジェットが見つからない」だけで
真因が見えない。

**原因**: Worker 側のコード（例: ダイアログ `__init__` 内の DB アクセス）で
例外が起きている。古い版では `execute_python` がこれを `success=False` の dict に
詰めて「正常応答」として返していたため silent になっていた。

**対処**:

1. pytest ラッパ `execute_python` は **`WorkerCodeError` を raise** する。Worker 側
   トレースバックが例外メッセージに載るので、真因（DB のテーブル不在など）が
   一発で分かる。
2. stdout 等や成否を自前で検査したい場合は `execute_python_detailed(code)` を使い、
   返ってくる `ExecResult`（`.success` / `.traceback` / `.stdout` 等）を見る
   （こちらはコード失敗で raise しない）。
3. 後段が timeout する E2E では、まず操作の起点となった `execute_python` が
   raise していないかを疑う。

> 補足: `execute_python` は Worker 側コードが **`_result` に代入した値** を返す
> （明示規約。式の自動評価のような構文依存の魔法は持たない）。`_result` 未代入なら
> `None`。`_result` が JSON 直列化不可（QGIS layer 等）なら
> `NonSerializableResultError` を raise するので、QGIS 側で `.name()` /
> `.featureCount()` 等の素データに変換してから返すこと。

### `register_handler` が `reserved_namespace` で失敗

**症状**: ホストアプリ側で `worker.register_handler("qgis_foo", ...)` がエラー

**原因**: `qgis_*` は Core 名前空間予約。外部からの登録は `mydomain.*` /
`extensions.*` / `test.*` のみ可。

**対処**: Core ハンドラを上書きしたい場合は `register_core_handler` を使う
（プラグイン内部のみ）。外部 plugin から提供する場合は `mydomain.*` の prefix
を使う。

---

## Roadmap

将来検討中の項目。詳細は [ADR-0002 Roadmap](architecture/0002-e2e-test-architecture.md) を参照:

- **Auto-wait `stable` チェック** — アニメーション中操作の吸収
- **Auto-wait `receives_events`** — hit-test ベースの occlusion 検出
- **入れ子 modal** — modal stack 内の特定 widget 取得
- **pytest-xdist 並列実行 / env 並列実行** — 複数 Worker での並列テスト
- **Hub/Worker プロトコル拡張** — push event / deferred 実行モード
- **Test isolation 強化** — 専用 QGIS profile 自動構築 / プラグイン state reset
- **HTML サマリレポート** — `summary.json` → スタンドアロン HTML

---

## ライセンスとサポート

- コア / pytest plugin: Apache-2.0
- QGIS plugin: GPL-3.0-or-later

Issue / PR: <https://github.com/oruharo/qgis-puppeteer/issues>

質問や非自明な変更は Issue を立ててください。
