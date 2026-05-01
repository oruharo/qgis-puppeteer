# ADR-0001: QGIS Puppeteer アーキテクチャ（複数 QGIS 同時操作基盤）

- **Status**: Accepted
- **Date**: 2026-04-23
- **Deciders**: qgis-puppeteer maintainers
- **Related**: `plugins/qgis_puppet/` と `packages/qgis-puppeteer/src/qgis_puppeteer/gateways/mcp.py` が本 ADR のランタイム実装

## Context

### 現行構成

Claude Desktop から QGIS を操作するため、以下の構成で MCP 連携を実現している。

```
[Claude Desktop (Code 機能)]
         │ stdio
         ▼
[MCP サーバー (Python, uv spawn)]
         │ TCP :9876 (raw JSON)
         ▼
[QGIS + ドメインプラグイン] ← socket server
```

- ドメインプラグインが QGIS 起動時に TCP 9876 で `listen()`
- MCP サーバーが `localhost:9876` に `connect()`
- 1 コネクション 1 リクエストの単純なプロトコル

### 現行構成の限界

複数の QGIS を同時に立ち上げて使い分けたい運用要求に応えられない。

| 問題 | 詳細 |
|------|------|
| **ポート衝突** | 2 つ目の QGIS が `bind(9876)` に失敗 |
| **宛先指定の欠如** | プロトコルに「どの QGIS か」を指定する仕組みがない |
| **ディスカバリの欠如** | MCP サーバーは固定ポートにしか繋がない |

さらに将来的には別 PC の QGIS も操作したい要求があり、その際はファイアウォール開放を最小限にするため **単一ポート集約** が必須になる。

加えて、同等の操作基盤を E2E テスト（pytest）からも共有したい（ADR-0002 参照）。現行構成は MCP 専用に作られており、他の利用者から使える汎用性がない。

### 検討した代替案

| 案 | 方式 | 却下理由 |
|---|---|---|
| A. 手動マルチ登録 | QGIS ごとに別ポート・別 MCP エントリを `claude_desktop_config.json` に記載 | インスタンス数固定、ユーザー手作業が増える |
| B. レジストリファイル | プラグイン起動時に `%APPDATA%\qgis_puppeteer\instances.json` に自分の情報を書き、MCP が読む | stale エントリ掃除、競合、ファイル I/O のライフサイクル管理が煩雑 |
| C. ポートレンジスキャン | プラグインがレンジから空きポートを取得、MCP が全ポート probe | 単一 PC では動くが remote 時にファイアウォール開放が広くなり破綻 |

## Decision

**中央集約プロセス（Hub）+ 逆向き接続モデル** を採用し、アーキテクチャを 2 レイヤーに分離する。

### 2 レイヤー構造

1. **QGIS Puppeteer Layer**（MCP 非依存）
   - `Hub`：中央集約プロセス。単一ポートで listen、ルーティングを担う
   - `Worker`：QGIS プラグイン。Hub に dial out してコマンドを実行
   - `AutomationClient`：自動化クライアント。pytest など MCP を介さない利用者が直接使う
   - プロトコル：JSON over WebSocket（内部プロトコル）

2. **MCP Protocol Layer**（MCP 依存）
   - `McpGateway`：Claude Desktop が stdio で spawn する MCP サーバー。MCP プロトコルを自動化層の内部プロトコルに変換する薄いレイヤー
   - 外部 MCP 識別子：`qgis-puppeteer`（`claude_desktop_config.json` の `mcpServers` key）
   - MCP ツール名プレフィックス：`mcp__qgis-puppeteer__*`

アーキテクチャ全体図は `docs/adr/assets/0013-architecture.svg` を参照。

### 基本方針

1. **Worker は server ではなく client**
   - Worker は起動時に Hub へ WebSocket で dial out
   - 2 台目以降の QGIS はすでに起動している Hub に繋ぐだけ
   - ポート衝突が構造的に起きない

2. **Hub は自律的にライフサイクル管理する**
   - 初回 QGIS 起動時に Worker プラグインが spawn
   - 全 Worker 接続が切れたらグレース 30 秒後に idle 自動終了
   - デーモン管理・手動起動・Windows サービス登録不要

3. **単一ポートで全員喋る**
   - McpGateway ⇄ Hub ⇄ Worker / AutomationClient ⇄ Hub すべて 9876
   - remote 対応時は Hub の外向きポートだけ開ければ済む

4. **プロトコルは WebSocket**
   - Worker が client でも、コマンド方向（Hub → Worker）が自然に書ける
   - QGIS にバンドル済みの `PyQt5.QtWebSockets` を使い pip 依存ゼロ

5. **MCP は 1 つのエントリポイントに過ぎない**
   - Automation 層は MCP を一切知らない
   - MCP 以外のエントリ（pytest 用 `AutomationClient`、将来の HTTP API、CLI）を同じ基盤にぶら下げられる

## 詳細設計

### 1. コンポーネント

| コンポーネント | 実行環境 | WebSocket ライブラリ | 役割 |
|---|---|---|---|
| `McpGateway` | `uv --directory ... run` | `websockets`（pypi） | MCP プロトコル実装。`@mcp.tool()` 定義群。Claude Desktop が spawn |
| `Hub` | QGIS の `python.exe` を subprocess 起動 | `PyQt5.QtWebSockets.QWebSocketServer` | 単一ポート listen、ルーティング、ライフサイクル管理 |
| `Worker` | QGIS の Python（Qt 込み） | `PyQt5.QtWebSockets.QWebSocket` | Hub に dial out、コマンド受信・実行・応答 |
| `AutomationClient` | 任意の Python 環境 | `websockets`（pypi） | Hub への WS クライアントラッパ。pytest や外部ツールから利用 |

### 2. パッケージ構造

```
qgis_puppeteer/                        ← 自動化層のコアパッケージ
    __init__.py
    hub.py                               ← Hub クラス（Qt 配線）
    hub_state.py                         ← Hub 純ロジック（Qt 非依存、ユニットテスト用）
    hub_spawn.py                         ← Hub subprocess 起動のロック/probe ロジック
    _hub_bootstrap.py                    ← Hub subprocess 内の sys.path セットアップ
    worker.py                            ← Worker クラス（Qt 配線）
    worker_state.py                      ← Worker 純ロジック（namespace 検証、caller_role 判定）
    client.py                            ← AutomationClient クラス
    protocol.py                          ← 内部プロトコル定義（dataclass）
    qgis_tools/                          ← Tier 1 Core handler の実装（qgis_*）
        layer_tools.py / ui_tools.py / screenshot_tools.py /
        python_executor.py / permission_manager.py / code_analyzer.py
    gateways/
        __init__.py
        mcp.py                           ← McpGateway（将来 http.py, cli.py を並列に）
    scripts/
        bench_rtt.py                     ← RTT 実測用ベンチ

plugins/qgis_puppet/                   ← QGIS プラグイン（Worker 外殻 + Core 登録）
    __init__.py
    plugin.py                            ← qgis_puppeteer.Worker を生成・起動、extension discover
    plugin_helpers.py                    ← PyQt5 非依存のヘルパ（Python 解決、spawn 引数構築、discover）
    handlers.py                          ← Tier 1 Core handler の組み立て（iface と bind）
    metadata.txt                         ← QGIS プラグインメタデータ

test_e2e/helpers/
    automation_client.py                 ← AutomationClient の pytest 向けラッパ（ADR-0002）
```

