**English** · [日本語](README.md)

# CloseCraw

![CloseCraw](assets/logo/logo.jpg)

> **In one line**
> When Prometheus detects an anomaly, CloseCraw picks a read-only diagnostic script from a lookup table, runs it over SSH, summarizes the result, and posts it to Telegram. **The AI never generates commands and never even picks the script** — script selection is a deterministic table lookup; the AI's only job is the summary.

An LLM-assisted alert auto-triage agent for homelabs (a mix of Linux / OpenWrt / Windows).
It started as memory/SSD-leak monitoring for a single Windows server in a home lab and has since grown to watch several machines.

## Why I built it / What improved

**The problem**: a Windows server in my home lab was slowly eating memory and SSD. Alerts fired, but every investigation meant hand-typing SSH commands, I got woken up at night, and the notifications were raw logs that were hard to read.

**The move**: automate alert → investigate → summarize → notify. But **never let the AI "decide"** — script selection is a deterministic table lookup, and the only things it can run are pre-approved read-only scripts. The AI's job is limited to the final "2–4 sentence summary."

**Today**: a mixed Linux / OpenWrt / Windows fleet is monitored as a resident systemd service. Alerts are batched into a single message every 5 minutes (spam control), notifications are summaries rather than raw logs, and a fallback design guarantees the notification still goes out even when the LLM is down.

**The point**: least privilege, deterministic decisions, AI for summaries only — the opposite of "let the AI agent do everything." It's a practical take on wiring an LLM into monitoring/automation *safely*. See [docs/DESIGN.md](docs/DESIGN.md) for the reasoning.

---

## Data flow

```
[each node: node_exporter / windows_exporter]
            ↓ scrape metrics
[Prometheus :9090]
            ↓ alert fires (threshold breached)
[Alertmanager :9093]
            ↓ Webhook POST → /alert
[alert_handler.py :5001]  ← Flask
            ↓ enqueue, return 200 immediately
   ─── a background thread processes the batch every 5 minutes ───
            ↓
   (1) pull the last 1h of metrics via the Prometheus API
   (2) pick ONE diagnostic script by alertname × OS (ALERT_TO_SCRIPT lookup — no LLM)
   (3) run the script on the target host over SSH
   (4) the LLM condenses the output into an SRE-style 2–4 sentences
       (on failure, the raw output is attached instead)
            ↓
   [Telegram Bot → all alerts rolled into a single message]
```

**The LLM only does step (4), the summary** (script selection is a table lookup — deterministic, zero quota). Why the LLM was taken out of the decision path is in [docs/DESIGN.md](docs/DESIGN.md).
The LLM backend is switchable between **ollama** (local inference) and the **Gemini API** via `LLM_PROVIDER`. If either goes down it never raises — it falls back so that the notification always gets sent.

---

## Repository layout

```
closecraw/
├── .env                 ← tokens / API keys / paths (gitignored)
├── system_status.sh     ← whole-stack health check (one-shot)
├── src/
│   ├── alert_handler.py ← Flask webhook server (port 5001), queue, LLM abstraction
│   └── notifier.py      ← Telegram delivery
├── scripts/
│   ├── host_loader.py                 ← classify the inventory JSON by OS
│   ├── generate_prometheus_targets.py ← generate Prometheus static_configs from the JSON
│   ├── script_tester.sh
│   ├── linux_memory_pressure.sh       ┐
│   ├── linux_disk_inode.sh            │
│   ├── linux_cpu_top.sh               │
│   ├── linux_journal_anomaly.sh       ├ approved scripts (whitelist)
│   ├── win_process_delta.sh           │
│   ├── win_eventlog_anomaly.sh        │
│   └── win_disk_anomaly.sh            ┘
├── docs/
│   ├── DESIGN.md                      ← design notes (why the LLM was removed from selection + lessons)
│   └── inventory.example.json         ← sample host inventory
├── assets/logo/         ← logo
└── logs/                ← log output (gitignored)
```

> The Docker stack (Prometheus / Alertmanager / Grafana, etc.) and the Alertmanager config
> live in a separate directory and are not part of this repository.

---

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask requests python-dotenv

