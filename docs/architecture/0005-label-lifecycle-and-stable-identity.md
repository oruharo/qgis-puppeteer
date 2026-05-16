# ADR-0005: Label のライフサイクルと安定 identity — 同一 label 連続再起動を一級ユースケースにする

- **Status**: Accepted（Phase 1–3 実装済み: nonce instance_id / SUPERSEDE /
  RESUME / launch_token / sticky 再解決 / conflict 3 モード / heartbeat liveness）
- **Date**: 2026-05-16
- **Deciders**: qgis-puppeteer maintainers
- **Related**:
  - ADR-0001 §5「instance 命名と selector 解決」「Worker 再接続時の instance_id 引き継ぎ」 — **本 ADR が当該部を supersede する**
  - ADR-0002 §10.4 `@pytest.mark.fresh_qgis` — テスト粒度の QGIS 再起動（label は据え置きで再起動するパターンの典型）
  - ADR-0004 Inline QGIS spawn helper — test 内 spawn でも同一 label 再利用が起こり得る

## Context

### 解決したい問題

ADR-0001 §5 は **label をグローバル一意**（grace 期間中の entry も予約扱い）に設計した。
一方、実際の利用で圧倒的に多いのは次のループである:

```
QGIS を label=A で起動 → プラグイン編集 → QGIS を kill
  → 同じ label=A で再起動 → … （日に何十回も繰り返す）
```

ここでの利用者の意図は明確に **「A は同じ論理ロール。プロセスが変わっただけ」** であり、
新しい A は古い A を**引き継ぐ**べきものであって、衝突として弾かれるべきではない。

現状の問題点:

1. **grace 窓（60 秒）中の同一 label 再起動が `LABEL_CONFLICT` で拒否される。**
   kill 直後に再起動する開発ループはほぼ必ずこの窓に入り、2 台目が
   **未登録のまま放置**される（メッセージバー警告のみで silent に近い）。
2. **`instance_id = worker-{label}-{pid}` が pid 依存。** 再起動で pid が変わるたびに
   instance_id が変わる。OS が pid を再利用すると別プロセスと衝突し得る
   （ADR フォローアップで指摘済みの穴 A/B）。
3. **sticky が instance_id を凍結するため再起動で壊れる。**
   `qgis_use_instance("A")` は内部で instance_id（pid 入り）に解決して固定するので、
   A を再起動した瞬間に sticky が宙に浮き、毎回 `use_instance` し直しが必要。
   これは「同じロールを使い続けたい」という利用者の期待に反する。

### 利用側ユースケース（本 ADR の判断基準）

| # | ユースケース | 現状の挙動 | 望ましい挙動 |
|---|---|---|---|
| U1 | **連続再起動・同一ロール（支配的な開発ループ）** kill → 即 同 label 起動 | grace 衝突で 2 台目が未登録 | 新プロセスが透過的に label を**継承**。client 側 sticky も無調整で継続 |
| U2 | **同時 multi-instance** A と B を並行起動（別 label） | OK | OK（維持） |
| U3 | **事故的な同時同 label** 誤って A を 2 つ**同時**起動（両方 live） | conflict（弾く） | 真に曖昧なので**明示エラー**で気づける（維持・改善） |
| U4 | **Hub クラッシュ→再 spawn→worker 再接続** 同一プロセスが復帰 | resume（previous_instance_id 一致） | 維持。pid を一致条件から外す |
| U5 | **pid 再利用** OS が pid を使い回す | 穴 A/B（auto-label 衝突 / pid selector 誤配） | identity から pid を排除して無効化 |
| U6 | **client からの参照** per-call `instance="A"` / sticky `use_instance("A")` | 再起動で壊れる | 「**現在 live な A**」へ常に再解決され、再起動を跨いで安定 |
| U7 | **kill -9 直後の再起動（clean `bye` 無し）** | Hub が旧 A を active と誤認 → conflict | takeover で旧 A を陳腐化し継承（opt-in） |
| U8 | **非制御 launch・規約なし** 人間が素の `qgis.exe` を label 無しで起動。client は「自分が依頼した起動」を同定したい | auto-label（project+pid）になり client は当て推量（newest/only/project）でしか拾えない＝**当て推量** | best-effort 相関を**仕様として明示**し racy と明記。決定的にしたいなら helper 経由へ誘導 |
| U9 | **半制御 launch** 人間/エージェントが**公式 helper 経由**で起動（env 注入は helper が代行） | 仕組みなし | helper が `launch_token` を発番・注入し**呼び出し元へ返す**→ client は token で**決定的**に相関 |

