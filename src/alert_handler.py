#!/usr/bin/env python3
"""
Alert Handler (キューイング＆フォールバック対応版)
Alertmanager Webhook → キューに追加 → 5分ごとにまとめて診断・通知

スクリプト選択は LLM ではなく対応表 (ALERT_TO_SCRIPT) の表引き。
LLM が担うのは「診断結果の要約」だけ。詳細は docs/DESIGN.md を参照。
"""
import json
import re
import subprocess
import sys
import time
import threading
from collections import deque
from pathlib import Path
from notifier import send_telegram

import requests
from flask import Flask, request, jsonify

import os
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)


# ===== 設定 =====
PROMETHEUS_API = "http://localhost:9090/api/v1/query_range"
OLLAMA_URL = "http://server1.example:11434/api/generate"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
SPEC_FILE = os.getenv("CLOSECRAW_SPEC_FILE", "/opt/closecraw/inventory.json")
SCRIPTS_DIR = Path(os.getenv("CLOSECRAW_SCRIPTS_DIR", "/opt/closecraw/scripts"))
TIMEOUT_LLM = 60
TIMEOUT_SCRIPT = 120
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PROCESS_INTERVAL = 300  # 5分ごとにキュー処理

# Gemini 無料枠(RPM)対策。アラートストーム時の連投を抑える。
# 呼び出し間隔の下限(秒)。
GEMINI_MIN_INTERVAL = float(os.getenv("GEMINI_MIN_INTERVAL", "7"))
# 1回の掃き出しで処理する最大アラート数。超過分は次サイクルへ繰り越す(捨てない)
MAX_ALERTS_PER_FLUSH = int(os.getenv("MAX_ALERTS_PER_FLUSH", "20"))

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
# 既定は flash-lite。無印 flash は無料枠(1日あたりのリクエスト数=RPD)が小さいことがあり、
# 2〜4文の要約には flash-lite で十分。実枠は 429 応答の body / プロバイダのダッシュボードで確認する。
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash-lite")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{LLM_MODEL}:generateContent"
)

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set in .env file")

# ホワイトリスト
WHITELIST = [
    "win_process_delta.sh",
    "win_eventlog_anomaly.sh",
    "win_disk_anomaly.sh",
    "linux_memory_pressure.sh",
    "linux_disk_inode.sh",
    "linux_journal_anomaly.sh",
]

# alertname → PromQL マッピング
ALERT_TO_QUERY = {
    "Load": "node_load1",
    "Memory": "node_memory_MemAvailable_bytes",
    "Disk": "node_filesystem_avail_bytes",
    "CPU": "rate(node_cpu_seconds_total{mode='idle'}[5m])",
}

# alertname × OS → 診断スクリプト マッピング。
# 発火しうる alertname は自分で書いた Alertmanager ルールで閉じている（未知のアラートは来ない）ため、
# LLM に選ばせず表引きで決定する（LLM クォータ消費ゼロ・決定的・即時）。
# キーは alertname の部分一致（大文字小文字無視）。該当なしはログ全般調査に落とす。
ALERT_TO_SCRIPT = {
    "disk":   {"linux": "linux_disk_inode.sh",      "windows": "win_disk_anomaly.sh"},
    "memory": {"linux": "linux_memory_pressure.sh", "windows": "win_process_delta.sh"},
    "cpu":    {"linux": "linux_memory_pressure.sh", "windows": "win_process_delta.sh"},
    "load":   {"linux": "linux_memory_pressure.sh", "windows": "win_process_delta.sh"},
}
DEFAULT_SCRIPT = {
    "linux": "linux_journal_anomaly.sh",
    "windows": "win_eventlog_anomaly.sh",
}


def select_script(alertname, os_type):
    """alertname と OS 種別から診断スクリプトを表引きで決定する"""
    name = alertname.lower()
    for key, by_os in ALERT_TO_SCRIPT.items():
        if key in name:
            return by_os[os_type]
    return DEFAULT_SCRIPT[os_type]