ドメイン拡張時のレイアウト想定（§12.4 パターン A に沿う）:

```
mydomain_puppeteer/                       ← ドメイン固有 MCP Gateway（パターン A、§12.4）
    mydomain_puppeteer/gateways/mcp.py      ← @mcp.tool() で mydomain.* を宣言

plugins/mydomain/                         ← ドメイン本体プラグイン
    ...
    puppeteer_api.py                     ← qgis_puppet が discover する entry point（§12.3）

plugins/qgis_puppet_test_helpers/      ← Tier 3 Test 用 extension plugin
    __init__.py
    plugin.py
    puppeteer_api.py                     ← test.* handler の提供
```

### 3. プロトコル

#### フレーミング
- WebSocket テキストフレーム 1 つが 1 メッセージ
- ペイロードは JSON
- 全メッセージ共通フィールド：`type`, `id`（UUID 文字列、リクエスト／レスポンス対応用）

#### メッセージ種別

##### Worker → Hub（dial out 直後）

```json
{
  "type": "register",
  "id": "uuid-1",
  "protocol_version": 1,
  "role": "worker",
  "pid": 1234,
  "label": "A",
  "project": "D:/work/a.qgz",
  "started_at": "2026-04-23T09:00:00Z"
}
```

Hub は label の重複検証後、登録成立を返す：

```json
{"type": "register_ack", "id": "uuid-1", "ok": true, "instance_id": "worker-a-1234"}
```

label が空の場合は `{project_basename}-{pid 下4桁}` を自動採番。

##### McpGateway / AutomationClient → Hub

```json
{
  "type": "register",
  "id": "uuid-2",
  "protocol_version": 1,
  "role": "mcp_gateway"
}
```

または `"role": "automation_client"`。

##### コマンド呼び出し

```json
// client → hub
{
  "type": "request",
  "id": "uuid-3",
  "instance": "A",
  "command": "qgis_list_layers",
  "params": {}
}

// hub → worker (instance "A" の接続に転送、id は維持)
// ... 同じ内容

// worker → hub → client
{
  "type": "response",
  "id": "uuid-3",
  "ok": true,
  "result": { "layers": [...] }
}
```

##### インスタンス一覧

```json
{"type": "list_instances", "id": "uuid-4"}
```

```json
{
  "type": "list_instances_response",
  "id": "uuid-4",
  "instances": [
    {"instance_id": "worker-a-1234", "label": "A", "pid": 1234, "project": "a.qgz"},
    {"instance_id": "worker-b-5678", "label": "B", "pid": 5678, "project": "b.qgz"}
  ]
}
```

##### エラー

```json
{
  "type": "response",
  "id": "uuid-3",
  "ok": false,
  "error": {"code": "instance_not_found", "message": "No instance matches 'C'"}
}
```

エラーコード初期セット：
- `instance_not_found`: 指定 instance が見つからない
- `instance_ambiguous`: 複数マッチ（instance 未指定で 2 台以上）
- `instance_timeout`: Worker からの応答がタイムアウト
- `protocol_version_mismatch`: プロトコルバージョン不整合
- `invalid_command`: 未知のコマンド
- `worker_execution_error`: Worker 側で例外発生（stack trace を `error.details` に）

### 4. ライフサイクル

#### Hub の起動競合回避（親主導の spawn 確認モデル）

Windows の `subprocess.Popen` は `pass_fds=` を受け付けない（POSIX 専用）ため、ロック fd を子プロセスに継承する設計は不可。**親プロセスがロックを保持したまま Hub の listen 成功を確認し、確認後にロックを解放する**モデルを採る。

**ロック / pid ファイルのパスは port 番号サフィックスで分離**する。xdist 並列実行や複数 port での Hub 同時起動（ADR-0002 の E2E 構成）で固定パスが共有されロック競合する事態を避ける:

- `%APPDATA%\qgis_puppeteer\hub-{port}.lock`
- `%APPDATA%\qgis_puppeteer\hub-{port}.pid`

**外部所有者モード（§6 参照）ではロック機構をスキップする**。`QPUPPETEER_HUB_PORT` / `QPUPPETEER_HUB_HOST` が設定されている場合、Worker は Hub spawn 自体を行わないためロック競合は発生しない。

```python
# Worker（プラグイン）側
_BACKOFF = [0.3, 0.6, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0]  # 初回接続用

def ensure_hub_connected() -> QWebSocket:
    for attempt, delay in enumerate(_BACKOFF):
        try:
            return connect_to_hub()
        except ConnectionRefusedError:
            if attempt == 0:
                try_spawn_hub_with_lock()  # ロック獲得できた者だけが spawn
            time.sleep(delay)
    raise HubStartupError(f"Failed to reach hub after {len(_BACKOFF)} attempts")

def try_spawn_hub_with_lock() -> None:
    """
    %APPDATA%\\qgis_puppeteer\\hub.lock を排他ロック取得 → Hub spawn → listen 成功を probe
    → ロック解放。ロック取得に失敗したら他プロセスが spawn 中なので何もしない。
    """
    lock_path = Path(os.environ["APPDATA"]) / "qgis_puppeteer" / "hub.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # non-blocking
    except OSError:
        os.close(fd)
        return  # 他プロセスが spawn 中

    try:
        # Hub spawn（ロックは親が保持したまま）
        proc = subprocess.Popen(
            [sys.executable, "-m", "qgis_puppeteer.hub"],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=open(hub_log_path, "a"),
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        # Hub が :9876 で listen するまで最大 5 秒 probe
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise HubStartupError(f"Hub exited immediately with code {proc.returncode}")
            if _tcp_probe("127.0.0.1", 9876, timeout=0.2):
                break
            time.sleep(0.1)
        else:
            proc.kill()
            raise HubStartupError("Hub did not start listening within 5s")
    finally:
        # probe 成否に関わらずロック解放（Hub プロセスは独立して生存）
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)

def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    """Hub が listen しているか確認（connect して即切断）"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False
```

**TCP probe の限界と補償**：`_tcp_probe` は TCP connect 成功のみ確認する。`QWebSocketServer.listen()` が成功していれば TCP accept は即座に可能だが、ごく短い窓で WebSocket handshake 未準備の場合がありうる。これは Worker の初回 connect 時のバックオフ先頭（0.3 秒）で吸収できる前提とする。念のため **Hub 側で `pid_file` を「WS server の `newConnection` シグナル接続完了後」に書き込む**ことで、probe の代替として `pid_file` 出現を待つ選択肢も残しておく。

