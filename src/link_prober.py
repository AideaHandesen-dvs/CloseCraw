#!/usr/bin/env python3
"""つながりの沈黙検知（LinkStuckDown）プローバ ―― Phase 1。

「常駐は動いているのに、中身の線だけ切れたまま」を捕まえる。プロセスは active のままなので
failed の見張りにも定時ジョブの見張りにも引っかからない。

■ なぜ在る（2026-10-13）
  電話の箱（phonebox-bt）の HFP が 2026-10-11 20:38:43 に BT_DOWN になり、約31時間そのまま
  だった＝秘書が外の電話を受けられなかった。常駐は active・SMS の線は生きていた。気づいたのは
  板の棚卸しの途中（PLAN_deadman.md §7 の MTTD 実例）。

受動のみ（PLAN_deadman.md §2 IN①）: journald に残っている「つながった/切れた」の記録を読むだけで、
対象には一切触らない（つなぎ直しは対象側の仕事）。

判定（対象ごと）:
  - 最後に一致した記録が「切れた」で、その時刻から max_down_s を過ぎた → 「切れたまま」
  - 常駐自体が active でない → 「止まっている」（max_down_s を過ぎたら鳴らす・見え始めた時刻から数える）
  - 一致する記録が窓（lookback）の中に無い → 「不明」＝鳴らさない（数字は -1）

対象の一覧は JSON（LINK_TARGETS・既定 deploy/link_targets.json）:
  [{"name": "phonebox-hfp", "unit": "phonebox-bt.service", "scope": "user",
    "up": "EVENT BT_UP", "down": "EVENT BT_DOWN", "max_down_s": 600}]

出力:
  - textfile: closecraw_link.prom（node_exporter textfile collector にそのまま置ける）
  - state   : 前回どれが「切れたまま」だったか（変化した時だけ鳴らすため）
  - --alert : 切れたままに入った/抜けた対象を 1 通にまとめて Telegram へ
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
_deflog = os.path.join(_root, "logs")
TARGETS = os.environ.get("LINK_TARGETS", os.path.join(_root, "deploy", "link_targets.json"))
TEXTFILE = os.environ.get("LINK_TEXTFILE", os.path.join(_deflog, "closecraw_link.prom"))
STATE_FILE = os.environ.get("LINK_STATE_FILE", os.path.join(_deflog, "link_state.json"))
LOOKBACK = os.environ.get("LINK_LOOKBACK", "30d")


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""


def unit_active(t: dict) -> bool:
    scope = ["--user"] if t.get("scope", "user") == "user" else []
    return _run(["systemctl", *scope, "is-active", t["unit"]]).strip() == "active"


def last_event(t: dict, until: str | None = None):
    """窓の中で up/down に一致した最後の記録。返り＝("up"|"down"|None, unix秒 or None)。
    until＝過去の時点で再生する時だけ（journalctl --until）。"""
    scope = ["--user"] if t.get("scope", "user") == "user" else []
    window = ["--since", f"-{LOOKBACK}"] if not until else ["--until", until]
    out = _run(["journalctl", *scope, "-u", t["unit"], *window,
                "--no-pager", "-o", "json", "--output-fields=MESSAGE,__REALTIME_TIMESTAMP",
                "-g", f'{t["up"]}|{t["down"]}'])
    up, down = re.compile(t["up"]), re.compile(t["down"])
    state, ts = None, None
    for line in out.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        msg = j.get("MESSAGE")
        if isinstance(msg, list):          # journald はバイト列を数値の配列で返すことがある
            msg = bytes(msg).decode("utf-8", "replace")
        msg = msg or ""
        when = int(j.get("__REALTIME_TIMESTAMP", 0)) / 1e6
        if down.search(msg):
            state, ts = "down", when
        elif up.search(msg):
            state, ts = "up", when
    return state, ts


def evaluate(targets: list[dict], prev: dict, now: float, until: str | None = None) -> list[dict]:
    rows = []
    for t in targets:
        max_down = float(t.get("max_down_s", 600))
        active = unit_active(t) if not until else True   # 再生では常駐は動いていた前提（過去の is-active は引けない）
        state, since = last_event(t, until)
        key = t["name"]
        if not active:
            # 止まった時刻は journal から取らない（再起動の繰り返しで紛れる）。見え始めた時刻から数える。
            since = prev.get("inactive_since", {}).get(key, now)
            state, why = "down", "常駐が止まっている"
        elif state is None:
            why = "不明（窓の中に記録なし）"
        elif state == "down":
            why = "切れている"
        else:
            why = "つながっている"
        down_for = (now - since) if (state == "down" and since) else 0.0
        rows.append({"name": key, "unit": t["unit"], "scope": t.get("scope", "user"),
                     "active": active, "state": state, "since": since, "why": why,
                     "down_for": down_for, "max_down": max_down,
                     "stuck": state == "down" and down_for >= max_down})
    return rows


def _fmt_age(s: float) -> str:
    s = int(max(s, 0))
    if s >= 86400:
        return f"{s // 86400}日{s % 86400 // 3600}時間"
    if s >= 3600:
        return f"{s // 3600}時間{s % 3600 // 60}分"
    if s >= 60:
        return f"{s // 60}分"
    return f"{s}秒"


def _prom(rows: list[dict], now: float) -> str:
    def g(name, help_, vals):
        out = [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
        out += [f'{name}{{name="{r["name"]}",unit="{r["unit"]}",scope="{r["scope"]}"}} {v}' for r, v in vals]
        return "\n".join(out) + "\n"
    up = lambda r: 1 if r["state"] == "up" else (0 if r["state"] == "down" else -1)
    return (g("closecraw_link_up", "1 = link up, 0 = down, -1 = unknown (no matching event in the lookback window).",
              [(r, up(r)) for r in rows])
            + g("closecraw_link_state_since_seconds", "Unix time the current up/down state began (0 = unknown).",
                [(r, f'{r["since"] or 0:.0f}') for r in rows])
            + g("closecraw_link_stuck_down", "1 if the link has stayed down longer than max_down_s.",
                [(r, 1 if r["stuck"] else 0) for r in rows])
            + g("closecraw_link_max_down_seconds", "Seconds a link may stay down before it counts as stuck.",
                [(r, f'{r["max_down"]:.0f}') for r in rows])
            + "# HELP closecraw_link_prober_run_seconds Unix time of the last prober run.\n"
              "# TYPE closecraw_link_prober_run_seconds gauge\n"
              f"closecraw_link_prober_run_seconds {now:.0f}\n")


def _atomic_write(path: str, data: str):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_")
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _first_chat(s: str) -> str:
    for tok in s.replace(",", " ").split():
        return tok.strip()
    return ""


def build_message(entered: list[dict], left: list[str], now: float) -> str:
    lines = ["🔴 CloseCraw deadman — つながりが切れたまま" if entered else "🟢 CloseCraw deadman — つながり復旧"]
    for r in entered:
        lines.append(f"・{r['name']}（{r['unit']}）{r['why']} {_fmt_age(r['down_for'])}"
                     f"（{_fmt_age(r['max_down'])}を超えた）")
    for key in left:
        lines.append(f"・復旧: {key}")
    lines.append(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="つながりの沈黙検知プローバ (LinkStuckDown)")
    ap.add_argument("--alert", action="store_true",
                    help="切れたままに入った/抜けた時だけ Telegram へ送る。既定はドライラン表示")
    ap.add_argument("--simulate", metavar="NAME",
                    help="指定対象を max_down の 2 倍前から切れていたことにして、検知→通知を本物に触れず検証する")
    ap.add_argument("--at", metavar="'YYYY-MM-DD HH:MM:SS'",
                    help="過去の時点で判定を再生する（journal をその時刻までで切る・状態も数字も書かない・鳴らさない）")
    ap.add_argument("-v", "--verbose", action="store_true", help="全対象の判定を表示")
    args = ap.parse_args()

    now = time.mktime(time.strptime(args.at, "%Y-%m-%d %H:%M:%S")) if args.at else time.time()
    targets = json.load(open(TARGETS))
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            state = json.load(open(STATE_FILE))
        except Exception:
            state = {}
    rows = evaluate(targets, state, now, args.at)

    if args.simulate:
        for r in rows:
            if r["name"] == args.simulate:
                r.update(state="down", since=now - 2 * r["max_down"], down_for=2 * r["max_down"],
                         stuck=True, why="切れている（simulate）")

    prev = set(state.get("stuck", []))
    cur = {r["name"] for r in rows if r["stuck"]}
    entered = [r for r in rows if r["stuck"] and r["name"] not in prev]
    left = sorted(prev - cur)

    alerted = "変化なし"
    if entered or left:
        msg = build_message(entered, left, now)
        if not args.alert or args.at:
            alerted = "dry-run"
            print("---- 送られるメッセージ ----\n" + msg + "\n---------------------------")
        else:
            token = os.environ.get("LINK_TG_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
            chat = (os.environ.get("LINK_TG_CHAT") or os.environ.get("TELEGRAM_CHAT_ID")
                    or _first_chat(os.environ.get("TG_CLAUDE_ALLOWED_CHATS", "")))
            if not (token and chat):
                alerted = "no-creds"
                print("---- 送られるメッセージ ----\n" + msg + "\n---------------------------")
            else:
                sys.path.insert(0, _here)
                import notifier   # requests 依存＝送る時だけ import
                alerted = "sent" if notifier.send_telegram(token, chat, msg) else "send-failed"

    # 送れなかった時は状態を進めない＝次の回にもう一度送る。simulate は状態に残さない。
    if not (args.simulate or args.at) and alerted not in ("send-failed", "no-creds"):
        inactive = {r["name"]: r["since"] for r in rows if not r["active"]}
        _atomic_write(STATE_FILE, json.dumps({"stuck": sorted(cur), "inactive_since": inactive,
                                              "last_run": now}, ensure_ascii=False, indent=2))
    if not (args.simulate or args.at):
        _atomic_write(TEXTFILE, _prom(rows, now))

    if args.verbose:
        for r in rows:
            print(f"  {r['name']:20} {r['unit']:28} {r['why']:20} "
                  f"since={time.strftime('%m-%d %H:%M:%S', time.localtime(r['since'])) if r['since'] else 'なし':>14} "
                  f"down_for={_fmt_age(r['down_for']):>8} max={_fmt_age(r['max_down'])}")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    print(f"{stamp} links={len(rows)} stuck={len(cur)} entered={len(entered)} left={len(left)} alert={alerted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