# ===== キューイング機構 =====
alert_queue = deque()
queue_lock = threading.Lock()


def load_spec():
    with open(SPEC_FILE, "r") as f:
        data = json.load(f)
    # インベントリ JSON はトップレベルが {"hosts": [...]} の辞書想定。
    # detect_os はホストのリストを期待するため hosts を取り出す。
    # 旧形式（トップレベルがリスト）にも後方互換で対応。
    if isinstance(data, dict):
        return data.get("hosts", [])
    return data


def detect_os(hostname, spec):
    for host in spec:
        if host["hostname"] == hostname:
            os_name = host.get("os", "")
            if "Windows" in os_name:
                return "windows"
            elif "OpenWrt" in os_name or "Debian" in os_name:
                return "linux"
    return "linux"


def parse_alert(alert):
    alertname = alert["labels"]["alertname"]
    instance = alert["labels"]["instance"]
    severity = alert["labels"].get("severity", "unknown")
    summary = alert["annotations"].get("summary", "")
    hostname = instance.split(":")[0]
    return {
        "alertname": alertname,
        "hostname": hostname,
        "instance": instance,
        "severity": severity,
        "summary": summary,
    }


def prometheus_query(alertname, instance):
    query = None
    for key, q in ALERT_TO_QUERY.items():
        if key in alertname:
            query = f'{q}{{instance="{instance}"}}'
            break
    if not query:
        query = f'up{{instance="{instance}"}}'

    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        end = now.strftime('%Y-%m-%dT%H:%M:%SZ')
        resp = requests.get(PROMETHEUS_API, params={
            "query": query,
            "start": start,
            "end": end,
            "step": "60s"
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if not results:
            return "直近1時間のメトリクスがありません。"
        lines = []
        for r in results:
            metric = r.get("metric", {})
            values = r.get("values", [])
            lines.append(f"metric: {json.dumps(metric)}")
            if values:
                lines.append(f"  first: {values[0]}")
                lines.append(f"  last:  {values[-1]}")
        return "\n".join(lines)
    except Exception as e:
        return f"Prometheus API エラー: {str(e)}"


# ===== LLM抽象層 =====

def _call_ollama(prompt, temperature=0.1):
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature}
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT_LLM)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as e:
        print(f"[ERROR] ollama 呼び出し失敗: {e}", file=sys.stderr)
        return None


_gemini_throttle_lock = threading.Lock()
_gemini_last_call = [0.0]


def _gemini_throttle():
    """無料枠の RPM 超過を防ぐため、Gemini 呼び出し間隔の下限を強制する。"""
    with _gemini_throttle_lock:
        wait = GEMINI_MIN_INTERVAL - (time.time() - _gemini_last_call[0])
        if wait > 0:
            print(f"[INFO] gemini レート制御: {wait:.1f}s 待機", file=sys.stderr)
            time.sleep(wait)
        _gemini_last_call[0] = time.time()


def _parse_retry_delay(body):
    """429 ボディの RetryInfo (例 '"retryDelay": "17s"') から待ち秒数を取り出す。
    無ければ None。上限 20s に丸める。"""
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s?"', body)
    return min(float(m.group(1)), 20) if m else None