設計判断は上表 U1–U9 を満たすことを基準に行う。

### 根本原因

ADR-0001 は **label を identity の主キー兼一意制約**として扱い、かつ
**instance_id の一意性を pid に依存**させた。しかし利用実態では:

- label は「**安定した論理ロール名**」であり、寿命はプロセスより長い（再起動を跨ぐ）
- 一意であるべきは「**ある時点で active な label**」だけで、過去/grace は含めない
- instance_id は「**1 回の登録セッションの handle**」であり、pid とは無関係であるべき

### Launch 制御モデルと相関（attribution）問題

ADR-0001 は **QGIS の起動を MCP の制御外**に置いた（Worker = QGIS プラグインが
dial out して register する。Hub/Gateway は QGIS を起こさない）。このため
**「自分が起こした（はずの）インスタンスを、起動後にどう同定するか」**＝
attribution が独立した問題として存在する。uniqueness/lifecycle（U1–U7）とは別軸。

launch を制御度で 3 分類する:

| モデル | 起動主体 | キー注入 | 相関の質 | 該当 |
|---|---|---|---|---|
| **制御下** | テストコード / 将来 `qgis_launch_instance` | 起動側が env を注入でき、pid も知る | **決定的** | pytest `spawn_qgis()`（ADR-0004） |
| **半制御** | 人間/エージェントだが**公式 helper 経由** | helper が代行注入し token を呼び出し元へ返す | **決定的** | Claude Desktop + 公式 launch helper（U9） |
| **非制御** | 人間が素の `qgis.exe` を直接 | 不可 | 規約 or 当て推量 | 手動起動・規約なし（U8） |

重要な認識: 非制御 launch では **client は「起動時点の identity」を *取得* できない**。
できるのは次の 2 つだけ:

1. **規約（label contract）**: 起動側が `QPUPPETEER_WORKER_LABEL=A` を**事前合意で**
   設定し、client は `A` で参照する。鍵は runtime に *derive* されるのではなく
   **起動前に両者が知っている約束**。これが唯一の堅牢解で、D2/D3 はこの規約成立を
   前提に「A を再起動跨ぎで安定参照」を担保する。
2. **規約が無い時の当て推量（best-effort）**: 「active が 1 台だけ」「project basename
   一致」「依頼時刻以降に register された最新」等。同時起動・pid 再利用・grace 残骸が
   あると外す。**これは識別ではなく attribution の推定**であり、racy。

したがって設計の方向性は **「非制御を減らし、半制御へ寄せる」**: 公式 launch helper が
**人間が label を発明しなくても決定的に相関できる専用トークン**を発番・伝播する
（D6）。これにより U8 の当て推量に頼る場面を構造的に縮小する。

label を「ロール」、instance_id を「登録セッション handle」と再定義すれば
U1–U7 は自然に解ける。

## Decision

### D1. instance_id を pid 非依存の per-registration nonce にする

`instance_id = worker-{label}-{pid}` を廃止し、登録ごとに採番する不透明 ID にする:

```
instance_id = "w-" + base32(random 80bit)        # 例: w-7k3p9q2m4x8a
```

- pid は identity から外し、`InstanceInfo` の**人間向け補助メタデータ**としてのみ残す
  （`list_instances` 表示・診断用）。
