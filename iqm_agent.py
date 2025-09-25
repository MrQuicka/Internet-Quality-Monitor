import os
import csv
import time
import json
import math
import socket
import sqlite3
import statistics
import subprocess
import shlex
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Response, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
import uvicorn

# =========================
# Konfigurace (ENV s defaulty)
# =========================
TZ = os.getenv("TZ", "Europe/Prague")
INTERVAL_SEC = int(os.getenv("IQM_INTERVAL_SEC", "300"))
WARMUP_RUNS = int(os.getenv("IQM_WARMUP_RUNS", "1"))
RETRY_MAX = int(os.getenv("IQM_RETRY_MAX", "3"))
CSV_PATH = os.getenv("IQM_CSV_PATH", "/app/data/results.csv")
SQLITE_PATH = os.getenv("IQM_SQLITE_PATH", "/app/data/results.sqlite")
USE_SQLITE = os.getenv("IQM_USE_SQLITE", "0") == "1"
RETENTION_DAYS = int(os.getenv("IQM_RETENTION_DAYS", "90"))

TARGETS = [t.strip() for t in os.getenv("IQM_TARGETS", "8.8.8.8,1.1.1.1").split(",") if t.strip()]
PING_COUNT = int(os.getenv("IQM_PING_COUNT", "10"))
PING_INTERVAL_S = float(os.getenv("IQM_PING_INTERVAL_S", "0.2"))

BIND = os.getenv("IQM_BIND", "0.0.0.0")
PORT = int(os.getenv("IQM_PORT", "5001"))

# Preferujeme Ookla CLI; fallback je python speedtest-cli (HTTPS)
SPEEDTEST_BIN = os.getenv("IQM_SPEEDTEST_BIN", "speedtest")
SPEEDTEST_EXTRA = shlex.split(os.getenv("IQM_SPEEDTEST_ARGS", ""))
ENABLE_SPEEDTEST = os.getenv("IQM_ENABLE_SPEEDTEST", "1") == "1"

# =========================
# Prometheus metriky
# =========================
registry = CollectorRegistry()
runs_total = Counter("iqm_runs_total", "Počet spuštění měření", registry=registry)
errors_total = Counter("iqm_errors_total", "Počet chyb měření", registry=registry)
download_g = Gauge("iqm_download_mbps", "Download Mbps", registry=registry)
upload_g = Gauge("iqm_upload_mbps", "Upload Mbps", registry=registry)
ping_g = Gauge("iqm_ping_ms", "Ping ms", registry=registry)
jitter_g = Gauge("iqm_jitter_ms", "Jitter ms", registry=registry)
loss_g = Gauge("iqm_packet_loss_pct", "Packet loss %", registry=registry)
duration_h = Histogram(
    "iqm_run_duration_seconds",
    "Délka běhu měření (s)",
    registry=registry,
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 240),
)
speedtest_enabled_g = Gauge("iqm_speedtest_enabled", "1 pokud je povolen speedtest, jinak 0", registry=registry)

# =========================
# Aplikace + cache posledních výsledků
# =========================
app = FastAPI(title="Internet Quality Monitor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
_recent: deque[Dict[str, Any]] = deque(maxlen=500)

# =========================
# Helpers — čas a CSV
# =========================
def ensure_csv_header() -> None:
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "download_mbps", "upload_mbps", "ping_ms", "jitter_ms", "packet_loss_pct"])