```python
# Hub 側起動時
def main():
    pid_file = Path(os.environ["APPDATA"]) / "qgis_puppeteer" / "hub.pid"
    try:
        server.listen(QHostAddress.LocalHost, 9876)  # 127.0.0.1 固定
    except OSError:
        # 既に他の Hub が bind している → 自分は降りる（敗者）
        sys.exit(0)
    pid_file.write_text(f"{os.getpid()}\n{time.time()}\n")
    try:
        app.exec_()
    finally:
        pid_file.unlink(missing_ok=True)
```

これにより：
- 親がロック保持中に listen 成否を確認できる → 勝者がクラッシュしても検知して失敗する
- ロック解放までは他プロセスの並列 spawn が抑制される
- 敗者 Hub は `bind()` 失敗で即自壊（ゾンビ化しない）
- Windows `pass_fds` 制約を回避

#### Hub の idle 自動終了（条件の厳格化）

- 状態：`worker_connections: dict[str, WorkerEntry]`, `client_connections: list[QWebSocket]`
- `WorkerEntry` は以下を保持：`ws`, `instance_id`, `label`, `pid`, `project`, `disconnected_at`（切断時刻、接続中は None）
- Worker / Client 接続切断 → 直ちに削除せず、**`disconnected_at` をセットして grace 期間保持**（後述 "Worker 再接続時の instance_id 引き継ぎ" 参照）
- **`active_worker_connections` の定義**：`WorkerEntry.disconnected_at is None` のものだけ。grace 中の Worker は「居ない」として扱う
- **`len(active_worker_connections) == 0` かつ `len(client_connections) == 0`** になった時点で 30 秒タイマー開始
- **grace 60 秒 vs idle 自動終了 30 秒の関係**：全 Worker が同時切断すると、Hub は grace 終了（60 秒）を待たずに idle 自動終了（30 秒）する。これは意図通りで、再接続したい Worker は新規 Hub を spawn して再接続する（新 `instance_id` が発行され、sticky は再設定が必要）。grace は単一 Worker の一時切断 + 他 Worker 健在時の引き継ぎ用途に限定される
- タイマー満了時に両方 0 のままなら、`QCoreApplication.quit()` → `pid_file` を削除して終了
- **タイマーキャンセル条件：TCP accept 発生時点で即キャンセル**（`QWebSocketServer.newConnection` シグナル接続時、register 受信を待たない）。register 到達前に満了して Hub が quit する窓を塞ぐ
- ADR-0002 の E2E 構成では pytest session fixture が `automation_client` として persistent 接続を保持する（`client_connections >= 1`）ため、Worker 未接続期間でも idle shutdown は発火しない

**スレッドモデルと race 対策**：Hub は単一の Qt イベントループ（`QCoreApplication`）上で全ソケットを扱う。接続ハンドラ・タイマーコールバック・idle 自動終了判定はすべて同一スレッドで逐次実行されるため、カウンタ更新と判定の間に race は発生しない。この前提を実装コメントとして明記する。

E2E テスト（ADR-0002）で Client が先に繋いで Worker を待つケースがあるため、「Worker 0 = 即 idle 自動終了開始」ではなく、**両方 0 の場合のみ**にする。

#### Worker の自動再接続（指数バックオフ）

- WS 切断を検知したら指数バックオフで `ensure_hub_connected()` 再試行
- **バックオフ列は初回接続と再接続で共通化**：`[0.3, 0.6, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0]` を初回用、再接続時はこの末尾を繰り返して上限 30 秒まで伸ばす
- 連続 1 分間再接続できなければ `iface.messageBar().pushWarning("qgis-puppeteer", "Hub disconnected — reconnecting")` で QGIS 上に警告を表示
- 連続 10 分間再接続できなければ警告を `pushCritical` に格上げ、かつログファイルに詳細記録
- 再接続成功時は `register` を再送し、前回の `instance_id` を `previous_instance_id` フィールドで申告して引き継ぎを要求（後述）

#### Worker 再接続時の instance_id 引き継ぎ

Hub クラッシュ → 再 spawn → Worker 再接続のシナリオで、**MCP Gateway 側の sticky が同じ `instance_id` を参照し続けられる**ように引き継ぎを用意する。

仕様：
- Worker 切断時、Hub は即削除せず `WorkerEntry.disconnected_at = now` をセットして **60 秒間保持**
- Worker が `register` 再送時に `previous_instance_id` を含めた場合、Hub は grace 中の entry とマッチング：
  - `pid` + `label` + `previous_instance_id` がすべて一致 → 同じ `instance_id` を返却（引き継ぎ成功）
  - 不一致 → 新規 `instance_id` を発行（通常の新規登録扱い）
- grace 60 秒を超過した entry は削除される

これにより Hub 瞬断からの復帰で Claude 側の sticky が自動的に有効のまま継続する。

##### register メッセージ拡張

```json
{
  "type": "register",
  "id": "uuid-N",
  "protocol_version": 1,
  "role": "worker",
  "pid": 1234,
  "label": "A",
  "project": "D:/work/a.qgz",
  "previous_instance_id": "worker-a-1234",
  "started_at": "2026-04-23T09:00:00Z"
}
```

##### register_ack の成功/失敗レスポンス

```json
// 成功（新規 or 引き継ぎ）
{
  "type": "register_ack",
  "id": "uuid-N",
  "ok": true,
  "instance_id": "worker-a-1234",
  "resumed": true       // 前回 id を引き継いだ場合 true
}

// label 重複エラー
{
  "type": "register_ack",
  "id": "uuid-N",
  "ok": false,
  "error": {
    "code": "label_conflict",
    "message": "Label 'A' is already in use",
    "suggested_label": "A (2)"
  }
}
```

Worker は `label_conflict` を受けた場合、`suggested_label` で再送（自動 rename）または UI でユーザーに入力を求める。

#### Graceful close（QGIS 終了時）

Qt の `unload()` は QGIS のシャットダウンシーケンス末尾で呼ばれ、この時点ではイベントループが停止している可能性がある。WS の close フレームを確実に送るため **`aboutToQuit` シグナル + `disconnected` 連動の待機**を組み合わせる：

```python
class Worker:
    def __init__(self, ...):
        QgsApplication.instance().aboutToQuit.connect(self._graceful_close)

    def _graceful_close(self):
        if not self._ws or self._ws.state() != QAbstractSocket.ConnectedState:
            return
        self._ws.sendTextMessage(
            json.dumps({"type": "bye", "instance_id": self._instance_id})
        )
        self._ws.close()

        # 正常時: disconnected 発火で即抜け / タイムアウト時: 500ms で強制抜け
        loop = QEventLoop()
        self._ws.disconnected.connect(loop.quit)
        QTimer.singleShot(500, loop.quit)
        loop.exec_()
```

`disconnected` シグナルに接続することで、close フレームが実際に送信されて Hub が応答を返した瞬間に抜けられる。500ms タイマーはタイムアウト保険。

Hub 側は `bye` 受信時に該当 Worker エントリを **grace 期間を経ずに即削除**し（再接続引き継ぎを期待しない意思表示なので）、他 Client への通知イベント（`instance_removed`）を発火する。**`bye` 送信後に同じプロセスが再接続してきた場合も、新規登録として扱う**（`previous_instance_id` を送ってきても引き継ぎ対象がないため新規発行）。

