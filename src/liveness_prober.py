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


def evaluate(now: float):
    """一次対象 broadcast の生存を判定して dict を返す。"""
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
        else:
            why = []
            if not obs_ok:   why.append("OBS断")
            if not legs_ok:  why.append("relay leg down")
            if not mkv_fresh: why.append(f"mkv凍結({mkv_age:.0f}s無更新)" if mkv_age is not None else "mkv不在")
            verdict = "live・沈黙=" + "/".join(why)

    return {
        "reachable": reachable, "streaming": streaming, "obs_ok": obs_ok,
        "legs_ok": legs_ok, "mkv_age": mkv_age, "mkv_fresh": mkv_fresh,
        "healthy": healthy, "verdict": verdict,
    }


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


def main() -> int:
    now = time.time()
    ev = evaluate(now)
    state = _load_state()

    # 健全（＝生存 or off-air）な時だけ last_ok を前進させる。壊れなら据え置き＝古びさせる。
    if ev["healthy"]:
        state.setdefault(TARGET, {})["last_ok"] = now
    state.setdefault(TARGET, {})["last_run"] = now

    _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))
    _atomic_write(TEXTFILE, _prom(state, ev, now))

    last_ok = state[TARGET]["last_ok"]
    silent_for = now - last_ok
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    print(f"{stamp} target={TARGET} verdict={ev['verdict']} "
          f"healthy={int(ev['healthy'])} last_ok前={silent_for:.0f}s "
          f"mkv_age={('%.0f' % ev['mkv_age']) if ev['mkv_age'] is not None else 'NA'}s "
          f"-> {TEXTFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