def _call_gemini(prompt, temperature=0.1, retries=3):
    if not GEMINI_API_KEY:
        print("[ERROR] GEMINI_API_KEY が未設定", file=sys.stderr)
        return None
    _gemini_throttle()
    # Gemini は過負荷時に断続的に 503/429 を返す。一時エラーはリトライ。
    # ただし 429 のうち日次枠(RPD)超過は翌日まで回復しないため、リトライせず即諦める。
    # リトライ自体もクォータを消費する。
    for attempt in range(retries):
        try:
            # キーはヘッダで渡す。クエリパラメータだと requests の例外メッセージ
            # (URL込み)経由でログにキーが漏れる。
            resp = requests.post(
                GEMINI_URL,
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature},
                },
                timeout=TIMEOUT_LLM,
            )
            # 認証・リクエスト不正系(400/401/403)はリトライしても無駄なので即諦める。
            if resp.status_code in (400, 401, 403):
                if resp.status_code == 400 and "API_KEY_INVALID" in resp.text:
                    print("[ERROR] gemini API キーが無効。.env の GEMINI_API_KEY を確認", file=sys.stderr)
                else:
                    print(f"[ERROR] gemini {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                return None
            # 日次枠超過 (quotaId に PerDay) は待っても無駄 → リトライせずフォールバック
            if resp.status_code == 429 and "PerDay" in resp.text:
                print("[ERROR] gemini 日次無料枠(RPD)超過 → リトライせず即フォールバック: "
                      + " ".join(resp.text[:200].split()), file=sys.stderr)
                return None
            if resp.status_code in (429, 503):
                if attempt < retries - 1:
                    # RPM 超過など一時的なもの。RetryInfo があればその秒数、無ければ指数バックオフ
                    wait = _parse_retry_delay(resp.text) or 2 ** attempt
                    print(f"[WARN] gemini {resp.status_code}, {wait:.0f}s後リトライ ({attempt+1}/{retries})", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"[ERROR] gemini {resp.status_code} リトライ上限到達: "
                      + " ".join(resp.text[:200].split()), file=sys.stderr)
                return None
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"[WARN] gemini 呼び出し失敗 ({e}), {wait}s後リトライ ({attempt+1}/{retries})", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"[ERROR] gemini 呼び出し失敗: {e}", file=sys.stderr)
            return None


# LLM呼び出し全体のハード上限(秒)。requests の timeout は DNS 解決などを
# カバーせず無限ハングしうる。正常時の最悪ケース(リトライ+RetryInfo待ち+スロットル)を
# 見込んで TIMEOUT_LLM の 4 倍を上限にする。
TIMEOUT_LLM_HARD = TIMEOUT_LLM * 4


def call_llm(prompt, temperature=0.1):
    """
    LLMを呼び出し、応答テキストを返す。
    失敗時は None を返す（例外を投げない）。
    LLM_PROVIDER で ollama / gemini を切り替える。
    呼び出しがハングしても TIMEOUT_LLM_HARD で見切って None を返し、
    スケジューラ全体が止まるのを防ぐ。
    """
    fn = _call_gemini if LLM_PROVIDER == "gemini" else _call_ollama
    result = [None]

    def _target():
        result[0] = fn(prompt, temperature)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(TIMEOUT_LLM_HARD)
    if t.is_alive():
        print(f"[ERROR] LLM呼び出しが{TIMEOUT_LLM_HARD}s以内に完了せずハング → 見切ってフォールバック",
              file=sys.stderr)
        return None
    return result[0]


def ask_llm_summarize(alert_info, script_output):
    """診断結果を要約。失敗時は None"""
    prompt = f"""# 役割
シニアSREとして診断結果を要約せよ。

# ルール
- 2〜4文で出力する
- 敬語は禁止
- 最も重要なプロセスのみ言及する
- 軽微な変動は「わずか」と表現する
- 推測しない
- 最後の文は必ず次のいずれかで終える
  - アクション不要。
  - 要監視。
  - 要調査。

# 必ず含める項目
1. 最も増加したプロセス名
2. 増加量
3. 実害の有無
4. 結論

#診断結果
元アラート: {alert_info['alertname']} (host: {alert_info['hostname']})
severity: {alert_info['severity']}
summary: {alert_info['summary']}

診断結果:
{script_output[:3000]}
"""

    print("=== SUMMARIZE PROMPT ===", flush=True)
    print(prompt[:4000], flush=True)
    print("=== END PROMPT ===", flush=True)

    return call_llm(prompt, temperature=0.3)