- selector 解決順から **`pid` 完全一致を削除**（U5 穴 B を構造的に閉じる）。
  pid で当てたい運用は無くす。どうしても必要なら診断コマンドで pid→instance_id を
  引く別 API を用意（本 ADR スコープ外）。
- ADR-0001 §5「数字のみ label は pid と衝突するため無効」の理由は消えるが、
  **数字のみ label は引き続き禁止**（selector の曖昧さ回避・後述 D3 の一意制約と
  人間可読性のため）。

### D2. label を「ロール」とし、一意制約を *active のみ* に緩める + 再起動継承

register 時の判定を以下の優先順に再定義する（ADR-0001 §5 の判定を置換）:

1. **RESUME**: `previous_instance_id` が指定され、それが grace 中 entry と一致し、
   かつ `label` 一致 → 同じ instance_id を返す（**pid は一致条件から除外**、U4/U5）。
2. **SUPERSEDE（新設・U1 の核心）**: 当該 label に **active な entry が無く**、
   grace 中の entry がある → grace entry を**即時 evict** して、
   **新しい instance_id で新規登録**（label は継承、`superseded: true` を ack に付与）。
   `LABEL_CONFLICT` は返さない。
3. **CONFLICT（U3）**: 当該 label に **active な entry が存在**する → 後述 D4 /
   **C1=(b)（下記 amendments）** に従う。既定は「即エラーにせず incumbent の
   生死を ping 確認し、生存なら reject / 死亡なら SUPERSEDE」。
4. **FRESH**: 上記いずれにも該当しない → 新規登録。

ポイント: 「一意であるべきは *active な label*」に制約を緩める。grace entry は
予約ではなく**陳腐化候補**として扱う。これで U1（連続再起動）が無摩擦になる。

### D3. sticky は instance_id を凍結せず「解決済み selector（label 優先）」を保持

`qgis_use_instance(selector)` の MCP Gateway 側挙動を変更:

- 現状: selector → instance_id に解決し instance_id を凍結保持
- 変更: **解決に使った安定キー（label があれば label、無ければ instance_id）を保持**し、
  **各 dispatch 時に Hub へ再解決を問い合わせる**

これにより U6 が成立: `use_instance("A")` を 1 回呼べば、A を何度再起動しても
dispatch のたびに「現在 live な A」へ解決される。再解決の単一責任は Hub
（ADR-0001「selector→instance_id 解決は Hub の責務」を維持）。

- 再解決で「active な A が 0 件」→ `INSTANCE_NOT_FOUND`（A 再起動の谷間。
  client は短期リトライ可能）。
- 再解決で「active な A が 2 件以上」→ `INSTANCE_AMBIGUOUS`（U3 の事故。fail-safe）。
- instance_id をそのまま渡された場合（label 無し参照）は従来通り厳密一致。
  再起動で消えるのは仕様（instance_id はセッション handle なので当然）。

> **前提条件**: 本 D3 の「再起動跨ぎ安定参照」は **label contract が成立している
> 場合のみ**成り立つ（D6 の `launch_token` でも同等の安定参照が可能）。規約も
> token も無い U8 では sticky は discover した instance_id 凍結に退避し、再起動で
> 失効する（contract が無い以上、安定 identity は原理的に提供不能）。

### D4. active 同 label 衝突（U3/U7）のポリシーを明示・選択可能化

当該 label に **active entry あり**の場合の挙動を 3 モードにし、env で選択:

| モード | env | 挙動 | 用途 |
|---|---|---|---|
| `reject`（既定） | （無指定） | `LABEL_CONFLICT` + `suggested_label` を返す。新 worker は未登録のまま（現状互換だが原因が明示される） | U3 の事故を確実に気づかせたい |
| `takeover` | `QPUPPETEER_WORKER_TAKEOVER=1` | 既存 active entry を強制 evict（`evicted_by_takeover` を旧 worker へ通知）し、新 worker が label を奪取 | U7（kill -9 で旧 A が active 誤認のまま）/ 開発ループの確実な継承 |
| `suffix` | `QPUPPETEER_WORKER_LABEL_SUFFIX=1` | `suggested_label`（`A (2)` 等）で**自動再登録**し未登録放置しない | 同時多重起動を許容しつつ全部繋ぎたい CI 探索用途 |