### 5. Instance 選択セマンティクス

#### 責務分離

| 処理 | 担当 | 理由 |
|---|---|---|
| sticky 選択の記憶 | McpGateway（接続スコープ） | セッション単位の状態 |
| selector → instance_id 解決 | Hub | 唯一の正解を持つ（接続状態を知っている） |

McpGateway は sticky 値を保持するが、解決ロジック（label 照合など）は Hub に委ねる。

#### MCP ツール API（`McpGateway` 側）

既存ツール全部に `instance: str | None = None` を追加。加えて新設：

- `qgis_list_instances() -> list[dict]`
- `qgis_use_instance(selector: str) -> dict`

#### sticky 選択のスレッド安全性

`qgis_use_instance` の結果を **モジュール global 変数で保持してはならない**。`FastMCP` の実装は async であり、Claude からの並行呼び出し（`asyncio.gather` 的）で壊れる。

**`contextvars.ContextVar` で管理**する：

```python
import contextvars

_current_instance: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "qgis_puppeteer_current_instance", default=None
)

@mcp.tool()
def qgis_use_instance(selector: str) -> dict:
    resolved = hub_client.resolve_instance(selector)  # Hub に問い合わせ
    _current_instance.set(resolved["instance_id"])
    return resolved

@mcp.tool()
def qgis_list_layers(instance: str | None = None) -> str:
    target = instance or _current_instance.get()
    # target=None の場合は Hub が自動選択または ambiguous エラー
    ...
```

ContextVar は asyncio の Task 単位で隔離されるため、並行ツール呼び出し間で sticky 値が混線しない。

将来、MCP サーバーが単一プロセスで複数 Claude セッションを扱うようになった場合は、「Claude セッション ID をキーにした dict」に切り替える拡張余地がある（プロトコル変更不要）。

#### selector 解決順（Hub 側実装）

1. `@`+`label` 完全一致（例: `@A`）
2. `label` 完全一致（非数字の label のみ。数字のみの label は `pid` と衝突するため無効）
3. `instance_id` 完全一致（`worker-a-1234` など）
4. `project` basename 一致（拡張子有無両方で照合）
5. `pid` 文字列一致

**label の制約**：
- 1 文字以上、32 文字以下
- 数字のみの label は禁止（`pid` と衝突するため）
- **label 自体は `@` で始められない**（`@` は selector 側のプレフィックスとして予約）
- selector 側で先頭に `@` を付けることで「label として解釈する」を明示できる（例: selector `@1234` は「数字 1234 という label を探せ」の意味。ただし数字のみの label は自動採番含め存在しないので、実質は `@A` 等の英数字 label の曖昧性回避用途）

Worker 側の label 自動採番（`{project_basename}-{pid 下4桁}`）は文字列なので数字単独にはならない。

#### 曖昧時の挙動

- インスタンスが **1 台のみ** かつ `instance=None`：自動選択
- インスタンスが **2 台以上** かつ `instance=None` かつ sticky 未設定：エラー `instance_ambiguous`、レスポンスに `candidates: [...]` を含めて Claude が Claude 自身で再選択できるようにする
- selector が複数マッチ：エラー `instance_ambiguous`、同上

`instance_ambiguous` のレスポンス例：

```json
{
  "type": "response",
  "id": "uuid-X",
  "ok": false,
  "error": {
    "code": "instance_ambiguous",
    "message": "Multiple instances match. Specify via instance= or qgis_use_instance()",
    "candidates": [
      {"instance_id": "worker-a-1234", "label": "A", "project": "a.qgz"},
      {"instance_id": "worker-b-5678", "label": "B", "project": "b.qgz"}
    ]
  }
}
```

### 6. Hub の spawn と配置

#### 配置

- `qgis_puppeteer/hub.py`（自動化パッケージ内のエントリポイント）
- Worker プラグインが `subprocess.Popen` で起動（ただし「外部所有者モード」では抑制、後述）

#### 接続先と Hub CLI 引数

Hub / Worker / Client の接続先は以下の環境変数で差し替え可能：

| 環境変数 | 既定値 | 用途 |
|---|---|---|
| `QPUPPETEER_HUB_URL` | — | Hub の WS URL を丸ごと上書き（`ws://host:port` 形式）。セットすると `HOST`/`PORT` より優先 |
| `QPUPPETEER_HUB_HOST` | `127.0.0.1` | Hub の bind / 接続先ホスト |
| `QPUPPETEER_HUB_PORT` | `9876` | Hub の listen / 接続先ポート |
| `QPUPPETEER_HUB_ORIGIN` | `http://localhost` | Worker / Client が register 時に送る Origin ヘッダ |
| `QPUPPETEER_WORKER_LABEL` | — | Worker の自己申告 label（未指定時は Hub が自動採番） |

Hub は CLI 引数 `--host` / `--port` も受け付け、env より優先する：

```bash
python -m qgis_puppeteer.hub --host 127.0.0.1 --port 19876
```

McpGateway / AutomationClient / Worker は上記 env を読んで接続先を決定する。

#### spawn コマンド（Worker 主導・既定動作）

Worker プラグインが以下を実行（env 未設定時の通常経路）：

```python
subprocess.Popen(
    [sys.executable, "-m", "qgis_puppeteer.hub"],
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    stdin=subprocess.DEVNULL,
    stdout=open(hub_log_path, "a"),
    stderr=subprocess.STDOUT,
    cwd=plugin_dir,
)
```

- `sys.executable` は QGIS の `python.exe`
- DETACHED により QGIS 終了後も Hub は残る（他 QGIS がまだ動いていれば）
- ログは `%APPDATA%\qgis_puppeteer\hub.log` にローテート

#### 外部所有者モード（Hub spawn 抑制）

`QPUPPETEER_HUB_URL` / `QPUPPETEER_HUB_HOST` / `QPUPPETEER_HUB_PORT` の **いずれかが設定されている場合**、Worker は **Hub の spawn を行わず、接続のみ試行**する。接続失敗時もバックオフ再試行のみで、spawn は一切しない。

用途：
- **E2E テスト**：pytest が Hub を独自ポートで spawn して所有権を持つ（ADR-0002）
- **remote 運用**（将来検討）：既に別マシンで動いている Hub に接続
- **CI の port 隔離**：dev 環境と E2E の Hub を完全分離

判定ロジック：

```python
def should_spawn_hub() -> bool:
    """env が一切設定されていない（= 既定 127.0.0.1:9876）なら Worker が spawn 責務を持つ。"""
    return not any(
        name in os.environ
        for name in ("QPUPPETEER_HUB_URL", "QPUPPETEER_HUB_HOST", "QPUPPETEER_HUB_PORT")
    )
```

これにより「Hub の所有者は誰か」が env の有無で一意に決まり、E2E の port 隔離と dev 環境汚染防止が構造的に担保される。

### 7. 外部 MCP 識別子（`qgis-puppeteer`）

