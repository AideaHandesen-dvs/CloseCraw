#!/usr/bin/env python3
"""定時ジョブの沈黙検知（TimerSkipped）プローバ ―― Phase 1。

「落ちた」ではなく「黙って走らなかった」を捕まえる。failed になった unit は既存の見張りが拾うが、
timer が止まっている・発火が来ない、という沈黙は failed にならず、誰も気づかない。

受動のみ（PLAN_deadman.md §2 IN①）: systemd が既に覚えている痕跡を読むだけで、ジョブには一切触らない。
  - systemctl list-timers --all --output=json  … unit 名・最後に発火した時刻(last)
  - systemctl show <timer>                     … 有効/起動状態・OnCalendar・間隔・ゆらぎ(RandomizedDelay)
  - systemd-analyze calendar --iterations=N    … OnCalendar の発火間隔（最大の間＝その timer の周期）

判定（timer ごと）:
  - 有効(enabled)なのに起動していない(inactive) → 「止まっている」
  - 周期 P が分かる timer で、最後の発火から P＋ゆらぎ＋精度＋猶予 を過ぎても次が来ない → 「発火が来ない」
      猶予 = max(TIMER_GRACE_MIN_S, P×TIMER_GRACE_RATIO)
  - 一度も発火していないのに、起動してから同じ締切を過ぎた → 「一度も動いていない」
  - 周期が無い timer（OnBootSec だけ等の一回きり）・無効(disabled)の timer・
    Condition*= が満たされず起動しなかった timer（作者の意図どおりの停止）は対象外。

出力:
  - textfile: closecraw_timer*.prom（node_exporter textfile collector にそのまま置ける）
  - state   : 前回どれが沈黙していたか（変化した時だけ鳴らすため）
  - --alert : 沈黙に入った/抜けた timer を 1 通にまとめて Telegram へ（変化時だけ＝オオカミ少年にしない）
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# --- 設定（liveness_prober と同じく環境変数で上書き可） ---
GRACE_MIN_S = float(os.environ.get("TIMER_GRACE_MIN_S", "120"))
GRACE_RATIO = float(os.environ.get("TIMER_GRACE_RATIO", "0.1"))
CAL_ITER = int(os.environ.get("TIMER_CAL_ITERATIONS", "16"))
# 見ない timer（カンマ区切りの glob）。試験用の一時 unit などを外す。
IGNORE = [p for p in os.environ.get("TIMER_IGNORE", "test-*,gatetest*").split(",") if p.strip()]
_here = os.path.dirname(os.path.abspath(__file__))
_deflog = os.path.join(os.path.dirname(_here), "logs")
TEXTFILE = os.environ.get("TIMER_TEXTFILE", os.path.join(_deflog, "closecraw_timer.prom"))
STATE_FILE = os.environ.get("TIMER_STATE_FILE", os.path.join(_deflog, "timer_state.json"))

_UNIT_S = {"us": 1e-6, "usec": 1e-6, "ms": 1e-3, "msec": 1e-3, "s": 1, "sec": 1,
           "min": 60, "m": 60, "h": 3600, "hr": 3600, "d": 86400, "w": 604800,
           "month": 2629800, "M": 2629800, "y": 31557600}


def parse_duration(text: str) -> float:
    """systemd の時間表記（"1min 30s" "20min" "0" "infinity"）を秒へ。読めなければ 0。"""
    text = (text or "").strip()
    if not text or text == "infinity":
        return 0.0
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)\s*([a-zA-Z]*)", text):
        total += float(num) * _UNIT_S.get(unit or "s", 0)
    return total


def _systemctl(scope: str, *args: str) -> str:
    cmd = ["systemctl"] + (["--user"] if scope == "user" else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout


def list_timers(scope: str) -> list[dict]:
    out = _systemctl(scope, "list-timers", "--all", "--output=json", "--no-pager")
    try:
        return json.loads(out or "[]")
    except json.JSONDecodeError:
        return []


def show_props(scope: str, units: list[str]) -> dict[str, dict]:
    if not units:
        return {}
    out = _systemctl(scope, "show", "--no-pager", "-p",
                     "Id,ActiveState,UnitFileState,TimersCalendar,TimersMonotonic,"
                     "RandomizedDelayUSec,AccuracyUSec,ActiveEnterTimestamp,ConditionResult",
                     "--timestamp=unix", *units)
    props: dict[str, dict] = {}
    cur: dict = {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur.get("Id"):
                props[cur["Id"]] = cur
            cur = {}
            continue
        k, _, v = line.partition("=")
        # TimersCalendar / TimersMonotonic は複数行になりうる（OnCalendar を複数書いた timer）
        cur.setdefault(k, [] if k.startswith("Timers") else v)
        if k.startswith("Timers"):
            cur[k].append(v)
    return props


def calendar_period(spec: str) -> float | None:
    """OnCalendar の発火時刻を N 回先まで出し、隣り合う間の最大値を周期とする（曜日指定など不等間隔にも効く）。"""
    out = subprocess.run(["systemd-analyze", "calendar", f"--iterations={CAL_ITER}", spec],
                         capture_output=True, text=True, timeout=30).stdout
    times = []
    for m in re.finditer(r"\(in UTC\):\s+\w+ (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC", out):
        times.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp())
    if len(times) < 2:
        return None
    return max(b - a for a, b in zip(times, times[1:]))


def timer_period(p: dict) -> float | None:
    """その timer が何秒おきに発火するはずか。一回きりの timer は None。"""
    periods = []
    for entry in p.get("TimersCalendar", []):
        m = re.search(r"OnCalendar=(.*?)\s;", entry)
        if m:
            per = calendar_period(m.group(1).strip())
            if per:
                periods.append(per)
    for entry in p.get("TimersMonotonic", []):
        # OnUnitActiveSec / OnUnitInactiveSec だけが繰り返し。OnBootSec 等は一回きり。
        m = re.search(r"OnUnit(?:Active|Inactive)USec=(.*?)\s;", entry)
        if m:
            per = parse_duration(m.group(1))
            if per > 0:
                periods.append(per)
    return max(periods) if periods else None


def evaluate(scope: str, now: float) -> list[dict]:
    rows = []
    timers = [t for t in list_timers(scope)
              if not any(fnmatch.fnmatch(t.get("unit", ""), pat.strip()) for pat in IGNORE)]
    props = show_props(scope, [t["unit"] for t in timers])
    for t in timers:
        unit = t["unit"]
        p = props.get(unit, {})
        last = (t.get("last") or 0) / 1e6 or None          # usec → 秒。一度も無ければ None
        row = {"scope": scope, "unit": unit, "last": last, "period": None,
               "deadline": None, "silent": False, "why": "健全"}
        enabled = p.get("UnitFileState") in ("enabled", "enabled-runtime", "static", "linked")
        if p.get("ActiveState") != "active":
            if p.get("ConditionResult") == "no":
                # Condition*= が満たされずに起動しなかった＝作者の意図どおりの停止（例: 片付ける物が無い）
                row["why"] = "条件不成立＝対象外"
            elif enabled:
                row.update(silent=True, why="有効なのに止まっている")
            else:
                row["why"] = "無効＝対象外"
            rows.append(row)
            continue
        period = timer_period(p)
        if period is None:
            row["why"] = "一回きり＝対象外"
            rows.append(row)
            continue
        slack = (parse_duration(p.get("RandomizedDelayUSec", "")) +
                 parse_duration(p.get("AccuracyUSec", "")))
        deadline = period + slack + max(GRACE_MIN_S, period * GRACE_RATIO)
        row.update(period=period, deadline=deadline)
        if last is None:
            since = float((p.get("ActiveEnterTimestamp") or "@0").lstrip("@") or 0)
            if since and now - since > deadline:
                row.update(silent=True, why="一度も動いていない")
        elif now - last > deadline:
            row.update(silent=True, why="発火が来ない")
        rows.append(row)
    return rows


def _fmt_age(sec: float | None) -> str:
    if sec is None:
        return "NA"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if sec >= size:
            return f"{sec / size:.1f}{unit}"
    return f"{sec:.0f}s"


def _prom(rows: list[dict], now: float) -> str:
    def g(name, help_, rows_vals):
        out = f"# HELP {name} {help_}\n# TYPE {name} gauge\n"
        return out + "".join(f'{name}{{scope="{r["scope"]}",unit="{r["unit"]}"}} {v}\n'
                             for r, v in rows_vals)
    watched = [r for r in rows if r["period"] is not None or r["silent"]]
    return (g("closecraw_timer_silent", "1 if the timer is silently not firing (stopped or overdue).",
              [(r, 1 if r["silent"] else 0) for r in watched])
            + g("closecraw_timer_last_trigger_seconds", "Unix time the timer last fired (0 = never).",
                [(r, f'{r["last"] or 0:.0f}') for r in watched])
            + g("closecraw_timer_deadline_seconds", "Seconds after the last fire before it counts as silent.",
                [(r, f'{r["deadline"]:.0f}') for r in watched if r["deadline"]])
            + "# HELP closecraw_timer_prober_run_seconds Unix time of the last prober run.\n"
              "# TYPE closecraw_timer_prober_run_seconds gauge\n"
              f"closecraw_timer_prober_run_seconds {now:.0f}\n")


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
    lines = ["🔴 CloseCraw deadman — 定時ジョブの沈黙" if entered else "🟢 CloseCraw deadman — 定時ジョブ復旧"]
    for r in entered:
        lines.append(f"・{r['unit']}（{r['scope']}）{r['why']}"
                     f" 最後の発火={_fmt_age(now - r['last']) + '前' if r['last'] else 'なし'}"
                     f" 周期={_fmt_age(r['period'])}")
    for key in left:
        lines.append(f"・復旧: {key}")
    lines.append(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="定時ジョブの沈黙検知プローバ (TimerSkipped)")
    ap.add_argument("--scope", choices=["user", "system", "both"], default="both")
    ap.add_argument("--alert", action="store_true",
                    help="沈黙に入った/抜けた時だけ Telegram へ送る。既定はドライラン表示")
    ap.add_argument("--simulate", metavar="UNIT",
                    help="指定 timer の最後の発火を周期の 3 倍前に偽装し、検知→通知を本物に触れず検証する")
    ap.add_argument("-v", "--verbose", action="store_true", help="全 timer の判定を表示")
    args = ap.parse_args()

    now = time.time()
    scopes = ["user", "system"] if args.scope == "both" else [args.scope]
    rows = [r for s in scopes for r in evaluate(s, now)]

    if args.simulate:
        for r in rows:
            if r["unit"] == args.simulate and r["period"]:
                r.update(last=now - 3 * r["period"], silent=True, why="発火が来ない（simulate）")

    state = {}
    if os.path.exists(STATE_FILE):
        try:
            state = json.load(open(STATE_FILE))
        except Exception:
            state = {}
    prev = set(state.get("silent", []))
    cur = {f'{r["scope"]}:{r["unit"]}' for r in rows if r["silent"]}
    entered = [r for r in rows if r["silent"] and f'{r["scope"]}:{r["unit"]}' not in prev]
    left = sorted(prev - cur)

    alerted = "変化なし"
    if entered or left:
        msg = build_message(entered, left, now)
        if not args.alert:
            alerted = "dry-run"
            print("---- 送られるメッセージ ----\n" + msg + "\n---------------------------")
        else:
            token = os.environ.get("TIMER_TG_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
            chat = (os.environ.get("TIMER_TG_CHAT") or os.environ.get("TELEGRAM_CHAT_ID")
                    or _first_chat(os.environ.get("TG_CLAUDE_ALLOWED_CHATS", "")))
            if not (token and chat):
                alerted = "no-creds"
                print("---- 送られるメッセージ ----\n" + msg + "\n---------------------------")
            else:
                sys.path.insert(0, _here)
                import notifier   # requests 依存＝送る時だけ import
                alerted = "sent" if notifier.send_telegram(token, chat, msg) else "send-failed"

    # 送れなかった時は状態を進めない＝次の回にもう一度送る。simulate は状態に残さない。
    if not args.simulate and alerted not in ("send-failed", "no-creds"):
        _atomic_write(STATE_FILE, json.dumps({"silent": sorted(cur), "last_run": now},
                                             ensure_ascii=False, indent=2))
    if not args.simulate:
        _atomic_write(TEXTFILE, _prom(rows, now))

    if args.verbose:
        for r in rows:
            print(f"  {r['scope']:6} {r['unit']:45} {r['why']:12} "
                  f"last={_fmt_age(now - r['last']) if r['last'] else 'なし':>7} "
                  f"period={_fmt_age(r['period']):>7} deadline={_fmt_age(r['deadline']):>7}")
    watched = sum(1 for r in rows if r["period"] is not None)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    print(f"{stamp} timers={len(rows)} watched={watched} silent={len(cur)} "
          f"entered={len(entered)} left={len(left)} alert={alerted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