def run_script(script_name, hostname):
    """許可スクリプトを SSH 実行"""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        return f"エラー: スクリプトが見つかりません ({script_path})"
    try:
        result = subprocess.run(
            [str(script_path), hostname],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SCRIPT
        )
        if result.returncode != 0:
            return f"スクリプト実行エラー (exit={result.returncode}):\n{result.stderr[:1000]}"
        return result.stdout
    except subprocess.TimeoutExpired:
        return "エラー: スクリプト実行がタイムアウトしました。"
    except Exception as e:
        return f"スクリプト実行例外: {str(e)}"


def process_single_alert(info, spec):
    """単一アラートを処理し、通知メッセージを返す。LLM失敗時はフォールバック"""
    hostname = info["hostname"]
    os_type = detect_os(hostname, spec)

    print(f"\n処理中: {info['alertname']} on {hostname}")

    # Prometheus メトリクス
    metrics = prometheus_query(info["alertname"], info["instance"])

    # スクリプト選択（表引き。LLM は使わない）
    selected = select_script(info["alertname"], os_type)
    print(f"選択スクリプト: {selected}")

    # スクリプト実行
    script_output = run_script(selected, hostname)
    print(f"スクリプト出力先頭500文字: {script_output[:500]}")

    # LLM 要約
    summary = ask_llm_summarize(info, script_output)

    # 要約失敗時のフォールバック通知
    if summary is None:
        summary = f"（AI要約に失敗しました。スクリプト生出力を以下に示します）\n\n```\n{script_output[:1500]}\n```"

    # 通知メッセージ
    message = f"*【診断完了】{hostname}: {info['alertname']}*\n\n{summary}"
    return message


def process_queue():
    """キューに溜まった全アラートを処理し、まとめて1通のTelegram通知を送る"""
    with queue_lock:
        if not alert_queue:
            return
        n = min(len(alert_queue), MAX_ALERTS_PER_FLUSH)
        alerts = [alert_queue.popleft() for _ in range(n)]
        remaining = len(alert_queue)

    if remaining:
        print(f"[WARN] アラート急増: {len(alerts)}件を処理、残り{remaining}件は次サイクルに繰り越し",
              file=sys.stderr)

    print(f"\n=== キュー処理開始: {len(alerts)}件 ===")
    spec = load_spec()
    messages = []
    for alert in alerts:
        try:
            info = parse_alert(alert)
            msg = process_single_alert(info, spec)
            messages.append(msg)
        except Exception as e:
            print(f"アラート処理エラー: {e}", file=sys.stderr)
            messages.append(f"⚠ アラート処理中にエラー: {e}")

    # 全結果を1通にまとめて送信
    combined = "\n\n---\n\n".join(messages)
    print(f"DEBUG: 送信メッセージ長: {len(combined)} 文字")
    print(f"DEBUG: メッセージ先頭300文字: {combined[:300]}")

    success = send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, combined)
    if success:
        print("Telegram 送信成功（まとめ通知）")
    else:
        print("Telegram 送信失敗", file=sys.stderr)
    print(f"=== キュー処理完了 ===\n")


def scheduler_loop():
    """定期的にキューを処理するバックグラウンドスレッド"""
    while True:
        time.sleep(PROCESS_INTERVAL)
        process_queue()


threading.Thread(target=scheduler_loop, daemon=True).start()

# ===== Flask エンドポイント =====

@app.route("/alert", methods=["POST"])
def handle_alert():
    """Alertmanager Webhook → キューに追加するだけ"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "invalid JSON"}), 400

        alerts = data.get("alerts", [])
        with queue_lock:
            for alert in alerts:
                alert_queue.append(alert)

        print(f"キュー追加: {len(alerts)}件 (現在のキュー長: {len(alert_queue)})")
        return jsonify({"status": "queued", "count": len(alerts)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "queue_length": len(alert_queue)})


if __name__ == "__main__":
    # バックグラウンドスレッド起動
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    print(f"alert_handler 起動中 (port 5001, キューイングモード, LLM={LLM_PROVIDER}/{LLM_MODEL})...")
    app.run(host="0.0.0.0", port=5001, debug=False)
