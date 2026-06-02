# CloseCraw

![CloseCraw](assets/logo/logo.jpg)

> **一言で言うと**
> Prometheus が異常を検知したら、AI が状況を判断して SSH 経由で調査スクリプトを実行し、結果を要約して Telegram に通知する。**AI はコマンドを生成しない**（許可スクリプトを選ぶだけ）。

ホームラボ（Linux / OpenWrt / Windows 混在）向けの、LLM 補助つきアラート自動診断エージェント。
当初はあるホームラボの Windows サーバのメモリ/SSD リーク監視が目的で、現在は複数台の監視に拡張済み。

---

## データフロー

```
[各ノード node_exporter / windows_exporter]
            ↓ メトリクス収集
[Prometheus :9090]
            ↓ アラート発火（閾値超過）
[Alertmanager :9093]
            ↓ Webhook POST → /alert
[alert_handler.py :5001]  ← Flask
            ↓ キューに追加（即 200 を返す）
   ─── 5 分ごとにバックグラウンドスレッドがまとめて処理 ───
            ↓
   ① Prometheus API で直近 1h のメトリクス取得
   ② LLM が許可スクリプトを 1 つ選択（失敗時は OS 別デフォルト）
   ③ SSH で対象ホスト上にスクリプトを流し込んで実行
   ④ LLM が出力を SRE 風に 2〜4 文へ要約（失敗時は生出力を添付）
            ↓
   [Telegram Bot → 全アラートを 1 通にまとめて通知]
```

LLM は **ollama**（ローカル推論）と **Gemini API** を `LLM_PROVIDER` で切替可能。どちらが落ちても例外を投げず、フォールバックして通知だけは必ず出す設計。

---

## ディレクトリ構成

```
closecraw/
├── .env                 ← トークン / APIキー / パス（gitignore 対象）
├── system_status.sh     ← 全体ヘルスチェック（ワンショット）
├── src/
│   ├── alert_handler.py ← Flask Webhook サーバ (port 5001)・キュー・LLM 抽象層
│   └── notifier.py      ← Telegram 送信
├── scripts/
│   ├── host_loader.py                 ← インベントリ JSON を OS 種別に分類
│   ├── generate_prometheus_targets.py ← JSON から Prometheus static_configs を生成
│   ├── script_tester.sh
│   ├── linux_memory_pressure.sh       ┐
│   ├── linux_disk_inode.sh            │
│   ├── linux_journal_anomaly.sh       ├ 許可スクリプト（ホワイトリスト）
│   ├── win_process_delta.sh           │
│   ├── win_eventlog_anomaly.sh        │
│   └── win_disk_anomaly.sh            ┘
├── docs/
│   └── inventory.example.json         ← ホストインベントリのサンプル
├── assets/logo/         ← ロゴ
└── logs/                ← ログ出力先（gitignore 対象）
```

> Prometheus / Alertmanager / Grafana などの Docker スタックや Alertmanager 設定は
> 別ディレクトリで管理しており、このリポには含まれない。

---

## セットアップ

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask requests python-dotenv

# .env を作成し、下表の変数を埋める
touch .env
```

### `.env`

| 変数 | 説明 |
|------|------|
| `TELEGRAM_TOKEN` | Telegram Bot トークン（必須） |
| `TELEGRAM_CHAT_ID` | 通知先チャット ID（必須） |
| `LLM_PROVIDER` | `ollama` または `gemini`（デフォルト `ollama`） |
| `LLM_MODEL` | 使用モデル名（例 `gemini-2.5-flash`） |
| `GEMINI_API_KEY` | Gemini 利用時のみ必須 |
| `CLOSECRAW_SPEC_FILE` | ホストインベントリ JSON のパス（デフォルト `/opt/closecraw/inventory.json`） |
| `CLOSECRAW_SCRIPTS_DIR` | 許可スクリプトの配置ディレクトリ（デフォルト `/opt/closecraw/scripts`） |

> ホストインベントリの形式は `docs/inventory.example.json` を参照。
> ollama 利用時の接続先・モデルは `src/alert_handler.py` の `OLLAMA_URL` / `OLLAMA_MODEL`（env `OLLAMA_MODEL`）で設定。

---

## 起動

```bash
# 直接起動（開発用）
python src/alert_handler.py        # → 0.0.0.0:5001