Claude Desktop の `claude_desktop_config.json` には以下で登録：

```json
{
  "mcpServers": {
    "qgis-puppeteer": {
      "command": "C:\\Users\\haruo\\.local\\bin\\uv.exe",
      "args": ["-m", "qgis_puppeteer.gateways.mcp"]
    }
  }
}
```

これにより Claude からは `mcp__qgis-puppeteer__qgis_list_layers` 等のツール名でアクセス可能。

「`qgis-puppeteer`」という server key は MCP 命名慣例（ハイフン区切り・ブランド + 対象システム、例: `google-drive`, `github`）に沿う。内部の `McpGateway` クラス名とは独立しており、将来的に別プロトコル層を追加しても衝突しない。

### 8. エラー・障害処理

| 事象 | Worker 側対応 | Hub 側対応 | McpGateway / Client 側対応 |
|---|---|---|---|
| Hub 起動失敗 | エラーダイアログ表示、機能無効化 | — | — |
| Hub クラッシュ | WS 切断検知 → 再 spawn 試行 | — | WS 切断 → 次回ツール呼び出し時に再接続 |
| Worker クラッシュ | — | 該当接続削除、カウント減、他 Worker と Client は継続 | 該当 instance への呼び出しは `instance_not_found` で即時エラー |
| McpGateway クラッシュ | — | 該当接続削除、処理継続 | Claude Desktop が再 spawn |
| ネットワーク（remote 時） | — | TLS + トークン認証（Roadmap） | 同左 |

### 9. セキュリティ境界

WebSocket は HTTP ハンドシェイクなので、localhost 運用であってもブラウザ経由の攻撃面が残る。以下を組み込む。

#### 9.1 バインドアドレス

Hub は **`127.0.0.1` 固定で listen**（`QHostAddress.LocalHost`）。`0.0.0.0` やマシンの LAN IP へのバインドは remote 対応 + 認証実装が入るまで禁止（`## Roadmap` 参照）。

```python
server.listen(QHostAddress.LocalHost, 9876)
```

#### 9.2 Origin ヘッダ検証（DNS rebinding 防止）

ブラウザが `http://evil.example.com` を訪れた際、DNS rebinding 攻撃で `127.0.0.1:9876` にリクエストを投げられる。WebSocket ハンドシェイクの `Origin` ヘッダで以下を検証：

- **許可**：`Origin` ヘッダなし（Python クライアントは通常送らない）
- **許可**：`Origin` の hostname（`urllib.parse.urlparse(origin).hostname`）が **`localhost` または `127.0.0.1` に完全一致**
- **拒否**：上記以外（HTTP 403 を返してハンドシェイク中断）

**重要**：`startswith` による前方一致は `http://localhost.evil.com` を通してしまうため禁止。必ず hostname の完全一致で判定する。

```python
from urllib.parse import urlparse

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1"})

def on_origin_auth(authenticator: QWebSocketCorsAuthenticator):
    origin = authenticator.origin()
    if not origin:
        authenticator.setAllowed(True)
        return
    hostname = urlparse(origin).hostname
    authenticator.setAllowed(hostname in _ALLOWED_HOSTS)
```

### 10. 将来検討事項

詳細は末尾の `## Roadmap` を参照。主な項目:

- **remote 運用**: 別マシンの Hub に接続できるよう、`127.0.0.1` 固定を緩和し TLS + トークン認証を追加。プロトコルは現行のまま流用可能なので、接続層の拡張で済む。
- **`qgis_launch_instance` ツール**: Gateway から QGIS プロセス自体を起動する MCP ツール。現状は QGIS を手動起動しないと Worker が存在しないため、完全自動化シナリオでブロックになる。実装は `subprocess.Popen` + `list_instances` poll で `register` 到達を検出する。
- **`InstanceInfo.project` フィールドの扱い**: register 時点のスナップショットしか持っておらず常に `null` になりがち。選択肢: (a) `fileNameChanged` シグナル連動 push、(b) register payload に乗せる、(c) フィールド削除。暫定方針は (c)。

### 12. ハンドラ拡張機構

`qgis_puppeteer` は「外部から QGIS にアクセスするためのインターフェース」のみを提供し、**ドメイン固有の API は外部（ユーザのドメインプラグインや test helper 等）が所有して注入する**。これにより Core の責任範囲を明確に保ち、Core パッケージを OSS として切り出し可能な状態に保つ。

#### 12.0 本章で使う用語

本章では「拡張」を表す語がレイヤーごとに異なる意味を持つため、以下で固定する:

| 用語 | 定義 |
|---|---|
| **Core** | `qgis_puppeteer` 本体が提供する汎用機能。namespace `qgis_*` |
| **Tier** | Handler の所有層（Tier 1 Core / Tier 2 Domain / Tier 3 Test）。責任分担軸 |
| **puppeteer_api provider** | Worker 側で handler を提供する QGIS プラグイン（`puppeteer_api.py` 規約）。例: ドメインプラグイン、`qgis_puppet_test_helpers` |
| **MCP extension** | Gateway 側で MCP ツールを提供する Python パッケージ。パターン B（§12.4）採用時のみ存在し、entry_points 経由で登録される |
| **QGIS plugin** | QGIS 本体が load する Python プラグイン全般。`qgis_puppet` も puppeteer_api provider もこれの一種 |
| **Handler** | `(params: dict) -> Any` の Callable。Worker 側で実行される |
| **Tool** | MCP tool（Claude に見える関数）。Gateway 側で `@mcp.tool()` 登録 |

Handler と Tool は**別のレイヤー**（Worker 側と Gateway 側）で、1 対 1 対応するとは限らない（generic passthrough の場合、1 Tool が複数 Handler を呼べる）。

#### 12.1 設計思想：双方向 pull モデル

qgis_puppeteer の内部（Worker / Gateway）がそれぞれ「外部から API を pull する」対称的な構造を採る。外部側のドメインプラグインは qgis_puppeteer を知らなくても動作できる。

```
     [Worker 側]                          [Gateway 側]
 qgis_puppet プラグイン              qgis-puppeteer MCP サーバー
        ↓ pull                              ↓ pull
 plugins/*/puppeteer_api.py          entry_points または分離 MCP pkg
 （各プラグインが provide）          （各ドメイン pkg が provide）
```

ハンドラの実行は Worker（QGIS 内）、MCP ツールの露出は Gateway（別プロセス）で、それぞれ独立に拡張が可能。

#### 12.2 3 層の責任分担

| Tier | 提供元 | 名前空間 | Worker 側配置 | Gateway 側配置 |
|---|---|---|---|---|
| Tier 1: Core | `qgis_puppeteer` 本体 | `qgis_*` | `qgis_tools/*` + `qgis_puppet` プラグインが登録 | `qgis_puppeteer/gateways/mcp.py` の `@mcp.tool()` |
| Tier 2: Extension | 各ドメイン固有機能 | `mydomain.*`, `extensions.{name}.*` 等 | `plugins/<domain>/puppeteer_api.py` | 下記 §12.4 のパターン A または B |
| Tier 3: Test | E2E テスト支援 | `test.*` | `plugins/qgis_puppet_test_helpers/puppeteer_api.py`（E2E 時のみ有効） | 不要（pytest は AutomationClient を直接使用） |

