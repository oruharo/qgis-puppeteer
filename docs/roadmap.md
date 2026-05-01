# qgis-puppeteer Roadmap

このドキュメントは qgis-puppeteer の今後の作業項目を集約します。
詳細な設計判断は各 ADR（`docs/architecture/`）を参照してください。

最終更新: 2026-05-02（0.1.0 リリース時点）

---

## 現状: 0.1.0

初回公開リリース。実装済の主機能は [CHANGELOG.md](../CHANGELOG.md) を参照。
要約すると以下が揃っています:

- Hub / Worker / MCP gateway / `qgis_puppet` プラグイン
- QGIS tools 一式（layer / python executor / screenshot / UI / dialog handler / signal spy / exception recorder）
- pytest プラグイン: Locator + web-first assertions + 拡張 selector
- Test environments（ADR-0003 ほぼ全機能 ※ env 並列のみ未実装）
- Inline spawn helper（ADR-0004 全 Phase）
- Diagnostic bundle（actions.jsonl / per-action screenshots / spawn stdout / Hub log / meta / traceback / uncaught exceptions）
- Qt/Python 未捕捉例外の test failure 連動
- GitHub Actions CI（Ubuntu + Windows × Python 3.10/3.11/3.12, ruff hard gate）

---

## 効果カテゴリ

各タスクが「ユーザーにとって何が良くなるか」を以下のカテゴリで分類します。

| 略号 | カテゴリ | ユーザー視点での効果 |
|------|----------|----------------------|
| 🧪 **DX** | コード簡潔性 | テストコードが短く、書きやすく、読みやすくなる |
| 🌐 **適用** | 適用範囲拡大 | これまで書けなかったシナリオのテストが書けるようになる |
| 🛡️ **安定** | テスト安定性 | flaky が減る、silent failure（緑なのに壊れてる）が無くなる |
| 🔍 **解析** | 失敗解析 | テスト失敗時の調査が早く・正確になる |
| 📊 **可視** | CI 集約・可視化 | 多 env / CI 全体の結果が一目で分かる |
| ⚡ **規模** | スケール | 並列実行・大規模運用が現実的になる |
| 🏗️ **保守** | 内部保守性 | プロジェクト側の保守性向上 |

---

## 今後の作業

工数は個人開発 1 営業日 ≒ 1d 換算の見積。優先度の目安として「次リリース候補（0.2.0）」と「将来」に分けています。
具体的なバージョン番号は需要・優先順位を見て確定するため、ここではゆるく分類するに留めます。

### 0.2.0 候補

| タスク | 工数 | カテゴリ | 内容 |
|--------|------|----------|------|
| **PyPI publish workflow** | 1d | （リリース運用） | `packages/*` を PyPI に公開する GitHub Actions release workflow。tag 駆動 |
| **pytest-xdist 実機検証** | 2d | ⚡ 規模 | 並列実行下の Hub/QGIS 隔離テスト・課題抽出 |
| **ADR-0003 env 並列実行** | 2d | ⚡ 規模 | `--env=all` 下で env を並列に走らせる（xdist 検証と併せて判断） |

### 将来

| タスク | 工数 | カテゴリ | 内容 |
|--------|------|----------|------|
| **B5c**: `qgis_message.log` を diagnostic bundle に同梱 | 1d | 🔍 解析 | `QgsMessageLog` tail を Worker 側 push で bundle に追加 |
| **Hub/Worker deferred mode** | 3d | 🌐 適用 | プロトコルに `mode: "deferred"` 追加（長時間 slot 対応） |
| **Hub/Worker push event** | 3d | 🧪 DX / 🛡️ 安定 | Worker → Client の event 機構（poll を消せる） |
| **Flaky 自動検出** | 3d | 🛡️ 安定 / 📊 可視 | CI 履歴解析で quarantine 候補レポート |
| **Summary HTML レンダラー** | 4d | 📊 可視 | `summary.json` → スタンドアロン HTML + PR コメント |
| **動画録画 diagnostic** | 3d | 🔍 解析 | Qt 連続キャプチャ（アニメ解析用） |
| **remote / caller_role 転送** | 3d | 🌐 適用 | 別 PC 越しでの遠隔実行対応 |

### カテゴリ別サマリー

| カテゴリ | 該当タスク |
|----------|------------|
| 🔍 **解析** | B5c, 動画録画 |
| 🛡️ **安定** | push event, Flaky 検出 |
| 📊 **可視** | Flaky 検出, Summary HTML |
| 🧪 **DX** | push event |
| 🌐 **適用** | deferred mode, remote |
| ⚡ **規模** | pytest-xdist 実機検証, env 並列 |

---

## 推奨実施順の根拠

「次に何が困るか」を起点に並べています。

1. **PyPI publish workflow** — 0.1.0 を実際に配布するなら最初に必要。tag 駆動の release workflow を整える。
2. **xdist 実機検証 + env 並列** — 大規模 env 運用に着手したいケースが出てきた時にまとめて。並列の壁（Hub の状態、QGIS の Qt 多重起動）を実機で叩いてから設計判断。
3. **B5c（qgis_message.log）** — 失敗解析でログが欲しくなったタイミングで。Worker 側 push が必要なため、push event と同時期に着手すると効率が良い。
4. **push event / deferred mode** — poll ベースで困るユースケース（長時間処理、リアルタイム監視）が具体化してから。プロトコル拡張なので互換性に注意。
5. **Flaky 検出 / Summary HTML / 動画録画 / remote** — どれも「あれば便利」系。具体的な要望に応じて優先度を上げる。

「使い込んで困った順に詰める」が基本方針です。

---

## 関連 ADR

- [ADR-0001: qgis-puppeteer アーキテクチャ](architecture/0001-qgis-puppeteer-architecture.md)
- [ADR-0002: E2E テストアーキテクチャ](architecture/0002-e2e-test-architecture.md)
- [ADR-0003: Test Environments](architecture/0003-test-environments.md)
- [ADR-0004: Inline QGIS Spawn Helper](architecture/0004-inline-qgis-spawn-helper.md)