開発ループ（U1+U7）の推奨は **label 明示 + `QPUPPETEER_WORKER_TAKEOVER=1`**。
takeover を既定にしない理由: U2/U3 の同時 multi-instance 安全性を既定で守るため
（事故的同 label を silent に奪い合うと診断困難になる）。

### D5. kill -9 直後の継承を速くする（liveness 検出の補強）

U7 の根因は「旧 A の TCP half-open を Hub が検出する前に新 A が来る」。
takeover（D4）で機能的には解決するが、`reject` 既定運用でも摩擦を減らすため:

- Hub→Worker の **WS ping/pong ハートビート**（既定 10s 間隔・2 回欠落で
  disconnected 認定）を導入し、旧 entry を速やかに grace へ落とす。
- これにより `reject` 運用でも「kill → 数十秒待てば SUPERSEDE 経路で素直に継承」
  が成立する（待てない人は takeover を使う）。

### D6. `launch_token` による決定的相関 + 公式 launch helper

attribution（U8/U9）を解くため、**label とは別の専用相関トークン**を導入する。

#### なぜ label と分けるか

「起動時にユニークキーを発番して label に入れる」案は uniqueness は解くが、
**label に毎回ランダム値を入れると U1/U6 の *安定ロール* が壊れる**
（`use_instance("A")` の `A` が起動ごとに変わる uuid になり、人間が `@A` で
選べない・sticky の意味が消える）。よって 2 つの関心を分離する:

| 概念 | 役割 | 寿命 | 値 | 誰が決める |
|---|---|---|---|---|
| `label` | 人間向け**安定ロール名** | プロセスより長命（再起動跨ぎ） | 任意・人間可読 | 人間（任意） |
| `launch_token` | **1 起動の決定的相関ハンドル** | その起動プロセス限り | 自動発番 nonce | launch helper |
| `instance_id` | 登録セッション handle（D1） | 1 register 限り | 自動発番 nonce | Hub |

`launch_token` はユーザーの「自動発番ユニークキー」案を、安定 identity を壊さない
位置（label ではなく専用フィールド）に置いたもの。

#### 仕組み

1. **公式 launch helper を提供**（CLI `qgis-puppeteer launch` / wrapper `.bat` /
   pytest `spawn_qgis()` は内部でこれを使用）。helper は起動前に
   `launch_token = "lt-" + base32(random 80bit)` を発番。
2. helper が QGIS プロセス env に `QPUPPETEER_LAUNCH_TOKEN=<token>`（必要なら
   `QPUPPETEER_WORKER_LABEL` も任意で）を注入して起動。
3. helper は **token を呼び出し元へ返す**（CLI: stdout / `--print-token`、
   API: 戻り値、`spawn_qgis()`: `handle.launch_token`）。
4. QGIS プラグインは env を読み、`register` payload に `launch_token` を載せる。
5. client は selector に `launch_token` を渡せる。Hub は selector 解決の
   **最上位 tier**で `launch_token` 完全一致を判定（最も特定的・決定的・
   pid 非依存・人間規約不要）。

これで U9 が決定的に解ける。helper を経由すれば人間/エージェントは label を
発明しなくてよく、当て推量（U8）に落ちない。

#### selector 解決順（D1 と統合した最終形）

ADR-0001 §5 の解決順を以下で置換:

1. `launch_token` 完全一致（**最優先**・決定的相関）
2. `@`+`label` 完全一致
3. `label` 完全一致（active のみ。D2/D3）
4. `instance_id` 完全一致（セッション handle・再起動で失効）
5. `project` basename 一致（best-effort・U8 当て推量の一手段）
6. ~~`pid` 一致~~ → **廃止**（D1）

#### RESUME キーの強化（D2 と統合）

