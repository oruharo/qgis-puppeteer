# ADR-0002: E2E テストアーキテクチャ（外部プロセス + qgis-puppeteer 基盤）

- **Status**: Accepted
- **Date**: 2026-04-23
- **Deciders**: qgis-puppeteer maintainers
- **Related**: ADR-0001（qgis-puppeteer アーキテクチャ）に依存

## Context

### 現行構成

QGIS プラグインとして実装された `plugins/pytest_runner/` が、QGIS プロセス内で `pytest.main()` を呼び出して E2E テストを実行している。

```
[run_qgis_e2e_pytest.bat]
        ↓ 環境変数設定、QGIS 起動
[QGIS プロセス（単一）]
  ├─ test_e2e/support/startup.py（QGIS 起動マクロ）
  │   ├─ QTimer.singleShot(500, handle_login_dialog)  # モーダル事前処理
  │   └─ QTimer.singleShot(20000, run_pytest)         # 20秒後に pytest 実行
  └─ plugins/pytest_runner/
      └─ TestExecutor.execute_pytest()
          └─ pytest.main()  ← QGIS メインスレッドで実行
```

### 構成要素

| コンポーネント | 役割 |
|---|---|
| `plugins/pytest_runner/pytest_runner.py` | QGIS プラグイン本体。メニュー・ツールバー登録、TestSelectorDialog 起動 |
| `plugins/pytest_runner/ui/test_selector_dialog.py` | テスト選択・実行・結果表示 UI（22 KB） |
| `plugins/pytest_runner/core/test_executor.py` | `pytest.main()` を QGIS 内で呼ぶ |
| `plugins/pytest_runner/core/{test_history,trend_analyzer,smart_selector,...}` | 履歴・傾向分析・スマート選択 |
| `test_e2e/support/startup.py` | QGIS 起動時に実行、ログインダイアログ事前処理、pytest 起動トリガ |
| `test_e2e/helpers/custom_runner.py` | `QGISCustomRunner`：ダイアログ検索、ログイン実行、スクショ |
| `test_e2e/helpers/test_helpers.py` | `wait_for_dialog`, `find_widget_by_name`, `simulate_user_input` 等 |
| `test_e2e/conftest.py` | fixtures（`qgis_iface`, `qgis_project`, `domain_dock`, `login_results`） |

### 現行構成の限界

#### 1. モーダルダイアログが構造的に扱えない

pytest テスト関数と QGIS UI が **同一プロセス・同一スレッド**で動くため、`QDialog.exec_()` による nested event loop に入るとテスト関数は戻ってこない。

```python
def test_something():
    button.click()         # 内部で exec_() が呼ばれると戻らない
    ok_button.click()      # ← 実行されない
```

現行の workaround は「exec_() 呼び出し前に `QTimer.singleShot` でハンドラを登録」する方式（`startup.py` のログイン処理がこれ）。しかし：

- ハンドラ登録は exec_() の「前」に書く必要があり、テスト関数の素直な逐次構造を壊す
- ワークフローが深く入れ子になるほど破綻する
- 結果として「exec_() 系モーダルが関与する機能は E2E テストしない」ルールに退化する

#### 2. テスト実行環境が QGIS に縛られる

- `pytest.main()` は QGIS の Python 環境（3.12）・バンドルされた依存バージョンで動く
- pytest 本体や関連プラグイン（`pytest-qt`, `pytest-image-snapshot` 等）は `.venv` から `sys.path.insert()` で借用する応急措置
- pytest バージョンを独立して更新できない、依存管理が `pyproject.toml` 一元で行えない

#### 3. 既存テストは限定的

- `test_login_workflow.py`：ログインが「startup.py で既に処理済み」である前提の検証のみ。テスト関数内では何も操作していない
- `test_search_workflow.py`：`QgsAttributeDialog` は `show()` ベースの非モーダルなので成立している。真の `exec_()` モーダルは一切扱っていない
- メンテナ方針として「既存テストは試作のため捨ててよい」との確認済み

#### 4. `test_helpers` / `custom_runner` と ADR-0001 の Worker 側コマンドが機能重複

両者とも以下を実装している：

- ウィジェット検索（objectName / text / type ベース）
- ダイアログ検索と待機
- フォーム入力・ボタンクリック
- スクリーンショット撮影

ADR-0001 実装完了後は Worker 側の UI 操作コマンド（`qgis_click_widget`, `qgis_set_widget_value`, `qgis_snapshot_ui` 等）がより汎用・高機能な実装を提供するため、既存 helper は二重メンテになる。

#### 5. 複数 QGIS 同時テストが構造的に不可能

pytest 自身が 1 つの QGIS 内で動くため、「QGIS-A を subject にするテストと QGIS-B を subject にするテストを並行実行」ができない。ADR-0001 で導入する複数 QGIS 構成の恩恵が E2E で受けられない。

### 検討した代替案

| 案 | 方式 | 評価 |
|---|---|---|
| **A: プラグイン廃止、完全外部化**（採用） | pytest は外部プロセスで動作、QGIS Puppeteer 基盤（Hub 経由）で QGIS を操作 | 最もクリーン。Playwright/Cypress と同じ位置づけ |
| B: プラグインをランチャー化 | プラグインは外部 pytest を spawn、結果表示だけ担当 | 「QGIS 起動しっぱなしでテストを繰り返す」便利ワークフロー。ただし開発中の汚染状態にテストが依存するアンチパターンを誘発 |
| C: デバッガプラグインとして残す | UI スナップショット可視化等の補助機能のみ | `qgis_snapshot_ui` が同機能を提供済みで重複 |

## Decision

**案 A：プラグイン完全廃止、pytest を QGIS 外部プロセスで実行する** を採用する。

### 基本方針

1. **pytest は QGIS とは別プロセスで動かす**
   - `uv run pytest test_e2e/` で起動
   - QGIS は pytest セッションフィクスチャが subprocess で起動・終了管理する
   - CI 環境でも同じコマンドが通る

2. **QGIS への操作はすべて QGIS Puppeteer 基盤（Hub 経由）で行う**
   - `test_e2e/helpers/automation_client.py` が `qgis_puppeteer.AutomationClient` を使い Hub に WebSocket 接続
   - ADR-0001 で定義した `qgis_click_widget`, `qgis_set_widget_value`, `qgis_snapshot_ui` 等をそのまま利用（Worker 側実装）
   - モーダル `exec_()` 中でも操作可能（nested event loop 中の QTimer 発火を利用）

3. **既存のランナープラグイン・ヘルパは廃止する**
   - `plugins/pytest_runner/` を削除
   - `test_e2e/helpers/{custom_runner,test_helpers}.py` を削除
   - `test_e2e/support/startup.py` のモーダル処理・pytest 自動起動ロジックを削除
   - `plugins/pytest_runner/` の廃止に伴い、`test_e2e/test_login_workflow.py` および `test_e2e/test_search_workflow.py` も一旦破棄（試作のため）

4. **新方式のテストは「外部 pytest → Hub → Worker (QGIS)」の 3 プロセス構成で書き直す**

## 詳細設計

### 1. アーキテクチャ

```
[CI / ターミナル / IDE]
         │
         │ uv run pytest test_e2e/
         ▼
[pytest プロセス]
  ├─ session fixture: QGIS を subprocess spawn
  ├─ session fixture: Hub 接続確立を待機
  │
  ├─ qgis fixture: AutomationClient（Hub への WS クライアント）
  │
  └─ テスト関数ごとに qgis を使って QGIS を操作
         │
         │ WebSocket
         ▼
[QGIS Puppeteer Hub]  ← ADR-0001 で導入
         │
         ▼
[Worker (QGIS + plugins/qgis_puppet/)]
```

### 2. `test_e2e/helpers/automation_client.py`（新規）

`qgis_puppeteer.AutomationClient` を pytest 向けに薄くラップする層。pytest 側から使いやすい同期/async API を提供する。

#### 責務

- Hub への WS 接続確立・切断（内部で `qgis_puppeteer.AutomationClient` を利用）
- ADR-0001 プロトコル（`register`, `request`, `response`）の送受信
- 非同期/同期どちらのインターフェースも用意（`pytest-asyncio` で async、デフォルト同期版も）
- 便利メソッドの提供：
  - `click_widget(selector)`
  - `set_widget_value(selector, value)`
  - `snapshot_ui()` → 現在の UI ツリー
  - `list_layers()`
  - `execute_python(code)`
  - `screenshot(path)`
  - `wait_for_modal(title_contains=..., timeout=5)`
  - `wait_for_modal_closed(timeout=5)`
  - `wait_for_widget(selector, visible=True, timeout=5)`
  - `instance: str | None` プロパティ（複数 QGIS 時の target 指定）

#### API イメージ

```python
import pytest

@pytest.mark.e2e
async def test_login_flow(qgis):
    # プロジェクト読み込みトリガ（exec_() でログインダイアログが開く）
    await qgis.execute_python_no_wait(
        "QgsProject.instance().read(r'D:/work/test_project.qgs')"
    )

    # モーダル出現を待つ
    await qgis.wait_for_modal(title_contains="ログイン", timeout=5)

    # モーダル内操作（exec_() 中でも動く）
    await qgis.set_widget_value({"object_name": "username_input", "scope": "modal"}, "haruo")
    await qgis.set_widget_value({"object_name": "password_input", "scope": "modal"}, "aaa")
    await qgis.click_widget({"object_name": "login_button", "scope": "modal"})

    # モーダルクローズ待ち
    await qgis.wait_for_modal_closed(timeout=10)

    # アサーション
    layers = await qgis.list_layers()
    assert len(layers) > 0, "プロジェクトのレイヤーが読み込まれていない"
```

### 3. `test_e2e/conftest.py`（刷新）

#### Hub / Worker の起動責務（E2E では pytest が Hub を所有）

ADR-0001 Rev.4 で導入された「外部所有者モード」を採用する。pytest session fixture が Hub を独立ポートで spawn・所有し、QGIS subprocess には `QPUPPETEER_HUB_PORT` / `QPUPPETEER_HUB_HOST` env を渡すことで Worker 側の自動 spawn を抑制する。

**なぜこうするか**:

