#!/usr/bin/env python3
"""沈黙検知（deadman / liveness）プローバ ―― Phase 0。

CloseCraw の欠けた片割れ＝「出るべき信号が出ていない＝沈黙」を受動で捕まえる。
本ファイルは MVP の一次対象 `broadcast`（無人配信枠）だけを見る。

生存の証（2026-08-21 実測で確定・PLAN_deadman.md §4/§10）:
  1) `:8100/api/status` を受動 GET  … 配信制御サーバの自己申告
       - streaming        : 今この時間、放送しているはずか（＝ゲート）
       - obs_connected    : OBS と繋がっているか
       - relay_legs.*.up  : YouTube/Twitch に実際に届いているか（外向き）
  2) live 録画 `.mkv` の mtime 鮮度  … 制御が "OK" でもエンコーダが凍結していないか（クロスチェック）

判定（deny-by-default だが誤報を避ける向き付け）:
  - streaming=false（off-air）        → 対象外。沈黙ではない＝last_ok を now に進める（鳴らさない）。
  - streaming=true かつ 健全          → 生存。last_ok=now。
  - streaming=true かつ 壊れ          → last_ok を進めない＝時間が経つと staleness ルールが発火する。
  - go-live 猶予（LIVENESS_GOLIVE_GRACE_S 内・streaming が false→true に転じた直後）で、
    欠けが「mkv 鮮度」だけ（OBS/leg は健全）→ 暖機中＝録画が新鮮化する数十秒を沈黙としない。
    ＝制御盤は go-live で即 streaming=true を返すのに録画 .mkv が新鮮に映るまで約1分のレースがあり、
      その1周期だけ「live・沈黙=mkv不在」を誤射していた（実測 2026-08-25 23:00 / 08-26 22:00 枠頭）。
  - :8100 に届かない                  → 制御プレーンが応答しない。mkv 鮮度だけで best-effort 判定し degraded を立てる。

出力（Phase 0 は自己完結・Prometheus 配線は step3）:
  - textfile: `closecraw_liveness*.prom`（node_exporter textfile collector にそのまま置ける形式）
  - state   : last_ok を跨ぎで保つ小さな JSON（健全な時だけ前進）
  - ログ    : 1 行の人間可読な判定

このプローバ自身も timer で定期実行される想定。プローバが死ねば last_ok も古びる＝
自分の沈黙もいずれ staleness で露見する（＝deadman の deadman の弱い版）。
prober_run_seconds を別に出して「番犬は生きてるが放送が壊れた」と「番犬が死んだ」を後で切り分ける。
"""
from __future__ import annotations

import argparse
import json
import glob
import os
import sys
import tempfile
import time
import urllib.request

# --- 設定（CloseCraw の .env 流儀・全て環境変数で上書き可） ---
STATUS_URL   = os.environ.get("LIVENESS_STATUS_URL", "http://127.0.0.1:8100/api/status")
STREAM_DIR   = os.environ.get("LIVENESS_STREAM_DIR", "/mnt/data/stream")
MKV_STALE_S  = float(os.environ.get("LIVENESS_MKV_STALE_S", "30"))     # mkv がこの秒数書かれてなければ凍結
# go-live 直後、制御盤は即 streaming=true を返すが録画 mkv が新鮮に映るまで約1分かかる。
# その転移から本秒数の間は「mkv だけが未鮮度」を沈黙とせず暖機中扱いにする（枠頭の1周期誤射を吸収）。
# 定常配信中の凍結検知(MKV_STALE_S)には影響しない＝猶予は false→true 転移時にだけ効く。
GOLIVE_GRACE_S = float(os.environ.get("LIVENESS_GOLIVE_GRACE_S", "120"))
HTTP_TIMEOUT = float(os.environ.get("LIVENESS_HTTP_TIMEOUT", "4"))
_here = os.path.dirname(os.path.abspath(__file__))
_deflog = os.path.join(os.path.dirname(_here), "logs")
TEXTFILE     = os.environ.get("LIVENESS_TEXTFILE", os.path.join(_deflog, "closecraw_liveness.prom"))
STATE_FILE   = os.environ.get("LIVENESS_STATE_FILE", os.path.join(_deflog, "liveness_state.json"))
TARGET = "broadcast"


