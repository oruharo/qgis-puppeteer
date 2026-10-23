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
9. [地図のキャンバスの操作（マウスとキー）](#map-canvas-input)
10. [地図の画像スナップショット（`qgis_render_map`）](#map-image-snapshot)
11. [Uncaught Qt/Python 例外と test fail linkage](#uncaught-exceptions)
12. [QgsMessageLog の記録と判定](#message-log)
13. [MCP の操作記録](#mcp-recording)
14. [複数 QGIS（multi-instance）](#multi-instance)
15. [Permissions / 信頼モード](#permissions)
16. [環境変数リファレンス](#環境変数リファレンス)
17. [Troubleshooting](#troubleshooting)
18. [Roadmap](#roadmap)

---

## はじめに

qgis-puppeteer は QGIS を **WebSocket 経由で外部から操作する** 基盤。

```
[Claude Desktop / pytest / your script]
                ↓ WebSocket
        [qgis-puppeteer Hub]   ← 単一ポート、ルーティング
                ↓ WebSocket (Worker dial out)
        [QGIS + QGIS Puppeteer plugin]
```

3 つの配布物で構成:

| 配布物 | 役割 | ライセンス |
|---|---|---|
| `qgis-puppeteer`（PyPI） | コア（Hub / Worker / AutomationClient / MCP gateway） | Apache-2.0 |
| `pytest-qgis-puppeteer`（PyPI） | pytest 用ヘルパ（Locator / fixture / 診断バンドル） | Apache-2.0 |
| QGIS Puppeteer プラグイン（フォルダ名 `qgis_puppeteer`） | QGIS 内で動く Worker。リポジトリの `plugins/qgis_puppeteer/`。コアの写しを含むので単体で動く。プラグイン固有の部分は `qgis_plugin/` | GPL-3.0-or-later（写しの部分は Apache-2.0） |

ペルソナ別の入り口:

- **Claude に QGIS を触らせたい** → [Claude Desktop quickstart](#quickstart-claude-desktop)
- **QGIS プラグインの E2E テストを書きたい** → [pytest quickstart](#quickstart-pytest-e2e)
- **自動化スクリプトに組み込みたい** → [Python script quickstart](#quickstart-python-script)

---

## インストール

### 共通: QGIS plugin の配置

QGIS Puppeteer プラグインを QGIS に組み込む（必須）:

1. `plugins/qgis_puppeteer/` を、フォルダ名 `qgis_puppeteer`
   のまま QGIS のプラグインディレクトリにコピーする
   （Windows 既定: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`）。
   チェックアウトから試すならコピーではなくジャンクションでよい:
   ```bat
   mklink /J "%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\qgis_puppeteer" "<repo>\plugins\qgis_puppeteer"
   ```
2. QGIS を起動し、Plugin Manager で `QGIS Puppeteer` を有効化

プラグインのフォルダが `qgis_puppeteer` パッケージ一式を含むので、QGIS の
Python に追加で入れるものは無い（websockets も要らない。websockets を使うのは
AutomationClient と MCP gateway だけで、どちらも QGIS の外で動く）。

**QGIS の Python に `pip install qgis-puppeteer` しないこと。** QGIS は
プラグインフォルダを sys.path の先頭に足してから `import qgis_puppeteer` するので、
普通はプラグインが読まれる。ただし、それより前に別の `qgis_puppeteer`（QGIS の
site-packages、user site、ホストアプリの vendor 等にあるもの）が import されて
いると — `PYQGIS_STARTUP` のスクリプトや、先に読まれる別プラグインが import
した場合 — Python はそちらを使い、プラグインは隠れる。その場合プラグインは
読み込みを止め、邪魔をしているコピーの場所をエラーに出す。

### Claude Desktop ユーザー向け

Claude Desktop 起動側の Python に MCP gateway を入れる:

```bash
pip install "qgis-puppeteer[mcp]"
```

`[mcp]` extra が MCP SDK を連れてくる。付けずに入れると
`python -m qgis_puppeteer.gateways.mcp` が ImportError で起動しない。

### pytest E2E 作者向け

```bash
pip install pytest-qgis-puppeteer
# qgis-puppeteer は依存として自動的に入る
```

### Pre-release（dev ブランチ）から入れる場合

PyPI への公開前は GitHub から直接（Claude Desktop 用には `[mcp]` を付ける）:

```bash
pip install "qgis-puppeteer[mcp] @ git+https://github.com/oruharo/qgis-puppeteer.git@dev#subdirectory=packages/qgis-puppeteer"
pip install "pytest-qgis-puppeteer @ git+https://github.com/oruharo/qgis-puppeteer.git@dev#subdirectory=packages/pytest-qgis-puppeteer"
```

### アップグレード（dev を追いかけている場合）

動いているプロセスは 3 種類あり、それぞれ入れ替わるタイミングが違う。
**全部そろえないと、進行中のセッションでは MCP ツールが落ちる。**

| プロセス | コードの出どころ | 新しくなるのは |
|---|---|---|
| MCP gateway | `uvx --from ...@dev`（MCP クライアントが起動） | **MCP クライアントのセッションを張り直したとき**。セッション開始時に取得したものに固定される |
| Hub | プラグインが auto-spawn。プラグインと同じ `qgis_puppeteer` のコピーで動く | Hub プロセスを kill したとき。Worker が数秒で新しい Hub を立て直して再接続する。MCP クライアントが繋がっている間は idle 終了しないので、放っておくと古いまま |
| Worker（プラグイン） | QGIS のプラグインフォルダ（`QGIS_PLUGINPATH` 等）の `qgis_puppeteer` | QGIS を起動し直したとき |

手順:

1. コードを更新する（`git pull` / vendor 更新）
2. Hub プロセスを kill する。QGIS は起動したままでよい — Worker が数秒で
   Hub を立て直して再接続する。**instance_id は変わる**（grace は Hub の
   メモリ上の状態なので、Hub と一緒に消える。引き継がれるのは Worker 側だけが
   切れて同じ Hub に戻る場合）。`launch_token` / label のセレクタと sticky は
   そのまま効く。プラグイン側（`qgis_plugin/`、Worker）も変えたなら QGIS を
   起動し直す
3. **MCP クライアントのセッションを張り直す**（Claude Desktop なら再起動、
   Claude Code なら `/mcp` で reconnect か新セッション）

3 を飛ばすと、古い gateway が新しい Hub に繋ぎに行く。gateway は再起動された
Hub への再接続を自分で張り直すが、新しいフィールドやツールは知らないままなので
挙動が食い違う。

#### gateway が二重に立っていないか確認する

Claude Desktop の `claude_desktop_config.json` にも `qgis-puppeteer` を定義して
いると、Desktop の Code タブのセッションには **Desktop が立てた gateway と
`.mcp.json` の gateway の両方**が同じ名前で注入される。ツール一覧と呼び出しは
片方（Desktop 側）に流れるので、`.mcp.json` をいくら新しくしても効かない。
症状は「ツール一覧に新しいツール（`qgis_wait_ready` など）が無い」「gateway.log
に何も書かれない」「一部のツールだけ本文なしの `Error executing tool` で落ちる」。

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'gateways\.mcp' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine
```

`python -m qgis_puppeteer.gateways.mcp` が複数あり、親が `Claude.exe`（Desktop）
と `claude.exe`（Code CLI）に分かれていればこれ。Desktop 側の定義を消すか、
両方を同じスペック（git の `@dev`）に揃える。

どのビルドが応答しているかは、gateway が起動時に名乗る 1 行で照合できる
（stderr と `gateway.log` の両方に出る）:

```
INFO Gateway qgis-puppeteer 0.1.0, commit 1c1ff51… (git+https://…@dev), code at C:\…\site-packages\qgis_puppeteer, pid 12345, python 3.13.5
```

git インストールなら `serverInfo.version` も `0.1.0+g1c1ff51` の形になるので、
Claude Code の MCP ログ（`Connection established with capabilities: … "version"`）
でも commit が分かる。

#### QGIS のプラグインと pytest 側の版の食い違い

QGIS のプラグインのフォルダはライブラリの写しで、版や commit を持たない。そこで
両方が、写しの対象になるファイルの中身から**指紋**（12 桁の 16 進）を作る。
`sync_plugin.py --check` が通る組み合わせなら同じになり、改行の違いは数えない。

- Worker は登録のときに指紋を送り、`list_instances` / MCP の `qgis_list_instances`
  の `source_fingerprint` に出る（旧プラグインは `null`）
- pytest は session の QGIS が使える状態になったとき（`hub_ready`）に 1 回、
  自分の qgis-puppeteer の指紋と比べる。食い違っていれば
  `pytest_qgis_puppeteer.QgisPluginMismatchWarning` を出す（失敗にはしない）。
  旧プラグインで指紋が無いときも、比べられないことを警告する。失敗にしたいときは
  `-W error::pytest_qgis_puppeteer.QgisPluginMismatchWarning`
- MCP の gateway は、起動時に名乗る 1 行に自分の指紋を入れる（`fingerprint …`）。
  `qgis_list_instances` の `source_fingerprint` と比べる

片側にしか入っていない修正は効かないので、uv.lock の固定とプラグインの入れ直しは
同じ commit にそろえる。

#### `uvx --from <ローカルディレクトリ>` は src の変更で再ビルドしない

vendor コピーなどローカルパスを `uvx --from` に渡すと、uvx はその環境を
キャッシュして **src が変わっても作り直さない**（uv 0.8 で確認。pyproject.toml
を書き換えても気づかない）。`make vendor` で更新したつもりが古いビルドのまま、
が起きる。回避:

- `uvx --reinstall-package qgis-puppeteer --from <dir> ...`（毎回ビルド、数秒）
- または git スペック `--from "qgis-puppeteer[mcp] @ git+...@dev#subdirectory=..."`
  （commit が変われば作り直す）
- `uv run --project <dir>` なら `[tool.uv] cache-keys` で src の変更に追従する

`--with mcp` は上限なしで最新の mcp を連れてくるので、`[mcp]` extra
（`mcp>=2,<3`）で入れること。

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

QGIS が起動すると QGIS Puppeteer プラグインが自動的に Hub を spawn し、
Worker として登録される。Hub のポートは既定 `9876`（127.0.0.1 のみ）。

### 3. Claude Desktop を起動して試す

Claude に話しかける:

> 「今開いているレイヤを一覧して」

Claude 側で以下のような MCP ツール呼び出しが行われる:

- `mcp__qgis-puppeteer__qgis_list_layers`
- `mcp__qgis-puppeteer__qgis_select_features`
- `mcp__qgis-puppeteer__qgis_screenshot`
- `mcp__qgis-puppeteer__qgis_use_instance`（multi-instance 時）
- `mcp__qgis-puppeteer__qgis_wait_ready`（QGIS 起動直後に、呼び出しが通る状態
  になるまで待つ）
- ... 他

### よくある初期トラブル

| 症状 | 原因 | 対処 |
|---|---|---|
| Claude から QGIS が見えない | QGIS が起動していない、Hub が未起動 | QGIS を起動してから Claude Desktop を再起動 |
| `instance_not_found` | Worker register 完了前に呼んだ | 数秒待って再試行 |
| `execute_python` が実行されずに返る | ホワイトリスト外のコードで confirm ゲートに当たった | 承認を取って `qgis_execute_with_permission` で再実行（[Permissions](#permissions)） |

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
| `screenshot.png` | マップキャンバスの画像（`qgis_screenshot`）。メインウィンドウやダイアログは写らない |
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

fresh の QGIS のラベル（`list_instances` やログに出る名前）は `fresh-<テスト名>`。
パラメータの値が長くてラベルの上限（128 文字）を超えるときは、値の代わりに
パラメータの番号を使う（`fresh-test_open_project[2]`）。テストの完全な名前は、
起動時のログ（`fresh_qgis: <nodeid> runs on instance_id=... (label=...)`）と、
失敗時の診断バンドルの `fresh_qgis.json` に残る。ラベルを決め打ちして送り先に
使わないこと（`qgis` がすでに切り替えている）。

dev モード（`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`）下では `fresh_qgis` テストは
skip される（既存 QGIS を再起動できないため）。

#### 失敗の後に QGIS を起こし直す: `qgis_auto_fresh_after_failure`

失敗したテストが QGIS に残したもの（閉じ忘れのダイアログ、読み込んだレイヤなど）で、
後のテストが巻き添えで落ちるのを防ぐ。既定は `false`（何もしない）。

```toml
[tool.pytest.ini_options]
qgis_auto_fresh_after_failure = "session"   # または "true"
```

| 値 | 失敗の後に起きること |
|---|---|
| `session` | 次に `qgis` を使うテストの準備で、session の QGIS を閉じて起こし直す。以降のテストも新しい QGIS で動く |
| `true` | 次の 1 本だけを新しい QGIS（`fresh_qgis`）で動かす。その次からは元の session の QGIS に戻る |

`true` では、汚れた session の QGIS はそのまま残る。そのため「落ちる → 次の 1 本は
通る → また落ちる」と交互になることがある。ふつうは `session` を勧める。
起こし直しのコストは、失敗 1 件につき QGIS の起動 1 回分（数秒〜十数秒）。

- `session` でも、失敗したテストが `fresh_qgis` だった場合は起こし直さない
  （session の QGIS を使っていないため）
- 起こし直した QGIS は instance_id が変わる。`qgis` / `automation_client` は
  そのまま新しい QGIS に届くが、`hub_ready` fixture の値（最初の instance_id）は古くなる
- 起こし直しの呼び出し（閉じる依頼、起動の待ち）は、次のテストの操作記録に入らない
- 起こし直せなかったときは、そのテストを error にして、次に `qgis` を使うテストで
  もう一度試す
- dev モードや、`qgis_process` fixture を override している場合（[10.](#10-完全制御が必要な場合-fixture-オーバーライド)）は
  起こし直せないので、警告だけ出して何もしない
- conftest から自分で起こし直すこともできる。`qgis_process` fixture の値
  （`SessionQgis`）の `restart()` を呼ぶ
- 失敗の後でなくても、決まったテストの前に起こし直したいときは、印
  `@pytest.mark.qgis_restart_before` を付ける（例: データベースに書くテストの前）。
  `qgis_auto_fresh_after_failure` の設定に関わらず効く。`fresh_qgis` のテストでは
  session の QGIS を使わないので起こし直さない。起こし直した QGIS では
  `pytest_qgis_ready` が `kind="restart"` で呼ばれるので、ホスト固有の準備
  （データベースを戻した後の確認、ログインを待つ、など）はそこに書く
  （[QGIS の起動の間のメッセージ](#qgis-の起動の間のメッセージ)）

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

**ラベルと、登録を断られたとき**:

- `label=`（または環境変数 `QPUPPETEER_WORKER_LABEL`）が Hub の受け付けない形なら、
  QGIS を起こす前に `ValueError` になる。受け付けるのは 1〜128 文字で、数字だけでなく、
  `@` で始まらないもの。ラベルを省くと Hub が自動で付ける
- 起こした QGIS の登録を Hub が断ったら（同じラベルの QGIS が生きている、など）、
  待ち時間を待たずに `WorkerRegisterRejected` で失敗する。メッセージに断った理由が入り、
  断られた QGIS は止めてある。`WorkerRegisterTimeout` の下位型なので、既存の
  `except WorkerRegisterTimeout` でも拾える
- `register_timeout_s` までに使える状態にならなかったら `WorkerRegisterTimeout` に
  なる。そのとき Hub に自分の QGIS が見えていれば（登録はされたがプロジェクトの
  読み込みで塞がっている、断られた登録の記録にある）、その pid で止めてから失敗する。
  閉じるよう頼まないのは、塞がった QGIS は応答しないため
- 待ちが切れると、その QGIS の `launch_token` を「もう待たない」と Hub に知らせる。
  後から遅れて登録してきた QGIS は Hub に断られ、QGIS Puppeteer プラグインが
  QGIS を自分で閉じる（閉じきれなければ約 12 秒後に強制終了する。どちらも理由を
  QgsMessageLog と stderr に残す）。プラグインが古い QGIS は自分では閉じない
- 自分で閉じるのは、`launch_token` 付きで起こされ、まだ一度も登録できていない QGIS
  だけ。登録を断られた理由は問わない。一度でも登録できた QGIS は、つなぎ直しを
  断られても閉じない（Hub が起動し直した後など）。人が普通に起こした QGIS は
  token を持たないので対象外
- 待ちが切れた時点で Hub に自分の QGIS が何も見えなければ（登録の要求を送って
  こない。プラグインが読み込まれない、など）、環境変数の `launch_token` で探して
  止める。Windows では、名前が `qgis` で始まる実行ファイル（`qgis-ltr-bin.exe`
  など）のうち、起動から待ちが切れるまでの間に作られ、環境変数
  `QPUPPETEER_LAUNCH_TOKEN` が自分のものと一致するものだけを止める。token は
  ランチャーの途中（cmd、Python、`start` など）を通って QGIS まで届くので、途中の
  プロセスが先に終わっていても見つかる。人が起こした QGIS は token を持たないので
  巻き込まない。posix では、起動したコマンドのプロセスグループごと止める
- それでも残りうるのは、Hub が止まった後（セッションの終わり）に登録しようとする
  QGIS、ランチャーが環境変数を QGIS に渡さない作り、QGIS の実行ファイルの名前を
  変えている環境、環境変数を読めない QGIS（管理者として動いているなど）

`launch_token` の Worker が register し、**かつ軽い read-only の往復が返る**
まで待ってから返る（`wait_for_ready()`）。重いプロジェクトを開く QGIS では
`register_timeout_s` を伸ばすこと — 一覧に載ってからレイヤが揃うまで実測で
20 秒の開きがあった。
register タイムアウト（既定 60s）は `register_timeout_s=` で上書き可能。

### 8. dev モード（既存 QGIS に相乗り）

新しい QGIS を spawn せず、既に開いている QGIS でテストを走らせるモード。
書き始め・UI 操作系のデバッグ用途。

```bash
# 環境変数を立ててから pytest 実行
export QPUPPETEER_E2E_USE_RUNNING_QGIS=1
pytest test_e2e/
```

注意: `execute_python` を多用するテストは confirm ゲートで弾かれる
（`ConfirmationRequiredError`）ので、dev モードでは UI 操作系のみ走らせるのが
安全（または対象 QGIS で `QPUPPETEER_TRUSTED_MODE=1` を立てておく）。

### 9. 起動ごとの準備: `pytest_qgis_before_spawn`

pytest が QGIS を起こす**直前**に、conftest のフック `pytest_qgis_before_spawn` が
呼ばれる（#29）。起動のたびに準備が要るもの（新しいプロファイルを一時フォルダに
作る、など）をここで済ませ、その起動だけに足す引数と環境変数を返す。
`pytest_qgis_ready`（起こしたあと）の対。

```python
# conftest.py
def pytest_qgis_before_spawn(config, kind):
    """kind は "session"（最初の QGIS）/ "restart"（起こし直し）/ "fresh"。"""
    return {"args": [...], "env": {...}}  # どちらも省ける。足すものが無ければ None
```

- `args` は `qgis_args` の後ろ（`qgis_command` を使うときはそのコマンドの後ろ）に、
  `env` は環境（`environments.toml` の `env`）に足す。いくつかの conftest が返した
  ものは合わせる。同じ環境変数に違う値を返すと `UsageError`
- QGIS を起こすたびに 1 回（session の最初、起こし直し、`fresh_qgis` のそれぞれ）。
  `spawn_qgis()` を直接使うとき（引数をその場で渡せる）と dev モードでは呼ばれない
- 例外を出すと QGIS は起こさず、そのテストの準備のエラーになる
- 足した引数はログに出る。環境変数は名前だけ出る（値に鍵などを入れることがあるため）

#### 例: 起動ごとに新しいプロファイルを作る

利用者の普段のプロファイル（入っているプラグイン、設定、最近のプロジェクト）から
テストを切り離し、テストに要るプラグインだけを有効にしたプロファイルで起こす。
どのプラグインを入れるかは利用側が決めることなので、上流は作り方を持たず、例で示す:

```python
# conftest.py
import shutil
import tempfile
from pathlib import Path

# テストに要るプラグインを並べたフォルダ（qgis_puppeteer を含む）
PLUGINS = Path(__file__).parent / "e2e_plugins"


def pytest_qgis_before_spawn(config, kind):
    root = Path(tempfile.mkdtemp(prefix="qgis-e2e-"))
    profile = root / "profiles" / "e2e"
    plugins_dir = profile / "python" / "plugins"
    shutil.copytree(PLUGINS, plugins_dir)
    enabled = "\n".join(f"{p.name}=true" for p in plugins_dir.iterdir() if p.is_dir())
    (profile / "QGIS").mkdir(parents=True)
    (profile / "QGIS" / "QGIS3.ini").write_text(f"[PythonPlugins]\n{enabled}\n", encoding="utf-8")
    config.add_cleanup(lambda: shutil.rmtree(root, ignore_errors=True))  # 終わったら消す
    return {"args": ["--profiles-path", str(root), "--profile", "e2e"]}
```

- `--profiles-path` は `profiles` フォルダの**親**（QGIS がその下の
  `profiles/<名前>` を使う）
- QGIS に同梱のコアのプラグイン（`processing` / `db_manager` / `MetaSearch` /
  `grassprovider`）は、新しいプロファイルでも既定で有効。止めたいなら
  `QGIS3.ini` の `[PythonPlugins]` に `processing=false` などと書く
- 起こし直し（`qgis_restart_before` や `qgis_auto_fresh_after_failure = session`）の
  たびに別の新しいプロファイルになる。同じプロファイルを使い回したいなら、作るのを
  1 回にして（`kind == "session"` のときだけ作り、あとは同じ引数を返す）
- 設定フォルダごと切り替えるなら `{"env": {"QGIS_CUSTOM_CONFIG_PATH": ...}}` でもよい
- **`qgis_command` で間に launcher（バッチや Python のスクリプト）をはさむ場合は、
  `args` ではなく `env` で渡す**。足した `args` は launcher の引数として読まれ、QGIS
  まで届かない（launcher が知らない引数として断ることもある）。環境変数は launcher を
  通って QGIS まで引き継がれるので、`QGIS_CUSTOM_CONFIG_PATH` で新しいプロファイルを
  指す

QGIS 3.34 で、session・起こし直し・`fresh_qgis` のそれぞれが別の新しい
プロファイルで起き、有効なプラグインがコアと `qgis_puppeteer` だけになることを
確かめている。

### 10. 完全制御が必要な場合: fixture オーバーライド

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

QGIS Puppeteer プラグインを有効化した QGIS を起動しておく。Hub は自動 spawn。

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

Worker は失敗時に `{"success": false, "error": "<code>", ...}` を返す。pytest の
`E2EAutomationClient.click_widget()` / `set_widget_value()` と Locator の操作は、
これを **`WidgetOperationError`** にする（`error` に code、`widget` に対象の要約、
`result` に戻り値全体、#23）。MCP の `qgis_click_widget` / `qgis_set_widget_value`
は tool error にする。これまでは失敗の dict がそのまま返り、戻り値を見ないと
押せていない・入っていないのに気付かなかった。

| `error` コード | 発生 RPC | 意味 | Locator での扱い |
|---|---|---|---|
| `widget_not_found` | 両方 | scope 内に該当 widget が無い | `_wait_actionable` が `exists=False` と見て poll を継続、timeout で `WidgetNotActionableError` |
| `selector_ambiguous` | 両方 | 複数マッチ + `index` 未指定（strict mode） | poll を続けず即 `SelectorAmbiguousError` |
| `widget_disabled` | 両方 | `widget.isEnabled() == False` | poll で待機。timeout 内に enabled になれば成功 |
| `widget_readonly` | set のみ | `isReadOnly() == True` の input への書き込み | `fill()` は `editable=True` を auto-wait |
| `widget_not_clickable` | click のみ | `QAbstractButton` / `QAction` / `click()` 持ちのいずれでもない | `WidgetOperationError` |
| `combo_item_not_found` | set のみ | `QComboBox` に該当テキストが無い（`available` に候補） | `WidgetOperationError` |
| `tab_not_found` | set のみ | `QTabWidget` に該当タブが無い | `WidgetOperationError` |
| `list_item_not_found` | set のみ | `QListWidget` に該当アイテムが無い（完全一致） | `WidgetOperationError` |
| `index_out_of_range` | set のみ | 番号が範囲外（`count` に数）。ComboBox / Tab / List / View | `WidgetOperationError` |
| `invalid_value` | set のみ | 値の形が違う（`expected_form` に形）。下の「値の形」 | `WidgetOperationError` |
| `value_not_applied` | set のみ | 入れたあとに読み直した値（`value_after`）が頼んだ値と違う。Qt が黙って丸めた・切り詰めた・無視した | `WidgetOperationError` |
| `unsupported_widget_type` | set のみ | 値設定に未対応の widget クラス | `WidgetOperationError` |
| `set_value_failed` | set のみ | setter 内部で例外（`message` フィールドに詳細） | `WidgetOperationError` |
| `row_not_found` / `row_ambiguous` | set / click | 表・ツリーのセルの文字に当たる行が無い / 複数ある（`rows` に番号）。`column` で絞るか `row` で指す | `WidgetOperationError` |
| `row_hidden` | set / click | `setRowHidden` で隠した行を `row` で指した | `WidgetOperationError` |
| `column_not_found` | set / click | `column` の見出しの名前か番号が無い（`available` に見出し） | `WidgetOperationError` |
| `cell_needs_a_view` | click のみ | 表・ツリー・リストでない widget に `cell` を付けた | `WidgetOperationError` |
| `no_model` | set のみ | View に model が未設定 | `WidgetOperationError` |

### 値の形と、入ったあとの確かめ（`qgis_set_widget_value`）

| widget | 渡す値 |
|---|---|
| `QLineEdit` / `QTextEdit` / `QPlainTextEdit` | 文字 |
| `QComboBox` / `QTabWidget` / `QListWidget` | 項目の文字（完全一致）か番号 |
| `QTableWidget` / `QTableView` / `QTreeWidget` / `QTreeView` / `QListView` | 行の番号、または `{"row": N}` / `{"text": セルの文字, "column": 見出しの名前か番号}`（下の「表の行」） |
| `QCheckBox` / `QRadioButton` | `true` / `false`、または文字の `"true"` / `"false"` |
| `QSpinBox` | 整数（`3.7` は断る。黙って切り捨てるため） |
| `QDoubleSpinBox` | 数 |
| `QDateEdit` | `"yyyy-MM-dd"` |
| `QTimeEdit` | `"HH:mm:ss"` |
| `QDateTimeEdit` | `"yyyy-MM-ddTHH:mm:ss"` |
| `QgsFileWidget`（QGIS のファイル・フォルダ選択） | パスの文字（1 つ）。`setFilePath()` で入れ、`filePath()` で読み直す。まだ無い場所も入れる。区切り（`\` と `/`）や末尾の区切りの違いは失敗にしない |

- 入れたあとの値を `value_after` に返す（`prior_value` と同じ形）
- 頼んだ値と違えば `value_not_applied`（範囲外の数・日付が丸められた、`maxLength`
  で切り詰められた、グループの中で排他のラジオボタンが外れなかった、など）。
  `verify=False`（Locator の `fill` / `select` / `check` / `uncheck` も受ける）で
  比べない
- QGIS やホストの部品（Qt のクラスの派生）も同じように比べる。値を自分で直す欄
  （表示用に日付を整形する欄など）は `value_not_applied` になるので、頼む値を
  整形後のものにするか、その欄だけ `verify=False` を付ける。どの部品が値を直すかの
  一覧は持たない（部品の増減や版ごとの違いに追われるため）。ホストの部品の知識は
  利用側の conftest に置く
- `QLineEdit.setText` は validator を通さない。validator がある欄は、入った文字が
  受け付けられる形か（`acceptable_input`）を添える

#### 表の行（`QTableWidget` / `QTableView` / ツリー / リスト）

- 行は番号か、`{"text": セルの文字, "column": 見出しの名前か番号}` で指す
  （`column` を省くと全列から探す。文字は表示値との完全一致）。複数の行に当たれば
  `row_ambiguous`（当たった行の番号付き）なので、`column` で絞るか番号で指す
- **行全体が選ばれる**（`QTableView.selectRow()` 相当。`selectionBehavior` に
  関わらず）。`currentChanged` と `selectionChanged` が人の操作と同じ形で出るので、
  行の選択で動くホストの処理（選んだ行の地物に地図を移す、など）が進む
- 行の番号は**今、画面に並んでいる順**（並べ替えた `QTableWidget` なら並べ替え後の
  行。`QTableView` に代理のモデルがあれば代理のモデルの行）
- `setRowHidden` で隠した行は、文字の候補から外す。番号で指せば `row_hidden`
- ツリーは一番上の階層だけ
- 読み直し（`value_after`）は選ばれた行の番号
- パスワード欄は `prior_value` / `value_after` / `expected` を伏せる（`masked`）
- `wait=False` のときは、入れる前に分かる失敗（読み取り専用・範囲外・候補に無い・
  形の違う値）だけを返す。入れたあとの失敗は `qgis_get_recent_exceptions` に残る

**重要**: `qgis_click_widget` を直接呼ぶとき disabled な widget は **即座に
`widget_disabled` で返り、`click()` は呼ばれない**。リトライしないので、
非同期で enabled に変わるのを待ちたい場合は Locator (`click()`) を使う。

### scope の挙動

- **`modal`**（既定）: `activeModalWidget()` 配下を検索。なければ
  `activeWindow()` にフォールバック（曖昧指定に寛容）
- **`active_window`**: `activeWindow()` 配下のみ
- **`popup`**: 開いているポップアップ（`activePopupWidget()`。`QMenu.exec()` の
  メニューなど）。メニューはモーダルではないので `modal` では届かない。項目は
  `{"class": "QAction", "text": ...}` で指す（下の「ポップアップのメニュー」）
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

# 開いている右クリックメニューの項目
{"scope": "popup", "class": "QAction", "text": "削除"}
```

### ポップアップのメニュー（`QMenu.exec()`）

右クリックのメニューやツールボタンのメニューを `QMenu.exec()` で出すと、選ばれるか
閉じられるまで呼び出しが返らない。メニューはモーダルではないので、`scope="popup"`
で扱う。

- `qgis_snapshot_ui` の `active_popup` に、開いているポップアップが出る。メニュー
  なら `menu_items` に項目（`text` / `enabled` / `checkable` / `checked` /
  `separator` / `submenu`）が並ぶ
- 項目を click すると、人が選んだのと同じ形で選ばれ、`exec()` がその項目を返す
  （`QAction.trigger()` では返らないので、選んだ項目で分岐する処理が進まない）。
  下位メニューを持つ項目なら、それが開く
- 項目の文言は、`&` と末尾の `(&N)` を除いても当てる（`"削除"` で「削除(&D)」に
  当たる。snapshot 経由の `wait_for_widget` でも同じ）
- メニューを開く操作そのものが `exec()` で止まるので、開くほうは `wait=False` で
  行う。右クリックの操作は無いので、右クリックのメニューは `qgis_execute_python`
  のコードの中で `QTimer.singleShot(0, ...)` と積んで `QContextMenuEvent` を送る

```python
# メニュー付きのツールボタンを押してメニューを開き、項目を選ぶ
qgis.locator({"object_name": "exportButton"}).click(wait=False)
qgis.wait_for_popup()  # 出るまで待つ（題名で絞るなら wait_for_popup("レイヤ")）
qgis.locator({"scope": "popup", "class": "QAction", "text": "削除"}).click()
```

`wait_for_popup(title_contains=None)` はポップアップが出るまで待ち、`active_popup`
（メニューなら `menu_items` 付き）を返す。`wait_for_popup_closed()` は閉じるまで待つ。
どちらも `wait_for_modal` / `wait_for_modal_closed` と同じ形（#27）。メニューの
`title` は `QMenu.title()`（dialog handler の predicate の `title` と同じ）。
`scope="popup"` の Locator の操作は auto-wait があるので、項目を押すだけなら
`wait_for_popup` は無くても通る。出たことを確かめたいとき、項目の一覧を見て分岐する
ときに使う。

### 窓（ダイアログ）を待つ

窓を待つ専用の道具は持たず、今ある道具で書く:

| したいこと | 書き方 |
|---|---|
| 開いている窓の一覧 | `qgis.snapshot_ui()` の `visible_dialogs`（モードレス）と `active_modal` |
| 題名で、モーダルが開くのを待つ | `qgis.wait_for_modal("保存")`（部分一致） |
| 題名で、モードレスの窓が開くのを待つ | `qgis.wait_for_widget({"title": "道路 - 地物属性", "scope": "any"})`（題名は完全一致） |
| モーダルが閉じるのを待つ | `qgis.wait_for_modal_closed()` |
| ある窓が閉じるのを待つ | `qgis.wait_for_widget({"title": ...}, visible=False)` |

決まった項目を毎回選ぶなら、dialog handler でも答えられる（「Dialog handler」の
「ポップアップに答える」）。

---

## Locator API

`pytest-qgis-puppeteer` が提供する Playwright 風の Locator。
`qgis.locator(selector)` で生成。state を持たないので使い回し OK。

### 操作系メソッド（auto-wait 内蔵）

| メソッド | 用途 | 待機条件 |
|---|---|---|
| `click()` | クリック。`kind="right"` で右クリック、`cell=` で表の行のセル | actionable |
| `dblclick()` | ダブルクリック（`cell=` で表の行のセル） | actionable |
| `fill(value)` | LineEdit / TextEdit / SpinBox 等への書き込み | actionable + **editable** |
| `select(value)` | ComboBox / Tab / List の選択 | actionable |
| `check()` | CheckBox / RadioButton を True に | actionable |
| `uncheck()` | 同 False に | actionable |

どれも `wait=False` を受ける。押すと確認を `exec()` で出す操作を、待たずに
QGIS 側へ積んですぐ返す（[Dialog handler](#dialog-handler) の「確認を出す操作は
`wait=False` と組み合わせる」）。

#### ダブルクリック・右クリック・セルのクリック

`click(kind=...)` の `kind` は `"left"`（既定）/ `"double"` / `"right"`。`dblclick()` は
`kind="double"` の糖衣。既定の左クリックは今までどおり `click()` / `trigger()` で押し、
それ以外（と `cell` 付き）は Qt のマウスのイベントを widget（表・ツリーなら viewport）に
送る。

- `cell` は表・ツリー・リストの行（「表の行」と同じ `{"row": N}` /
  `{"text": ..., "column": ...}`）。そのセルの位置に送るので、ダブルクリックの最初の
  押しで行が選ばれ（`currentChanged`）、それから `doubleClicked` /
  `cellDoubleClicked` が出る。戻り値に `cell: {"row", "column"}` が付く
- `kind="right"` は右ボタンの押し・離しのあと `QContextMenuEvent` も送るので、
  `customContextMenuRequested` が出る。開いたメニューは `exec()` で止まるので、
  `wait=False` で積み、`scope="popup"` で項目を選ぶ

```python
results = qgis.locator({"object_name": "resultTable"})
results.select({"text": "大通り", "column": "名前"})  # 行を選ぶ（地図が移る）
results.dblclick(cell={"text": "大通り", "column": "名前"})  # フォームを開く
results.click(kind="right", cell={"row": 0}, wait=False)  # 右クリックのメニュー
qgis.locator({"scope": "popup", "class": "QAction", "text": "削除"}).click()
```

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

### リスト・表・ツリーの中身

`qgis_snapshot_ui` / `snapshot()` は、リスト・表・ツリーの中身も返す（モデルの表示値）。

| widget | フィールド |
|---|---|
| `QListWidget` / `QListView` | `items`（文字列の list）、`current_row`（選択が無ければ `null`） |
| `QTableWidget` / `QTableView` / `QTreeWidget` / `QTreeView` | `columns`（見出し）、`rows`（セルの 2 次元 list）、`current`（`{"row", "column"}` か `null`） |
| どれも | `row_count`（全体の行数） |

- 返すのは先頭 **100 行・50 列**まで。超えた分は返さず `"truncated": true` を付ける
  （表は `column_count` も）。属性テーブルのような大きな表で応答が膨らまないように
- ツリーは一番上の階層だけ（子の行は返さない）
- 見出しを隠している表・ツリー（QGIS のレイヤパネルなど）には `columns` が付かない
- 値は、文字列・数値・真偽値はそのまま、NULL は `null`、日付は ISO 形式（`"2026-10-20"`）、
  それ以外は文字列
- 中身は伏せない。パスワード欄（下）と違い、モデルの値には伏せる手がかりが無い
- QGIS の `QgsAttributeTableView` などは、QGIS が Python に公開しているクラス名
  （`QTableView` など）で出る。`object_name` で探すとよい

それ以上（子の行、101 行目以降）が要るときは `execute_python` でモデルを読む。

### パスワード欄は文字を返さない

`echoMode` が `Normal` 以外の `QLineEdit`（`Password` / `PasswordEchoOnEdit` /
`NoEcho`）と、QGIS の `QgsPasswordLineEdit` は、入っている文字を外に出さない。
テストや人が入れたパスワードが、MCP の応答（エージェントの文脈）や診断バンドルに
残らないようにするため。

- `qgis_snapshot_ui` / `snapshot()`：`text` を返さず `"masked": true` を付ける。長さも出さない
- `get_text()` / `get_value()`：`None`
- click / set_value / actionability の結果に入る widget の要約にも `text` は入らない
- selector の `text` は、パスワード欄の中身とは一致しない
- `qgis_set_widget_value` は入れられる。戻り値の `prior_value` / `value_after` は `null`、`masked` は `true`（`value_not_applied` で失敗しても、頼んだ値と入った値は返さない）
- 入れた値（`value`）は、pytest の `actions.jsonl` と MCP の操作記録では `"***"` に置き換える

中身を確かめる必要があるテストは、`execute_python` で明示的に読む
（`_result = dlg.findChild(QLineEdit, "password").text()` など）。そのコードと
戻り値は記録に残るので、本物のパスワードには使わない。

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
`activePopupWidget()` / `activeModalWidget()` を polling し、predicate にマッチ
すれば action を実行。
action は Qt のイベントループに積んで行う（action の中でホストのコードが
さらに確認を出しても、見回りが止まらない）。

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
- `action`: `"accept"` / `"reject"` / `"close"` / `"button"` のいずれか
- `button` (`action="button"` のとき必須): 押すボタン。標準ボタン名（`"Ok"` /
  `"Yes"` / `"No"` / `"Cancel"` / `"Save"` / `"Discard"` など）か、ボタンの文言
- `once` (optional): True で 1 度発火したら自動 unregister

### `accept` は QMessageBox の「OK」ではない

`accept` / `reject` は `QDialog.accept()` / `reject()` を呼ぶだけで、
`QMessageBox` ではどのボタンが押されたか（`clickedButton()`）が決まらない。
そのため `exec()` や `QMessageBox.question()` の戻り値が `Ok` / `Yes` にならず、
「OK のときだけ進む」処理は進まない。押されたボタンで分岐する確認には
`action="button"` を使う:

```python
automation_client.call("qgis_register_dialog_handler", {
    "name": "confirm-delete",
    "predicate": {"title": "削除の確認"},
    "action": "button",
    "button": "Yes",
})
```

- `QMessageBox` のほか、`QDialogButtonBox` を持つダイアログでも押せる
- 標準ボタンの文言は QGIS の言語で変わる（日本語では「はい(&Y)」）ので、標準
  ボタンは名前（`"Yes"`）で指すのがよい。文言で指すときは `&` と末尾の `(&N)` を
  除いて比べる（`"いいえ"` で「いいえ(&N)」に当たる）
- ボタンが見つからなければ何もしない（確認は開いたまま）。その失敗は
  `qgis_get_recent_exceptions` に `QueuedOperationFailed` として残り、
  `qgis_fail_on_uncaught_exception`（既定 ON）でテストが落ちる

### 確認を出す操作は `wait=False` と組み合わせる

押すと確認を `exec()` で出すボタンを普通に click すると、確認が閉じるまで
click が返らず、時間切れになる（操作は QGIS の GUI スレッドでその場で行うので、
ホストのコードが `exec()` で止まると応答を返せない）。`wait=False` を付けると、
操作を QGIS 側に積んですぐ返す（戻り値に `queued: true`）。確認には dialog
handler で答える:

```python
automation_client.call("qgis_register_dialog_handler", {
    "name": "confirm-delete",
    "predicate": {"title": "削除の確認"},
    "action": "button",
    "button": "Yes",
    "once": True,
})
qgis.locator({"object_name": "deleteButton"}).click(wait=False)
```

- `wait=False` を受けるもの: `click_widget` / `set_widget_value` /
  `select_features` / `set_canvas_extent`（pytest のクライアント・MCP とも）と、
  Locator の `click` / `fill` / `select` / `check` / `uncheck`
- widget が見つからない・無効・読み取り専用などは、その場で確かめて返す。
  積んだ処理の中での失敗（候補に無い値など）は戻り値ではなく
  `qgis_get_recent_exceptions` に残る
- `set_widget_value` の `prior_value` は積む前の値。`select_features` は
  `selected_count` を返さない
- 積んでから動くまでの間に対象の widget が消えたら、触らずに失敗として残す
  （QGIS が作った widget は、消えても Python 側が気付かず、触ると QGIS が落ちる）
- 積んだ処理は、呼び出しが返ったあとで動く。結果は `get_value()` や
  `wait_for_widget` などで確かめる
- `qgis_execute_python` のコードから確認を出す処理を呼ぶときも同じで、
  コードの中で `QTimer.singleShot(0, 処理)` と積めば、確認を待たずに返る

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

### ポップアップに答える

開いているメニューなどのポップアップには、predicate に `"popup": true` を書いた
handler だけが答える（`{}` などの何にでも当たる handler が、コンボボックスの
ドロップダウン（これもポップアップ）まで閉じないように）。`title` はメニューの題名。

- `action="button"`: `button` の文言の項目を選ぶ（文言の比べ方はボタンと同じ）
- `action="close"`: メニューを閉じる（`exec()` は何も返さない）

```python
automation_client.call("qgis_register_dialog_handler", {
    "name": "layer-menu",
    "predicate": {"popup": True, "title": "レイヤ"},
    "action": "button",
    "button": "削除",
})
```

### ネイティブのファイルダイアログ

Windows で `QFileDialog.getOpenFileName` などを既定のまま呼ぶと、OS のダイアログが
出る。Qt のウィジェットではないので、UI の一覧にも dialog handler にも見えず、
答えられない。pytest を `QPUPPETEER_QT_DIALOGS=1` を付けて回すと、pytest が
起こした QGIS（launch token が付く）で、QGIS Puppeteer プラグインが
`Qt.AA_DontUseNativeDialogs` を立て、Qt のダイアログ（`QFileDialog`）になる。
人が普通に起こした QGIS は変わらない（見た目と挙動が変わるので、オプトイン）。

Qt のダイアログなら、ファイル名を入れて「開く」を押せる:

```python
# ファイルを選ぶダイアログを開く操作（exec() で止まるので wait=False）
qgis.locator({"object_name": "importButton"}).click(wait=False)
qgis.locator({"object_name": "fileNameEdit"}).fill("data.gpkg")  # QFileDialog の欄
automation_client.call("qgis_register_dialog_handler", {
    "name": "open-file",
    "predicate": {"class": "QFileDialog"},
    "action": "button",
    "button": "Open",  # 保存のダイアログなら "Save"
    "once": True,
})
```

ファイル名の欄は、そのダイアログのフォルダからの相対か、絶対パス。

QGIS のファイル・フォルダ選択の部品（`QgsFileWidget`）なら、ダイアログを開かずに
`fill(パス)` で直接入れられる（#25）。snapshot には `file_path` と `storage_mode`
（`GetFile` / `GetDirectory` / `SaveFile` / `GetMultipleFiles`）が出る。

### 注意点

- 同じ modal instance を 2 回処理しない（modal ごとに振るキーで見る。action で
  閉じない modal にも 2 度目は発火しない）
- handler を登録したときは、今開いている modal も見直す（登録より先に開いた
  確認にも効く）
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
  ため、この仕掛けは QGIS Puppeteer プラグインには**あえて組み込んでいない**（静かな
  過小報告を避ける）。
- **`source` / `[paths]`**: 対象は自分の package を指定（qgis-puppeteer 自体ではなく）。
  QGIS 内とランナーでパスが違うなら `[paths]` で alias して combine をマージ。
- **外部 QGIS / dev モード**: 起動中 QGIS を再利用（`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`）
  したり独自 `qgis_command` wrapper を使う場合は env 継承が自前管理になる。その QGIS を
  `COVERAGE_PROCESS_START`（と `COVERAGE_FILE`）付きで起動するか、wrapper で forward する。

詳細手順とテンプレ本体は [`examples/coverage/README.md`](../examples/coverage/README.md)。
---

<a id="map-canvas-input"></a>

## 地図のキャンバスの操作（マウスとキー）

QGIS のマップツール（作図・移動・選択など）は、キャンバスが受けたマウスとキーの
イベントで動く。`qgis.canvas()` で、それを人の操作と同じ形で送る（#26）。

```python
canvas = qgis.canvas()                       # メインのキャンバス
qgis.locator({"object_name": "mActionAddFeature", "root_object_name": "QgisApp"}).click()
canvas.click((-40800.0, 11200.0))            # 頂点を置く（地図の座標）
canvas.click((-40200.0, 11200.0))
canvas.click((-40900.0, 11900.0))            # 間違えた
canvas.press_key("Backspace")                # 最後の頂点を取り消す
canvas.click((-40200.0, 11800.0))
canvas.right_click((-40200.0, 11800.0))      # 作図を終える
canvas.press_key("Escape")                   # 作図を中止する
canvas.wheel((-40500.0, 11500.0), 1)         # その位置で拡大
```

| 道具 | 送るもの |
|---|---|
| `click(point, button=)` | その位置へ動かす → 押す → 離す（`right_click` は右ボタン） |
| `dblclick(point)` | press → release → doubleClick → release |
| `press(point)` / `release(point)` / `move(point)` | 1 つずつ（長押しや、押したまま別の操作をするとき） |
| `drag(points, steps=5)` | 最初の点で押し、点から点へ `steps` 回ずつ動かして、最後の点で離す |
| `wheel(point, delta)` | ホイールを `delta` ノッチ（正で奥へ＝拡大） |
| `press_key(key, action=)` | キー（`"Escape"` / `"Backspace"` / `"Return"` / `"Space"` / `"F2"` など Qt のキーの名前か 1 文字）。`action="press"` / `"release"` で押しっぱなし（スペースキーを押している間のパンなど） |
| `type_text(text)` | 文字を 1 文字ずつ（既定はフォーカスのある widget へ）。マップツールがキャンバスの上に出す数値の欄など、キーのイベントでしか入らない欄用 |

- 座標は**地図の座標**で、既定はキャンバスの CRS。`qgis.canvas(crs="EPSG:4326")` で
  別の CRS、`pixel=True`（`canvas_mouse`）で viewport の画素
- 画素への変換は、**実際に送る時点**の地図で行う（積んだあとに地図が動いても、その
  時点の位置に送る）
- click の前にその位置へ動かすので、スナップの表示や輪郭の線、移動のツールで付いて
  くる地物が、人の操作と同じに更新される
- 戻り値の `map_point` は、**マップツールが受け取る地図の点**。画素に丸めるので、
  頼んだ点から最大で半画素（`map_units_per_pixel` の半分）ずれる。作った地物の座標を
  比べるときはその幅で比べるか、スナップに頼る。`map_tool` は今のマップツールの
  クラス名（思ったツールになっていないときの切り分け用）。`extent` は送ったあとの範囲
- どの操作も `modifiers=["shift", "ctrl", "alt"]` と `wait=False`（#21）を受ける
- 地図の外（viewport の外）の点は、送る前に `point_outside_canvas` で断る（人には
  押せないので）。失敗は `WidgetOperationError`
- ダイアログの中の小さなキャンバスなどは `qgis.canvas({"object_name": ...})` で指す
- キーの送り先は `target` で変えられる（`"canvas"` 既定 / `"focus"` / selector）。
  表の行を選んで Enter、なども `press_key("Return", target={"object_name": ...})` で送れる

### 作図を終えたあとのフォーム

QGIS 3.34 の標準の作図ツールでは、作図を終えたあとの属性のフォームは**モーダルでは
ない**（`visible_dialogs` に出る）ので、dialog handler は答えない。フォームの OK を
Locator で押す:

```python
form = qgis.wait_for_widget({"title": "道路 - 地物属性", "scope": "any"})
qgis.locator({"title": form["title"], "scope": "any"}).locator(
    {"class": "QPushButton", "text": "OK"}).click()
```

ホストのマップツールが作図を終えるところでフォームを `exec()` で開くなら、右クリックを
`wait=False` にして、dialog handler（`action="button"`）で答える。

### MCP

`qgis_canvas_mouse` / `qgis_canvas_key` で同じことができる。エージェントは
`qgis_get_canvas_extent` で範囲を見てから座標を決める。

---

<a id="map-image-snapshot"></a>

## 地図の画像スナップショット（`qgis_render_map`）

キャンバスの描画を画像で比べたいときに使う。`qgis_screenshot` はキャンバスに
**今映っているもの**を保存するので、ウィンドウの大きさや画面の表示スケールが
変わるだけで画像が変わり、比較の基準にならない。`qgis_render_map` はキャンバスの
見た目に依存せず、範囲・CRS・ピクセルサイズ・DPI・レイヤを指定して描画する。

| | `qgis_screenshot` | `qgis_render_map` |
|---|---|---|
| 写るもの | キャンバスに今映っている画像 | 指定した条件で描き直した地図 |
| サイズ | キャンバスの大きさ（表示スケール込み）。指定できない | `width` / `height` / `dpi` で固定 |
| 範囲・CRS | キャンバスのまま | `extent` / `crs` で指定（省略時はキャンバス） |
| 用途 | 失敗時の様子見、診断バンドル | 画像スナップショットの比較 |

```python
def test_parcels_style(automation_client, tmp_path):
    out = tmp_path / "parcels.png"
    res = automation_client.render_map(
        output_path=str(out),
        extent=(139.70, 35.65, 139.72, 35.67),
        crs="EPSG:4326",
        width=800,
        height=600,
        dpi=96,
        layers=[parcels_layer_id],
        background_color="#ffffff",
    )
    assert res["render_errors"] == []   # 画像には出ない描画失敗
    # 同じ環境ならバイト単位で一致する。許容誤差つきで比べたいなら
    # pytest-image-snapshot などの比較ツールに out を渡す
    assert out.read_bytes() == Path("snapshots/parcels.png").read_bytes()
```

パラメータ（すべて省略可）:

| 名前 | 既定 | 説明 |
|---|---|---|
| `extent` | キャンバスの範囲 | `[xmin, ymin, xmax, ymax]`（`crs` の座標）。出力の縦横比に合わせて**広げられる**（地図は歪めない）。実際に描いた範囲は戻り値の `extent`。キャンバスが無いときは `layers` の範囲（無ければ表示中のレイヤの範囲）。範囲が空なら `success: false` |
| `crs` | キャンバスの CRS | `"EPSG:3857"` など。`extent` を省いて `crs` だけ変えると、元の範囲をその CRS に変換して使う |
| `width` / `height` | 800 / 600 | ピクセル。両方指定するか両方省く。1 辺 10000 まで |
| `dpi` | 96 | シンボルやラベルの大きさに効く。PNG の DPI メタデータにも書く |
| `layers` | キャンバスに表示中のレイヤ | レイヤ ID のリスト（先頭が最前面） |
| `background_color` | キャンバスの背景色 | `"#RRGGBB"` / `"#AARRGGBB"` / 色名 |
| `antialiasing` / `draw_labels` / `draw_selection` | すべて true | 描画フラグ。編集中レイヤの頂点マーカーは常に描かない |

戻り値: `path` / `width` / `height` / `dpi` / `crs` / `extent` / `layers` /
`render_ms` / `render_errors`（レイヤごとの描画エラー）。引数が不正なときは
`success: false` と `error`（MCP では tool error）。

MCP の `qgis_render_map` は、描画した PNG を結果に**画像として添える**
（`include_image=false` でパスだけ）。エージェントがその場で描画結果を見られる。

### 同じ画像にするための注意

同じ入力・同じ環境なら毎回同じ PNG になる（バイト単位で一致することを
`test_render_tools_integration.py` で確かめている）。そのために、レイヤを 1 つずつ
順に描く逐次の描画ジョブを使い、画面の表示スケール（devicePixelRatio）を 1 に
固定している。それでも次のものがあると画像は変わる:

- **スタイルを固定していないレイヤ。** QGIS は新しく作ったベクタレイヤに
  毎回ランダムな色を付ける。テストで作るレイヤには色を明示するか、`.qml` /
  プロジェクトでスタイルを与える
- **ネットワーク越しのレイヤ**（XYZ / WMS など）。タイルの取得結果が変わる
- `now()` など時刻に依存する式、シード無しのランダム配置
- **別の機械。** フォントやアンチエイリアスの実装差で一致しない。基準画像は
  環境（CI のイメージ等）ごとに持つ

描画が終わるまで Worker の GUI スレッドは待って止まる。重い描画の間は Hub への
応答が止まるので、一覧で一時的に `unresponsive` と出ることがある（描画が終われば
戻る）。

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

MCP からも `qgis_get_recent_exceptions` / `qgis_clear_recent_exceptions` で呼べる。

---

<a id="message-log"></a>

## QgsMessageLog の記録と判定

QGIS プラグインは、例外を捕まえて `QgsMessageLog.logMessage(..., Qgis.Critical)`
に流す書き方をよくする。これは `sys.excepthook` を通らないので、上の
[Uncaught exceptions](#uncaught-exceptions) では拾えない。画面は正常に見えるのに
裏ではエラーがログに出ている、を見逃さないために、Worker は QgsMessageLog
（QGIS の「ログメッセージ」パネルに出るもの）も記録している。

### 記録されるもの

Worker 側 `qgis_tools.message_recorder` が `QgsApplication.messageLog().messageReceived`
を購読し、直近 1000 件を次の形で持つ:

```python
{"seq": 42, "ts": "...", "level": "critical", "tag": "MyPlugin",
 "message": "failed to load ...", "source": "log", "thread": "MainThread"}
```

- `level` は `info` / `warning` / `critical` / `success` / `none`。重さの順は
  info（= success）< warning < critical。QGIS の数値（Success=3）では比べない
- **メッセージバーに出たもの**も入る。`QgsMessageBar` は表示したメッセージを
  「タイトル : 本文」でログにも書くので、それを `source: "message_bar"` として
  記録する（そのタグは翻訳されるので、日本語の QGIS では "Messages" ではない。
  絞り込みは `source` で）
- `seq` は通し番号。消去しても戻らないので、「前回以降の分」を `after_seq` で取れる
- 記録を始められるのは QGIS Puppeteer プラグインが読み込まれてから。名前順で
  それより前に読まれるプラグインの初期化中のメッセージは入らない

### pytest で落とす（opt-in）

```toml
[tool.pytest.ini_options]
qgis_fail_on_message_level = "critical"   # info / warning / critical。既定: 空（無効）
qgis_message_allowlist = [
    "Could not load the style",             # 本文に含まれれば除外（re.search）
    "OGR::^Warning 1: .*",                  # <タグ>::<正規表現>
]
```

`pytest.ini` / `tox.ini` なら `qgis_message_allowlist` は 1 行 1 ルールで並べる。
有効にすると、call の直前に記録を消去し、call の後に指定レベル以上のメッセージが
あれば **pass → fail** にする（既に fail ならセクションに追記）。流れは
uncaught exception と同じで、dev モードでは自動で skip される。

最初の `::` がタグの区切りなので、本文の `::`（`QgsVectorLayer::` など）は
`\:\:` と書く（`pyproject.toml` では `'QgsVectorLayer\:\:load'` のように単引用符の
文字列で）。`::` の前に正規表現の記号があると、取り違え防止でエラーになる。

レベルは `warning` か `critical` を勧める。`info` にすると、QGIS Puppeteer
自身がメッセージバーに出す登録の通知（「QGIS Puppeteer: <id>」。call 中に
再接続したとき）でも落ちる。

判定を有効にしているのに Worker に照会できなかったとき（Worker が古くて
コマンドを持たない等）は、テストは落とさず、結果に「could not run」の
セクションを付けて警告を出す。

テスト単位の除外はマーカーで:

```python
@pytest.mark.qgis_allow_message(r"slow provider", tag="WMS")
@pytest.mark.qgis_allow_message(source="message_bar")   # メッセージバーのものは無視
def test_x(qgis): ...
```

`pattern` / `tag` / `source` を組み合わせ、全部に合うメッセージを除外する。
レベル名の綴り違いはセッション開始時、マーカーの正規表現の誤りはそのテストの
setup でエラーになる（黙って判定が無効になることはない）。

失敗時の diagnostic bundle には、判定の有無に関わらず `qgis_messages.jsonl`
（記録されているメッセージ全部）が入る。

### QGIS の起動の間のメッセージ

テスト本体の直前に記録を消去するので、QGIS の起動からそこまでに出たもの
（プロジェクトの読み込みの失敗、プラグインの初期化のエラー、起動の間の処理の
ログ）は、上の判定に入らない。そこで、その QGIS を初めて使うテストの `qgis`
fixture の準備で、それまでの分を「起動の間のメッセージ」として取っておく。
session の QGIS、起こし直した QGIS、`fresh_qgis` の QGIS のそれぞれで 1 回。

- **読む**：fixture `qgis_startup_messages`（そのテストが動く QGIS の分。
  `get_recent_messages()` と同じ形）。session の QGIS の分は、`qgis_process`
  fixture の値（`SessionQgis`）の `startup_messages` にも入る

  ```python
  def test_auto_login_happened(qgis_startup_messages):
      assert any(m["message"] == "Logged in automatically" for m in qgis_startup_messages)
  ```

- **判定**：`qgis_fail_on_message_level` が有効なら、同じレベルと ini の
  `qgis_message_allowlist` で判定する。引っかかったものがあっても、**どのテストも
  落とさない**（そのテストのせいではないため）。代わりに、pytest の最後のまとめに
  「QGIS startup messages」の節を出して、テストが全部通っていても**終了コードを
  失敗（1）にする**。CI はここで落ちる。マーカー（`qgis_allow_message`）は効かない
  （どのテストのものでもないため）。外すなら ini の許可リストに書く
- 起動の間の分には、QGIS の起動が終わった後、最初のテストの準備までに出たものも
  入る（起動の後もプロジェクトの読み込みが続くことがあるため）
- QGIS Puppeteer プラグインが読み込まれる前に出たメッセージは入らない
  （プラグインは名前順に読まれる）
- `qgis` を使わず `automation_client` だけを使うテストでは取らない。dev モードでは空

**ホストの準備を待ってから取る**: ホストのプラグインがプロジェクトを開いたあとに
ログイン画面を出すなど、テストに使えるまでに準備が要るときは、conftest で
フック `pytest_qgis_ready` を書く。上流は、その QGIS で起動の間のメッセージを取る
直前に呼ぶので、準備の間に出たメッセージも起動の間の分に入る。

```python
# conftest.py
def pytest_qgis_ready(config, client, instance_id, kind):
    """kind は "session"（最初の QGIS）/ "restart"（起こし直し）/ "fresh"。"""
    wait_until_logged_in(client)  # ホスト側の準備（例）
```

- その QGIS ごとに 1 回（session の最初、起こし直し、`fresh_qgis` のそれぞれ）。
  `client` はその QGIS に届くよう切り替えてあり、ここでの呼び出しはテストの
  操作記録に入らない
- 例外を出すと、そのテストの準備のエラーになり、メッセージはまだ取らない。次に
  その QGIS を使うテストで、もう一度呼ばれる
- dev モードでは呼ばれない（起動の間はもう過ぎている）

自分で取ったメッセージを、テストと同じ規則で判定したいときは
`unexpected_messages` を使う:

```python
from pytest_qgis_puppeteer import unexpected_messages

bad = unexpected_messages(messages, request.config)                  # ini のレベルと許可リスト
bad = unexpected_messages(messages, request.config, item=request.node, min_level="warning")
```

`item` を渡すとそのテストのマーカーも効く。`min_level` を省くと ini の
`qgis_fail_on_message_level` を使い、それも無ければ `ValueError`。

### 手動 API / MCP

```python
qgis.get_recent_messages(min_level="warning", tags=["MyPlugin"])
qgis.get_recent_messages(sources=["message_bar"])
qgis.get_recent_messages(after_seq=last_seen)   # 前回以降の分
qgis.clear_recent_messages()
```

MCP からは `qgis_get_recent_messages` / `qgis_clear_recent_messages`。未捕捉の
例外も `qgis_get_recent_exceptions` / `qgis_clear_recent_exceptions` で見られる。
エージェントは操作の後にこの 2 つで「画面に出ない失敗」を確かめられる。
`qgis_get_recent_messages` の結果の `last_seq` を次の呼び出しの `after_seq` に
渡すと、増えた分だけが返る。

---

<a id="mcp-recording"></a>

## MCP の操作記録（探索を pytest に書き起こす元資料）

エージェントが MCP 経由で QGIS を操作して手順を探り、安定した手順を pytest の
E2E テストに書き起こす、という使い方のために、gateway は呼ばれたツールを
JSONL に記録できる。形式は pytest の診断バンドルの `actions.jsonl` と同じ
（`command` / `params` / `result` / `error` / `instance` / `duration_ms` /
`step_path`、加えて `source: "mcp"`）なので、同じ道具で読める。テストコードの
自動生成はしない（ADR-0002 §16）。記録は人やエージェントが書き起こすときの
材料。

```text
qgis_record_start(name="parcel-edit")      → {"recording": true, "path": ".../20261011-221500_parcel-edit.jsonl"}
qgis_record_step(name="レイヤを開く")       → 以降の記録の step_path が ["レイヤを開く"]
...（普段どおりに操作）
qgis_record_stop()                          → {"recording": false, "path": "...", "actions": 12}
```

- **全ツールが記録される**（`qgis_list_instances` のように Worker を呼ばない
  ものも含む）。`qgis_record_*` 自身は記録しない
- `params` は `None` を除いた引数。`qgis_execute_python` のコードも入る。
  パスワード欄に入れた `value` は `"***"` に置き換わる
  （[パスワード欄は文字を返さない](#パスワード欄は文字を返さない)）
- `result` は本文の JSON。2000 文字を超えるもの（UI スナップショット等）は
  `{"truncated": true, "chars": N, "head": "..."}`。画像は中身を書かず大きさだけ
- 失敗（tool error）は `error.message` に本文全体
- `instance` は明示の `instance` 引数、無ければ `qgis_use_instance` で選んだもの
- 1 行ずつ追記するので、gateway が落ちてもそこまでの記録は残る

**保存先はエージェントには選ばせない。** `QPUPPETEER_MCP_RECORD_DIR`（既定
`<TEMP>/qgis_puppeteer/recordings`）の下に `<日時>_<name>.jsonl` で作る。名前の
英数・`-`・`_` 以外は `_` になる。任意のパスを受け付けると、エージェントに
「どこにでも書ける」道具を渡すことになるため。

エージェントに頼まず、セッション全体を常に記録したいときは、gateway の環境変数
に `QPUPPETEER_MCP_RECORD=1` を立てる（`.mcp.json` の `env`）。起動時に
`session` という名前で記録を始め、終了時に閉じる。

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
#               launch_token=None, registered_seq=1, state="active", last_seen_ago=0.4),
#  InstanceInfo(instance_id="w-2f5e1c8b0d6a", label="B", pid=5678, project="...",
#               launch_token="lt-...", registered_seq=2, state="unresponsive",
#               last_seen_ago=47.2)]
# instance_id は pid 非依存の不透明 nonce（ADR-0005）。再起動で変わるので
# ハードコードせず label / @label / launch_token で参照する。
# state="unresponsive" は「QGIS は生きているが GUI スレッドが塞がっていて
# ハートビートに 20 秒以上応答していない」。呼び出しは受け付けられ、GUI が
# 空き次第処理される（Troubleshooting「instance が unresponsive」参照）。
# list_instances(include_disconnected=True) なら切断直後（grace 中）の entry も
# state="disconnected" と grace_expires_in 付きで見える。
# project は QGIS 側でプロジェクトを開く・閉じる・別名保存するたびに更新される
# （iface.projectRead / QgsProject.cleared / fileNameChanged → update_info）。
# projectRead は読み込み**完了後**に発火するので、project が入っていれば
# レイヤも出そろっている。使えるまで待つなら qgis_wait_ready / wait_for_ready
# （レイヤを触るなら require_project=True）。

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

なお Hub は WS ping/pong で active の応答性を監視している。20 秒 pong が
無い entry は `unresponsive` になるが、切断扱いにはしない（QGIS の GUI
スレッドが塞がっているだけのことが多いため。Troubleshooting「instance が
`unresponsive`」参照）。kill -9 等で TCP が half-open のまま残った旧 entry に
同 label で再登録すると、Hub が旧 entry へ ping を撃ち、2 秒以内に pong が
無ければ SUPERSEDE で継承する（`reject` 既定でも待たずに済む）。

---

## Permissions

`qgis_execute_python` は QGIS 内で任意の Python コードを実行するため、
**3-tier 権限システム** で保護されている:

| Tier | 挙動 |
|---|---|
| **whitelist** | 既知の安全コード（`QgsProject.instance().fileName()` 等の参照系）→ 即実行 |
| **session** | テスト session 内で 1 度許可されたパターン → 即実行 |
| **confirm** | 上記以外 → **実行せず** `requires_confirmation` と危険度分析を返す |

confirm は「QGIS 側で止めて人に聞く」仕組みではない。**QGIS に確認 UI は無く**、
`qgis_execute_python` は実行せずに `requires_confirmation: true` と危険度分析を
返して終わる。そこから先は呼び出し側の役目になる:

- **Claude（MCP）**: 内容をユーザーに示して承認を取り、`qgis_execute_with_permission`
  に `once`（この 1 回だけ）か `session`（この QGIS が動いている間）を渡して再実行する。
  **`always` は MCP からは選べない** — ホワイトリストへの永続追加は人間の操作に限る。
- **pytest / スクリプト**: ラッパが `ConfirmationRequiredError` を投げる。呼び出し元が
  本人のコードである前提なので、`always` も使える。

つまり、実際に人へ確認を出しているのは **MCP クライアント側の承認 UI**（Claude Code
なら呼び出しごとの確認）であり、qgis-puppeteer 自身は「何を実行しようとしているか」を
判断材料として返すところまでを担う。恒久的に許可を足したいときは
`<project root>/.claude/qgis_whitelist.json` を直接編集する。

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
| `QPUPPETEER_HUB_LOG_FILE` | `<TEMP>/qgis_puppeteer.spawn.log` | Hub subprocess の stdout/stderr 出力先 |
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

### MCP gateway

| 変数 | 既定 | 用途 |
|---|---|---|
| `QPUPPETEER_GATEWAY_LOG` | `<TEMP>/qgis_puppeteer/gateway.log` | gateway のログファイル |
| `QPUPPETEER_MCP_RECORD` | unset | `1` で起動時から操作を記録する（[MCP の操作記録](#mcp-recording)） |
| `QPUPPETEER_MCP_RECORD_DIR` | `<TEMP>/qgis_puppeteer/recordings` | 操作記録の保存先 |

### coverage（標準 coverage.py 変数 — 参考）

qgis-puppeteer 独自ではないが、[E2E カバレッジ計測](#e2e-カバレッジ計測)で使う。
spawn 時に親 env が QGIS へ継承されるので、pytest 実行 env に立てれば QGIS 内
coverage が有効化される。

| 変数 | 用途 |
|---|---|
| `COVERAGE_PROCESS_START` | `.coveragerc` への絶対パス。QGIS 側の subprocess 計測を有効化 |
| `COVERAGE_FILE` | data file の出力先。cwd の違う QGIS とランナーで同じ場所に集約する用 |

---

## 開発者向け: Qt / QGIS 統合テスト

`uv run pytest` は Hub / Worker の Qt 配線を **通らない**。venv には PyQt5 も
qgis も無いので、`test_worker_integration` / `test_integration_hub_client` /
`test_hub_spawn_integration` / `test_worker_liveness_integration` /
`test_ui_tools_snapshot` は skip される（`5 skipped` の正体）。ハートビート・
再接続・Hub の立て直し・`update_info` のような「現場で壊れた」挙動は全部この層に
あるので、`hub.py` / `worker.py` / `plugins/qgis_puppeteer/qgis_plugin/` を触ったら push 前に
QGIS 同梱の Python で回す:

```bash
python scripts/qt_tests.py            # 上記 5 ファイル（約 1 分）
python scripts/qt_tests.py -k respawn -v
```

初回は QGIS の pip で `.qgis-site/` に pytest を入れる。`QGIS_ROOT` で
インストール先を指定できる。venv に PyQt5 を入れて済ませない理由: Windows の
PyQt5 wheel（5.15.11）と Qt（5.15.2）の版がずれていて、Hub subprocess が
client 切断時に heap corruption で落ちる（QGIS の Qt では起きない）。

CI の ubuntu ジョブには PyQt5 が dev 依存で入るので、同じテストが `uv run pytest`
で走る（`qgis` が要る `test_ui_tools_snapshot` を除く）。

`test_worker_liveness_integration.py` は実機検証で見つかった
シナリオをそのまま再現している: Hub kill → Worker が立て直して再登録、GUI を
28 秒止める → `unresponsive` のまま一覧に残り復帰、`update_project` →
`project` が入り basename で引ける、bye 無し切断 → `instance_disconnected`。

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

1. そのまま再起動してよい → Hub は旧 entry に ping を撃ち、2 秒以内に pong が
   無ければ死亡とみなして再登録を **SUPERSEDE** で通す（旧 entry は
   `qgis_list_instances` に `state: "unresponsive"` で出ている）
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

### `value_not_applied`

**症状**: `WidgetOperationError`（`error == "value_not_applied"`）。`result` に
`requested`（頼んだ値）・`expected`（入るはずだった値）・`value_after`（実際の値）。

**原因**: 入れたあとに読み直した値が違う。Qt が黙って変えた（範囲外の数・日付を
最小・最大に丸めた、`maxLength` で切り詰めた、`QDoubleSpinBox` の桁数で丸めた、
グループの中で排他のラジオボタンを外さなかった）か、値の変更に結ばれたホストの
処理が値を戻した。

**対処**:

1. `value_after` を見て、範囲・桁数・`maxLength` を確かめる。頼んだ値が間違いなら
   テストを直す
2. ホストが値を直すのが正しい動きなら、`verify=False` を付けて `value_after` を
   テストで確かめる

### Hub spawn 失敗

**症状**: QGIS 起動時に「QGIS Puppeteer: Hub startup failed」のメッセージバー

**原因の切り分け**:

1. `<TEMP>/qgis_puppeteer.spawn.log` を確認（Python 起動前の fatal がここに出る）
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
いない。または register 完了前に呼んだ。QGIS が動いているのにこれが出る
なら、プロセスが落ちて Hub の grace（60 秒）も過ぎたか、selector が違う。
GUI スレッドが塞がっているだけではこの error にならない（下の
「instance が `unresponsive`」を参照）。

**対処**:

1. `qgis_list_instances` で現在の Worker 一覧を確認
2. pytest なら `wait_for_ready()` を使って「使える」まで待つ（fixture 経由なら
   `hub_ready` / `spawn_qgis()` が内部で呼ぶ）
3. multi-instance のラベル衝突 → `QPUPPETEER_WORKER_LABEL` を変える

### `Error executing tool …`（本文が無い）

**症状**: MCP ツールが `Error executing tool qgis_list_instances` のように、
JSON 本文の無い一文だけで失敗する。

**原因**: gateway の中で想定外の例外が tool の外へ抜けた。MCP SDK はそれを
「クラッシュ」として扱い、**例外の型もメッセージも呼び出し側には渡さず**、
gateway の stderr にだけ記録する。呼び出し側からは何が起きたか分からない。

**対処**: gateway は全ツールを最後の砦で包んでいて、どこで例外が出ても
`code: "gateway_internal_error"` に例外の型・traceback の末尾・ログファイルの
場所を載せて返す。接続まわりの例外は 1 回だけ張り直して再試行する。

それでも本文なしの形が出る、または原因を追いたいときは **gateway のログ
ファイル**を見る。stderr は MCP クライアントが飲み込むが、同じ内容が
`%TEMP%\qgis_puppeteer\gateway.log`（`QPUPPETEER_GATEWAY_LOG` で変更可）に
残る。MCP SDK が「unexpected exception」として記録する traceback もここに入る。
Claude Code 自身のログ（`%LOCALAPPDATA%\claude-cli-nodejs\Cache\<project>\mcp-logs-qgis-puppeteer\*.jsonl`）
には呼び出しの成否しか無く、traceback は無い。

### `instance_disconnected`

**症状**: `error: "instance_disconnected"` が返る。`grace_expires_in` が付く。

**原因**: selector に一致する Worker は居たが、接続が切れて Hub が再接続を
待っている（grace 60 秒）。QGIS を閉じた直後、プラグイン再読込でプロセスが
落ちた直後、または 5 分以上 unresponsive だったので Hub 側から閉じた直後。
`instance_not_found` とは違い、**selector は合っている**。

**対処**:

1. QGIS が生きているなら自動再接続を待つ（`grace_expires_in` 以内なら
   instance_id も引き継がれる）。`qgis_wait_ready` で待てる
2. QGIS が落ちているなら起動し直す。同じ label / launch_token で登録されれば
   sticky はそのまま効く

### QGIS を起動した直後に呼び出しが通らない

**症状**: `qgis_list_instances` には出るのに最初の呼び出しが長く待たされる、
または timeout する。

**原因**: register はプラグインのロード時で、プロジェクトの読み込みはその後
GUI スレッドで走る。その間 Worker は `unresponsive` で、呼び出しはソケットに
溜まる。

**対処**: `qgis_wait_ready`（MCP）/ `wait_for_ready()`（client / pytest）を
先に呼ぶ。`state == "active"` になり、かつ軽い read-only の往復
（`qgis_get_canvas_extent`）が返るまで待つ。`qgis_wait_ready` は Hub がまだ
listen していない段階で呼んでも `timeout_s` の内で待つ（他のツールは
`hub_unreachable` を即返す）ので、QGIS 起動直後の最初の呼び出しにそのまま
使える。

pytest では `hub_ready` fixture と `spawn_qgis()` が内部で `wait_for_ready()`
を呼ぶので、テスト側で待つ必要はない。`wait_for_worker()` は「register 済みの
active が居る」までしか見ない（往復は確認しない）ので、明示的に使う場合は
その差に注意。

**ready ≠ プロジェクト読み込み完了**に注意。QGIS は起動直後、プロジェクトを
読み始める前に一瞬 GUI が空く。実測（39 レイヤ・リモート PostGIS）:

```
[ 10.4s] state=active
[ 11.2s] wait_ready が返る        ← GUI は空いている
[ 12.1s] レイヤ数 0
[ 31.0s] レイヤ数 39              ← 読み込み完了
```

レイヤを触るなら **`require_project=True`** を付ける。`project` は
`iface.projectRead`（読み込み完了後）で Hub に届くので、それが入ってから往復を
確認する。プロジェクトを開かない QGIS では永遠に満たされないので、その場合は
付けない。

### instance が `unresponsive` と出る / 呼び出しが返ってこない

**症状**: `qgis_list_instances` に `"state": "unresponsive"` で載っている。
呼び出しが長く待たされる、または timeout する。QGIS のウィンドウは固まって
見える。

**原因**: Worker の WebSocket は QGIS の **GUI スレッド** に載っている。
重いプロジェクトの読み込み、ネットワークドライブの走査、長い
`qgis_execute_python` など GUI スレッドを塞ぐ処理の間は Hub のハートビートに
pong を返せないので、20 秒で `unresponsive` になる。プロセスも接続も生きて
いて、**その間に送った呼び出しはソケットに溜まり、GUI が空いた時点で順に
処理される**。pong が戻れば `active` に復帰する。

**普通の起動でもこうなる。** 何も異常が無い起動で `last_seen_ago` は 17 秒台
まで伸びる（実測 17.4 秒、閾値まで 2.6 秒）。そこに呼び出しが 1 つ乗れば
超える。起動中の `unresponsive` は想定内で、Hub のログも INFO。

**対処**:

1. `last_seen_ago` を見る。伸び続けているなら GUI がまだ塞がっている。
   数十秒〜数分の読み込みなら待てば戻る
2. 起動直後なら `qgis_wait_ready`（MCP）/ `wait_for_ready()`（client / pytest）
   を先に呼ぶ。`active` になり往復が返るまで待つ
3. 5 分（`UNRESPONSIVE_CLOSE_SECONDS`）応答が無いと Hub はプロセス死亡
   （kill -9 の half-open）とみなしてソケットを閉じる。実は生きていた
   Worker は GUI が空いた時点で切断に気づいて自動再接続する。60 秒の grace
   内なら instance_id も引き継ぐ
4. 20 秒を超える `qgis_execute_python` は自分自身を `unresponsive` にする。
   害はない（次の pong で戻る）が、返るまで他の呼び出しも待たされるので、
   長い処理は Worker 側で QThread に逃がすか分割する

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

### confirm ゲートで弾かれる（dev モード）

**症状**: `execute_python` が `ConfirmationRequiredError` で失敗する（QGIS 側に
確認ダイアログは出ない。呼び出しはその場で返る）

**原因**: 信頼モードが OFF の QGIS で `execute_python` を呼んでいる

**対処**:

1. テストには `QPUPPETEER_TRUSTED_MODE=1` 付き subprocess を使う（pytest fixture
   既定）
2. dev モード（既存 QGIS 相乗り）で execute_python 多用テストを走らせない
3. または対象 QGIS を起動するときに `QPUPPETEER_TRUSTED_MODE=1` を立てておく

> pytest ラッパ `execute_python` は confirm ゲートを `ConfirmationRequiredError`
> として raise するので、原因がすぐ分かる（dev モードで信頼モード未設定のサイン）。

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