D2 の RESUME 一致条件を **`launch_token` 単独**でも成立可能にする
（worker はメモリに token を保持し再接続時に再送できる。token は 1 プロセス内で
不変かつ一意なので、pid を排した理想的な resume キー）。最終形:

- RESUME 成立 = `launch_token` 一致（token あり）**または**
  (`previous_instance_id` + `label`) 一致（token 無しの後方互換経路）。

#### best-effort 相関の仕様化（U8）

token も label 規約も無い場合に限り、Hub は `registered_seq`（単調増加の登録
連番）と `registered_at` を `InstanceInfo` に持たせ、client 向けに
`wait_for_new_instance(since_seq)` を提供する（「`since_seq` 以降に register された
最初の 1 件」）。**これは racy と明記**し、同時起動時は曖昧になり得る旨を docs に
警告として書く。U8 はあくまで最後の手段であり、helper 経由（U9）が正道。

## Review-driven amendments（Accepted・実装済み）

multi-perspective レビュー（architect + code-reviewer）の指摘を受けた確定事項。

### C1=(b): CONFLICT は即 reject せず incumbent を ping 確認

レビュー C1: 支配的ループ `kill -9 → 即再起動` では旧 entry がまだ active
（heartbeat demotion 前）なので D2 step 3 が CONFLICT に落ち、既定 reject だと
2 台目が未登録になる＝ADR の看板ユースケースが env 設定なしでは無摩擦に
ならない。**採用案 (b)**:

- active 同 label 衝突を検出したら即決せず **PENDING** とし、Hub が incumbent
  へ WS ping を撃つ。`LIVENESS_PROBE_SECONDS`(=2s) 後に判定:
  - incumbent が pong 応答（= 生存）→ 真の衝突として **reject**（U3 fail-safe 維持）
  - 誰も応答しない（= 死亡）→ evict して **SUPERSEDE**（U1/U7 を env なしで救済）
- `takeover` / `suffix` は従来どおり**明示 opt-in の即時オーバーライド**として残す
  （probe を経ない）。これにより H2（takeover 既定化の危険）を回避。
- 純ロジック: `register_worker` が `pending=True` + `liveness_probe_conn_ids` を
  返し、Qt 層が ping + タイマ後 `finalize_pending_registration()` を呼ぶ。
  newcomer が probe 中に切断したら `cancel_pending_registration()`。
- 却下した (a)「label 明示時 takeover 既定」: 多数派の明示 label 構成で
  takeover が既定 ON になり U3 fail-safe と共有 Hub 安全性を失うため。

### H3: タイマ不変条件（idle-shutdown を grace 中は抑止）

レビュー H3: idle-shutdown(30s) < grace(60s) のため、kill→demotion 後の谷間で
Hub が自殺し grace entry/sticky/`registered_seq` を喪失し SUPERSEDE が不能化。
**確定**: 不変条件 `LIVENESS_PROBE < HEARTBEAT_TIMEOUT < GRACE` を維持し、
かつ **grace entry が 1 件でも存在する間は idle-shutdown を抑止**する
（ADR-0001 §4「grace は idle-shutdown を妨げない」を本 ADR が上書き）。誰も
戻らなければ grace 満了後に通常終了するので Hub が永久に残ることはない。

### H4: best-effort attribution は silent に誤帰属しない

`wait_for_new_instance(since_seq)` は窓内に新規 instance が**複数**現れたら
「最初の 1 件」を黙って返さず `AttributionAmbiguousError` を送出する
（automation で最悪の silent 誤りを排除）。決定的にしたい場合は helper +
`wait_for_instance(launch_token)`。

### sticky は *明示* label のみ採用（auto-label roaming 防止）

`InstanceInfo.label_explicit` を追加。sticky 安定キーは
`launch_token > 明示 label > instance_id` の順。auto 採番 label / `suffix` で
rename された label は再起動で値が変わるため sticky には使わず instance_id
凍結に退避する（D3 の「contract が無ければ凍結退避」を厳密化）。

