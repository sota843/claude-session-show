# Claude Usage Tray (Win11)

Claude Code の **5時間セッション枠**と**週次枠**の使用量を、Windows 11 のタスクトレイに
**二重リングの円グラフアイコン**で常時表示する常駐アプリです。

- **外周リング** = 5時間ローリングセッション枠の使用率
- **内周リング** = 週次枠の使用率
- 色: 〜60% 緑 / 〜85% 黄 / 〜100% 赤
- アイコンにマウスを乗せると、各枠の使用% と次のリセット時刻を表示
- 右クリックメニュー: 今すぐ更新 / 自動起動 ON・OFF / 終了
- **タスクバー埋め込みメーター** — 通知領域（`^`）のすぐ左に、バッテリー風の横長メーターで
  `5h ▰▰▱▱ 42%  2h13m` / `7d ▰▰▰▱ 73%  3d4h` を常時表示（下記「タスクバー表示」）

![sample](sample_95.png)

## データ源（2種類）
1. **`oauth`（既定・公式）** — Claude Code の `/usage` と同じ非公開エンドポイント
   `GET https://api.anthropic.com/api/oauth/usage` を呼び、**公式の利用率%・リセット時刻**
   （`five_hour` / `seven_day`）を取得します。認証はローカルの OAuth トークン
   （`~/.claude/.credentials.json`）。**トークン予算の設定は不要**で、`/usage` の表示と一致します。