#### 12.3 Worker 側：`puppeteer_api.py` convention による pull discovery

**規約**: 各 QGIS プラグインは、qgis_puppeteer にハンドラを提供したい場合、プラグイン配下に `puppeteer_api.py` を置き、以下のシグネチャで `build_handlers` 関数を公開する。

```python
# plugins/mydomain/puppeteer_api.py
from qgis.gui import QgisInterface

def build_handlers(iface: QgisInterface) -> dict[str, Callable[[dict], Any]]:
    """qgis_puppet が discover 時に呼ぶ entry point。
    
    Returns:
        コマンド名 → handler 関数 の dict。
        handler は params dict を受け取り、JSON 化可能な値を返す。
    """
    from .api import do_foo_action
    return {
        "mydomain.foo_action": lambda p: {
            "result": do_foo_action(int(p["item_id"]))
        },
    }
```

**重要**: `puppeteer_api.py` は `qgis_puppeteer` を一切 import しない。戻り値の型は純 Python（`dict[str, Callable]`）で、`Any` を返しても JSON 化層（`_coerce_leaf`）が吸収する。これによりドメインプラグインは qgis_puppeteer に対する import 依存を持たない。

**qgis_puppet プラグインの discover 機構**:

```python
# plugins/qgis_puppet/plugin.py
def initGui(self):
    # ... Worker 生成、Core qgis_* ハンドラ登録 ...
    self._discover_extensions()

def _discover_extensions(self):
    from qgis.utils import plugins as qgis_plugins
    import importlib
    for name in qgis_plugins:
        if name == "qgis_puppet":
            continue
        try:
            mod = importlib.import_module(f"{name}.puppeteer_api")
        except ImportError:
            continue
        try:
            handlers = mod.build_handlers(self.iface)
        except Exception:
            logger.exception("%s.puppeteer_api raised", name)
            continue
        for cmd, handler in handlers.items():
            try:
                self._worker.register_handler(cmd, handler)
            except Exception:
                logger.exception("register_handler failed: %r from %s", cmd, name)
```

**ロード順問題の構造的解消**:
- QGIS は `classFactory` で登録した直後に `qgis.utils.plugins` に plugin_instance を入れ、その後 `initGui` を順次呼ぶ
- プラグイン名のアルファベット順で `qgis_puppet` は後方なので、`qgis_puppet.initGui` 時にはドメインプラグイン等は既に登録済み
- 従って pull 側で全 extension を発見できる（pending キュー不要）

#### 12.4 Gateway 側：ドメイン API の露出方法

Claude に `mydomain.*` 等のドメインツールを露出する方法として、本 ADR は **パターン A（分離 MCP サーバー）を default に規定**する。パターン B（entry_points mixin）は Open Questions に格下げして将来検討余地として残す（§12.4.3）。

##### パターン A（採用）：分離 MCP サーバー

各ドメインが独自の MCP サーバー pkg を持ち、Claude Desktop に別個に登録する。

```
qgis_puppeteer/              # Core（qgis_* の @mcp.tool()）
└── gateways/mcp.py          # MCP 名: qgis-puppeteer

mydomain_puppeteer/             # ドメイン固有（mydomain.* の @mcp.tool()）
└── gateways/mcp.py          # MCP 名: mydomain-puppeteer
```

```json
// .mcp.json
{
  "mcpServers": {
    "qgis-puppeteer":   { "command": "...", "args": [... "qgis_puppeteer.gateways.mcp"] },
    "mydomain-puppeteer":  { "command": "...", "args": [... "mydomain_puppeteer.gateways.mcp"] }
  }
}
```

**特徴**:
- MCP サーバー 2 プロセスが同じ Hub に別 Client として接続
- 各 Gateway が独立、相互干渉なし
- OSS 切り出しが単位ごとに自然（ドメイン非依存の環境は `qgis-puppeteer` のみ install）
- Claude Desktop の設定に複数エントリが並ぶ

##### パターン A の採用理由

- Worker 側の pull discovery（§12.3）と構造的に対応（プラグイン = domain package 1 対 1）
- OSS 切り出しが単位ごとに自然（汎用環境は `qgis-puppeteer` のみ install）
- 将来 remote 対応する際 TLS 境界や認証トークンをドメインごとに独立に引ける
- MCP サーバープロセスを個別に再起動 / デバッグ可能
- 実装がシンプル（entry_points 機構が不要）

デメリット（容認）:
- Claude Desktop 設定に複数エントリが並ぶ
- Claude 側のツール一覧が MCP サーバー単位で分かれる（実用上は prefix で区別できる）

##### パターン B（Open Questions 扱い）：entry_points による mixin

**採用検討は先送り**（§12.4.3 参照）。以下に設計案のみ記載する。

`qgis_puppeteer.gateways.mcp` が単一の `FastMCP` instance を持ち、entry_points で登録された extension が同じ instance にツールを追加する。

```toml
# mydomain_puppeteer/pyproject.toml
[project.entry-points."qgis_puppeteer.mcp_extensions"]
mydomain = "mydomain_puppeteer.mcp_extension:register_tools"
```

```python
# mydomain_puppeteer/mydomain_puppeteer/mcp_extension.py
def register_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def mydomain_foo_action(ctx, house_id: int, instance: str | None = None) -> str:
        return await _call(ctx, "mydomain.foo_action", {"house_id": house_id}, instance=instance)
```

```python
# qgis_puppeteer/gateways/mcp.py
from importlib.metadata import entry_points

mcp = FastMCP("qgis-puppeteer")

@mcp.tool()
async def qgis_list_layers(...): ...  # Core tools

def _load_extensions(mcp):
    for ep in entry_points(group="qgis_puppeteer.mcp_extensions"):
        try:
            ep.load()(mcp)
        except Exception:
            logger.exception("failed to load %s", ep.name)

_load_extensions(mcp)
```

**特徴**:
- MCP サーバー 1 プロセスに全ツールが集約
- Claude から見ると単一 MCP の中に `qgis_*` と `mydomain.*` が混在
- `.mcp.json` のエントリは 1 つ
- Gateway の uv 環境にドメイン extension pkg が install されているかどうかで露出が切り替わる
- Python 標準の entry_points 機構を使うので pip / uv / poetry など全パッケージマネージャで動く

##### 12.4.3 パターン B を Open Questions 扱いとする理由

パターン A/B の選択基準が「Claude 側ツール一覧の見え方」という審美的判断に大きく依存し、実需要ベースで切り分ける軸が乏しい。両案を併記したままだと decision paralysis になりやすい。本 ADR では A を確定採用し、以下の条件が揃った時点で B への移行を検討する:

- **条件 1**: ドメインが 3 つ以上に増え、Claude Desktop 設定エントリが煩雑になる
- **条件 2**: Claude 側で「ツール一覧をまとめて見たい」という運用上の明確な要求が出る
- **条件 3**: ドメイン間で共通 helper（例: instance selector、エラー整形）を Gateway レイヤーで共有する利点が実害化する