| 問題 | 対策 |
|---|---|
| dev の Claude Desktop が使う Hub（`:9876`）と衝突し、テストが dev の QGIS に誤爆する | port を free port で取得し env で隔離 |
| Hub ready を pytest 側で判定できない（Worker 所有だと spawn 完了を知る手段がない） | pytest 自身が spawn するので proc の log / probe を直接監視できる |
| teardown で Hub を誰が kill するか不明（idle_shutdown 任せだとログ・トークンが残る） | session teardown で pytest が明示 kill |
| pytest-xdist 並列時に port 衝突 | xdist worker ごとに別 free port を割り当て |

#### session scope fixtures

| fixture | 責務 |
|---|---|
| `hub_port` | `socket.bind((127.0.0.1, 0))` で free port を取得 |
| `hub_process` | `python -m qgis_puppeteer.hub --port {hub_port}` を spawn、TCP probe で listen 確認、yield、teardown で terminate |
| `automation_client` | `AutomationClient(host=127.0.0.1, port=hub_port)` インスタンスを session 全期間保持。Client 接続を維持することで Hub の idle shutdown を抑止する（ADR-0001 §4 参照） |
| `qgis_process` | `env={"QPUPPETEER_HUB_PORT": str(hub_port), "QPUPPETEER_TRUSTED_MODE": "1", "QPUPPETEER_ALLOW_TEST_HANDLERS": "1", ...}` を渡して QGIS subprocess 起動、終了時に kill |
| `hub_ready` | `automation_client` を使って `list_instances` をポーリングし、Worker register 完了を待機（既定 30 秒タイムアウト） |

依存順：`hub_port` → `hub_process` → `automation_client` → `qgis_process` → `hub_ready`

`automation_client` を `qgis_process` より**先に**作る点が重要です:

- Hub spawn 直後に Client 接続を確立して idle shutdown 発火を防ぐ
- `hub_ready` が `list_instances` を叩くための Client としても再利用する（短命 WS を別途生成しない）
- QGIS 起動の数秒間、Hub に Client だけがいる状態でも安全

#### dev 用モード（既存 QGIS への相乗り）

環境変数 `QPUPPETEER_E2E_USE_RUNNING_QGIS=1` が設定されている場合：

- `hub_port` は既定の `9876` を返す（free port 取得しない）
- `hub_process` fixture は**スキップ**（dev の既存 Hub を使用）
- `qgis_process` fixture も**スキップ**（既存 QGIS を使用）
- `automation_client` のみ生成して既存 Hub に接続

この場合テストは dev の QGIS 状態を変更するため、**使用は開発中のデバッグ用途に限定**。CI では使わない。

#### function scope fixtures

| fixture | 責務 |
|---|---|
| `qgis` | `automation_client` を受け取り、テスト関数ごとに「クリーン状態」へのリセットを試みる |
| `screenshot_dir` | テスト失敗時のスクショ保存先 |

#### テスト失敗時のスクショ自動撮影

`pytest_runtest_makereport` フックで失敗を検出 → `qgis.screenshot(...)` を呼んで `outputs/screenshots/{test_name}_{timestamp}.png` に保存。

### 4. QGIS 起動ストラテジ

#### 起動順序

```
1. hub_port fixture        : free port 取得（例: 54321）
2. hub_process fixture     : python -m qgis_puppeteer.hub --port 54321
                             → TCP probe で listen 確認（最大 5 秒）
3. automation_client       : port=54321 で Hub に Client として接続
                             → idle shutdown を抑止しつつ list_instances 用の接続を確保
4. qgis_process fixture    : QGIS を subprocess 起動
                             env: QPUPPETEER_HUB_PORT=54321,
                                  QPUPPETEER_TRUSTED_MODE=1,
                                  QPUPPETEER_ALLOW_TEST_HANDLERS=1
                             → Worker が自動で Hub に connect（spawn はしない）
5. hub_ready fixture       : automation_client 経由で list_instances を poll、
                             Worker の register 到着を待つ
```

#### session scope で 1 回だけ起動する

- spawn コスト（数秒）をテスト毎に払わない
- 代わりにテスト間の状態リセットをフィクスチャで行う
  - プロジェクトをテンプレートから再読み込み
  - 選択解除、ズームリセット等

#### CI での起動

- Windows Runner（前提：GitHub Actions の Windows runner または自前 Windows CI）
- QGIS はランナーに事前インストール
- `QGIS_PREFIX_PATH` / `QT_QPA_PLATFORM=offscreen` で headless 化検討

#### 開発中のデバッグ用

- 環境変数 `QPUPPETEER_E2E_USE_RUNNING_QGIS=1` を渡すと pytest は新規 spawn せず、既に起動中の QGIS の Hub（`:9876`）に接続する
- テスト失敗時に QGIS が残って状態を確認できる
- dev の作業状態を変更するので、CI では使わない

#### pytest-xdist での並列実行

各 xdist worker が **独立した `hub_port` と `qgis_process`** を持つ（session fixture が worker ごとに独立するため自然に隔離される）。port は free port 取得なので衝突しない。

ただし現状はシングル worker 運用とし、xdist 対応は将来検証する（`## Roadmap`）。

### 5. Instance 選択（複数 QGIS）

ADR-0001 の `qgis_use_instance` をそのまま利用（Worker が複数繋がっている場合）。

```python
@pytest.fixture
def qgis_a(automation_client):
    automation_client.use_instance("A")
    return automation_client

@pytest.fixture
def qgis_b(automation_client):
    automation_client.use_instance("B")
    return automation_client

def test_multi_instance(qgis_a, qgis_b):
    # A, B それぞれを独立に操作
    ...
```

ただし現状は単一 QGIS でのテストのみを対象とし、複数 Worker テストは `## Roadmap` 扱い。

### 6. 廃止範囲

#### 削除するコード

- `plugins/pytest_runner/` 全体
- `test_e2e/helpers/custom_runner.py`
- `test_e2e/helpers/test_helpers.py`
- `test_e2e/support/startup.py` のモーダル処理・pytest 自動起動ロジック
- `test_e2e/test_login_workflow.py`
- `test_e2e/test_search_workflow.py`
- `test_e2e/run_qgis_e2e_pytest.bat`（または新構成用に再設計）

#### 削除する設定

- `pyproject.toml` の pytest_runner 関連依存（もしあれば）
- QGIS の有効化済みプラグインリストから `pytest_runner` を除外

#### 残すコード

- `test_e2e/conftest.py`（大部分リライト）
- `test_e2e/pytest.ini`
- `test_e2e/outputs/`（出力ディレクトリ構造）
- `test_e2e/fixtures/`（テストデータ、スナップショット）

### 7. 提供コンポーネント

本 ADR が定める E2E 基盤が提供するもの:

- `pytest_qgis_puppeteer.automation_client.E2EAutomationClient`（pytest 用便利ラッパ）
- `pytest_qgis_puppeteer` plugin（fixture: `qgis`, `automation_client`, QGIS 起動など）
- Locator 抽象（§11）+ auto-wait
- Selector 仕様の E2E 拡張（§8）
- Test 専用ハンドラ provider（`qgis_puppet_test_helpers` プラグイン）

各層は ADR-0001 の AutomationClient / Hub / Worker の上に乗る。

### 8. Selector 仕様

`qgis_click_widget` / `qgis_set_widget_value` が受け取る selector dict は `qgis_puppeteer/qgis_tools/ui_tools.py::_find_widget` で解決される。現状の仕様と E2E 向けに追加する拡張を以下に定める。

#### 8.1 現状仕様

| キー | 型 | 用途 |
|---|---|---|
| `object_name` | `str` | Qt `objectName()` 完全一致 |
| `text` | `str` | ボタン / ラベル / QAction テキスト完全一致 |
| `class` | `str` | Python クラス名完全一致（例: `"QPushButton"`） |
| `title` | `str` | `windowTitle()` 完全一致 |
| `label` | `str` | `QLabel.buddy()` で対応付くラベル文字列（Playwright `getByLabel` 相当） |
| `placeholder` | `str` | LineEdit/TextEdit の `placeholderText()` 完全一致（Playwright `getByPlaceholder` 相当） |
| `index` | `int` | マッチ候補中の 0-based index。**未指定時に複数マッチすると `selector_ambiguous`（strict mode）**。明示指定で N 番目を採用 |
| `root_object_name` | `str` | トップレベルを objectName で絞る |
| `scope` | `str` | `"modal"`（既定） / `"active_window"` / `"any"` |

複数キー指定時は AND セマンティクス。

既定 `scope="modal"` は `activeModalWidget()` を root にし、**なければ `activeWindow()` にフォールバック**する（Claude からの曖昧指定に寛容な挙動）。

##### Strict mode（複数マッチで fail）

`index` を明示しないまま selector が複数 widget にマッチすると、Worker は
`reason="selector_ambiguous"` を返す。silent に先頭を採用していた旧挙動は
objectName 衝突を発見不能にする落とし穴で、対応として既定で fail する。

- `click_widget` / `set_widget_value`: `error="selector_ambiguous"` を返す
  （`error="widget_not_found"` とは別コード）
- `check_actionability`: `exists=False` + `diagnostics.reason="selector_ambiguous"`
- Locator (`pytest-qgis-puppeteer`): auto-wait 中に検出すると **timeout を待たず即時 abort**
  し `SelectorAmbiguousError` を上げる（poll を続けても候補数は減らないため）
- diagnostics には `match_count` と上位 10 件の候補（class / object_name / text）
  が `candidates` として入る。失敗ログから即「何を選んでしまっていたか」が読める

opt-out: `index` を明示指定すれば旧挙動（N 番目を採用）に戻る。

```python
# 旧: silent に先頭が選ばれていた
qgis.locator({"class": "QPushButton"}).click()  # ← 複数あれば SelectorAmbiguousError

# 新: 明示的に N 番目を選ぶ
qgis.locator({"class": "QPushButton", "index": 0}).click()  # ← OK

# 推奨: そもそも一意になるよう絞る
qgis.locator({"object_name": "ok_btn"}).click()
```

#### 8.2 E2E 向けに追加する拡張

| キー | 型 | 用途 |
|---|---|---|
| `scope="main_window"` | — | `QgisApp` トップレベルを自動解決するエイリアス（dock widget / メニューバー操作用） |
| `strict` | `bool` | `True` の場合、`scope="modal"` の activeWindow フォールバックを無効化し「モーダル不在」を `no_root_widget` エラーで返す |
| `text_contains` | `str` | 部分一致（`text` と排他）。「保存(&S)」等のニーモニック対応 |

