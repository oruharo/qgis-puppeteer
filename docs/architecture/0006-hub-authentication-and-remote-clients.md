# ADR-0006: Hub につなぐ相手の確かめ方・版の確かめ合い・別の機械からの接続

- **Status**: Accepted（実装済み：寛容な復号、プロトコル 2 の挨拶、Hub ファイル、`--listen` と TLS）
- **Date**: 2026-10-30
- **Deciders**: qgis-puppeteer maintainers
- **Related**:
  - ADR-0001 §3「挨拶（hello と proof）」「エラーコード」、§4「Hub の起動競合回避」、§6「Hub の spawn と配置」、§9「セキュリティ境界」 — **本 ADR が決めたことの細部はそこに書いてある**（プロトコルの仕様は ADR-0001 §3 が正）
  - ADR-0002「Hub / Worker の起動責務」（pytest の `hub_process` が Hub ファイルで Hub を起こす）
  - ADR-0005 `launch_token`（見捨てた launch の拒否を `launch_abandoned` にした）

## Context

最初の公開の前に、Hub への接続まわりを見直した。困っていたことは 4 つある。

### 1. 誰でも Hub につなげた

- Hub が確かめていたのは WebSocket の Origin ヘッダだけで、Origin が無ければ通した。
  Origin はブラウザしか送らないので、同じ機械の**ほかの OS ユーザー**のプロセスも
  Hub のポートにつなぎ、QGIS に何でも（`execute_python` も）頼めた。
- role は自己申告で、`list_instances` は register の前でも通った。
- つなぐ側は、相手が Hub かを確かめなかった。そのポートで先に待ち受けたプロセスは
  Hub になりすませた。共有の機械では、2 人目の QGIS が 1 人目の Hub に登録されえた。

### 2. 接続先の決め方がばらばらだった

- Hub はポート 9876 で待ち受け、接続先は `QPUPPETEER_HUB_URL` / `HOST` / `PORT` /
  `ORIGIN`（pytest は `QPUPPETEER_E2E_HUB_URL`）で変えた。読み方が場所ごとに違い、
  Hub は文書に書いてあるほど環境変数を読んでいなかった。
- pytest は空いたポートを探していったん閉じてから Hub に渡すので、その間に別の
  プロセスに取られえた。シェルの `QPUPPETEER_HUB_URL` は pytest が起こす QGIS に漏れた。
- 「Hub が動いている」は「そのポートで何かが TCP を受ける」でしかなかった。
- lock は POSIX では `/tmp` にユーザー別でなく置かれた。pid ファイルは書くだけで
  誰も読まなかった。

### 3. 版を誰も比べなかった

- `protocol_version` は送るだけで、誰も比べなかった。版の違う QGIS のプラグインと
  pytest が、黙って食い違ったまま動いた。
- 復号が厳格で、知らないエラーコードが来ると応答ごと読み捨て、呼び出し側は
  時間切れまで待った。新しいエラーコードを足せなかった。
- Hub は知らない type のメッセージに id `"<unknown>"` で答え、誰にも届かなかった。
  応答を id だけで振り分け、送り主を確かめなかった。

### 4. 別の機械から操作できなかった

- どこも localhost を前提にしていた。Hub は 127.0.0.1 だけで待ち受け、通信は暗号化していない。
- Windows の VM や別の PC の QGIS を手元から動かしたい、WSL の中から Windows の QGIS を
  動かしたい、という使い方に応えられなかった。

## Decision

### D1. 信頼の境界

- **Hub はいつも QGIS と同じ機械に置く**。別の機械にあってよいのはクライアント
  （MCP gateway・pytest・`AutomationClient`）だけ。Worker と Hub の間は今のまま
  127.0.0.1 で、request の期限（ADR-0001 §3 `deadline`）の壁時計の比較も同じ機械の中で済む。
- **同じ機械の中の境界は「同じ OS ユーザー」**。同じユーザーで動くプロセスは、どのみち
  QGIS を操作できるので、そこは守らない。
- **ネットワーク越しの境界は「接続情報ファイル（Hub ファイル）を持っている人」**。
  ファイルには token と証明書の指紋が入る。**ファイルを渡した相手は QGIS に Python を
  実行させられる**。これを利用ガイドに明記する。
- 守るもの：同じ機械のほかのユーザーのプロセス、ブラウザ経由（DNS rebinding。Origin の
  検査は残す）、古い Hub ファイルが指すポートを別のプログラムが使っている場合、
  ネットワーク上の盗聴・なりすまし・乗っ取り。