def _get_status():
    """:8100/api/status を受動 GET。届かない/壊れたら None。"""
    try:
        req = urllib.request.Request(STATUS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _newest_mkv_age(now: float):
    """STREAM_DIR 直下の最新 .mkv の経過秒。無ければ None。"""
    try:
        mkvs = glob.glob(os.path.join(STREAM_DIR, "*.mkv"))
        if not mkvs:
            return None
        newest = max(mkvs, key=lambda p: os.path.getmtime(p))
        return now - os.path.getmtime(newest)
    except Exception:
        return None


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _atomic_write(path: str, data: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)   # temp→rename の原子書込（PLAN §8 atomic）
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def probe(now: float) -> dict:
    """生の生存信号だけ採る（判定は decide へ分離＝go-live 猶予に state が要るため）。"""
    status = _get_status()
    mkv_age = _newest_mkv_age(now)
    mkv_fresh = (mkv_age is not None and mkv_age < MKV_STALE_S)

    reachable = status is not None
    streaming = bool(status.get("streaming")) if reachable else None
    obs_ok = bool(status.get("obs_connected")) if reachable else None

    # relay leg：streaming 中に列挙された leg で up=false があれば「届いていない」
    legs_ok = True
    legs = (status or {}).get("relay_legs") or {}
    for name, leg in legs.items():
        if isinstance(leg, dict) and leg.get("up") is False:
            legs_ok = False

    return {
        "reachable": reachable, "streaming": streaming, "obs_ok": obs_ok,
        "legs_ok": legs_ok, "mkv_age": mkv_age, "mkv_fresh": mkv_fresh,
    }


def decide(sig: dict, now: float, streaming_since: float | None = None) -> dict:
    """信号→生死判定。streaming_since 指定時は go-live 猶予を効かせる。

    streaming_since = streaming が false→true に転じた unix 秒（main が state で追跡）。
    暖機中（転移後 GOLIVE_GRACE_S 内・欠けが mkv 鮮度だけ・OBS/leg 健全）は沈黙とせず healthy=True＝
    last_ok を進めて枠頭の1周期誤射（BroadcastSilent / プローバ直 --alert）を抑える。
    """
    reachable, streaming = sig["reachable"], sig["streaming"]
    obs_ok, legs_ok = sig["obs_ok"], sig["legs_ok"]
    mkv_age, mkv_fresh = sig["mkv_age"], sig["mkv_fresh"]
    warming = False

    if not reachable:
        # 制御プレーンが応答しない。mkv 鮮度で best-effort。degraded を立てる。
        healthy = bool(mkv_fresh)
        verdict = "degraded(:8100不応答)" + ("・mkv新鮮=推定生存" if mkv_fresh else "・mkvも凍結=沈黙濃厚")
    elif not streaming:
        # off-air（ゲート）。沈黙ではない＝OK 扱いで last_ok を進める。
        healthy = True
        verdict = "off-air(対象外)"
    else:
        healthy = bool(obs_ok and legs_ok and mkv_fresh)
        if healthy:
            verdict = "live・健全"
        elif (streaming_since is not None and (now - streaming_since) < GOLIVE_GRACE_S
              and obs_ok and legs_ok and not mkv_fresh):
            # go-live 猶予：立ち上げ直後で欠けが mkv 鮮度だけ＝録画が新鮮化する前の暖機中。
            warming = True
            healthy = True
            verdict = f"live・暖機中(go-live {now - streaming_since:.0f}s・mkv待ち)"
        else:
            why = []
            if not obs_ok:   why.append("OBS断")
            if not legs_ok:  why.append("relay leg down")
            if not mkv_fresh: why.append(f"mkv凍結({mkv_age:.0f}s無更新)" if mkv_age is not None else "mkv不在")
            verdict = "live・沈黙=" + "/".join(why)

    ev = dict(sig)
    ev.update({"healthy": healthy, "verdict": verdict, "warming": warming})
    return ev


def evaluate(now: float, streaming_since: float | None = None) -> dict:
    """probe→decide の薄いラッパ（後方互換・単発判定用）。"""
    return decide(probe(now), now, streaming_since)


def _prom(state: dict, ev: dict, now: float) -> str:
    def g(name, help_, val, labels=""):
        lb = "{" + labels + "}" if labels else ""
        return f"# HELP {name} {help_}\n# TYPE {name} gauge\n{name}{lb} {val}\n"
    last_ok = state.get(TARGET, {}).get("last_ok", now)
    out = []
    out.append(g("closecraw_liveness_last_ok_seconds",
                 "Unix time of last confirmed-healthy (or off-air) check.",
                 f"{last_ok:.0f}", f'target="{TARGET}"'))
    out.append(g("closecraw_liveness_healthy",
                 "1 if target healthy or off-air this run, else 0.",
                 1 if ev["healthy"] else 0, f'target="{TARGET}"'))
    out.append(g("closecraw_liveness_streaming",
                 "1 if control plane reports streaming (a slot should be live).",
                 (1 if ev["streaming"] else 0) if ev["reachable"] else -1, f'target="{TARGET}"'))
    out.append(g("closecraw_liveness_mkv_fresh",
                 "1 if newest live .mkv was written within the stale window.",
                 1 if ev["mkv_fresh"] else 0, f'target="{TARGET}"'))
    out.append(g("closecraw_liveness_control_reachable",
                 "1 if :8100/api/status responded.",
                 1 if ev["reachable"] else 0, f'target="{TARGET}"'))
    out.append(g("closecraw_liveness_prober_run_seconds",
                 "Unix time of the last prober run (prober's own heartbeat).",
                 f"{now:.0f}"))
    return "".join(out)


def build_alert_message(ev: dict, now: float) -> str:
    """沈黙を人が読める1通に。Phase0(③B)は生の判定を送る＝LLM要約は後段/パイプライン側。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    age = f"{ev['mkv_age']:.0f}s" if ev["mkv_age"] is not None else "NA"
    return (
        "🔴 CloseCraw deadman — broadcast 沈黙検知\n"
        f"{ev['verdict']}\n"
        f"streaming={ev['streaming']} obs={ev['obs_ok']} legs_ok={ev['legs_ok']} mkv_age={age}\n"
        f"{stamp}"
    )


def _first_chat(s: str) -> str:
    for tok in s.replace(",", " ").split():
        return tok.strip()
    return ""


def _apply_simulate(kind: str):
    """本物の放送に触れず沈黙を注入して検知→通知ループを検証する（③B の "わざと止める"）。"""
    global _get_status, _newest_mkv_age
    live = {"streaming": True, "obs_connected": True,
            "relay_legs": {"youtube": {"up": True}, "twitch": {"up": True}}}
    if kind == "mkv-frozen":       # 録画は回るが中身が黙る（TTSハング）
        _get_status = lambda: dict(live); _newest_mkv_age = lambda now: 300
    elif kind == "leg-down":       # YouTube に届いていない
        s = {**live, "relay_legs": {"youtube": {"up": False}, "twitch": {"up": True}}}
        _get_status = lambda: s; _newest_mkv_age = lambda now: 5
    elif kind == "obs-down":       # OBS 断
        s = {**live, "obs_connected": False}
        _get_status = lambda: s; _newest_mkv_age = lambda now: 5
    elif kind == "control-down":   # 制御プレーン死＋mkv凍結
        _get_status = lambda: None; _newest_mkv_age = lambda now: 300


def main() -> int:
    ap = argparse.ArgumentParser(description="broadcast 沈黙検知プローバ (Phase0)")
    ap.add_argument("--simulate", choices=["mkv-frozen", "leg-down", "obs-down", "control-down"],
                    help="本物の放送に触れず沈黙を注入して検証する")
    ap.add_argument("--alert", action="store_true",
                    help="沈黙時に実際に Telegram へ送る(要 TELEGRAM_BOT_TOKEN + chat)。既定はドライラン表示")
    args = ap.parse_args()

    if args.simulate:
        _apply_simulate(args.simulate)

    now = time.time()
    state = _load_state()
    st = state.setdefault(TARGET, {})

    sig = probe(now)
    # go-live 猶予のため streaming の false→true 転移時刻を跨ぎで憶える。
    if sig["streaming"] is True:
        if not st.get("streaming_prev"):
            st["streaming_since"] = now          # 立ち上がった瞬間を刻む（既に true なら据え置き）
        streaming_since = st.get("streaming_since")
    else:
        st.pop("streaming_since", None)          # off-air/不応答は転移追跡をリセット
        streaming_since = None
    st["streaming_prev"] = (sig["streaming"] is True)

    ev = decide(sig, now, streaming_since)

    # 健全（＝生存 or off-air）な時だけ last_ok を前進させる。壊れなら据え置き＝古びさせる。
    if ev["healthy"]:
        st["last_ok"] = now
        st.pop("alerted_at", None)   # 復旧＝次の沈黙で再通知できるようにする
    st["last_run"] = now

    # ---- 沈黙時のアラート（③B: notifier 直結の近道・cooldownでスパム抑制=DESIGN.md流儀） ----
    alerted = "healthy"
    if not ev["healthy"]:
        msg = build_alert_message(ev, now)
        cooldown = float(os.environ.get("LIVENESS_ALERT_COOLDOWN_S", "900"))
        recent = (now - st.get("alerted_at", 0)) < cooldown
        if not args.alert:
            alerted = "dry-run"
        elif recent:
            alerted = "cooldown中(抑制)"
        else:
            token = os.environ.get("LIVENESS_TG_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
            chat = (os.environ.get("LIVENESS_TG_CHAT") or os.environ.get("TELEGRAM_CHAT_ID")
                    or _first_chat(os.environ.get("TG_CLAUDE_ALLOWED_CHATS", "")))
            if not (token and chat):
                alerted = "no-creds(TELEGRAM_BOT_TOKEN と chat を環境に)"
            else:
                import notifier   # requests 依存＝送る時だけ import（ドライランは不要）
                ok = notifier.send_telegram(token, chat, msg)
                if ok:
                    st["alerted_at"] = now
                alerted = "sent" if ok else "send-failed"
        if alerted in ("dry-run", "no-creds(TELEGRAM_BOT_TOKEN と chat を環境に)"):
            print("---- 送られるメッセージ ----\n" + msg + "\n---------------------------")

    _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))
    _atomic_write(TEXTFILE, _prom(state, ev, now))

    last_ok = st.get("last_ok", now)
    silent_for = now - last_ok
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    print(f"{stamp} target={TARGET} verdict={ev['verdict']} healthy={int(ev['healthy'])} "
          f"alert={alerted} last_ok前={silent_for:.0f}s "
          f"mkv_age={('%.0f' % ev['mkv_age']) if ev['mkv_age'] is not None else 'NA'}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