no-match 時の診断情報拡張：
- 同 `class` の widget 候補 Top 10（objectName 付き）
- 同 `object_name` 部分一致の widget 候補 Top 10

これにより E2E 失敗解析時に「何が似てて何が無いか」が diagnostics だけで分かる。

#### 8.3 操作系 API の auto-wait 内蔵（Playwright 流）

Client 側の操作系 API（`click_widget`, `set_widget_value`, Locator メソッド群）は **既定で auto-wait を内蔵**する。ユーザーが `wait_for_widget` を明示的に呼ばなくても操作が flaky にならない設計にする。

操作実行前の actionability checks（既定 timeout=5s）:

| チェック項目 | 内容 |
|---|---|
| exists | `_find_widget` で候補が見つかる |
| visible | `widget.isVisible()` |
| enabled | `widget.isEnabled()` |
| not covered | 上にモーダル等が被ってない（scope が modal の場合は modal 内 widget であること） |
| stable | ✅ 実装済（opt-in `Locator(stable=True)`）。直前 2 回 ``check_actionability`` の geometry が同じ |

いずれか満たさない場合は polling を続け、timeout で `widget_not_actionable` エラー。

##### Polling 戦略と RTT の考慮

ADR-0001 §Performance の実測値では定常コマンドの RTT が 10〜140ms、最大 1170ms（list_layers の外れ値）に達します。Client 側で 4 項目を個別にコマンド発行して poll すると 1 操作が数秒〜数十秒に膨らむため、以下の戦略を採ります:

- **Worker 側で actionability 判定を 1 コマンドに束ねる**：`qgis_check_actionability(selector)` を Tier 1 ハンドラとして追加し、4 項目を QGIS 内で 1 回の Qt API 呼び出しで評価して返す（各 probe を個別に往復しない）
- **Poll 間隔は 250ms**（既定）：RTT 中央値より十分長く、CPU 負荷も抑えられる。100ms だとメディアン RTT とほぼ同じで poll がオーバーラップし、Hub が詰まる
- **最初の 1 回は即時評価**：widget が既に actionable な場合は 0ms で次工程へ進む
- **timeout に達したら最後の diagnostics を詳細付きで返却**：どの項目で落ちたかを診断バンドルに残す

明示的な `wait_for_*` API は特殊ケース用に残す:

- `wait_for_modal(title_contains=..., timeout=5)` — 操作対象でないモーダルの出現待ち（トーストやダイアログの単純確認用）
- `wait_for_modal_closed(timeout=5)` — modal の object identity（`id()`）を追跡して nested modal にも対応
- `wait_for_widget(selector, visible=True, timeout=5)` — 操作を伴わない単純待機

通常の click / fill 等では `wait_for_widget` を呼ばない書き方を推奨する。

#### 8.4 将来検討

- ~~`text_re`: 正規表現マッチング（動的テキスト「項目 12 件」等）~~ ✅ 実装済
  （`record_matches_selector` で `re.search` セマンティクス、不正 regex は silently skip）
- ~~`text_contains`: 部分一致（Qt ニーモニック「保存(&S)」対応）~~ ✅ 実装済
  （§8.2 にも記載、selector_match.py で実装）
- ~~`attr`: 任意プロパティマッチ~~ ✅ 実装済
  （`{"attr": {"is_visible": True}}` のように record の任意キーを AND マッチ。
  将来 record に新キーを追加した時の前方互換口）
- ~~Child selector chain~~ ✅ 実装済（`parent.locator(child)`）
- ダブルクリック / 右クリック / キー入力の専用コマンド（現状 `click_widget` のみ）
- ~~`getByRole`: Qt accessible role（`QAccessibleInterface`）経由の selector~~ ✅ 実装済
  - `record_matches_selector` で `role` キーをサポート
  - Worker 側 `_widget_summary` / `_describe_widget` が `QAccessible.queryAccessibleInterface(widget)`
    から enum 名（"Button" / "ComboBox" / "EditableText" 等）を埋める
  - QAction や accessible interface を持たない widget は role=None で不一致扱い

### 9. execute_python の権限制御（E2E 信頼モード）

`qgis_execute_python` は `qgis_puppeteer/qgis_tools/permission_manager.py` の 3-tier システム（whitelist / session / confirm）で保護されている。これは Claude Desktop 経由で LLM が破壊的コードを黙って実行するのを防ぐ UX フローであり、毎回新しいコードが走る E2E では **confirm 段階で UI 応答待ちになりテストが固まる**。

#### 9.1 決定：env var による信頼モード

QGIS subprocess に環境変数 `QPUPPETEER_TRUSTED_MODE=1` を渡した場合、`PermissionManager.check_permission()` は常に `"whitelist"` を返し、確認ステップを skip する。

```python
# PermissionManager.__init__
self._trusted_mode = os.environ.get("QPUPPETEER_TRUSTED_MODE") == "1"

# PermissionManager.check_permission
def check_permission(self, code: str) -> PermissionLevel:
    if self._trusted_mode:
        return "whitelist"
    # 既存ロジック（whitelist → session → confirm）
```

#### 9.2 なぜ env var 方式か（代替案との比較）

| 案 | 採否 | 理由 |
|---|---|---|
| **A. env var（採用）** | ○ | ADR-0001 Rev.4 の `QPUPPETEER_HUB_PORT` と同じメカニズムで隔離を表現できる。プロトコル変更ゼロ |
| B. Hub が caller_role を転送、Worker が role 別 policy | 見送り | E2E Worker は pytest 専用 Hub にのみ繋ぐので混在シナリオが存在しない。remote / 混在対応が必要になれば拡張（`## Roadmap`） |
| C. fixture で session_allowed に全パターン事前登録 | 却下 | 新コード追加のたびに fixture 更新、brittle |

#### 9.3 監査ログ

信頼モードでも実行内容は QGIS 側で記録する（失敗解析・セキュリティレビュー用）:

```python
# python_executor._execute_code_internal
if self._trusted_mode:
    QgsMessageLog.logMessage(
        f"[trusted mode] exec: {code[:200]}",
        "qgis_puppeteer",
        Qgis.Info,
    )
```

#### 9.4 pytest fixture での付与

`qgis_process` fixture が `QPUPPETEER_HUB_PORT` と一緒に subprocess env に設定する。**信頼モードと test ハンドラ許可モードは env を分離**しており、両方を明示的に設定する必要がある（ADR-0001 §12.9 参照）:

```python
@pytest.fixture(scope="session")
def qgis_process(hub_port: int) -> Iterator[subprocess.Popen]:
    env = {
        **os.environ,
        "QPUPPETEER_HUB_PORT": str(hub_port),
        "QPUPPETEER_TRUSTED_MODE": "1",          # execute_python の bypass
        "QPUPPETEER_ALLOW_TEST_HANDLERS": "1",   # test.* 登録許可
    }
    proc = subprocess.Popen([qgis_bin, ...], env=env)
    try:
        yield proc
    finally:
        proc.terminate()
```

加えて、Hub は request 転送時に `caller_role` を Worker へ伝達する（ADR-0001 §12.9）。Tier 3（`test.*`）ハンドラは `caller_role == "automation_client"` の呼び出しでのみ実行され、McpGateway 経由からは `handler_not_visible` エラーで拒否される。これにより、CI port に dev Claude Desktop が誤接続した場合でも `test.*` ハンドラが露出しない。

#### 9.5 セキュリティ境界

- この env は **subprocess スコープで pytest fixture が明示的に設定**する想定。ユーザー / システムスコープで永続設定することは自己妨害であり、セットアップ手順書でも警告する
- dev の既存 QGIS（Claude Desktop 経由で操作中）には env が伝播しないので信頼モードにならない
- `QPUPPETEER_E2E_USE_RUNNING_QGIS=1` dev モードでは `qgis_process` fixture をスキップするので env も付与されない。dev モードの QGIS では通常の permission flow が走り、confirm で固まる = **dev モードは execute_python を多用するテストには向かない**（UI 操作系のデバッグ用途が主）
- 将来 remote / 混在シナリオが出てきた場合は §9.2 の案 B（caller_role 転送）に移行する（`## Roadmap`）

### 10. テスト状態リセット戦略

E2E は「テスト間で状態が持ち越されない」ことが前提。リセット対象は 3 レイヤーあり、テスト内容に応じて粒度を選択できる設計にする。

#### 10.1 リセット対象の 3 レイヤー

| レイヤー | 内容 | 例 |
|---|---|---|
| L1: QGIS プロセス | プロジェクト / 選択状態 / キャンバス extent / 開いたダイアログ / QSettings / プラグイン singleton / メモリ | 編集中レイヤー、アンドゥスタック、前テスト由来の modal |
| L2: DB | バックエンド DB のテストスキーマ | プラグインが書き込んだレコード、操作ログ |
| L3: ファイルシステム | 添付ファイル / エクスポート / 印刷出力 / 一時ディレクトリ | host アプリが書き出す中間ファイル |

#### 10.2 環境分岐とリセット粒度の関係

QGIS の起動構成（QGIS バージョン / プロファイル / プラグインセット / ホスト
アプリの起動引数等）でテストを分割する仕組みは [ADR-0003 Test
Environments](0003-test-environments.md) で扱う。

リセット戦略（本節）は **同一 environment 内** での粒度を担当する:

- environment = QGIS の起動構成（session スコープで固定）
- リセット粒度 = environment 内でテスト間に何を初期化するか（function スコープ）

両者は直交する。例: `with_authentication` env 内で:

- 既定リセット（プロジェクト reload + canvas 初期化）
- `@pytest.mark.reset_db` 付きなら DB も
- `@pytest.mark.fresh_qgis` 付きなら QGIS プロセスごと再起動（env の構成は維持）

#### 10.3 マーカー駆動のリセット粒度

テスト内容に応じてリセットコストを選択する階層的マーカー設計:

| マーカー | L1 リセット | L2 リセット | L3 リセット | コスト |
|---|---|---|---|---|
| `@pytest.mark.read_only` | なし | なし | なし | 最速 |
| （マーカー無し・既定） | プロジェクト reload + canvas 初期化 | なし | 一時 subdir | 〜0.5 秒 |
| `@pytest.mark.reset_db` | 既定 + DB 再作成後の再接続 | テンプレートから再作成 | 〃 | 〜2 秒 |
| `@pytest.mark.fresh_qgis` | QGIS プロセス再起動 | reset_db 相当 | 〃 | 〜10 秒 |

マーカーは階層的：重いマーカーが付いていれば軽いリセットを含意する。

#### 10.4 `@pytest.mark.fresh_qgis` の詳細

**用途**（reload では拭えない残留を対象にする）:

- QSettings / プラグイン singleton / global 変数の初期化検証
- メモリリーク回帰テスト
- アサート失敗で閉じそこねた modal からの復旧
- QgsMessageLog のログ検証（前テストのログ混在防止）
- GDAL / PostGIS コネクションプールの状態依存問題の切り分け
- プラグインロード / 初期化の smoke test

**実装**:

```python
@pytest.fixture(scope="session")
def _qgis_session_state(hub_port):
    state = {"proc": _spawn_qgis(hub_port)}
    yield state
    if state["proc"]:
        state["proc"].terminate()

@pytest.fixture
def qgis(request, _qgis_session_state, hub_port, automation_client):
    if request.node.get_closest_marker("fresh_qgis"):
        old = _qgis_session_state["proc"]
        if old:
            old.terminate()
            old.wait(timeout=10)
        _qgis_session_state["proc"] = _spawn_qgis(hub_port)
        _wait_for_worker_register(automation_client, timeout=30)

    _soft_reset(automation_client, request.node)  # マーカーに応じたリセット
    return automation_client
```

**運用ルール**:

- 乱用禁止（10 秒/テスト × 件数で CI 時間が破綻）
- 「前テスト失敗の影響を受けずに確実に走らせたい」重要 smoke test に限定
- 通常の機能テストでは使わない（既定リセットで十分）

**pytest-xdist との相性**:

fresh_qgis 群は `--dist=loadgroup` で同一 worker に集約し逐次実行。他 worker と並列は可能だが局所化した方が予測可能。

#### 10.5 DB リセット機構

PostGIS レイヤーは `QgsVectorLayer` が独自コネクションプールを持つため、pytest 側のトランザクション rollback では隔離できない。**テンプレート DB + `CREATE DATABASE ... TEMPLATE`** で物理コピーする方式を採る。

DB リセット処理そのものは ADR-0001 §12 の **Tier 3（Test）ハンドラとして実装**する（`test.reset_db` 等）。`qgis_puppeteer` core には含めず、テスト支援側で所有する。

```
session start:
  - テンプレート DB `qgis_puppet_e2e_template` を確認
    - 無ければマイグレーション + seed で作成（初回のみ）
  - セッション用 DB を CREATE ... TEMPLATE で複製
  
function (reset_db / fresh_qgis マーカー付き):
  - 対象 DB を DROP + CREATE ... TEMPLATE で再作成
  - QGIS にプロジェクト reload させて再接続（接続情報を新 DB 名へ差し替え）
  
session end:
  - 全テスト DB を DROP
```

DB 名ネーミング: `qgis_puppet_e2e_${purpose}_${id}` 形式で本番 DB と区別する。
複数 environment（ADR-0003）で別 DB を使う場合は `${env_name}` を含めて一意化
（例: `qgis_puppet_e2e_with_authentication_<id>`）。

#### 10.6 DB 差し替え時の接続ハンドリング

`CREATE DATABASE ... TEMPLATE` での DB 差し替え後、`QgsProject.instance().clear()` + 新プロジェクト `read()` により `QgsVectorLayer` の古い接続が切れて新 DB を参照することは既知挙動として扱う。実装時に想定外の残存接続が観測された場合は §10.5 の代替案（`pg_terminate_backend`、プラグイン側での明示的 connection reset、または DB 名固定 + TRUNCATE ベース）で対処する。

#### 10.7 検討中の項目（Open Questions）

1. **テスト専用リソースの配置場所**（テンプレート SQL、seed データ、E2E 用設定）
   - 候補: `test_e2e/fixtures/sql/` / `test_e2e/fixtures/projects/` 等
   - 対象リソースが出揃ってから決定
2. **失敗時の自動 fresh_qgis**（前テスト失敗時に次テストへ動的マーカー付与）
   - 現状は明示マーカーのみ
3. **xdist 並列時の DB 分離戦略**
   - xdist worker ごとに別 DB 名を発行する仕組み（`${pytest_xdist_worker}` suffix 等）
   - environment 軸（ADR-0003）と直交。両軸を suffix で表現する想定
4. **テンプレート DB のマイグレーション追従**
   - マイグレーション更新時にテンプレート DB を invalidate する機構
5. **L3 ファイルシステム差し替えの具体手段**
   - 出力先パスを env 経由でパラメータ化する範囲

### 11. Locator 抽象（Playwright 流）

selector dict を毎回 API に渡す代わりに、**Locator オブジェクト**にカプセル化する。Locator は状態を持たず、操作のたびに内部で `_find_widget` を再実行する（widget pointer は保持しない）。

```python
login_btn = qgis.locator({"object_name": "login_button", "scope": "modal"})
login_btn.click()                  # auto-wait 付き click
text = login_btn.get_text()        # 読み取り
await expect(login_btn).to_be_visible()  # web-first assertion
```

#### 11.1 なぜ Locator か

- **stale widget pointer 問題の構造的回避**：QGIS 側で再描画されると Python 側の `QWidget*` が無効化される可能性があるが、Locator は毎回再探索するので壊れない
- **可読性**：テストコードが「対象を名付けてから操作」という自然な流れになる
- **チェーン可能**：`qgis.locator(parent).locator(child)` で階層的 selector を構築できる（将来の child selector chain と噛み合う）

#### 11.2 提供メソッド

| メソッド | 用途 |
|---|---|
| `click()` | auto-wait + click |
| `fill(value)` | 入力系（LineEdit / TextEdit / SpinBox 等） |
| `select(value)` | ComboBox / List / Tab |
| `check() / uncheck()` | CheckBox / RadioButton |
| `get_text() / get_value()` | 現在値取得 |
| `is_visible() / is_enabled() / exists()` | 即時状態チェック（wait なし） |
| `snapshot()` | 配下のウィジェットツリー取得 |

### 12. Web-first assertions（dev で実装済み）

アサーション自体がリトライする `expect()` オブジェクトを提供する。実装は
sync API（pytest 標準スタイル）:

```python
from pytest_qgis_puppeteer import expect

expect(qgis.locator({"object_name": "status"})).to_have_text("保存完了")
expect(qgis.locator({"title": "エラー"})).not_.to_be_visible()
```

既定 `timeout=5s`、`250ms` 間隔で再評価（§8.3 と同じ理由で RTT を考慮）。`not_` プロパティで否定形。

提供するマッチャ:

- `to_be_visible() / to_be_hidden()`
- `to_be_enabled() / to_be_disabled()`
- `to_have_text(expected)` — 完全一致
- `to_contain_text(substring)` — 部分一致
- `to_have_value(expected)` — 入力系の値
- `to_be_checked() / not_.to_be_checked()`

selector 多重マッチ時は `Locator._wait_actionable` 同様、poll を続けず即時
`SelectorAmbiguousError` を上げる。

> `to_have_count(n)` は未実装（複数マッチ候補数の検証）。実装には
> `_find_in_snapshot` で全マッチを返す API 拡張が必要なため、Roadmap 残し。

### 13. 失敗時診断バンドル

テスト失敗時、`outputs/[<env_name>/]diagnostics/<test_name>_<timestamp>/` に以下をまとめて出力する（env 別ディレクトリは ADR-0003 Phase 1 で導入）:

```
outputs/diagnostics/test_login_20260430-093000/
├── actions.jsonl          各 Worker call の timestamp / params / result / error / step_path /
│                          screenshot_path（B5a + B5b 実装済、schema_version=2）
├── screenshot.png         qgis_screenshot 結果（実装済）
├── screenshots/           各 action 後の per-action screenshot 列（B5b 実装済）
│   ├── 0000.png
│   ├── 0001.png
│   └── ...
├── snapshot_ui.json       qgis_snapshot_ui の出力（実装済）
├── list_instances.json    Hub に register 中の Worker 一覧（実装済）
├── meta.json              nodeid / env_name / qgis_bin / 例外 / instances（B4 実装済、schema_version=1）
├── traceback.txt          pytest report.longrepr テキスト（B4 実装済）
├── hub_stdout.log         Hub プロセスの stdout 直近 500 行（B4 実装済）
└── hub_stderr.log         Hub プロセスの stderr 直近 500 行（B4 実装済）

未実装（Phase B5c 以降に繰り延べ）:
└── qgis_message.log       QgsMessageLog の tail（Worker 側に messageReceived 購読 + tool 化が必要）
```

#### 13.1 収集タイミング

- **B5a 現状**: テスト失敗を `pytest_runtest_makereport` フックで検出 → 1 回だけ
  `screenshot` / `snapshot_ui` / `actions.jsonl` 等を bundle dir に書き出す。`actions.jsonl`
  はテスト中に `E2EAutomationClient.call(...)` を 1 箇所で wrap して timeline 蓄積。
- **B5b 実装済**: 各 action **後** に screenshot を `<diag_root>/_pending/<test>/<seq>.png`
  へ保存し、`ActionRecord.screenshot_path = "screenshots/<seq>.png"`（bundle 相対）として
  記録。成功テストでは fixture teardown で `_pending/<test>` を削除、失敗時には
  `_dump_diagnostic_bundle` が `_pending/<test>` を `<bundle>/screenshots/` へ rename。
  rename は同一 disk volume なので atomic。relative path で記録しているため成功 / 失敗
  どちらでも actions.jsonl の link が壊れない。
  - 「前」の screenshot は省略（N+1 の前 ≒ N の後 で代替）。工数 / 容量を半減。
  - opt-out: `qgis_diag_capture_screenshots = false` ini で per-action capture 無効化。
    RTT が action ごとに +1 回（10〜140ms）増えるので重いスイートで使う。
  - `command="qgis_screenshot"` 自身では per-action capture を skip（重複保存防止）。
  - 内部 screenshot 取得中は `_capture_in_progress` flag で recorder.record() を抑止し、
    再帰 / 二重記録を防ぐ。