### その他の確定（code-reviewer）

- **H-1**: selector 解決順を Hub / MCP gateway / client の 3 重実装から
  単一純関数 `hub_state.select_by_selector` に統合（drift を構造的に排除）。
- **H-2**: `_try_resume` の 2 index 張り替えを不整合窓なしの順序に修正。
- **H-3**: `sweep_stale_active` で `last_seen_at is None` を「即 stale」扱いに
  （永久 liveness 免除の穴を封鎖）。
- **M**: `launch_token` を register 時に検証（`lt-` prefix + 64 字上限）。
  `generate_instance_id` の docstring を hex 実装に合わせて訂正。

### 残存リスク（accepted / deferred）

- **H2（takeover 権限が worker env 側）**: C1=(b) で既定経路から takeover を
  外したため危険面は大幅縮小。明示 `QPUPPETEER_WORKER_TAKEOVER=1` を共有 Hub
  で使う場合の incumbent veto は **"session ownership" 軸**を要し、本 ADR
  スコープ外として **deferred**（docs に「単一ロール開発時のみ」と明記済み）。
- **M6（restart valley の `INSTANCE_NOT_FOUND`）**: client 短期リトライで吸収。
  Hub 側ディスパッチ保留は将来の最適化として deferred。

## Consequences

### Positive

- **U1（支配的ループ）が無摩擦**: 同一 label kill→再起動が SUPERSEDE で素通り。
- **U6 が成立**: client の `use_instance("A")` が再起動を跨いで生存。テスト/Claude
  双方で「A を一度選べばずっと A」。
- **U5 の穴が構造的に閉じる**: pid が identity から消え、pid selector も廃止。
- **U4 を維持しつつ堅牢化**: resume の一致条件から pid を除去（pid 再利用で誤
  resume しない）。
- **U3 を fail-safe で維持**: 真の同時同 label は明示エラー（既定 reject）。
- **U9 が決定的**: 公式 helper 経由なら `launch_token` で起動分を一意特定。
  人間が label を発明しなくてよく、attribution の当て推量に落ちない。
- **U8 を仕様化**: 規約なし非制御 launch でも racy と明記した best-effort 経路
  （`registered_seq`）を提供し、未定義動作にしない。

### Negative / トレードオフ

- **instance_id が不透明化**: `worker-a-1234` のような可読 ID が無くなる。
  → 緩和: `list_instances` は label/pid/project を併記。人間は label で参照する
    のが正となる（むしろ健全化）。
- **dispatch ごとに label 再解決の往復が増える**: Hub への問い合わせコスト。
  → 緩和: Hub は in-memory map で O(1)。gateway は短 TTL（例 1s）キャッシュ可
    （再起動検出のため長 TTL は不可）。
- **takeover の誤用リスク**: 同時 multi-instance 環境で TAKEOVER=1 を流用すると
  正規の別 A を奪う。
  → 緩和: 既定 off。docs で「単一ロールを連続再起動する開発時のみ」と明記。
- **既存 instance_id をハードコードした利用者の破壊的変更**: 旧 `worker-a-<pid>`
  形式を前提にした参照は壊れる。
  → 移行: そもそも pid 入り instance_id は再起動で変わるため固定参照は元々
    anti-pattern。label 参照へ移行を促す（移行ガイド後述）。

### Breaking changes

- selector 解決順から `pid` 完全一致を削除（ADR-0001 §5 step 5 廃止）。
- `instance_id` フォーマット変更（`worker-{label}-{pid}` → `w-<nonce>`）。
- `register_ack` に `superseded: bool` を追加（後方互換: 旧 client は無視可）。
- grace 中 label が「予約」から「陳腐化候補」に意味変更。
- `register` payload に `launch_token`（任意）追加、selector 解決順を D6 の最終形へ
  置換、`InstanceInfo` に `registered_seq` / `registered_at` 追加（いずれも加算的・
  旧 client 無視可）。

## Migration