### D2. Hub ファイル

Hub は待ち受けを始めたら、本人だけが読めるファイルに、アドレス・token・pid・
`package_version`・版の範囲・起動時刻を書く（Jupyter の connection file と同じ考え方）。

- token は起きるたびに作り直す（32 バイトの乱数）。止まるときは、中の token が自分の
  ものなときだけ消す。一時ファイルに書いて `os.replace` で一度に置き換える。
- 置き場所は、明示（Hub の `--hub-file`、`AutomationClient(hub_file=)`）→ 環境変数
  `QPUPPETEER_HUB_FILE` → 既定（Windows は `%LOCALAPPDATA%\qgis_puppeteer\hub.json`、
  POSIX は `$XDG_RUNTIME_DIR` か `~/.local/state` の `qgis_puppeteer/hub.json`。
  フォルダ 0700・ファイル 0600）。全員が同じ規則で決める。
- **ポートは OS に選ばせる（port 0）**。ユーザーごと・pytest の session ごとに別の Hub・
  別のポートになり、空きポートの取り合いが無くなる。
- 「生きた Hub」は、Hub ファイルがある・pid が生きている・ファイルのアドレスで挨拶（D3）が
  通る、の 3 つで決める。TCP が通るだけでは Hub とみなさない。
- lock は Hub ファイルの隣に置き（Hub ファイルごと・ユーザーごとに分かれる）、pid ファイルと
  ログの共有をやめる（ログも Hub ファイルの隣）。
- 環境変数は `QPUPPETEER_HUB_FILE` 1 つにする。`QPUPPETEER_HUB_URL` / `HOST` / `PORT` /
  `ORIGIN`、`QPUPPETEER_E2E_HUB_URL`、`QPUPPETEER_HUB_LOCK_PATH` は廃止する。
- 誰がどのファイルを使うか：

| 誰 | 使う Hub ファイル | Hub を起こすか |
|---|---|---|
| QGIS のプラグイン | `QPUPPETEER_HUB_FILE`、無ければ既定 | 立っていなければ、生きた Hub が無いとき起こす。立っていれば起こさない |
| MCP gateway | `QPUPPETEER_HUB_FILE`、無ければ既定 | 起こさない |
| pytest | session の一時フォルダ | `--hub-file` を付けて自分の Hub を起こす。起こす QGIS の env から `QPUPPETEER_HUB_*` を消し、`QPUPPETEER_HUB_FILE` を入れる |
| pytest の dev モード | `QPUPPETEER_HUB_FILE`、無ければ既定 | 起こさない |
| `AutomationClient` | `hub_file=`、`QPUPPETEER_HUB_FILE`、既定の順 | 起こさない。`address=` と `token=` で直接つなぐこともできる |

### D3. 挨拶：相互認証と版の確かめ合いを 1 往復半で（プロトコル 2）

```
接続 → Hub:   hello        {hub_version, protocol:{min,max}, nonce_h}
相手:         register     {…, protocol_version, protocol, package_version, nonce_c, proof}
Hub:          register_ack {…, protocol_version, package_version, hub_proof}
相手:         hub_proof を確かめる。違えば信用しない（HubAuthError）
```

- `proof = HMAC-SHA256(token, "qgis-puppeteer/client\n" + nonce_h + "\n" + nonce_c)`、
  `hub_proof` は向きの文字列を `qgis-puppeteer/hub` にし、nonce の順を入れ替えたもの。
- **token は通信に載せない**。互いに「同じ token を知っている」ことだけを示す。両方の nonce を
  混ぜるので録って流しても通らず、向きで文字列を変えるので相手の proof を返しても通らない。
- Hub は register が通るまでほかのメッセージを受け付けない（`unauthorized` で断って切る。
  `list_instances` の穴もふさがる）。10 秒以内に register が来なければ切る。
- **版を proof より先に見る**。重ならなければ `protocol_version_mismatch` を返し、details に
  双方の版の範囲と `package_version` を載せる。版 1 の相手は proof を持たないので、proof を
  先に見ると「版を合わせて」が伝わらない。QGIS のプラグインはメッセージバーに「同じ版を入れて」と出す。
- `PROTOCOL_VERSION = 2`。最初の公開の前なので、版 1 とは互換を取らない。
- `register` / `register_ack` / `InstanceInfo` に `package_version` を載せる。pytest は
  Hub や QGIS の `package_version` が自分と違えば警告する（失敗にはしない）。

### D4. 寛容な復号