- **B5c 未実装**: `qgis_message.log`（QGIS 側 `QgsMessageLog.messageReceived` の tail）は
  Worker 側に subscriber + tail buffer + Worker tool が必要なため別フェーズに繰り延べ。

#### 13.2 スクショ方式（動画ではない理由）

| 方式 | 容量 | 実装 | 失敗解析適性 |
|---|---|---|---|
| **スクショ列（採用）** | 小（数 MB / テスト） | 既存 `qgis_screenshot` 流用 | action との 1:1 対応で原因特定が容易 |
| 動画録画 | 大（数十 MB 〜） | Qt の連続キャプチャ実装必要 | アニメ中挙動には強いが通常の UI テストでは過剰 |

現状はスクショ列のみ。動画は `## Roadmap` 検討事項。

### 13bis. Dialog handler（グローバル auto-respond）

「想定外モーダル（Bad Layers / 認証失敗 / 古いプロジェクト警告 / WMTS タイル
取得失敗 等）」を毎テスト手動 dismiss する boilerplate を構造的に削減する
仕組み。Playwright の `page.on('dialog', ...)` 相当。

#### 設計

- Worker 側に `DialogHandlerRegistry` を 1 instance 持つ（module-level singleton）
- `qgis_register_dialog_handler` で predicate（selector dict）と action（`accept`
  / `reject` / `close`）と `once` フラグを登録
- Worker が QTimer で 250ms 周期で `app.activeModalWidget()` を polling し、
  registry に該当する handler があれば action を実行
- 同じ modal instance を 2 回処理しないよう、modal の `id()` を内部状態として保持
- `once=True` で 1 度発火したら自動 unregister

#### 提供 Tier 1 ハンドラ

| ハンドラ | 用途 |
|---|---|
| `qgis_register_dialog_handler` | predicate / action / once を指定して登録 |
| `qgis_unregister_dialog_handler` | name で削除 |
| `qgis_list_dialog_handlers` | 現状の登録一覧取得 |
| `qgis_clear_dialog_handlers` | 全削除（teardown 用） |

#### 利用例

```python
# pytest fixture (session スコープ)
@pytest.fixture(scope="session", autouse=True)
def auto_dismiss_bad_layers(automation_client):
    automation_client.call("qgis_register_dialog_handler", {
        "name": "bad-layers",
        "predicate": {"title": "利用できないレイヤを処理する"},
        "action": "accept",
    })
    yield
    automation_client.call("qgis_clear_dialog_handlers", {})

# Claude Desktop からも同じ MCP ツール経由で登録できる
```

#### 利用層

- pytest E2E: 想定外 modal の boilerplate dismiss
- Claude Desktop: ユーザーが「Bad Layers が出るたび閉じて」と言うと Claude が
  ハンドラ登録 → 以後自動処理
- スクリプト: 長時間実行スクリプトで modal による中断を防ぐ

これは **テスト専用機能ではない** — Worker 側の汎用機能として提供。

### 14. Test Environments（partition モデル）

→ **[ADR-0003: Test Environments](0003-test-environments.md)** を参照。

本節の旧設計（Playwright `projects` 準拠の cartesian モデル）は ADR-0003 で廃案。
QGIS の「状態違い」はドメイン固有性であり、同じテストを多 env で走らせる
cartesian product はほとんどのテストで意味のない red を生む。代わりに
**partition モデル**（テストは特定 env に所属、`@pytest.mark.qgis_env("with_authentication")`
のように marker で宣言）を採用する。

主な変更点:

- 概念名: `projects` → `environments`
- モデル: cartesian → partition
- 既定: 全テスト × 全 project → marker 指定 env のみ
- 多 env テスト: 暗黙の cartesian → marker への複数列挙による opt-in
- CLI: `--project=all` → `--env=all`（partition の合算）

複雑な exclusion ロジック（特定テストを「この project では skip」のような）は
不要になった。詳細は ADR-0003 へ。

### 15. Test steps（B5a 実装済）

複数 action をグルーピングして診断バンドルの action タイムラインを階層化する:

```python
with qgis.step("ログイン"):
    qgis.locator({"object_name": "username"}).fill("haruo")
    qgis.locator({"object_name": "password"}).fill("aaa")
    qgis.locator({"object_name": "login_btn"}).click()

with qgis.step("feature search"):
    qgis.locator({"object_name": "search_input"}).fill("Feature A")
    qgis.locator({"object_name": "search_btn"}).click()
```

実装場所: `pytest_qgis_puppeteer._action_recorder.ActionRecorder` + `E2EAutomationClient.step(name)`。

- step stack は `threading.local`（別 thread の step 名は混ざらない）
- 各 `call(...)` の `ActionRecord` に `step_path: tuple[str, ...]` が乗る
- 例外で抜けても `try/finally` で必ず pop（ネストは保持）
- recorder 未 attach の `step()` は真の no-op（テスト書き手は recorder 有無を意識しなくて良い）

診断バンドルの `actions.jsonl` は 1 行 1 record の JSONL（schema_version=1）で、失敗解析時に文脈が読みやすい。

### 16. Codegen は不要（Claude がテストを書く）

Playwright の Codegen（ユーザー操作を録画してテストコード生成）は実装しない。**Claude がテストを書く**ことを前提に設計する。

#### 16.1 Claude がテストを書くために必要な supporting 機能

Claude が自然にテストを書けるようにするには、**QGIS 側の UI を Claude が十分観察できる**必要がある。以下を満たす設計にする:

- `qgis_snapshot_ui` がテストシナリオの起点として使える粒度で UI ツリーを返す（objectName / class / text / 子要素を階層的に）
- 診断バンドル（§13）を Claude が読めるフォーマットで出力（JSON 中心、スクショは PNG）
- Locator syntax（§11）が Claude の学習データにあるパターン（Playwright 流）に近い

#### 16.2 Open: Claude がテストを書く上で要検討

以下は実運用しながら確認・調整する項目として保留:

- **対話的スクショ確認**：Claude がテスト作成中に「今どう見えてる？」と聞いて snapshot + screenshot を見られる環境（現状 MCP ツールでそのまま可能）
- **テンプレート / サンプルテストの整備**：Claude が真似られる canonical example を 3〜5 本用意
- **テストの意図を壊さない refactor 支援**：テスト失敗時に Claude が原因を絞り込める診断バンドルの情報量
- **snapshot 差分表示**：前回成功時の UI tree との差分を Claude に見せて「何が変わって失敗してるか」を推論させる
- **assertion 候補の推論材料**：snapshot から「この画面で何を検証すべきか」のヒントを返す機構

これらは Claude と実際にテスト作成を試行しながら、不足を順次追加する方針。

### 17. Flaky テスト対策

auto-wait（§8.3）と web-first assertions（§12）で多くの揺らぎは構造的に吸収されるが、それでも残る flaky 要因に対する方針を定める。

#### 17.1 Flaky の発生源と既存対策の対応

| 発生源 | 具体例 | 既存対策で吸収 |
|---|---|---|
| widget の表示遅延 | モーダル描画前の操作 | ✅ §8.3 auto-wait |
| 非同期処理の反映待ち | 保存後 DB 反映前のアサート | ✅ §12 web-first assertions |
| アニメーション中操作 | フェードイン中のボタンクリック | △ stable チェック（`## Roadmap`） |
| state leak | 前テスト残留の dialog / 選択状態 | ✅ §10 リセット戦略 |
| xdist worker 間干渉 | DB 名衝突、ポート衝突 | ✅ §10.6 / Model B |
| インフラ要因 | Hub 瞬断、ネットワーク共有遅延 | ❌ 本節で対応 |
| QGIS プロセスの一時不安定 | GDAL 空間インデックス等 | ❌ 本節で対応 |

#### 17.2 失敗種別ごとのリトライ戦略（既定）

失敗の例外種別に応じて自動リトライ方針を切り替える。auto-wait で既に待っている種別はリトライしない（時間の無駄になるため）。

| 失敗種別 | リトライ | 間に挟む処理 | 理由 |
|---|---|---|---|
| `widget_not_found` / `widget_not_actionable` | しない | — | §8.3 で既に 5 秒待機済み |
| web-first assertion の失敗 | しない | — | §12 で既に 5 秒再評価済み |
| `ConnectionError` / `TimeoutError`（Hub 切断） | 最大 2 回 | `fresh_qgis` | インフラ要因、QGIS 再起動で回復見込み |
| 予期せぬ Python 例外 | 1 回のみ | `fresh_qgis` | state leak の可能性、clean state で再試行 |

実装は `pytest_runtest_makereport` フックで例外種別を判定し、条件を満たせば次の実行をスケジュールします。

#### 17.3 Quarantine マーカー

発覚した flaky テストを一時的に隔離し、メイン結果に影響させない仕組みを用意します。

```python
@pytest.mark.quarantine(
    reason="flaky: ログイン後のモーダル閉鎖待ちで race",
    ticket="#123",
    until="2026-06-01",
)
def test_login_flow(qgis):
    ...
```

| 属性 | 必須 | 用途 |
|---|---|---|
| `reason` | ✅ | flaky の原因仮説（人間可読） |
| `ticket` | ✅ | 修正追跡用 issue 番号 |
| `until` | ✅ | 期限（YYYY-MM-DD）。過ぎたら CI で警告 |

運用ルール:

- quarantine されたテストは CI で実行するが、pass/fail をメイン結果に含めない（別レポートに集約）
- `until` 期限切れを CI の warning で検知し、修正 or 延長判断を強制
- 月次で quarantine 一覧をレビューし、放置を防ぐ

#### 17.4 `@pytest.mark.flaky`（問答無用リトライ）の使用ルール

`pytest-rerunfailures` による盲目的リトライは**原則使用しません**。理由は以下の通りです。

- 根本原因が隠れ、長期的にテストスイート全体の信頼性が劣化する
- CI 時間が不必要に伸びる
- §17.2 の失敗種別別リトライで多くのケースは既にカバーされる

例外的に使用する場合は以下を必須とします:

- PR レビューで使用妥当性を議論・合意
- コード上に根本原因と対応予定のコメントを残す
- §17.3 の quarantine と同様、期限を設定

#### 17.5 将来検討する項目