これらが揃う前は A のみ。移行時は本 ADR を改訂する。

#### 12.5 Test 用の経路

Test 用ハンドラは Worker 側のみに必要（Gateway 側は不要、pytest は `AutomationClient` を直接使う）。

**A. 専用テスト支援プラグイン（定常）**

`plugins/qgis_puppet_test_helpers/`（名称は仮）に `puppeteer_api.py` を置き、`test.reset_db` 等を provide する。このプラグインは E2E 実行時のみ有効化する運用（QGIS プラグインマネージャで ON/OFF、または pytest fixture が起動時に有効化）。

**B. `execute_python` 経由の動的登録（個別）**

テスト関数固有のハンドラは pytest から `execute_python` で注入する。

```python
await qgis.execute_python("""
    from test_handlers import custom_verify
    from qgis_puppeteer import get_worker  # 同一プロセス内から
    w = get_worker()
    w.register_handler("test.custom_verify", custom_verify, allow_overwrite=True)
""")
```

この B 経路は信頼モード（`QPUPPETEER_TRUSTED_MODE=1`）下でのみ実行可能。さらに `test.*` namespace への登録は `QPUPPETEER_ALLOW_TEST_HANDLERS=1` が必要。

#### 12.6 Worker 側登録 API

Worker の公開 API は 2 系統に分ける:

```python
class Worker:
    def register_core_handler(
        self, command: str, handler: Handler, *, allow_overwrite: bool = False
    ) -> None:
        """Core（`qgis_*`）ハンドラ専用。本パッケージ内部（qgis_tools/*）のみ使用。"""

    def register_handler(
        self, command: str, handler: Handler, *, allow_overwrite: bool = False
    ) -> None:
        """外部（Tier 2 / 3）からのハンドラ登録。namespace 検証を通す。"""

    def unregister_handler(self, command: str) -> None:
        """ハンドラ登録を解除する。"""
```

`register_handler` は以下を検証し、違反時は対応する例外を送出する:
- `qgis_*` プレフィックス → `ReservedNamespaceError`
- `test.*` プレフィックスで `QPUPPETEER_ALLOW_TEST_HANDLERS` 未設定 → `RegistrationDeniedError`
- 同名既存ハンドラと `allow_overwrite=False` → `HandlerAlreadyRegisteredError`

#### 12.7 名前空間とセキュリティ

- **名前空間プレフィックス規約**: `qgis_*` / `mydomain.*` / `extensions.{name}.*` / `test.*`。プラグイン名と namespace が一致することを推奨（ソフト規約、強制しない）
- **予約プレフィックス**: `qgis_*` は外部 `register_handler` から登録不可
- **Tier 3 登録 gating**: `QPUPPETEER_ALLOW_TEST_HANDLERS=1` が必須
- **既定で上書き禁止**: `allow_overwrite=True` で明示的に許可した場合のみ
- **caller_role による実行時可視性フィルタ**（§12.9 参照）: `test.*` は `automation_client` 呼び出しのみ実行許可

#### 12.8 プロトコルへの影響

プロトコル本体（§3）の Request / Response 構造は変えずに、以下のエラーコードを追加する:

- `reserved_namespace`: 予約プレフィックスへの登録試行
- `registration_denied`: 権限不足での登録試行
- `handler_already_registered`: 上書き禁止時の再登録試行
- `handler_not_visible`: caller_role に対して該当ハンドラが公開されていない（§12.9）

`list_handlers` 等の discover 用メッセージは**現状不要**（pull discovery は Worker 側内部で完結、Gateway 側は §12.4 パターン A の静的 `@mcp.tool()` 宣言で賄うため）。パターン B への移行や Claude 側の動的 tool 発見が必要になった時点でプロトコル拡張を別途検討する（`## Roadmap`）。

#### 12.9 caller_role 伝達

Tier 3（`test.*`）ハンドラを Claude Desktop 経由の McpGateway 接続から隔離するため、Hub は **request 転送時に caller_role を Worker へ伝達**する。env 分離だけでは、dev Claude Desktop が誤って E2E 用 Hub に接続した場合に権限昇格経路が成立するリスクがあるため、プロトコルレベルで caller 識別を行う。

##### プロトコル拡張

Hub → Worker に転送する request に `caller_role` フィールドを付加する:

```json
{
  "type": "request",
  "id": "uuid-3",
  "instance": "A",
  "command": "test.reset_db",
  "params": {},
  "caller_role": "automation_client"
}
```

Client → Hub の `request` メッセージに Client が `caller_role` を含めても Hub は上書きする（Client の自己申告は信用しない）。Hub が register 時の role を保持して付与する。

##### Worker 側の判定

| caller_role | 実行可能ハンドラ |
|---|---|
| `mcp_gateway` | Tier 1（`qgis_*`）+ Tier 2（`mydomain.*`, `extensions.*`） |
| `automation_client` | Tier 1 + Tier 2 + Tier 3（`test.*`） |

caller_role が Tier 3 ハンドラを呼べない場合は `handler_not_visible` エラーを返却する。

##### `caller_role` 未設定 request の扱い

プロトコル version 1 では Hub が必ず caller_role を注入する。Worker が `caller_role=None` の request を受け取ることは正常系では発生しない。ただし防御的実装として、**`caller_role is None` の場合も `AUTOMATION_CLIENT` 以外とみなして Tier 3 を拒否する**（fail-closed）。これにより直接 Worker に接続してくる不正 Client（将来的な mis-configured proxy 等）から `test.*` を隔離する。

##### `list_instances` の caller_role 扱い

`list_instances` は request / response 経由せず Hub が直接処理するため caller_role 注入の対象外。つまり `MCP_GATEWAY` からでも `AUTOMATION_CLIENT` からでも同じ instance 一覧が返る。Tier 3 Worker の存在は `list_instances` で可視になる。これは情報漏洩リスクとしては軽微（instance_id / label / pid のみで secret は含まない）。複数 Client グループを 1 つの Hub に同居させる運用に進化した場合、`list_instances` レベルでも caller_role フィルタを導入する余地がある（`## Roadmap`）。

##### env 分離

| env | 影響範囲 |
|---|---|
| `QPUPPETEER_TRUSTED_MODE=1` | `execute_python` の confirm フローを skip（ADR-0002 §9） |
| `QPUPPETEER_ALLOW_TEST_HANDLERS=1` | Worker が `test.*` 名前空間への登録を許可する |

E2E fixture は両方設定する。dev Claude Desktop ユーザーは両方未設定のまま運用する。

#### 12.10 実装範囲

現状の実装が提供するもの:

- Tier 1 Core ハンドラ（`qgis_*`）
- `register_core_handler` / `register_handler` API + namespace 検証
- caller_role 伝達（Hub 側注入、Worker 側フィルタ）
- env 分離（`QPUPPETEER_TRUSTED_MODE` / `QPUPPETEER_ALLOW_TEST_HANDLERS`）
- Worker 側の `puppeteer_api.py` discovery 機構

