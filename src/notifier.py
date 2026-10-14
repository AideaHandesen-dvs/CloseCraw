#!/usr/bin/env python3
"""通知モジュール（Telegram / Slack / メール）

送り先は CLOSECRAW_NOTIFY（カンマ区切り・既定 "telegram"）で選ぶ。
例: CLOSECRAW_NOTIFY=slack,email
  - telegram : TELEGRAM_TOKEN + TELEGRAM_CHAT_ID
  - slack    : SLACK_WEBHOOK_URL（Incoming Webhook）
  - email    : SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / SMTP_TO
               SMTP_SECURITY = starttls（既定）/ ssl / none

日本の会社は Telegram を使っていないことが多い＝売り物にするなら Slack とメールが要る。
send_telegram は既存のプローバ（liveness / timer / link）が直接呼んでいるので形を変えない。
"""
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import urlparse

import requests

TIMEOUT = 10
CHANNELS_ENV = "CLOSECRAW_NOTIFY"
KNOWN_CHANNELS = ("telegram", "slack", "email")


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


def send_slack(webhook_url, message):
    """Slack の Incoming Webhook に送る。
    本文の *太字* は Slack の mrkdwn でもそのまま太字になる（Telegram と同じ書き方）。
    Webhook URL そのものが鍵なので、例外メッセージには出さない。"""
    text = message[:39000]   # Slack の text 上限 40,000 文字に余裕を持たせる
    try:
        resp = requests.post(webhook_url, json={"text": text}, timeout=TIMEOUT)
        if resp.status_code != 200:
            print(f"Slack 送信エラー: HTTP {resp.status_code} {resp.text[:200]}", flush=True)
            return False
        return True
    except Exception as e:
        # 例外メッセージには URL かパス部分（どちらも鍵）が出うる＝両方伏せる
        err = str(e).replace(webhook_url, "***")
        path = urlparse(webhook_url).path
        if path and path != "/":
            err = err.replace(path, "/***")
        print(f"Slack 送信エラー: {err}", flush=True)
        return False


def send_email(message, *, host, port, sender, recipients, user=None, password=None,
               security="starttls", subject=None):
    """SMTP でメールを送る。件名は省略時に本文の1行目（* を除く）から作る。"""
    if subject is None:
        first = message.strip().splitlines()[0] if message.strip() else "CloseCraw"
        subject = first.replace("*", "").strip()[:120] or "CloseCraw"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(message)
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
        with smtp:
            if security == "starttls":
                smtp.starttls()
            if user:
                smtp.login(user, password or "")
            smtp.send_message(msg)
        return True
    except Exception as e:
        err = str(e)
        if password:
            err = err.replace(password, "***")
        print(f"メール送信エラー: {err}", flush=True)
        return False


def configured_channels(env=None):
    """CLOSECRAW_NOTIFY から送り先の一覧を返す。知らない名前は捨てずにエラーにする
    （綴り違いで通知が黙って消えるのが一番怖い）。"""
    env = os.environ if env is None else env
    raw = env.get(CHANNELS_ENV, "telegram")
    channels = [c.strip().lower() for c in raw.split(",") if c.strip()]
    unknown = [c for c in channels if c not in KNOWN_CHANNELS]
    if unknown:
        raise RuntimeError(f"{CHANNELS_ENV} に知らない送り先: {unknown}（使えるのは {KNOWN_CHANNELS}）")
    if not channels:
        raise RuntimeError(f"{CHANNELS_ENV} が空")
    return channels


def missing_settings(env=None):
    """選んだ送り先に足りない設定の名前を返す（起動時の点検用）。"""
    env = os.environ if env is None else env
    need = {
        "telegram": ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"],
        "slack": ["SLACK_WEBHOOK_URL"],
        "email": ["SMTP_HOST", "SMTP_FROM", "SMTP_TO"],
    }
    missing = []
    for ch in configured_channels(env):
        missing += [k for k in need[ch] if not env.get(k)]
    return missing


def notify(message, env=None):
    """選んだ全ての送り先に送る。1つでも届けば True。
    1つの送り先が落ちても他は送る（通知は必ず出す＝DESIGN.md の方針）。"""
    env = os.environ if env is None else env
    results = {}
    for ch in configured_channels(env):
        if ch == "telegram":
            results[ch] = send_telegram(env["TELEGRAM_TOKEN"], env["TELEGRAM_CHAT_ID"], message)
        elif ch == "slack":
            results[ch] = send_slack(env["SLACK_WEBHOOK_URL"], message)
        elif ch == "email":
            results[ch] = send_email(
                message,
                host=env["SMTP_HOST"],
                port=int(env.get("SMTP_PORT", "587")),
                sender=env["SMTP_FROM"],
                recipients=[r.strip() for r in env["SMTP_TO"].split(",") if r.strip()],
                user=env.get("SMTP_USER"),
                password=env.get("SMTP_PASSWORD"),
                security=env.get("SMTP_SECURITY", "starttls").lower(),
            )
        print(f"通知 {ch}: {'成功' if results[ch] else '失敗'}", flush=True)
    return any(results.values())