- 知らないエラーコードは文字列のまま持つ（`Error.code: ErrorCode | str`）。
  `e.code == ErrorCode.X` は今までどおり書ける。
- 知らないフィールドは無視する。
- 知らない type には、id が読めればその id で `unsupported_message` を返す（Hub も Worker も）。
  id が読めなければログだけ。
- 見捨てた launch の拒否は専用のコード `launch_abandoned` にする（`label_conflict` の流用をやめる）。
- Hub は、その request を転送した Worker の接続から来た応答だけを送り主に届ける。

### D5. ネットワークから受け付ける（`--listen`）

- **既定は 127.0.0.1 だけ**。`--listen <IP>:<port>` を明示したときだけ、もう 1 か所で待ち受ける。
  そちらは **TLS だけ**（平文では受けない）。
- **ポートは決め打ち**。port 0 だと Hub を起こし直すたびに、写したファイルが古くなる。
- ネットワーク側につなげるのは**クライアントだけ**。Worker の register は `unauthorized` で断る（D1）。
- **token はローカル用と分ける**。ネットワーク用の token・自己署名の証明書・鍵は、リモートの
  フォルダ（`--remote-dir`。既定は Hub ファイルの既定のフォルダの `remote`）に一度だけ作って
  残し、起きるたびに使い回す。写したファイルは Hub を起こし直しても使える。`--rotate` で作り直す。
  ローカル用の token は起きるたびに変わるので、漏れても次の起動で無効になる。ネットワーク用の
  ファイルを人に渡しても、ローカルの token は渡らない。
- Hub はリモートのフォルダに `hub.json` を書く。アドレス（`0.0.0.0` ならループバック以外の
  自機の IPv4）・ネットワーク用の token・`tls.fingerprint_sha256` を入れ、pid は入れない
  （Hub が止まっても消さない）。クライアントはこれを写して `QPUPPETEER_HUB_FILE` で指す。
- **証明書は pin で確かめる**。CA と名前は見ず、証明書の SHA-256 がファイルの指紋と一致するか
  だけを挨拶の前に確かめる。違えば切って `HubAuthError`。指紋で照合するので、証明書の署名は
  安全に関係しない。安全は鍵の強さ（RSA 2048）と token に懸かる。
- **挨拶は TLS の中でもそのまま行う**。TLS は通り道を守り、挨拶は相手が token を持っているかを
  確かめる。役目が違う。
- **証明書と鍵は Hub が純 Python で作る**（`qgis_puppeteer.selfsigned`。標準ライブラリだけ）。
  QGIS 3.34 の Qt は TLS を使える（OpenSSL 3）が、QGIS の Python には証明書を作る手段が
  無い（`cryptography` も pyOpenSSL も openssl.exe も無く、QGIS の認証の API にも無い）。
  鍵は一度作れば残すので、作るのに数秒かかっても困らない。生成した鍵と証明書を Qt が読み込み、
  TLS が実際につながることを Qt の統合テストで確かめる。
- `--listen` の Hub は人が QGIS の機械で常駐させるもので、idle で止まらない。既定の Hub ファイルも
  書くので、その機械の QGIS は自分で Hub を起こさずにこの Hub につなぐ（QGIS より先に起こす）。
- VM の NAT の先など、Hub の知らない宛先でつなぐときは、token と指紋をファイルから読んだまま
  アドレスだけを上書きできる（`AutomationClient(address=)`、pytest の `--qgis-hub-address`）。

### D6. WSL から

- WSL の pytest は Windows の QGIS を起こせない（別の機械の QGIS を起こすことは今回やらない）。
  それまでは、起動中の QGIS を使う dev モードで使う。
- NAT モード（WSL の既定）では WSL から Windows の 127.0.0.1 に届かないので、Windows で
  `--listen` の Hub を常駐させ、その `remote\hub.json` を指す。
- mirrored モード（`networkingMode=mirrored`）では WSL の 127.0.0.1 が Windows に届くので、
  Windows の既定の Hub ファイルを `/mnt/c/...` で直接指す使い方も考えた。ただし、その
  ファイルの pid は Windows の pid で WSL からは見えないため、pid を確かめる「生きた Hub」の
  判定（pytest の dev モード）は通らない。いまは `--listen` の Hub を使う。

## Consequences

### Positive

- 同じ機械のほかのユーザーも、ネットワーク上の第三者も、token を持たなければ QGIS を操作できない。
  つなぐ側も、token を知らない相手を Hub として信用しない。
