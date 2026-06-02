#!/usr/bin/env python3
"""
Phase 3: Alert Handler (キューイング＆フォールバック対応版)
Alertmanager Webhook → キューに追加 → 5分ごとにまとめて診断・通知
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

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")
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

# ===== キューイング機構 =====
alert_queue = deque()
queue_lock = threading.Lock()


def load_spec():
    with open(SPEC_FILE, "r") as f:
        return json.load(f)


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


def _call_gemini(prompt, temperature=0.1):
    if not GEMINI_API_KEY:
        print("[ERROR] GEMINI_API_KEY が未設定", file=sys.stderr)
        return None
    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature},
            },
            timeout=TIMEOUT_LLM,
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"[ERROR] gemini 呼び出し失敗: {e}", file=sys.stderr)
        return None


def call_llm(prompt, temperature=0.1):
    """
    LLMを呼び出し、応答テキストを返す。
    失敗時は None を返す（例外を投げない）。
    LLM_PROVIDER で ollama / gemini を切り替える。
    """
    if LLM_PROVIDER == "gemini":
        return _call_gemini(prompt, temperature)
    return _call_ollama(prompt, temperature)


def ask_llm_select(prompt):
    """LLM にスクリプト選択を依頼。失敗時は None"""
    response = call_llm(prompt, temperature=0.1)
    if response is None:
        return None
    # JSON {"script": "xxx.sh"} を抽出
    match = re.search(r'"script"\s*:\s*"([^"]+)"', response)
    if match:
        return match.group(1)
    for line in response.split("\n"):
        line = line.strip()
        for s in WHITELIST:
            if s in line:
                return s
    return response.strip()


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

    # LLM スクリプト選択
    whitelist_str = ", ".join(WHITELIST)
    select_prompt = f"""以下のアラートが発生した。許可スクリプト一覧から最適なものを1つ選び、JSONで返せ。
アラート: {info['alertname']}
インスタンス: {info['instance']}
OS種別: {os_type}
severity: {info['severity']}
summary: {info['summary']}

許可スクリプト: {whitelist_str}

回答形式: {{"script": "スクリプト名.sh"}}
"""
    selected = ask_llm_select(select_prompt)
    if selected is None or selected not in WHITELIST:
        print(f"スクリプト選択失敗 or 不正: {selected} → フォールバック")
        if os_type == "windows":
            selected = "win_process_delta.sh"
        else:
            selected = "linux_memory_pressure.sh"

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
        alerts = list(alert_queue)
        alert_queue.clear()

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