- **失敗連鎖防止の自動 fresh_qgis**：前テスト失敗時に次テストへ動的に `fresh_qgis` マーカーを付与（§10.7 の open 項目と連動）
- **flaky 検出の自動化**：CI 結果の履歴解析により「最近 10 回で 2 回以上失敗しているテスト」を自動 quarantine 候補としてレポート
- **stable チェック**（§8.3 の項目）：アニメーション中操作の吸収

### 18. Headless 化と CI 実行環境

CI で QGIS を走らせる場合、ディスプレイサーバーがない環境でも実行できる仕組みが必要です。Windows 前提のプロダクトを想定する場合、選択肢は実質的に Windows 環境での実 GUI or offscreen モードの 2 択になります。

#### 18.1 Headless 化の選択肢

| 方式 | 仕組み | 対応 OS | 備考 |
|---|---|---|---|
| `QT_QPA_PLATFORM=offscreen` | Qt が仮想サーフェスに描画、実ピクセル生成なし | 全 OS | 設定 1 行で済むが OpenGL 依存の map canvas でリスクあり |
| Xvfb | X 仮想 framebuffer で実描画に近い挙動 | Linux のみ | Windows 運用前提のプロダクトでは選択肢外 |
| Self-hosted runner（実 GUI） | 物理 / 仮想マシンに GUI を持たせる | 何でも | dev 環境と同等挙動だがインフラコスト増 |

#### 18.2 想定環境の制約（参考）

- QGIS のインストールは Windows 版を主に想定（macOS / Linux も動作するが本 ADR では未検証）
- ホストアプリの設定（SRID、DB 接続、ファイルパス）が Windows 前提の箇所がある可能性
- スクショベースの診断バンドル（§13）が成立しないと失敗解析が著しく困難になる
- 上記より、Linux 系の headless 方式は本 ADR のスコープ外とする

#### 18.3 方針：実 GUI モード第一選択

GitHub Actions の `windows-latest` runner は headed session を持つため、追加設定なしで QGIS の GUI が素で動作する可能性があります。以下の順で検証します。

1. **第一選択**：GitHub Actions Windows runner で QGIS を素のまま起動
   - offscreen 化を試す前に、何もせずに動くかを確認
   - 動けば追加設定ゼロで済むため最もシンプル

2. **Fallback A**：素で動かなかった場合に `QT_QPA_PLATFORM=offscreen` を部分適用
   - 起動まで到達するかをまず確認
   - map canvas 描画・スクショを必要とするテストは skip マーカーで分離
   - UI 操作ロジックのテスト（レイヤー描画を伴わない）だけ offscreen で実行

3. **Fallback B**：skip 数が多すぎる or 実行速度不足の場合に self-hosted Windows runner を検討（`## Roadmap`）

#### 18.4 着手時に実測する項目

以下を初期タスクとして実測し、最終方針を確定します。

| 確認項目 | 判定基準 |
|---|---|
| GitHub Actions Windows runner で QGIS 3.34.6 が起動するか | プロセスが 30 秒以内に起動完了 |
| 起動に必要な前提条件 | OSGeo4W インストール手順、`QGIS_PREFIX_PATH` / `PATH` 設定が CI で再現可能か |
| `QT_QPA_PLATFORM=offscreen` 時の map canvas 描画 | ベクターレイヤーのレンダリングが成立するか、スクショに期待した絵が出るか |
| モーダルダイアログの挙動 | `activeModalWidget()` のタイミングが dev 環境と一致するか |
| PostGIS 接続 | CI 環境からテスト用 PostGIS に接続できるか（service container 等の構成） |

#### 18.5 将来検討する項目

- **Self-hosted Windows runner**：skip 数が多い / runner スペック不足等の問題が出た場合
- **offscreen での描画系テスト skip 運用**：`@pytest.mark.requires_gui` マーカーの設計と適用範囲
- **複数 QGIS バージョン対応**：3.34 LTR 以外のバージョンを CI で並行検証する場合の matrix 構成
- **dev 環境との乖離監視**：CI で pass するが dev で fail、またはその逆が発生した場合の検知方法

### 19. pytest-qt の扱い

pytest-qt は「pytest フレームワークとしての側面」と「内部で利用している Qt test プリミティブ（`QSignalSpy` / `QTest.mouseClick` 等）」の 2 つの側面があり、新構成では扱いが異なります。

#### 19.1 pytest-qt フレームワークとしての側面

pytest と Qt アプリが同一プロセスである前提で設計されているため、pytest が QGIS 外部プロセスで動く新構成では構造的に利用できません。

| テスト種別 | 場所 | pytest-qt フレームワーク利用 |
|---|---|---|
| E2E テスト | `test_e2e/` | ❌ プロセス分離のため不可 |
| Unit テスト | `plugins/<your_plugin>/tests/` | ✅ 継続利用（QGIS 内で pytest を走らせる従来方式） |

#### 19.2 Qt test プリミティブの側面

`QSignalSpy` や `QTest.mouseClick` は PyQt5 標準の機能であり、pytest-qt に依存せず単独で利用可能です。**Worker 側ハンドラの実装内部では自由に利用できます**。

現状の `click_widget` は `widget.click()` を直接呼んでいますが、より厳密なイベントシミュレーションが必要になった場合は `QTest.mouseClick(widget, Qt.LeftButton)` への置き換えも選択肢です。

#### 19.3 pytest-qt 本体の機能は限定的

pytest-qt が提供する主要機能は以下の 5 種で、いずれも単純なラッパーです。必要が生じれば Worker 側ハンドラとして同等機能を個別実装できるため、フレームワークとしての代替品を用意する必要性は低いです。

- `qtbot.mouseClick` / `keyClicks` → `click_widget` / `set_widget_value` で代替済み
- `qtbot.waitSignal` → 下記 §19.4 で個別ハンドラ化可能
- `qtbot.waitUntil` → web-first assertions（§12）で代替
- `qtbot.waitExposed` → auto-wait（§8.3）で代替
- `QSignalSpy` → 下記 §19.4 で個別ハンドラ化可能

#### 19.4 将来検討する Worker 側ハンドラ候補

必要が生じた時点で追加する候補として以下を保留します。現時点で実装は行いません。**いずれも ADR-0001 §12 の Tier 3（Test）ハンドラとして登録する方針**で、`qgis_puppeteer` core には含めません。

- **シグナル待機ハンドラ**（`test.wait_for_signal`）：`QSignalSpy` を用いて指定オブジェクトのシグナル発火を待機
  ```python
  def wait_for_signal(
      object_selector: dict,
      signal_name: str,
      timeout_ms: int = 5000,
  ) -> dict:
      ...
  ```
- **厳密なイベントシミュレーション**（`test.click_strict` 等）：`QTest.mouseClick` / `QTest.keyClicks` ベースの操作ハンドラ
- **シグナル発火回数の検証**（`test.signal_spy_*`）：テスト中に指定シグナルが何回発火したかを記録・取得

#### 19.5 E2E 側の依存関係

`test_e2e/` の依存関係から pytest-qt を外し、以下のみに絞ります。

```
pytest
pytest-asyncio
pytest-xdist     # 並列実行
pytest-timeout   # テストタイムアウト強制
qgis_puppeteer # AutomationClient
```

Unit test 側（`plugins/<your_plugin>/tests/` 等）の依存関係は従来通り pytest-qt を含みます。

### 20. テスト専用機能の所有責任（ADR-0001 §12 との関係）

ADR-0001 §12 で定めたハンドラ拡張機構に基づき、**テスト専用機能は `qgis_puppeteer` core には含めず、テスト支援側で所有する**原則を本 ADR でも適用します。

#### 20.1 所有責任のマッピング

| 機能 | 所有場所 | Tier | 備考 |
|---|---|---|---|
| レイヤー / UI / python / screenshot 等の汎用操作 | `qgis_puppeteer.qgis_tools` | Tier 1 | §8〜§12 で利用する Core API |
| ドメイン固有操作（特定 feature の選択・編集等） | ドメインプラグイン本体 | Tier 2 | 必要性が出た時点で追加 |
| DB リセット（`test.reset_db`） | テスト支援側（§10.5） | Tier 3 | テンプレート DB 管理を含む |
| Seed データ投入（`test.seed_*`） | テスト支援側 | Tier 3 | environment / fixture 別 |
| Signal 待機 / 発火検証（`test.wait_for_signal` 等） | テスト支援側（§19.4） | Tier 3 | 必要性が出た時点で追加 |
| テスト用クリーン状態リセット（`test.soft_reset`） | テスト支援側 | Tier 3 | §10.4 の soft_reset 相当 |

#### 20.2 テスト支援側の配置

ADR-0001 §12.5 の通り、Tier 3 ハンドラは以下 2 経路で登録します。

- **定常的なハンドラ**: 専用テスト支援プラグイン `plugins/qgis_puppet_test_helpers/`（名称は仮）に `puppeteer_api.py` を置き、`qgis_puppet` の discover 機構で自動登録される。E2E 実行時のみ有効化する運用（プラグインマネージャ ON/OFF、または pytest fixture が起動時に有効化）
- **テスト個別のハンドラ**: pytest から `execute_python` 経由で動的登録（信頼モード下）

**注意**: Gateway 側には test 専用ツールは配置しない。pytest は `qgis_puppeteer.client.AutomationClient` を直接使って Hub に接続する設計のため、MCP Gateway（Claude 向け）を経由しないためである。

具体的な配置場所（`plugins/qgis_puppet_test_helpers/` vs `test_e2e/handlers/` 等の細部）は §10.7 の open 項目（テスト専用リソースの配置場所）とあわせて運用しながら決定します。

#### 20.3 役割境界としての利点

- Core の責任範囲が明確（「誰が使っても価値がある機能のみ」）
- Claude Desktop 利用時に無関係なコマンド（`test.*`）が露出しない
- テスト固有の実装詳細（DB リセット手順等）がプロダクションコードに紛れ込まない
- テスト側の変更が core パッケージの破壊的変更を要さない

### 21. テストラン全体のサマリレポート

§13 の診断バンドルは個別テストの失敗情報を対象とするのに対し、本節では**テストラン全体の集計とサマリ**の提示方法を定めます。

#### 21.1 サマリレポートの要件