# systemd ユーザーサービス（常駐運用）
systemctl --user status  alert-handler.service
systemctl --user restart alert-handler.service
journalctl --user -u alert-handler.service -n 50
```

> gunicorn 等で `__main__` を経由せず import される場合に備え、スケジューラスレッドは
> モジュール読み込み時にも起動するようになっている。

### エンドポイント

| パス | メソッド | 用途 |
|------|----------|------|
| `/alert` | POST | Alertmanager Webhook 受け口（キューに追加して即 200） |
| `/health` | GET | 死活確認（`queue_length` も返す） |

---

## AI の役割と制約

| やること | やらないこと |
|----------|--------------|
| 許可スクリプトから最適な 1 つを選ぶ | コマンドを自分で生成する |
| スクリプト出力を人間向けに要約する | SSH 先で自由にシェル操作する |
| JSON `{"script": "..."}` で名前を返す | ホワイトリスト外のスクリプトを呼ぶ |

---

## 許可スクリプト（ホワイトリスト）

| スクリプト | 対象 OS | 内容 |
|-----------|---------|------|
| `linux_memory_pressure.sh` | Linux | PSI / vmstat OOM / dmesg OOM |
| `linux_disk_inode.sh` | Linux | df / inode / 削除済み未解放ファイル |
| `linux_journal_anomaly.sh` | Linux | 直近のエラーログを頻度順集計 |
| `win_process_delta.sh` | Windows | 数秒間のワーキングセット増加 TOP10 |
| `win_eventlog_anomaly.sh` | Windows | イベントログのエラー種類別集計 |
| `win_disk_anomaly.sh` | Windows | 直近 24h で更新された大きいファイル |

各スクリプトは `script <hostname>` 形式で呼ぶと、内部で `ssh <hostname>` して対象機上で自身を実行する（`localhost` ならローカル実行）。

---

## ヘルスチェック

```bash
./system_status.sh    # Docker / Prometheus / Alertmanager / handler / ollama / SSH を一括確認
```

---

## トラブルシューティング

**通知が来ない**
1. `systemctl --user status alert-handler.service` が active か
2. `journalctl --user -u alert-handler.service -n 50` でエラー確認
3. `curl http://localhost:5001/health` でキュー長を確認（溜まったままなら処理スレッドが死んでいる）
4. `curl http://localhost:9090/api/v1/alerts` でアラートが発火しているか
5. Alertmanager の `repeat_interval` を確認（長すぎると来ない）

**通知がスパムになる**
1. Alertmanager の `repeat_interval` を確認（短すぎると即スパム）
2. アラートルールに instance フィルタが入っているか（特に OpenWrt の Load 系は全 Linux に誤爆しやすい）

**handler が起動しない**
```bash
pkill -f alert_handler.py
systemctl --user restart alert-handler.service
journalctl --user -u alert-handler.service -n 20
```

---

## 設計メモ

- アラートは即処理せず **5 分間キューに溜めてバッチ処理** → 通知を 1 通にまとめ、スパムと LLM 呼び出し回数を抑制。
- `alertname` → PromQL は `ALERT_TO_QUERY` で対応付け（未定義なら `up` をフォールバック取得）。
- LLM 選択・要約は両方とも失敗を許容（None 返却）し、選択は OS 別デフォルト、要約は生出力添付でフォールバック。
- 実行できるのは `WHITELIST` 内のスクリプトのみ（LLM が外の名前を返しても弾く）。
