#!/usr/bin/env python3
"""Telegram 通知モジュール"""
import requests

TIMEOUT = 10


def send_telegram(token, chat_id, message):
    """Telegram にメッセージを送信する。
    まず Markdown で送り、パース失敗(400)なら平文で再送する。
    LLM 要約やログ生出力には対になっていない * _ ` が混ざりうる
    (例: メトリクス名 node_memory_MemAvailable_bytes) ため、
    Markdown 失敗を理由に通知自体を落とさない。"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = message[:4096]   # Telegram の上限
    for parse_mode in ("Markdown", None):
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = requests.post(url, json=payload, timeout=TIMEOUT)
            if resp.status_code == 400 and parse_mode:
                print(f"Telegram 400 (Markdownパース失敗の可能性): {resp.text[:200]} → 平文で再送",
                      flush=True)
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                print(f"Telegram API エラー: {data}", flush=True)
                return False
            return True
        except Exception as e:
            # 例外メッセージには URL(=Bot トークン) が含まれるため伏せる
            print(f"Telegram 送信エラー: {str(e).replace(token, '***')}", flush=True)
            return False
    return False