1. CI 実行後の結果確認：PR に何が起きたかを即座に把握できる
2. 失敗時の deep dive：個別テストの診断バンドルへ素早く到達できる
3. flaky 傾向の追跡：過去実行との比較、quarantine 候補の発見
4. environment 別の pass/fail 把握：[ADR-0003 Test Environments](0003-test-environments.md) と連動

#### 21.2 既定方式：JSON + pytest_terminal_summary

現状は以下に絞って実装します。

- 構造化 JSON サマリを `outputs/e2e/{run_id}/summary.json` に出力
- pytest 実行後のコンソール出力に独自サマリブロックを追加（`pytest_terminal_summary` フック）
- HTML レンダラーは将来追加予定（JSON が基盤になる、`## Roadmap`）

pytest-html や JUnit XML の単独採用を見送った理由:

- pytest-html は診断バンドルへのリンクや environment 別集計が素で扱えない
- JUnit XML は CI サービス標準だが、独自メタ情報（quarantine、flaky 候補、environment）を表現できない
- JSON + 後段のレンダラー構成にすることで、CI 表示と将来の HTML ビューアを両立できる

#### 21.3 summary.json のスキーマ

```json
{
  "run_id": "2026-04-24T10-15-33_abc123",
  "started_at": "2026-04-24T10:15:33Z",
  "finished_at": "2026-04-24T10:28:12Z",
  "duration_seconds": 759,
  "runner_info": {
    "git_sha": "4f1bce0...",
    "branch": "main",
    "runner": "github-actions-windows",
    "qgis_version": "3.34.6",
    "python_version": "3.12.x"
  },
  "totals": {
    "passed": 42,
    "failed": 3,
    "skipped": 2,
    "quarantined": 1,
    "error": 0
  },
  "by_environment": {
    "smoke":               {"passed": 15, "failed": 1, "skipped": 0},
    "with_authentication": {"passed": 14, "failed": 1, "skipped": 1},
    "offline_mode":        {"passed": 13, "failed": 1, "skipped": 1}
  },
  "tests": [
    {
      "nodeid": "test_e2e/test_login.py::test_login_flow",
      "environment": "with_authentication",
      "status": "failed",
      "duration_seconds": 5.3,
      "markers": ["reset_db"],
      "diagnostic_bundle": "with_authentication/diagnostics/test_login_flow_2026-04-24T10-20-15/",
      "error_type": "widget_not_actionable",
      "retries": 0
    }
  ],
  "quarantined_tests": [
    {
      "nodeid": "...",
      "reason": "flaky: モーダル race",
      "ticket": "#123",
      "until": "2026-06-01",
      "until_status": "ok"
    }
  ],
  "flaky_candidates": [
    {"nodeid": "...", "recent_failure_rate": 0.2}
  ]
}
```

各フィールドの意味:

| フィールド | 用途 |
|---|---|
| `run_id` | `outputs/{run_id}/` ディレクトリ名と対応 |
| `runner_info` | 再現性確保のためのメタ情報（CI runner / git sha 等） |
| `by_environment` | environment（ADR-0003）ごとの集計 |
| `tests[].environment` | このテストが走った env 名 |
| `tests[].diagnostic_bundle` | §13 の診断バンドルへの相対パス（env 別ディレクトリ含む） |
| `tests[].error_type` | §17.2 のリトライ戦略判定に使った例外種別 |
| `quarantined_tests[].until_status` | `ok` / `expired`（期限切れ警告用） |
| `flaky_candidates` | §17.5 の flaky 自動検出の結果（将来拡充、`## Roadmap`） |

#### 21.4 コンソール出力フォーマット

`pytest_terminal_summary` フックで以下のブロックを追加表示します。

```
========= E2E Summary =========
Run ID:     2026-04-24T10-15-33_abc123
Duration:   12m 39s
Environments: smoke, with_authentication, offline_mode

By environment:
  smoke               : 15 pass / 1 fail
  with_authentication : 14 pass / 1 fail / 1 skip
  offline_mode        : 13 pass / 1 fail / 1 skip

Quarantined: 1 (until OK)
Flaky candidates: 1 test (recent failure rate 20%+)

Diagnostic bundles: outputs/2026-04-24T10-15-33_abc123/
```

#### 21.5 CI integration

- GitHub Actions の `upload-artifact` で `outputs/e2e/{run_id}/` を成果物化
- **容量最適化**：成功時は `summary.json` のみ artifact 化、失敗時のみ診断バンドル全量をアップロード
- PR コメントへの自動投稿は将来検討（`summary.json` をテンプレートでフォーマット、`## Roadmap`）

#### 21.6 将来検討する項目

- **HTML レンダラー**：`summary.json` → HTML のスタンドアロンツール（Python + Jinja2）
  - テストごとのカード表示
  - スクショ列のタイムライン表示（§13 の診断バンドルから）
  - 診断バンドルへの直リンク
  - GitHub Pages か CI artifact で配布
- **履歴比較機能**：前回成功実行との差分（新規失敗、回復した失敗、実行時間の変化）
- **flaky 自動検出との連動**（§17.5）：過去 N 実行の失敗率に基づく quarantine 候補レポート
- **PR コメント自動投稿**：`summary.json` を元に GitHub Actions ワークフローでコメント投稿

## Consequences

### Positive

- `exec_()` 系モーダルを含む真の E2E テストが書けるようになる
- テストコードが命令型の逐次構造で書け、Playwright/Cypress 風になり学習コストが低い
- pytest 実行環境が `uv` 管理の独立 venv になり、依存バージョン管理が `pyproject.toml` に一元化される
- 複数 QGIS 同時操作テストが可能（将来検証、`## Roadmap`）
- 保守対象コードが大幅に減る（`pytest_runner/`, helpers 合わせて数千行が消える）
- CI に乗せやすい（ターミナルから `pytest` を叩くだけの標準構成）

### Negative

- ADR-0001 の実装完了が前提（並行作業になる）
- 「QGIS プラグインメニューからワンクリックでテスト実行」のワークフローは消える（IDE のテストランナー等で代替）
- 既存 E2E テスト資産（試作レベル）は破棄される
- QGIS 起動 subprocess の管理（PID 管理、ゾンビプロセス対策）を新規に実装する必要

### Neutral

- `test_e2e/conftest.py` は大部分書き直し
- CI Windows runner の確保が必要（既存になければ）
- Headless QGIS の動作確認が必要（`QT_QPA_PLATFORM=offscreen`）

## Open Questions

1. **QGIS subprocess 管理の実装詳細**：
   - Windows 固有の `CREATE_NEW_PROCESS_GROUP` フラグで子プロセスを独立起動するか
   - 異常終了時のゾンビ QGIS 検出・クリーンアップ（pytest fixture の teardown で kill）
   - `tests/e2e/` 外から実行する時の PID 競合

2. **テスト間のクリーン状態リセット戦略**：
   - プロジェクト再読み込み vs QGIS 再起動 のトレードオフ
   - ログイン状態、選択状態、開いているダイアログのクリーンアップ
   - リセット失敗時の fallback（次のテストを skip するか強制 pass するか）

3. **Headless 化の可否**：
   - `QT_QPA_PLATFORM=offscreen` で動くか、OpenGL 依存のレイヤー描画でコケないか検証要
   - Xvfb を CI で使うか（Linux 版 QGIS 対応が必要になった場合）

4. **CI Runner の選定**：
   - GitHub Actions の Windows runner にするか、自前 Windows VM にするか
   - QGIS プリインストール済みイメージ or 毎回インストール

5. **Instance ラベルの自動付与**：
   - CI で起動する QGIS に `--instance-label=ci-test-session-1` 等を渡すか
   - 複数セッション並行時のラベル衝突回避

6. **テストデータの管理**：
   - 既存 `test_e2e/fixtures/` 構造の再利用可否
   - DB 状態のスナップショット・リセット戦略（PostGIS のトランザクション隔離？）

上記 Open Questions は本格対応時に個別に詰める。

## Roadmap

将来検討する拡張項目をここに集約する（実装順や実施可否は未確定）。

### Selector 拡張（§8.4）

- `text_re`: 正規表現マッチ（動的テキスト「項目 12 件」等）
- `attr`: 任意プロパティマッチ
- ダブルクリック / 右クリック / キー入力の専用コマンド
- ~~**`getByRole` 相当**: Qt Accessible role / `QAccessibleInterface` 経由の selector~~
  ✅ 実装済（§8.4 記載）

### ~~Locator chain (`parent.locator(child)`)~~ — dev で実装済み

→ Locator に `locator(child)` メソッドを追加。Worker 側の `_find_widget` /
`_find_in_snapshot` が `_scope_chain` キーを解釈して「親 widget の subtree 内で
子 selector を解決」する。Playwright 互換の chain semantics。

### ~~`getByLabel` / `getByPlaceholder`~~ — 実装済み

→ §8.1 selector 表に `label` / `placeholder` キーを追加。`label` は
`QLabel.buddy()` 経由でラベル文字列に紐付く widget を引く（objectName 未付与の
input を救う用途）。`placeholder` は LineEdit/TextEdit の `placeholderText()`
で絞る。snapshot 側もパリティ。

### ~~Strict mode（複数マッチで fail）~~ — 実装済み

→ §8.1「Strict mode」を参照。`_find_widget` で複数マッチ + `index` 未指定 →
`selector_ambiguous`、Locator 側で early-fail（`SelectorAmbiguousError`）。
`click_widget` / `set_widget_value` のエラーコードも分離。

### ~~Auto-wait `stable` チェック（§8.3）~~ ✅ 実装済

`Locator(stable=True)` で opt-in。Worker 側 `check_actionability` が widget の
`geometry: {x, y, width, height}` を返し、Client 側 `_wait_actionable` が直前 1 回
と一致したら ready と判定する。最低 2 回 RTT（初回は前値が無いので未確定）。
actionable が崩れた瞬間に stable の積み上げは reset される。普通の widget で
不要な 1 RTT を払いたくないので default は False（opt-in）。

### ~~Auto-wait `editable`~~ — 実装済み / occlusion 強化は継続

`qgis_check_actionability` の判定軸:

- **`editable`** ✅: `widget.isReadOnly()` 判定。`set_widget_value` 側に
  `widget_readonly` guard も追加。Locator の `fill()` のみが auto-wait で
  `editable=True` を要求（`click()` / `select()` / `check()` は要求しない）