1. **利用者**: instance を参照する箇所を **label / `@label` / `launch_token` に統一**。
   instance_id・pid のハードコードを除去（元々再起動非対応なため実害は限定的）。
2. **開発ループ運用**: 起動時に `QPUPPETEER_WORKER_LABEL=<role>` を設定し、
   連続再起動するなら併せて `QPUPPETEER_WORKER_TAKEOVER=1` を推奨設定に。
   自動化（Claude Desktop / CI）は **公式 launch helper 経由**にして
   `launch_token` で相関するのを既定とする。
3. **pytest（ADR-0002/0004）**: `qgis` fixture の instance 解決を
   `launch_token`（`spawn_qgis()` が helper 発番）ベースへ。手動既存 QGIS 相乗り
   時のみ label ベース。`spawn_qgis()` は内部で launch helper を呼び
   `handle.launch_token` を公開。
4. **docs/user-guide.md**: Multi-instance 章に「launch 制御モデル」「同一 label
   連続再起動」「launch helper と launch_token」節を追記。Troubleshooting に
   `label_conflict`（active 衝突時 3 モード）と「自分の起動分が見つからない
   （attribution）」を追加。

## Rollout（段階導入）

- **Phase 1**: D1（nonce instance_id）+ D2（SUPERSEDE/RESUME 判定の置換、pid 除外）。
  これだけで U1/U4/U5 の大半が解消。`pid` selector 廃止を含む。
- **Phase 2**: D3（sticky の label 再解決化）+ D6（`launch_token` + 公式 launch
  helper + selector 解決順最終形 + RESUME 強化）。U6 を成立させ、U8/U9 の
  attribution を仕上げる。`spawn_qgis()` を helper ベースへ移行。
- **Phase 3**: D4（conflict 3 モード + env）+ D5（ping/pong liveness）。
  U3/U7 を仕上げ。
- 各 Phase は独立 PR。Phase 1 完了時点で開発ループの主要痛点は消え、Phase 2 完了で
  自動化シナリオの相関が決定的になる。

## Alternatives considered

1. **label 一意制約を完全撤廃し、常に suffix 自動採番**
   → 却下。U6 が壊れる（`use_instance("A")` がどの `A`/`A (2)` か曖昧化）。
   ロール参照の安定性を失う。

2. **grace を 0 にして即削除（予約をなくすだけ）**
   → 部分対症療法。U4（Hub 瞬断 resume）が壊れ、U7（kill -9 半開）も未解決。
   SUPERSEDE の方が resume と両立できる。

3. **instance_id を pid のままにして resume 条件から pid だけ外す**
   → U5 穴 B（pid selector 誤配）と U6（sticky 凍結）が残る。中途半端。

4. **client 側で再起動検知してリトライ**
   → 各 client に再実装を強いる。identity の正解を持つ Hub に寄せる方が単純で
   一貫（ADR-0001 の責務分担原則とも整合）。

5. **takeover を既定にする**
   → U2/U3（同時 multi-instance）の安全性が既定で失われる。事故的同 label を
   silent に奪い合い診断困難。opt-in が安全側。

6. **発番ユニークキーを `label` にそのまま入れる（ユーザー初案の素直な実装）**
   → uniqueness は解くが U1/U6 が壊れる。label が毎起動 uuid 化し人間の `@A`
   選択・sticky の「安定ロール」が消える。→ 採用: キーは label と分離した専用
   フィールド `launch_token`（D6）に置き、label は人間向け安定ロールとして温存。

7. **worker が自前で nonce を生成して register（helper 不要）**
   → worker が一意 nonce を持っても、**非制御 launch では client がその nonce を
   知る術がない**ため attribution は解けない（現状の auto-label と同じ袋小路）。
   決定的相関には「**起動側が token を発番し呼び出し元へ返す**」経路が必須＝
   公式 launch helper（D6）が要る。worker 自前 nonce は instance_id（D1）で
   既に充足しており、相関問題とは別レイヤ。