将来検討項目は `## Roadmap` を参照。

## Consequences

### Positive

- 複数 QGIS 同時運用が可能になる
- ポート衝突、ディスカバリ、stale 管理の問題が構造的に消える
- remote 対応を後追いで追加しやすい（Worker・プロトコル無改修。`## Roadmap`）
- Hub プロセスはオンデマンド起動・idle 自動終了するので運用負担ゼロ
- MCP 非依存の自動化層として E2E テスト（pytest）からも同じ基盤を使える（ADR-0002）
- 将来 HTTP API や CLI 層を `qgis_puppeteer.gateways.*` として並列に追加できる
- 依存ライブラリが最小（pypi 追加は McpGateway / AutomationClient 側の `websockets` のみ）

### Negative

- コンポーネントが 1 つ増える（Hub）
- Hub spawn ロジックにレース対策コードが必要
- WS プロトコル仕様のバージョニング責任が発生する
- 現行の単純 TCP 実装からの移行で既存テストの書き直しが必要

### Neutral

- 既存の `qgis_socket_client.py` は全面書き換え
- `claude_desktop_config.json` の `mcpServers` key は `qgis-puppeteer`（命名の一貫性と MCP 慣例に合わせて改名）

## Open Questions

1. **Hub のログファイル肥大化対策**：`logging.handlers.RotatingFileHandler`（10MB × 5）で即対応予定
2. ~~**E2E テストでの Hub 起動戦略**~~：Rev.4 で解決。pytest session fixture が独立ポートで Hub を spawn し所有する（外部所有者モード）。詳細は ADR-0002
3. **プロトコル拡張項目**：
   - サーバー発イベント（`instance_added` / `instance_removed`）
   - 長時間コマンド用の進捗メッセージ（`progress`）
   - リクエストのキャンセル（`type=cancel` で `id` 指定の abort）
   - request 側 `timeout_ms` フィールド
   - プロトコルバージョン交渉（SemVer + `supported_versions`）
   - 追加エラーコード（`hub_overloaded`, `protocol_error`, `worker_busy`）
4. **ContextVar の sticky 効果の実機検証**：FastMCP の Task スコープで `qgis_use_instance` が次のツール呼び出しまで生存するか確認が必要。生存しない場合は「Claude セッション ID をキーにした dict」に切り替え（プロトコル無改修）

## Performance

### 計測条件

- 2026-04-23 / Windows / QGIS 3.34.6 / localhost / Hub 単一 Worker
- `qgis_puppeteer/scripts/bench_rtt.py` で `AutomationClient` から直接 Hub に
  接続し、`perf_counter` で各 `call()` の RTT を測定
- MCP / Claude / FastMCP レイヤはバイパス（= 純粋な Hub↔Worker 往復）
- 各コマンド N=5 回、同一セッション内で連続実行

### 結果 (ms)

| コマンド | min | median | max |
|---|---:|---:|---:|
| `qgis_get_canvas_extent` | 4.7 | 28.4 | 61.4 |
| `qgis_list_layers` | 6.6 | 13.0 | 1170.6 |
| `qgis_select_features` (1 件選択) | 127.7 | 139.9 | 392.5 |
| `qgis_get_selected_features` (limit=2) | 51.7 | 62.2 | 331.9 |
| `qgis_set_canvas_extent` | 53.3 | 64.4 | 343.0 |
| connect + register | — | 8.7 | — |

### 所見

- **定常状態 10〜140ms/call**。当面の要件には十分
- `select_features` が最重：QGIS 内での expression 評価 + 選択更新 + canvas 再描画
- max のばらつき（list_layers 1170ms 等）は Qt のキャンバス再描画 / レイヤ
  初期化のタイミング依存
- `connect + register` 8.7ms なので接続プーリングは必須ではないが、短時間に
  複数呼ぶ場合は保持推奨

### MCP 経由との比較

Claude Code から同じコマンドを叩くと **5〜15 秒/call**。差分（数百倍）はすべて
MCP / LLM レイヤのオーバーヘッド：

- Claude → MCP gateway (stdio) の往復
- FastMCP の tool dispatch / param serialize
- LLM 側の tool-use decode と次 turn の生成待ち

**結論**: 最適化するなら MCP 層側。Hub↔Worker のプロトコル自体は十分速い。

## Roadmap

将来検討する拡張項目をここに集約する（実装順や実施可否は未確定）。

### Remote 運用 + 認証

現状は `127.0.0.1` 固定 listen + Origin 検証のみ。別マシンの Hub に接続する運用が必要になった時点で以下を導入する:

- バインドアドレス指定の解放（`0.0.0.0` / 任意 IP）
- TLS（`wss://`）の終端を Hub または前段 reverse proxy で
- トークン認証（`register` メッセージに bearer token、起動時に env または config から渡す）
- `Origin` 検証ホワイトリストの設定化

プロトコル本体は現行のまま流用可能。接続層と register payload の拡張で済む。

### `qgis_launch_instance` MCP ツール

Gateway から QGIS プロセス自体を起動するツール。現状は QGIS を手動起動しないと Worker が存在しないため、完全自動化シナリオでブロックになる。実装は `subprocess.Popen(qgis_path, "--code", bootstrap_script)` + `list_instances` poll で `register` 到達を検出する。

### `InstanceInfo.project` フィールドの扱い

register 時点のスナップショットしか持っておらず、その後の Open Project で常に `null` になりがち。選択肢:

- (a) `QgsProject.fileNameChanged` シグナル連動で Hub に push し更新
- (b) register payload に乗せ続ける（古くなっても気にしない）
- (c) フィールド削除し、必要なら別途 `qgis_get_project_info` を追加

暫定方針は (c)。

### `list_instances` の caller_role フィルタ

複数 Client グループを同居させる運用に入る場合、Tier 3（`test.*`）Worker を `MCP_GATEWAY` には不可視にする必要が生じる可能性がある。Hub 側で caller_role を見て filter する。

### MCP ツール pattern B（entry_points mixin）への移行

§12.4.3 の条件（ドメイン 3 個以上 / 一覧集約要求 / 共通 helper 共有要求）が揃った時点で `qgis_puppeteer.mcp_extensions` entry_points 機構を導入し、`list_handlers` 系プロトコルメッセージの是非も併せて検討する。

### 動的 handler load/unload

E2E fixture で session スコープに enable/disable を切り替える運用が増えた場合、`unregister_handler` の使用機会が増える。現状は `register_handler(allow_overwrite=True)` で実用上足りているが、テスト終了時の cleanup を厳密化する用途で必要になる可能性がある。

## References

- WebSocket RFC 6455
- MCP 仕様: <https://modelcontextprotocol.io/>
- PyQt5.QtWebSockets: <https://doc.qt.io/qtforpython-5/PySide2/QtWebSockets/index.html>
- アーキテクチャ図: `docs/architecture/assets/0001-architecture.svg`
- ベンチマークスクリプト: `packages/qgis-puppeteer/scripts/bench_rtt.py`
