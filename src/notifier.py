#!/usr/bin/env python3
"""Telegram 通知モジュール"""
import requests

TIMEOUT = 10

def send_telegram(token, chat_id, message):
    """Telegram にメッセージを送信する（Markdown パース有効）"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message[:4096],   # Telegram の上限
        "parse_mode": "Markdown"
    }
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram API エラー: {data}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"Telegram 送信エラー: {e}", flush=True)
        return False