2. **`ccusage`（推定・フォールバック）** — `~/.claude/projects/**/*.jsonl` のトークン使用量を
   [`ccusage`](https://github.com/ryoppippi/ccusage) で 5時間ブロック・週次に集計し、設定した
   予算で割った**推定ゲージ**。oauth が失敗した時（後述）に自動で使われます。

> ⚠️ **oauth エンドポイントは強くレート制限されます。** ポーリングは 180 秒以上（既定 300 秒）。
> また必須ヘッダ `User-Agent: claude-code/<version>` を自動付与します。
> アクセストークンは短命ですが、**本アプリが `refreshToken` を使って自動更新**します
> （`auto_refresh: true`。既定 ON）。失効間近・失効時・usage が 401 を返した時に、
> `~/.claude/.credentials.json` の `refreshToken` で新しい `accessToken` を発行し書き戻します。
> **通常のトークン更新では Claude Code を起動しておく必要はありません。**
> `refreshToken` 自体が期限切れ・取り消しになった場合は、一度 Claude Code で再ログインが必要です。
> その間は `fallback_to_ccusage` が
> true なら推定表示に切り替わります。

## 必要環境
- Windows 11
- Python 3.10+
- （oauth のみなら Node 不要）Node.js は **ccusage フォールバックを使う場合のみ**必要

## セットアップ
```powershell
# Python 依存
python -m pip install -r requirements.txt

# （任意）フォールバック用 ccusage を事前導入しておくと初回が速い
npm install -g ccusage
```
`ccusage` をグローバル導入した場合は `config.json` の `ccusage_cmd` を
`["ccusage"]` に変更すると高速・オフラインで動きます。

## 実行
```powershell
# 通常起動（コンソール無し）
pythonw tray_app.py

# デバッグ起動（ログをコンソールに表示）
python tray_app.py
```
タスクトレイに二重リングアイコンが表示されます。

## タスクバー表示
トレイアイコンは正方形・固定サイズのため、横長表示はタスクバー（`Shell_TrayWnd`）の
子ウィンドウとして独自に描画しています（TrafficMonitor と同じ方式）。

- 2段表示: 上 = 5時間枠 / 下 = 週次枠。電池の色はリングと同じしきい値で緑・黄・赤
- 右端はリセットまでの残り時間（`taskbar_band_reset` で時刻表示・非表示に変更可）。1分ごとに更新
- 取得失敗時は最後の値を半透明で表示
- クリックはタスクバーへ素通り。ライト/ダークテーマと DPI に自動追従
- エクスプローラー再起動やトレイのアイコン増減にも 1 秒以内に追従・再埋め込み
- タスクバーのボタン（アプリ・スタート・検索・ウィジェット）とは重ならないよう、
  UI Automation でボタン位置を調べて空きスペースに配置（Windows 11）。
  入りきらないときは「リセット列なし」→「数字だけ（`5h 42%`）」の順に縮め、
  トレイ横に空きがなければ別の空き（ウィジェットとスタートの間など）へ移動。
  どこにも入らなければ一時的に非表示（トレイアイコンは常に表示）
- マルチモニターなら `"taskbar_band_monitor": "secondary"` でサブモニターのタスクバー
  （時計の左）に表示。通知領域がないぶん空きが広く、モニター切断中はメインに戻る
- `"taskbar_band_side": "left"` で左下に表示（ウィジェット＝天気の右隣。ウィジェットを
  オフにすると左端）。Windows 11 のみ
- 位置はタスクバーごとにも指定可: `{"primary": "right", "secondary": "left"}` なら
  サブモニター接続中は左下、モニター 1 枚のときはメインの右下
- 値が変わったときだけ短いアニメーション: バーがなめらかに伸縮し % がカウント、
  光が 1 回横切る、しきい値をまたぐと色がふわっと変化、リセット時はスーッと減る。
  危険域（85%〜）の間だけバーがゆっくり明滅（`taskbar_band_animate` / `taskbar_band_pulse` で OFF 可）
- 常時アニメーション: 約 5 秒ごとに光がバーを横切り（5h → 7d の順）、小さな星がランダムに
  キラッと瞬く（星はバーが短いほど控えめ。データが古いとき・mini 表示ではどちらも出ない。
  `taskbar_band_shimmer` / `taskbar_band_sparkle` で個別に OFF 可）
- 位置がずれる場合は `taskbar_band_offset_x` で調整（マイナスで左へ）

> ⚠️ 非公式な埋め込み方式のため、Windows の大型アップデートで表示が崩れる可能性があります。
> その場合は `"taskbar_band": false` にすればトレイアイコンのみの従来動作に戻ります。

## 自動起動（Windows ログイン時）
トレイアイコンを右クリック →「Windows起動時に自動起動」をチェック。
スタートアップフォルダに `ClaudeUsageTray.lnk`（`pythonw tray_app.py`）が作成されます。
解除はもう一度チェックを外すだけ。CLI からは `python autostart.py enable|disable|status`。

## 設定 (`config.json`)
| キー | 説明 |
|---|---|
| `source` | `"oauth"`（公式・既定）/ `"ccusage"`（推定） |
| `fallback_to_ccusage` | oauth 失敗時に推定へ自動フォールバック（既定 true） |
| `auto_refresh` | 失効時に `refreshToken` で accessToken を自動再発行（既定 true）。Claude Code の起動が不要になる |
| `user_agent` | 使用量取得用 UA。`null` で `claude --version` から自動検出。認証更新のヘッダーは別管理 |
| `poll_interval_sec` | 更新間隔（秒）。既定 300（oauth は 180 未満不可） |
| `ccusage_cmd` | ccusage の起動コマンド。グローバル導入なら `["ccusage"]` |
| `ccusage_extra_args` | 追加引数。既定 `["--offline"]`（料金データのオンライン取得を回避） |
| `budget_mode` | （ccusage 時のみ）`"fixed"` プラン予算 / `"auto"` 過去ピーク基準 |
| `plan` | （ccusage `fixed` 時）`pro` / `max5x` / `max20x` |
| `plans` | 各プランの `session_tokens` / `weekly_tokens`（実運用に合わせて調整） |
| `thresholds` | 色のしきい値 `warn` / `crit` |
| `colors` | リングの色（RGB） |
| `show_center_text` | アイコン中央に 5時間枠の使用%（整数）を重ねる |
| `icon_size` | 内部描画サイズ（既定 64、Windows 側で縮小） |
| `taskbar_band` | タスクバー埋め込みメーターを表示（既定 true） |
| `taskbar_band_offset_x` | メーターの横位置調整（96dpi 換算 px、マイナスで左へ。既定 0。`left` 時は空きの中でのみ動く） |
| `taskbar_band_reset` | リセット列: `"remaining"` 残り時間（既定）/ `"clock"` 時刻 / `"off"` 非表示 |
| `taskbar_band_monitor` | 表示するタスクバー: `"primary"` メイン（既定）/ `"secondary"` サブモニター |
| `taskbar_band_animate` | 値が変わったときのアニメーション（既定 true） |
| `taskbar_band_pulse` | 危険域の間だけバーをゆっくり明滅（既定 true） |
| `taskbar_band_shimmer` | 約 5 秒ごとに光がバーを横切る常時アニメーション（既定 true） |
| `taskbar_band_sparkle` | バーの上で星がキラキラ瞬く常時アニメーション（既定 true） |
| `taskbar_band_side` | 表示位置: `"right"` 通知領域・時計の左（既定）/ `"left"` 左下（天気の右隣）。`{"primary": "right", "secondary": "left"}` でタスクバーごとに指定 |

> `source: "oauth"` なら `budget_mode` / `plan` / `plans` は使われません（公式%をそのまま表示）。
> ccusage 推定を使う場合、`budget_mode: "auto"` は週次がピークに張り付くため `"fixed"` 推奨。

## ファイル構成
| ファイル | 役割 |
|---|---|
| `tray_app.py` | エントリ。pystray アイコン + ポーリングスレッド + メニュー |
| `usage.py` | ccusage を実行し 5時間枠・週次枠を集計（単体実行で数値確認可） |
| `gauge.py` | Pillow で二重リングアイコンを描画（単体実行でサンプル PNG 出力） |
| `taskbar_band.py` | タスクバー埋め込みメーター（Win32 子ウィンドウ + Pillow 描画） |
| `band_anim.py` | メーターのアニメーション（タイミングと状態のみ。描画は taskbar_band） |
| `taskbar_uia.py` | タスクバーのボタン位置を UI Automation で取得（メーターの重なり回避用） |
| `config.py` / `config.json` | 設定の読み込みと既定値 |
| `autostart.py` | スタートアップ登録の ON/OFF |
| `appdata.py` | ログファイルと状態（リフレッシュのバックオフ / 最後の取得値）の保存 |

## 動作確認
```powershell
python usage.py          # 5時間枠・週次枠の使用トークン/リセット時刻を表示
python usage.py diag     # 認証状態（トークン期限・バックオフ・ログ場所）を表示
python usage.py refresh  # トークン再取得をその場で強制実行
python gauge.py     # sample_30/70/95/unknown.png を出力（色分け確認）
python taskbar_band.py png  # band_dark/light*.png を出力（3 種類のレイアウトの見た目確認）
python taskbar_uia.py [secondary]  # タスクバーのボタン位置（x 範囲）と時計/通知領域の左端を表示
python taskbar_band.py 15   # ダミー値でタスクバーに 15 秒間埋め込み表示
python taskbar_band.py demo # アニメーションを一通りタスクバー上で再生（約 25 秒）
python tray_app.py  # トレイ常駐
python -m unittest -v test_usage test_band_anim  # 回帰テスト（実通信なし）
```

## トークン更新とトラブルシューティング
accessToken（有効 8 時間前後）が期限切れに近づくと、`refreshToken` で自動再発行します。
起動時点で期限切れの場合も同じ処理で復帰します。

- トークン発行先は **`https://platform.claude.com/v1/oauth/token`**（`scope` 付き JSON）。
  インストール済み Claude Code 2.1.247 本体と同じ宛先・同じ形です。
- **認証更新と使用量取得の HTTP ヘッダーを分離**しています。2026-09-28 の実機調査では、
  認証更新に `claude-code/<version>` を送る従来の形式は 429 で失敗しました。
  本体の Axios と同じ `User-Agent: axios/1.9.0` と `Accept: application/json, text/plain, */*`
  に変更すると、同じ認証情報で更新に成功しました。使用量取得は引き続き `claude-code/<version>` を使います。
- 更新失敗時は **5分 → 15分 → 30分 → 60分** と間隔を空けて再試行（通信エラー時は 1分 → 2分 → 5分 → 10分）。
  状態は `state.json` に保存され、再起動しても引き継がれます。
- 更新中は `~/.claude/.tray_oauth_refresh.lock` で排他し、直前に `credentials.json` を読み直します。
  他プロセス（Claude Code など）が先に更新していれば、そのトークンをそのまま使います。
- `credentials.json` の更新を 5 秒間隔で監視。Claude Code を起動した瞬間に取得を再開します。
- 取得に失敗している間は **最後に取得できた値**をリングに残し、ツールチップに
  「⚠ 取得失敗 / 表示は◯分前の値」と理由を表示します（値は `state.json` に保存され再起動後も復元）。
- 右クリックメニューの **「トークンを再取得」** でバックオフを無視して即再試行、
  **「ログを開く」** で `%LOCALAPPDATA%/ClaudeUsageTray/tray.log` を開けます。

`pythonw` 起動ではコンソールが無く失敗理由が見えないため、動作ログは常に上記ファイルへ出力されます。
`refreshToken` 自体が期限切れ・取り消しになった場合は、一度 Claude Code で再ログインしてください。

## 既知の限界
- `oauth` の数値は `/usage` と同一（公式）。ただしエンドポイントは非公開で、Anthropic 側の
  仕様変更で将来動かなくなる可能性があります。その場合は `source: "ccusage"` で推定運用へ。
- oauth エンドポイントはレート制限が厳しく、短間隔ポーリングや手動連打は 429 を招きます
  （アプリ側で 180s 下限＋手動更新の最小間隔を設けています）。
- `ccusage` 推定はトークン基準のため `/usage` の公式%とは一致しません。週次リセットは
  カレンダー週（週初+7日）での近似です。`npx ccusage` 初回はパッケージ取得で遅くなります。
