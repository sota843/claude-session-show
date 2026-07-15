# Claude Usage Tray (Win11)

Claude Code の **5時間セッション枠**と**週次枠**の使用量を、Windows 11 のタスクトレイに
**二重リングの円グラフアイコン**で常時表示する常駐アプリです。

- **外周リング** = 5時間ローリングセッション枠の使用率
- **内周リング** = 週次枠の使用率
- 色: 〜60% 緑 / 〜85% 黄 / 〜100% 赤
- アイコンにマウスを乗せると、各枠の使用% と次のリセット時刻を表示
- 右クリックメニュー: 今すぐ更新 / 自動起動 ON・OFF / 終了

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
> **Claude Code を起動しておく必要はありません。** `refreshToken` は更新のたびに延長されるため、
> トレイが定期的に動いていれば失効しません。ただし `refreshToken` 自体が期限切れ（長期間 PC 未起動など）
> になった場合のみ、一度 `claude` を起動して再認証が必要です。その間は `fallback_to_ccusage` が
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
| `user_agent` | oauth 用 UA。`null` で `claude --version` から自動検出 |
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

> `source: "oauth"` なら `budget_mode` / `plan` / `plans` は使われません（公式%をそのまま表示）。
> ccusage 推定を使う場合、`budget_mode: "auto"` は週次がピークに張り付くため `"fixed"` 推奨。

## ファイル構成
| ファイル | 役割 |
|---|---|
| `tray_app.py` | エントリ。pystray アイコン + ポーリングスレッド + メニュー |
| `usage.py` | ccusage を実行し 5時間枠・週次枠を集計（単体実行で数値確認可） |
| `gauge.py` | Pillow で二重リングアイコンを描画（単体実行でサンプル PNG 出力） |
| `config.py` / `config.json` | 設定の読み込みと既定値 |
| `autostart.py` | スタートアップ登録の ON/OFF |

## 動作確認
```powershell
python usage.py     # 5時間枠・週次枠の使用トークン/リセット時刻を表示
python gauge.py     # sample_30/70/95/unknown.png を出力（色分け確認）
python tray_app.py  # トレイ常駐
```

## 既知の限界
- `oauth` の数値は `/usage` と同一（公式）。ただしエンドポイントは非公開で、Anthropic 側の
  仕様変更で将来動かなくなる可能性があります。その場合は `source: "ccusage"` で推定運用へ。
- oauth エンドポイントはレート制限が厳しく、短間隔ポーリングや手動連打は 429 を招きます
  （アプリ側で 180s 下限＋手動更新の最小間隔を設けています）。
- `ccusage` 推定はトークン基準のため `/usage` の公式%とは一致しません。週次リセットは
  カレンダー週（週初+7日）での近似です。`npx ccusage` 初回はパッケージ取得で遅くなります。