# create .env and fill in the variables below
touch .env
```

### `.env`

| Variable | Description |
|------|------|
| `TELEGRAM_TOKEN` | Telegram Bot token (required) |
| `TELEGRAM_CHAT_ID` | destination chat ID (required) |
| `LLM_PROVIDER` | `ollama` or `gemini` (default `ollama`) |
| `LLM_MODEL` | model name (default `gemini-2.5-flash-lite`; plain `flash` can have a smaller free tier) |
| `GEMINI_API_KEY` | required only when using Gemini |
| `CLOSECRAW_SPEC_FILE` | path to the host inventory JSON (default `/opt/closecraw/inventory.json`) |
| `CLOSECRAW_SCRIPTS_DIR` | directory holding the approved scripts (default `/opt/closecraw/scripts`) |

> For the host inventory format, see `docs/inventory.example.json`.
> When using ollama, set the endpoint/model via `OLLAMA_URL` / `OLLAMA_MODEL` (env `OLLAMA_MODEL`) in `src/alert_handler.py`.

---

## Running

```bash
# direct run (development)
python src/alert_handler.py        # → 0.0.0.0:5001

# systemd user service (resident operation)
systemctl --user status  alert-handler.service
systemctl --user restart alert-handler.service
journalctl --user -u alert-handler.service -n 50
```

> In case the module is imported without going through `__main__` (e.g. under gunicorn),
> the scheduler thread also starts on module load.

### Endpoints

| Path | Method | Purpose |
|------|----------|------|
| `/alert` | POST | Alertmanager webhook receiver (enqueue and return 200 immediately) |
| `/health` | GET | liveness check (also returns `queue_length`) |

---

## What the AI can and can't do

| Does | Does NOT |
|----------|--------------|
| Summarize script output into 2–4 human-readable sentences | Generate commands on its own |
| | Run arbitrary shell over SSH |
| | Choose the diagnostic script (decided by the `ALERT_TO_SCRIPT` lookup — no LLM) |

> The only freedom given to the AI is "summarize." The commands it can run are limited to pre-approved, read-only scripts, and which one runs is decided mechanically by a table lookup.

---

## Whitelisted scripts

| Script | Target OS | What it does |
|-----------|---------|------|
| `linux_memory_pressure.sh` | Linux | PSI / vmstat OOM / dmesg OOM |
| `linux_disk_inode.sh` | Linux | df / inode / deleted-but-unreleased files |
| `linux_cpu_top.sh` | Linux | PSI(cpu) / vmstat us・sy・wa / instantaneous top CPU consumers / D-state processes |
| `linux_journal_anomaly.sh` | Linux | recent error logs tallied by frequency |
| `win_process_delta.sh` | Windows | top 10 working-set growth over a few seconds |
| `win_eventlog_anomaly.sh` | Windows | event-log errors tallied by type |
| `win_disk_anomaly.sh` | Windows | large files modified in the last 24h |

Each script is invoked as `script <hostname>`, which internally runs `ssh <hostname>` and executes itself on the target machine (local execution if `localhost`).

---

## Health check

```bash
./system_status.sh    # checks Docker / Prometheus / Alertmanager / handler / ollama / SSH in one shot
```

---

## Troubleshooting

**No notifications arrive**
1. Is `systemctl --user status alert-handler.service` active?
2. Check for errors with `journalctl --user -u alert-handler.service -n 50`.
3. Check the queue length with `curl http://localhost:5001/health` (if it stays full, the worker thread has died).
4. Check whether alerts are actually firing with `curl http://localhost:9090/api/v1/alerts`.
5. Check Alertmanager's `repeat_interval` (too long and nothing arrives).

**Notifications turn into spam**
1. Check Alertmanager's `repeat_interval` (too short and it's instant spam).
2. Check that alert rules have an instance filter (OpenWrt Load alerts in particular tend to misfire across all Linux hosts).

**The handler won't start**
```bash
pkill -f alert_handler.py
systemctl --user restart alert-handler.service
journalctl --user -u alert-handler.service -n 20
```

---

## Design notes

- Alerts are not processed immediately: they are **queued for 5 minutes and batched** → notifications are rolled into one, cutting both spam and the number of LLM calls. The per-flush cap is `MAX_ALERTS_PER_FLUSH` (the overflow carries to the next cycle).
- `alertname` → PromQL is mapped by `ALERT_TO_QUERY` (falls back to fetching `up` if undefined).
- **Diagnostic-script selection is an `ALERT_TO_SCRIPT` table lookup** (alertname substring × OS). No match falls back to a general log sweep. **The LLM is not used** (zero quota, deterministic). Background is in [docs/DESIGN.md](docs/DESIGN.md).
- LLM summarization is allowed to fail (returns None) and falls back to attaching the raw output (**the notification always goes out**). LLM calls have a hard timeout so a hang can't stall the scheduler.
- Only scripts in the `WHITELIST` can run.
- With Gemini, a 429 response's body is read to tell a daily-quota (RPD) breach from a per-minute (RPM) one; RPD breaches are not retried (hammering something that won't recover until tomorrow only burns more quota). The API key is passed in a header (as a query parameter it can leak into logs via exception messages).