def init_sqlite() -> None:
    if not USE_SQLITE:
        return
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results(
                ts TEXT PRIMARY KEY,
                download_mbps REAL,
                upload_mbps REAL,
                ping_ms REAL,
                jitter_ms REAL,
                packet_loss_pct REAL
            )
            """
        )

def prune_old_data() -> None:
    """Smaže záznamy starší než RETENTION_DAYS (CSV i SQLite)."""
    if RETENTION_DAYS <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    # CSV
    if os.path.exists(CSV_PATH):
        tmp = CSV_PATH + ".tmp"
        with open(CSV_PATH, "r", newline="") as src, open(tmp, "w", newline="") as dst:
            r = csv.reader(src); w = csv.writer(dst)
            header = next(r, None)
            if header: w.writerow(header)
            for row in r:
                try:
                    ts = datetime.fromisoformat(row[0])
                    if ts >= cutoff:
                        w.writerow(row)
                except Exception:
                    pass
        os.replace(tmp, CSV_PATH)
    # SQLite
    if USE_SQLITE and os.path.exists(SQLITE_PATH):
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute("DELETE FROM results WHERE ts < ?", (cutoff.isoformat(),))
            conn.commit()

def _parse_last(s: str) -> timedelta:
    """Parse '2h', '24h', '7d', '30d', '15m', '1w' → timedelta."""
    s = s.strip().lower()
    if not s: raise ValueError("empty")
    num = "".join(ch for ch in s if ch.isdigit())
    unit = s[len(num):]
    if not num or unit not in {"m","h","d","w"}: raise ValueError("bad duration")
    n = int(num)
    if unit == "m": return timedelta(minutes=n)
    if unit == "h": return timedelta(hours=n)
    if unit == "d": return timedelta(days=n)
    if unit == "w": return timedelta(weeks=n)
    raise ValueError("bad unit")

def _parse_iso(dt: str) -> datetime:
    x = datetime.fromisoformat(dt)
    if x.tzinfo is None:
        x = x.replace(tzinfo=timezone.utc)
    return x

def _row_to_obj(row: List[str]) -> Dict[str, Any]:
    return {
        "ts": row[0],
        "download_mbps": float(row[1]),
        "upload_mbps": float(row[2]),
        "ping_ms": float(row[3]),
        "jitter_ms": float(row[4]),
        "packet_loss_pct": float(row[5]),
    }

# =========================
# Speedtest
# =========================
def run_speedtest(aggregated_rtt_ms: Optional[float] = None) -> Dict[str, float]:
    """
    Vrátí dict s download/upload (Mb/s) a ping (ms).
    - Pokud ENABLE_SPEEDTEST = False → download/upload = 0 a ping = aggregated_rtt_ms (pokud je k dispozici).
    - Jinak: preferuj oficiální Ookla CLI, fallback speedtest-cli (HTTPS).
    """
    if not ENABLE_SPEEDTEST:
        return {"download_mbps": 0.0, "upload_mbps": 0.0, "ping_ms": float(aggregated_rtt_ms or 0.0)}

    try:
        cmd = [SPEEDTEST_BIN, "--format=json", "--progress=no", "--accept-license", "--accept-gdpr", *SPEEDTEST_EXTRA]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=240)
        data = json.loads(out.decode("utf-8", errors="ignore"))
        down = data.get("download", {}).get("bandwidth")
        up = data.get("upload", {}).get("bandwidth")
        ping = data.get("ping", {}).get("latency")

        down_mbps = (down * 8 / 1e6) if isinstance(down, (int, float)) else None
        up_mbps = (up * 8 / 1e6) if isinstance(up, (int, float)) else None
        ping_ms = float(ping) if isinstance(ping, (int, float)) else None
        if down_mbps is None or up_mbps is None or ping_ms is None:
            raise RuntimeError("Ookla CLI: nekompletní výstup")

        return {"download_mbps": down_mbps, "upload_mbps": up_mbps, "ping_ms": ping_ms}

    except Exception as cli_err:
        try:
            import speedtest  # type: ignore
            s = speedtest.Speedtest(secure=True)
            s.get_best_server()
            down = s.download() / 1e6
            up = s.upload() / 1e6
            ping = float(s.results.ping)
            return {"download_mbps": down, "upload_mbps": up, "ping_ms": ping}
        except Exception as e:
            raise RuntimeError(f"Speedtest failed: {e}") from cli_err

# =========================
# Ping / jitter / loss
# =========================
def ping_stats(target: str, count: int, interval_s: float) -> Dict[str, float]:
    """
    Vrátí median RTT (ms), loss (%) a jitter (ms) pro daný target pomocí systémového 'ping'.
    """
    out = subprocess.check_output(
        ["ping", "-n", "-c", str(count), "-i", str(interval_s), target],
        stderr=subprocess.STDOUT,
        timeout=20 + int(count * interval_s * 2),
    ).decode(errors="ignore")

    times: List[float] = []
    tx = count
    rx = 0
    loss_pct = 100.0

    for line in out.splitlines():
        line = line.strip()
        if " time=" in line:
            try:
                after = line.split(" time=")[1]
                val = after.split(" ")[0].strip()
                ms = float(val)
                times.append(ms)
                rx += 1
            except Exception:
                pass

        if "packets transmitted" in line and "received" in line:
            try:
                parts = line.replace("%", "").split(",")
                tx = int(parts[0].split()[0])
                rx = int(parts[1].split()[0])
                loss_pct = (100.0 * (tx - rx) / tx) if tx > 0 else 100.0
            except Exception:
                pass

    rtt_ms = statistics.median(times) if times else float("nan")
    jitter_ms = statistics.pstdev(times) if len(times) > 1 else 0.0
    return {"rtt_ms": float(rtt_ms), "loss_pct": float(loss_pct), "jitter_ms": float(jitter_ms)}

def aggregate_ping(targets: List[str]) -> Dict[str, float]:
    losses: List[float] = []
    jitters: List[float] = []
    rtts: List[float] = []
    for t in targets:
        try:
            s = ping_stats(t, PING_COUNT, PING_INTERVAL_S)
            losses.append(s["loss_pct"])
            jitters.append(s["jitter_ms"])
            rtts.append(s["rtt_ms"])
        except Exception:
            losses.append(100.0)
            jitters.append(float("nan"))
            rtts.append(float("nan"))

    jitter_vals = [j for j in jitters if not math.isnan(j)]
    rtt_vals = [r for r in rtts if not math.isnan(r)]
    jitter = statistics.median(jitter_vals) if jitter_vals else 0.0
    loss = statistics.median(losses) if losses else 100.0
    rtt = statistics.median(rtt_vals) if rtt_vals else 0.0
    return {"jitter_ms": float(jitter), "loss_pct": float(loss), "rtt_ms": float(rtt)}

# =========================
# Jedno měření (s retries)
# =========================
def measure_once() -> Dict[str, Any]:
    attempt, backoff = 0, 1.5
    last_err: Optional[Exception] = None

    while attempt <= RETRY_MAX:
        try:
            t0 = time.perf_counter()
            pp = aggregate_ping(TARGETS)
            st = run_speedtest(aggregated_rtt_ms=pp["rtt_ms"])

            now = datetime.now(timezone.utc).isoformat()
            res = {
                "ts": now,
                "download_mbps": float(st["download_mbps"]),
                "upload_mbps": float(st["upload_mbps"]),
                "ping_ms": float(st["ping_ms"] if ENABLE_SPEEDTEST else pp["rtt_ms"]),
                "jitter_ms": float(pp["jitter_ms"]),
                "packet_loss_pct": float(pp["loss_pct"]),
                "online": True  # Pokud jsme se dostali sem, jsme online
            }

            dt = time.perf_counter() - t0
            update_metrics(res, dt)
            return res

        except Exception as e:
            last_err = e
            attempt += 1
            if attempt > RETRY_MAX:
                break
            time.sleep(backoff + (0.2 * attempt))
            backoff *= 2

    # Pokud všechny pokusy selhaly, vrátíme offline stav
    now = datetime.now(timezone.utc).isoformat()
    offline_res = {
        "ts": now,
        "download_mbps": 0.0,
        "upload_mbps": 0.0,
        "ping_ms": 0.0,
        "jitter_ms": 0.0,
        "packet_loss_pct": 100.0,
        "online": False
    }
    errors_total.inc()
    return offline_res

def persist(res: Dict[str, Any]) -> None:
    ensure_csv_header()
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            res["ts"],
            res["download_mbps"],
            res["upload_mbps"],
            res["ping_ms"],
            res["jitter_ms"],
            res["packet_loss_pct"],
        ])

    if USE_SQLITE:
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO results
                (ts, download_mbps, upload_mbps, ping_ms, jitter_ms, packet_loss_pct)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    res["ts"],
                    res["download_mbps"],
                    res["upload_mbps"],
                    res["ping_ms"],
                    res["jitter_ms"],
                    res["packet_loss_pct"],
                ),
            )

def update_metrics(res: Dict[str, Any], duration_s: float) -> None:
    speedtest_enabled_g.set(1.0 if ENABLE_SPEEDTEST else 0.0)
    download_g.set(res["download_mbps"])
    upload_g.set(res["upload_mbps"])
    ping_g.set(res["ping_ms"])
    jitter_g.set(res["jitter_ms"])
    loss_g.set(res["packet_loss_pct"])
    duration_h.observe(duration_s)
    runs_total.inc()

def loop() -> None:
    # Warm-up
    for _ in range(WARMUP_RUNS):
        try:
            _ = measure_once()
        except Exception:
            pass
        time.sleep(2)

    while True:
        try:
            res = measure_once()
            persist(res)
            prune_old_data()
            _recent.append(res)
        except Exception:
            errors_total.inc()
        time.sleep(max(5, INTERVAL_SEC))

# =========================
# UI + API
# =========================
# ===== NOVÉ HOMER API ENDPOINTY =====

@app.get("/api/homer-status")
def homer_status():
    """Endpoint pro Homer message widget s aktuálním stavem připojení"""
    try:
        # Získej nejnovější data
        latest = list(_recent)[-1] if _recent else None
        
        if not latest:
            return {
                "title": "🔄 Internet Monitor",
                "content": "Žádná data k dispozici - spouštím první měření...",
                "style": "is-light"
            }
        
        # Vyhodnocení stavu
        ping = latest.get('ping_ms', 0)
        download = latest.get('download_mbps', 0) 
        upload = latest.get('upload_mbps', 0)
        loss = latest.get('packet_loss_pct', 0)
        online = latest.get('online', True)
        jitter = latest.get('jitter_ms', 0)
        
        # Formatování hodnot
        ping_str = f"{ping:.1f}" if ping and ping > 0 else "--"
        download_str = f"{download:.1f}" if download and download > 0 else "--"
        upload_str = f"{upload:.1f}" if upload and upload > 0 else "--"
        loss_str = f"{loss:.1f}" if loss and loss > 0 else "0.0"
        jitter_str = f"{jitter:.1f}" if jitter and jitter > 0 else "--"
        
        # Určení stylu a zprávy
        if not online:
            return {
                "title": "❌ Internet je offline",
                "content": "Připojení k internetu není k dispozici",
                "style": "is-danger"
            }
        
        # Kontrola kritických problémů
        if (loss and loss > 5) or (download and download < 50) or (ping and ping > 100):
            return {
                "title": "🚨 Kritické problémy s připojením",
                "content": f"📥 {download_str} Mbps • 📤 {upload_str} Mbps • 📡 {ping_str} ms • 📦 {loss_str}% ztráta • ⚡ {jitter_str} ms jitter",
                "style": "is-danger"
            }
        
        # Kontrola degradace
        elif (loss and loss > 1) or (download and download < 200) or (ping and ping > 50) or (jitter and jitter > 20):
            quality_issues = []
            if loss and loss > 1: quality_issues.append(f"ztráty {loss_str}%")
            if download and download < 200: quality_issues.append(f"pomalý DL")
            if ping and ping > 50: quality_issues.append(f"vysoký ping")
            if jitter and jitter > 20: quality_issues.append(f"vysoký jitter")
            
            issues_text = " • ".join(quality_issues) if quality_issues else ""
            return {
                "title": f"⚠️ Zhoršená kvalita připojení" + (f" ({issues_text})" if issues_text else ""),
                "content": f"📥 {download_str} Mbps • 📤 {upload_str} Mbps • 📡 {ping_str} ms • 📦 {loss_str}% • ⚡ {jitter_str} ms",
                "style": "is-warning"
            }
        
        # Vše v pořádku
        else:
            # Určení kvality podle rychlosti
            if download and download >= 500:
                quality = "Vynikající"
                emoji = "🚀"
            elif download and download >= 300:
                quality = "Výborná"
                emoji = "✨"
            else:
                quality = "Dobrá"
                emoji = "✅"
                
            return {
                "title": f"{emoji} Internet funguje skvěle",
                "content": f"📥 {download_str} Mbps • 📤 {upload_str} Mbps • 📡 {ping_str} ms • Kvalita: {quality}",
                "style": "is-success"
            }
            
    except Exception as e:
        return {
            "title": "❌ Chyba monitoru",
            "content": f"Nepodařilo se načíst stav připojení: {str(e)}",
            "style": "is-danger"
        }

@app.get("/api/homer-service-status")
def homer_service_status():
    """Endpoint pro dynamickou aktualizaci subtitle Internet Monitor služby"""
    try:
        latest = list(_recent)[-1] if _recent else None
        
        if not latest:
            return {
                "status": "loading",
                "subtitle": "🔄 Načítám...",
                "emoji": "🔄"
            }
        
        ping = latest.get('ping_ms', 0)
        download = latest.get('download_mbps', 0) 
        upload = latest.get('upload_mbps', 0)
        loss = latest.get('packet_loss_pct', 0)
        online = latest.get('online', True)
        
        # Formatování hodnot pro kompaktní zobrazení
        dl = f"{download:.0f}" if download and download > 0 else "--"
        ul = f"{upload:.0f}" if upload and upload > 0 else "--"
        ping_val = f"{ping:.0f}ms" if ping and ping > 0 else "--"
        
        if not online:
            return {
                "status": "offline",
                "subtitle": "🔴 Offline",
                "emoji": "🔴"
            }
        elif (loss and loss > 5) or (download and download < 50) or (ping and ping > 100):
            return {
                "status": "critical",
                "subtitle": f"🔴 Problémy • ↓{dl} ↑{ul} Mbps • {ping_val}",
                "emoji": "🔴"
            }
        elif (loss and loss > 1) or (download and download < 200) or (ping and ping > 50):
            return {
                "status": "warning", 
                "subtitle": f"🟡 Degradace • ↓{dl} ↑{ul} Mbps • {ping_val}",
                "emoji": "🟡"
            }
        else:
            return {
                "status": "ok",
                "subtitle": f"🟢 V pořádku • ↓{dl} ↑{ul} Mbps • {ping_val}",
                "emoji": "🟢"
            }
            
    except Exception:
        return {
            "status": "error",
            "subtitle": "❌ Chyba monitoru",
            "emoji": "❌"
        }

# ===== PŮVODNÍ UI + API =====

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root() -> str:
    return """