- **`receives_events`（hit-test ベース occlusion 検出）**: 未実装。
  `app.widgetAt(globalPos)` で「クリック座標で実際に最前面にいる widget」を
  確認。QToolBar / QSplitter などモーダル以外で物理的に覆われるケースを拾える。
  現状の `_is_not_covered_by_modal` はモーダル子孫判定のみ

### ~~入れ子 modal の取得（§8.4）~~ ✅ 実装済

`scope="modal_stack[N]"` で N 番目の modal を root に解決する（N=0 が外側、
N=-1 が最前面）。Worker 側 `_list_modal_stack()` が `app.topLevelWidgets()`
から `isVisible() and isModal()` な widget を集め、parent chain depth で sort
（深いほど後から開かれた）。Qt API は modal の open 順を直接保持しないため
sort は best-effort で、同 depth の順序は不定。
- 既存 `scope="modal"` は最前面のみ取る挙動を維持（= `modal_stack[-1]` 相当）。
- `modal_stack[N]` で stack が空 → `reason="no_modals_in_stack"`。
- index out of range → `reason="modal_stack_index_out_of_range"` + `stack_size`。
- `QFileDialog` の中で確認警告 `QMessageBox` が出る等の入れ子シナリオで、
  外側 dialog 内の widget に届きたい時に使う。

### pytest-xdist 並列実行（§4 / §10.7）

- Worker ごとに独立 Hub ポート + QGIS プロセスを spawn する fixture 構成は既に成立。並列環境での実機検証が未済
- Worker ごとの DB 分離戦略（`${pytest_xdist_worker}` suffix 等）。environment 軸（ADR-0003）と直交

### 複数 QGIS 同時操作テスト（§5）

複数 Worker fixture（`qgis_a`, `qgis_b`）を使ったマルチ instance シナリオの実機検証。

### Test Environments — 設計のみ（[ADR-0003](0003-test-environments.md)）

テスト群ごとに QGIS 起動構成を切り替える partition モデル。`environments.toml`
+ `@pytest.mark.qgis_env(...)` + `pytest --env=all` で CI 1 コマンドの多 env
実行を実現する。実装は ADR-0003 のフェーズに従う。

### remote / 混在シナリオでの caller_role 転送（§9.2 案 B）

remote 対応 + 異種 Client 混在が必要になった時点で Hub→Worker の caller_role 転送を組み込む。env による信頼モード分離からの切り替え。

### Worker 側 Test ハンドラ追加（§19.4）

- `test.wait_for_signal`（QSignalSpy 利用）
- `test.click_strict`（QTest.mouseClick / keyClicks 利用）
- `test.signal_spy_*`（シグナル発火回数の検証）

### ~~Dialog handler（グローバル auto-respond）~~ — 実装済み

→ §13bis「Dialog handler」を参照。Worker 側で 250ms 周期で
`activeModalWidget()` を polling し、登録された predicate にマッチすれば
`accept` / `reject` / `close` を実行。「想定外モーダル → 毎テスト手動 dismiss」
boilerplate を構造的に削減。Claude Desktop / pytest / スクリプトすべてで利用可能。

- Tier 1 ハンドラ: `qgis_register_dialog_handler` / `qgis_unregister_dialog_handler`
  / `qgis_list_dialog_handlers` / `qgis_clear_dialog_handlers`
- Action: `accept` / `reject` / `close`
- `once=True` で一度発火したら自動 unregister
- 同じ modal を複数回処理しない冪等性 (`id()` ベースの状態管理)

### 未捕捉 Qt 例外 → テスト失敗連動（§19.4 関連、✅ 実装済）

Qt slot 内で発生した未捕捉例外は `sys.excepthook` 経由で stderr に出るだけで
pytest の pass/fail には反映されない（C++ → Python boundary で握り潰される）。
これを救うため:

1. **Worker 側** (`qgis_puppeteer.qgis_tools.exception_recorder`): `build_handlers()`
   が `install_excepthook()` を呼び、未捕捉例外を maxlen=200 のリングバッファに
   記録。previous hook を chain して保持するので stderr 出力は維持される。
   `type` / `message` / `traceback` / `ts` / `thread` を辞書で保持。
2. **公開ハンドラ**: `qgis_get_recent_exceptions(limit?)` /
   `qgis_clear_recent_exceptions()`
3. **Plugin 側** (`pytest_qgis_puppeteer`):
   - ini `qgis_fail_on_uncaught_exception`（既定 ON、opt-out）
   - `pytest_runtest_call` hookwrapper: call 直前に `clear_recent_exceptions()`
     で buffer を空にする（前テストの exceptions が混入しないように）
   - `pytest_runtest_makereport` hookwrapper: call 後に `get_recent_exceptions()`
     を query。1 件でも有れば、test が pass していたら `report.outcome="failed"`
     に格上げ（longrepr に整形表示）、既に failed なら `report.sections` に追加
   - 失敗 test の diagnostic bundle に `uncaught_exceptions.json` を同梱
4. **dev mode** (`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`): check skip（既存 QGIS の
   蓄積例外を巻き込まない）

### Hub/Worker プロトコル: deferred 実行モード

長時間 Qt slot（例: ログイン → DB 接続 → プロジェクトロードまで一気に走る
ボタン click）を同期 RPC で叩くと 30 秒のリクエスト timeout を超える。現状は
`QTimer.singleShot(0, btn.click)` の fire-and-forget でテスト側がハック対応する
ことになっている。

構造的解決として Request にモード指定を入れる:

- `mode: "sync"`（既定、現状通り）
- `mode: "fire_and_forget"`: 即 ack を返し、結果は別途 `qgis_get_command_result(id)` で問い合わせ
- `mode: "deferred"`: `QTimer.singleShot(0, ...)` で event loop 復帰後に実行 + ack

プロトコル拡張案として ADR-0001 §3 の Request 構造を改訂する。

### Hub/Worker プロトコル: push event 機構

現状は Request/Response の一方向。「dialog が開いた」「project がロード完了
した」などのイベントを Worker が能動的に push できるとテスト側の poll が消せる。

- ADR-0001 §3 に `Event` メッセージ型を追加
- 購読は Client 側の `client.subscribe(event_type, callback)` で
- 既存の同期 Request/Response とは独立チャネル

コスト L、優先度低だが C2 / J6 系の課題が累積したら着手。

### 失敗連鎖防止の自動 fresh_qgis（§17.5）✅ 実装済

ini の `qgis_auto_fresh_after_failure = true`（既定 false）で opt-in。直前テストの
call フェーズが failed なら、`pytest_runtest_makereport` が config 上の flag を
立て、次テストの `pytest_runtest_setup` がそれを読んで現テストへ動的に
`@pytest.mark.fresh_qgis` を付与する。flag は読み次第クリアされるので連鎖は次
1 件のみ（連続失敗時は毎回 fresh する）。既に明示 `fresh_qgis` marker が付いて
いる test では二重付与しない。

### Flaky 検出の自動化（§17.5）

CI 結果の履歴解析により最近 N 回で M 回以上失敗しているテストを自動 quarantine 候補としてレポート。

### Self-hosted Windows runner（§18.5）

skip 数が多い / runner スペック不足等の問題が出た場合に検討。

### サマリレポート HTML レンダラー（§21.6）

- `summary.json` → HTML スタンドアロンツール（Python + Jinja2）
- スクショ列タイムライン、診断バンドルへの直リンク
- 履歴比較（前回成功実行との差分）
- PR コメント自動投稿

### 動画録画方式の診断バンドル（§13.2）

スクショ列に加え、Qt 連続キャプチャによる動画記録の検討。アニメ中挙動の解析用途で有効になった場合のみ追加。

### 診断バンドルへの log 同梱（§13）

現状の診断バンドルは screenshot / ui_snapshot / list_instances のみ。失敗解析を
強化するため以下を追加:

- **QGIS message log**: `QgsMessageLog` の出力を Worker 側で循環バッファに
  保持し、診断時に `qgis_get_message_log(tail_n)` で取得して `qgis_message.log`
  として保存
- **Hub stdout/stderr**: `hub_process` fixture が PIPE で読むだけで現状 teardown
  時に捨てている。診断時にファイル化する
- **Worker stdout/stderr**: 同様に保存経路を作る

### Test isolation 強化

Playwright の "browser context" 相当を Qt / QGIS で実現する:

- **専用 QGIS profile の自動構築**: 各 pytest session で独立 profile
  ディレクトリを `--profiles-path` に指定。QSettings / 認証情報 / プラグイン
  状態の汚染を防ぐ
- **プラグイン module-global の reset**: `MyPluginProject.instance()` のような
  シングルトンが session 跨ぎで残る問題に対応する Worker 側 hook
  （`test.reset_plugin_state(plugin_name)` ハンドラ）
- **テスト順序独立性のドキュメント化**: ADR 内で「ファイル名昇順依存は
  anti-pattern」「`@pytest.mark.fresh_qgis` で独立性を担保」と明記

### ~~Selector resolver の統合~~ — 実装済み

→ `qgis_puppeteer.selector_match` モジュールに `record_matches_selector` /
`resolve_with_index` を切り出し、`_find_widget`（live tree）と
`_find_in_snapshot`（snapshot DFS）の両方から呼ぶ単一実装に統合。strict mode /
`index` 解釈 / `label` / `placeholder` 等の selector 仕様が両経路で揃う。

### Locale 固定ガイダンス（ドキュメント）

QGIS の表示言語は OS locale 依存。テストで「保存」「キャンセル」等の文字列を
selector の `text` で引くと locale が変わると壊れる。OSS 側ドキュメントで:

- `QT_LOCALE` / QGIS の `--lang` での固定方法
- `text` ではなく `object_name` / `getByRole` を優先する慣行
- locale 依存になっている場合の lint 推奨

を明記する。コード変更は不要、README / ADR の追記のみ。

## References

- ADR-0001: QGIS Puppeteer アーキテクチャ（複数 QGIS 同時操作基盤）
- Playwright テスト設計思想: <https://playwright.dev/docs/best-practices>
- pytest-asyncio: <https://pytest-asyncio.readthedocs.io/>
- Qt Test Framework (QtTest) と実プロセス分離テストの比較: Qt 公式ドキュメント