- ポートの取り合い・環境変数の漏れ・ログの共有が無くなる。pytest の session ごとに Hub が分かれる。
- 版の違いが、時間切れや黙った食い違いではなく、両方の版を載せたエラーと警告で見える。
  新しいエラーコードや type を足しても、古い相手が時間切れまで待たない。
- 別の機械と WSL から、TLS で QGIS を操作できる。写すのはファイル 1 つで、Hub を起こし直しても
  写し直さなくてよい。

### Negative / トレードオフ

- **暗号に関わるコード（RSA の鍵の生成と証明書の組み立て）を自分で持つ**。読む人の負担になる。
  外部のレビューに出すならここを見てもらう。
- プロトコル 2 は版 1 とつながらない。QGIS のプラグイン・Hub・pytest 側を同じ版でそろえる必要がある。
- ネットワーク用の Hub ファイルは秘密の情報で、渡した相手は QGIS に Python を実行させられる。
  管理は利用者に任せる。
- WSL の mirrored モードで、Windows の既定の Hub ファイルを直接使えない（D6）。

### Breaking changes

- プロトコル 2（挨拶が要る）。版 1 の相手には `protocol_version_mismatch`、版 1 の Hub には
  `HubAuthError`（hello が来ない）。
- 環境変数 `QPUPPETEER_HUB_URL` / `HOST` / `PORT` / `ORIGIN`、`QPUPPETEER_E2E_HUB_URL`、
  `QPUPPETEER_HUB_LOCK_PATH` の廃止。pid ファイルと `--no-pid-file` の廃止。
- `AutomationClient(url=)` → `address=` / `token=` / `hub_file=`。pytest の `hub_port` /
  `hub_url` fixture、`spawn_qgis(hub_port=)`、`E2EAutomationClient(url=)` の廃止。
- `Error.code` が `ErrorCode | str` になる。見捨てた launch の拒否のコードが `launch_abandoned` になる。
- Hub の終了コード：0 ふつうに止まった、1 待ち受けられない（TLS の無い Qt を含む）、
  2 Hub ファイル（またはネットワーク用の token と鍵）を書けない、3 同じ Hub ファイルに生きた Hub がいる。

## 今回やらないもの

- 別の機械での QGIS の起動・停止。ネットワーク用の Hub をどう常駐させるか（同梱の起動の
  スクリプトなど）もそこで決める。
- ファイル（スクリーンショットの保存先など）を通信で運ぶこと。いまパスは QGIS の機械のパスで、
  QGIS は自分の機械に書く。
- `qgis_execute_with_permission` の `always` と whitelist の置き場所。
- Worker の capabilities（Worker が何をできるかを Hub やクライアントに伝えること）。
- Origin の許可リストの設定化（ADR-0001 `## Roadmap`）。

## Alternatives considered

- **token をハンドシェイクのヘッダに載せる**：相互認証にならない（相手が Hub かを確かめられない）。
  token が通信に載る。挨拶のメッセージにすれば、Hub の状態の機械として純粋に試験できる。
- **名前付きパイプ / Unix ドメインソケット**：Qt の `QWebSocketServer` では使えず、Hub・Worker・
  クライアントの 3 つの接続の実装を作り直すことになる。
- **Hub をクライアントの機械に置き、QGIS（Worker）がネットワーク越しにつなぐ**：Worker の数だけ
  外向きの接続ができ、期限の比較も機械をまたぐ。QGIS の機械に Hub を置けば、将来、別の機械での
  QGIS の起動も同じ Hub に頼める。
- **証明書と鍵を利用者が用意する**（`--tls-cert` / `--tls-key`）：暗号のコードを持たずに済むが、
  鍵を手元から QGIS の機械へ運び、接続情報を QGIS の機械から手元へ運ぶ、という往復の手間がかかる。
- **TLS をやめて SSH のトンネルを案内する**：コードはいちばん少ないが、QGIS の機械に SSH の
  サーバーが要り（Windows では任意の機能として入れる）、WSL の NAT モードでも使いにくい。
- **TLS-PSK**（証明書なしで token を鍵にする）：Qt は対応しているが、Python の `ssl` が PSK に
  対応したのは 3.13 からで、サポートしている 3.10〜3.12 では使えない。
- **ネットワーク用とローカル用で token を 1 つにする**：簡単だが、ローカル用の token も起こし直しで
  変わらなくなり、ネットワーク用のファイルを渡すとローカルの token も渡る。
- **ネットワーク側も port 0 にする**：起こし直すたびに、写したファイルが古くなる。