<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Internet Quality Monitor</title>
<style>
:root{--bg:#0b0f14;--fg:#e6edf3;--muted:#8b949e;--card:#10161e;--accent:#3b82f6;--ok:#10b981;--warn:#f59e0b;--bad:#ef4444}
@media (prefers-color-scheme: light){
  :root{--bg:#f7f9fc;--fg:#0b1220;--muted:#5b6577;--card:#ffffff;--accent:#2563eb;--ok:#059669;--warn:#d97706;--bad:#dc2626}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,"Helvetica Neue",Arial}
.container{max-width:1100px;margin:36px auto;padding:0 20px}
h1{margin:0 0 8px;font-size:32px}
.sub{color:var(--muted);margin:0 0 12px}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:6px 12px;font-weight:700}
.pill.ok{background:color-mix(in oklab,var(--ok) 20%,transparent);color:var(--ok)}
.pill.warn{background:color-mix(in oklab,var(--warn) 20%,transparent);color:var(--warn)}
.pill.bad{background:color-mix(in oklab,var(--bad) 20%,transparent);color:var(--bad)}
.actions{display:flex;gap:10px;flex-wrap:wrap}
.button,button{appearance:none;border:0;border-radius:12px;padding:10px 14px;font-weight:700;cursor:pointer;text-decoration:none}
.button{background:var(--accent);color:#fff}
.button.secondary{background:transparent;color:var(--fg);border:1px solid var(--muted)}
.grid{display:grid;gap:16px;margin-top:12px;grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border-radius:16px;padding:18px;box-shadow:0 2px 20px rgba(0,0,0,.15)}
.label{color:var(--muted);font-size:13px}
.metric{font-size:28px;font-weight:800;margin-top:6px}
/* Fix nafukování grafu: wrapper s pevnou výškou + canvas 100% */
.chart-box{position:relative; height:360px;}
.chart-box canvas{
  position:absolute; inset:0;
  width:100% !important; height:100% !important;
  display:block;
}
footer{color:var(--muted);font-size:12px;margin-top:10px}
fieldset{border:1px solid color-mix(in oklab,var(--muted) 40%,transparent); border-radius:12px; padding:12px 14px; margin:16px 0;}
legend{color:var(--muted); padding:0 6px; font-size:12px;}
.form-row{display:grid; gap:12px; grid-template-columns:repeat(3,minmax(0,1fr));}
.form-row label{display:flex; flex-direction:column; gap:6px; font-size:13px;}
input[type="text"],input[type="number"],select{padding:8px 10px; border-radius:8px; border:1px solid color-mix(in oklab,var(--muted) 40%,transparent); background:transparent; color:var(--fg)}
.switch{display:flex; align-items:center; gap:8px;}
.badge{font-size:12px; color:var(--muted); margin-top:6px;}
</style>
</head>
<body>
<div class="container">
  <h1>Internet Quality Monitor</h1>
  <p class="sub">Rychlý přehled kvality připojení s živým grafem a exportem. Níže lze měnit chování testů i zobrazený časový rozsah.</p>

  <div class="row">
    <div id="statusPill" class="pill">Načítám…</div>
    <div class="actions">
      <label class="label">Rozsah:
        <select id="range">
          <option value="2h">2 h</option>
          <option value="24h" selected>24 h</option>
          <option value="7d">7 dní</option>
          <option value="30d">30 dní</option>
          <option value="all">Vše</option>
        </select>
      </label>
      <button id="runBtn" class="button">Spustit měření</button>
      <a id="exportBtn" class="button secondary" href="/api/export.csv">Stáhnout CSV</a>
      <a class="button" href="/metrics" target="_blank" rel="noopener">Prometheus /metrics</a>
    </div>
  </div>

  <fieldset>
    <legend>Nastavení testování</legend>
    <div class="form-row">
      <label>Interval měření (s)
        <input id="interval" type="number" min="5" step="5" value="300">
      </label>
      <label>Počet pingů (na target)
        <input id="pingCount" type="number" min="1" max="50" step="1" value="10">
      </label>
      <label>Interval pingů (s)
        <input id="pingInterval" type="number" min="0.05" step="0.05" value="0.2">
      </label>
    </div>
    <div class="form-row" style="margin-top:8px">
      <label>Cílové adresy (comma)
        <input id="targets" type="text" placeholder="8.8.8.8,1.1.1.1">
      </label>
      <label>Speedtest server ID
        <input id="serverId" type="number" min="0" step="1" placeholder="(volitelné)">
      </label>
      <label>SMA okno (min)
        <input id="ma" type="number" min="1" step="1" value="60">
      </label>
      <label class="switch" style="grid-column:1/-1">
        <input id="enableSt" type="checkbox" checked>
        <span>Použít speedtest (download/upload)</span>
      </label>
    </div>
    <div class="actions" style="margin-top:10px">
      <button id="saveCfg" class="button secondary">Uložit nastavení</button>
      <span id="saveInfo" class="label"></span>
    </div>
  </fieldset>

  <div class="grid">
    <div class="card">
      <div class="label">Download</div>
      <div id="dl" class="metric">—</div>
      <div class="badge" id="dlSmaLbl">SMA (60 min): —</div>
      <div class="badge" id="dlP10Lbl">P10 (rozsah): —</div>
    </div>
    <div class="card">
      <div class="label">Upload</div>
      <div id="ul" class="metric">—</div>
    </div>
    <div class="card">
      <div class="label">Ping • Jitter • Loss</div>
      <div id="lat" class="metric">—</div>
    </div>
  </div>

  <div class="card" style="margin-top:16px; overflow:hidden">
    <div class="label" id="updated">Naposledy: —</div>
    <div class="chart-box"><canvas id="chart"></canvas></div>
  </div>

  <div class="card" style="margin-top:16px; overflow:hidden">
    <div class="label">Latence / Jitter / Packet loss</div>
    <div class="chart-box"><canvas id="chart2"></canvas></div>
  </div>

  <footer>Auto-refresh každých 30 s • Prahy pro barvy lze změnit přes parametry URL: <code>?dlow=200&uplow=200</code></footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
const qs = new URLSearchParams(location.search);
const DLOW = Number(qs.get('dlow')||200);
const ULOW = Number(qs.get('uplow')||200);
let chart, chart2;

function cls(r){
  if ((r.packet_loss_pct||0) > 5 || (r.download_mbps||0) < 5) return 'bad';
  if ((r.packet_loss_pct||0) > 1 || (r.jitter_ms||0) > 30 || (r.ping_ms||0) > 80 ||
      (r.download_mbps||0) < DLOW*0.5 || (r.upload_mbps||0) < ULOW*0.5) return 'warn';
  return 'ok';
}
function fmt(n,u){return (n==null || Number.isNaN(n)) ? '—' : (Number(n).toFixed(1)+' '+u)}
function sma(arr, k){
  k = Math.max(1, k);
  const out = new Array(arr.length).fill(null);
  let sum = 0;
  for (let i=0;i<arr.length;i++){
    const v = Number(arr[i] ?? 0);
    sum += v;
    if (i >= k) sum -= Number(arr[i-k] ?? 0);
    if (i >= k-1) out[i] = sum / k;
  }
  return out;
}
function percentile(arr, p){
  const a = arr.map(Number).filter(x=>Number.isFinite(x)).sort((x,y)=>x-y);
  if (!a.length) return NaN;
  const idx = (a.length-1) * p;
  const lo = Math.floor(idx), hi = Math.ceil(idx);
  if (lo === hi) return a[lo];
  return a[lo] + (a[hi]-a[lo]) * (idx-lo);
}

async function loadCfg(){
  const r = await fetch('/api/config'); const c = await r.json();
  document.getElementById('interval').value = c.interval_sec;
  document.getElementById('pingCount').value = c.ping_count;
  document.getElementById('pingInterval').value = c.ping_interval_s;
  document.getElementById('targets').value = c.targets.join(', ');
  document.getElementById('serverId').value = c.server_id || '';
  document.getElementById('enableSt').checked = c.enable_speedtest;

  const savedMa = Number(localStorage.getItem('iqm_ma') || '60');
  document.getElementById('ma').value = savedMa;
}

async function saveCfg(){
  const ma = Number(document.getElementById('ma').value) || 60;
  localStorage.setItem('iqm_ma', String(ma));

  const body = {
    interval_sec: Number(document.getElementById('interval').value),
    ping_count: Number(document.getElementById('pingCount').value),
    ping_interval_s: Number(document.getElementById('pingInterval').value),
    targets: document.getElementById('targets').value.split(',').map(s=>s.trim()).filter(Boolean),
    server_id: Number(document.getElementById('serverId').value) || null,
    enable_speedtest: document.getElementById('enableSt').checked
  };
  const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  await r.json();
  document.getElementById('saveInfo').textContent = 'Uloženo ✔';
  setTimeout(()=>document.getElementById('saveInfo').textContent='', 2000);
  load(); // přepočítej graf se SMA
}

async function load(){
  const range = document.getElementById('range').value;
  const url = range==='all' ? '/api/results?limit=2000' : `/api/results_range?last=${encodeURIComponent(range)}&limit=5000`;
  document.getElementById('exportBtn').href = range==='all' ? '/api/export.csv' : `/api/export.csv?last=${encodeURIComponent(range)}`;

  const r = await fetch(url);
  const data = await r.json();
  const arr = data.results || [];
  if (!arr.length) return;

  const last = arr[arr.length-1];
  document.getElementById('dl').textContent = fmt(last.download_mbps,'Mb/s');
  document.getElementById('ul').textContent = fmt(last.upload_mbps,'Mb/s');
  document.getElementById('lat').textContent =
    `${fmt(last.ping_ms,'ms')} • ${fmt(last.jitter_ms,'ms')} • ${(last.packet_loss_pct||0).toFixed(2)}%`;
  document.getElementById('updated').textContent = 'Naposledy: ' + new Date(last.ts).toLocaleString('cs-CZ');

  const pill = document.getElementById('statusPill'); const c = cls(last);
  pill.className = 'pill '+c;
  pill.textContent = c==='ok' ? 'Stav: OK' : c==='warn' ? 'Stav: Degradace' : 'Stav: Výpadek';

  const labels = arr.map(x => new Date(x.ts).toLocaleTimeString());
  const dl = arr.map(x => x.download_mbps);
  const ul = arr.map(x => x.upload_mbps);
  const ping = arr.map(x => x.ping_ms);
  const jitter = arr.map(x => x.jitter_ms);
  const loss = arr.map(x => x.packet_loss_pct);

  // SMA okno (minuty) -> počet vzorků dle intervalu
  const cfg = await (await fetch('/api/config')).json();
  const maMin = Number(localStorage.getItem('iqm_ma') || document.getElementById('ma').value || 60);
  const k = Math.max(1, Math.round((maMin*60) / cfg.interval_sec));
  const dl_sma = sma(dl, k);
  const p10 = percentile(dl, 0.10);

  document.getElementById('dlSmaLbl').textContent = `SMA (${maMin} min): ${fmt(dl_sma.at(-1),'Mb/s')}`;
  document.getElementById('dlP10Lbl').textContent = `P10 (rozsah): ${fmt(p10,'Mb/s')}`;

  const el = document.getElementById('chart');
  if (!chart){
    chart = new Chart(el, {
      type: 'line',
      data: { labels, datasets: [
        { label: 'Download (Mb/s)', data: dl },
        { label: 'Upload (Mb/s)', data: ul },
        { label: `Download SMA (${maMin} min)`, data: dl_sma, borderDash:[6,4] }
      ]},
      options: { animation:false, responsive:true, maintainAspectRatio:false, resizeDelay:150,
        interaction:{mode:'index',intersect:false},
        scales:{ y:{ beginAtZero:true } }
      }
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = dl;
    chart.data.datasets[1].data = ul;
    if (chart.data.datasets.length < 3) {
      chart.data.datasets.push({ label:`Download SMA (${maMin} min)`, data: dl_sma, borderDash:[6,4] });
    } else {
      chart.data.datasets[2].label = `Download SMA (${maMin} min)`;
      chart.data.datasets[2].data = dl_sma;
    }
    chart.update('none');
  }

  const el2 = document.getElementById('chart2');
  if (!chart2){
    chart2 = new Chart(el2, {
      type: 'line',
      data: { labels, datasets: [
        { label: 'Ping (ms)', data: ping },
        { label: 'Jitter (ms)', data: jitter, borderDash: [6,4] },
        { label: 'Loss (%)', data: loss, yAxisID: 'y1' }
      ]},
      options: { animation:false, responsive:true, maintainAspectRatio:false, resizeDelay:150,
        interaction:{mode:'index',intersect:false},
        scales:{ y:{ beginAtZero:true }, y1:{ beginAtZero:true, position:'right' } }
      }
    });
  } else {
    chart2.data.labels = labels;
    chart2.data.datasets[0].data = ping;
    chart2.data.datasets[1].data = jitter;
    chart2.data.datasets[2].data = loss;
    chart2.update('none');
  }
}

document.getElementById('runBtn').onclick = async () => {
  const b = document.getElementById('runBtn');
  b.disabled = true; b.textContent = 'Měřím…';
  try { await fetch('/run-now', {method:'POST'}); await load(); }
  catch(e){ alert('Měření se nepovedlo: '+e); }
  finally { b.disabled=false; b.textContent='Spustit měření';}
};
document.getElementById('saveCfg').onclick = saveCfg;
document.getElementById('range').onchange = load;

loadCfg();
load(); setInterval(load, 30000);
</script>
</body>
</html>
    """