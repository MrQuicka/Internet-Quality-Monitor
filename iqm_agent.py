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
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Response
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
TARGETS = [t.strip() for t in os.getenv("IQM_TARGETS", "8.8.8.8,1.1.1.1").split(",") if t.strip()]
BIND = os.getenv("IQM_BIND", "0.0.0.0")
PORT = int(os.getenv("IQM_PORT", "5001"))

# Preferujeme Ookla CLI; fallback je python speedtest-cli (HTTPS)
SPEEDTEST_BIN = os.getenv("IQM_SPEEDTEST_BIN", "speedtest")
SPEEDTEST_EXTRA = shlex.split(os.getenv("IQM_SPEEDTEST_ARGS", ""))

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
# Perzistence
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
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True
               )
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

# =========================
# Speedtest
# =========================
def run_speedtest() -> Dict[str, float]:
    """
    Vrátí dict s download/upload (Mb/s) a ping (ms).
    1) Preferuje oficiální Ookla CLI (binárka `speedtest`) s tichým souhlasem.
    2) Fallback: python speedtest-cli s secure=True (HTTPS) – řeší časté 403.
    """
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
def ping_stats(target: str, count: int = 10) -> Dict[str, float]:
    """
    Vrátí loss (%) a jitter (ms) pro daný target pomocí systémového 'ping'.
    """
    out = subprocess.check_output(
        ["ping", "-n", "-c", str(count), "-i", "0.2", target],
        stderr=subprocess.STDOUT,
        timeout=20,
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

    jitter_ms = statistics.pstdev(times) if len(times) > 1 else 0.0
    return {"loss_pct": float(loss_pct), "jitter_ms": float(jitter_ms)}


def aggregate_ping(targets: List[str]) -> Dict[str, float]:
    losses: List[float] = []
    jitters: List[float] = []
    for t in targets:
        try:
            s = ping_stats(t)
            losses.append(s["loss_pct"])
            jitters.append(s["jitter_ms"])
        except Exception:
            losses.append(100.0)
            jitters.append(float("nan"))

    jitter_vals = [j for j in jitters if not math.isnan(j)]
    jitter = statistics.median(jitter_vals) if jitter_vals else 0.0
    loss = statistics.median(losses) if losses else 100.0
    return {"jitter_ms": float(jitter), "loss_pct": float(loss)}

# =========================
# Jedno měření (s retries)
# =========================
def measure_once() -> Dict[str, Any]:
    attempt, backoff = 0, 1.5
    last_err: Optional[Exception] = None

    while attempt <= RETRY_MAX:
        try:
            t0 = time.perf_counter()
            st = run_speedtest()
            pp = aggregate_ping(TARGETS)

            now = datetime.now(timezone.utc).isoformat()
            res = {
                "ts": now,
                "download_mbps": float(st["download_mbps"]),
                "upload_mbps": float(st["upload_mbps"]),
                "ping_ms": float(st["ping_ms"]),
                "jitter_ms": float(pp["jitter_ms"]),
                "packet_loss_pct": float(pp["loss_pct"]),
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

    raise RuntimeError(f"Measurement failed after retries: {last_err}")

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
            _recent.append(res)
        except Exception:
            errors_total.inc()
        time.sleep(INTERVAL_SEC)

# =========================
# UI + API
# =========================
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
</style>
</head>
<body>
<div class="container">
  <h1>Internet Quality Monitor</h1>
  <p class="sub">Rychlý přehled kvality připojení s živým grafem a exportem.</p>

  <div class="row">
    <div id="statusPill" class="pill">Načítám…</div>
    <div class="actions">
      <button id="runBtn" class="button">Spustit měření</button>
      <a class="button" href="/metrics" target="_blank" rel="noopener">Prometheus /metrics</a>
      <a class="button secondary" href="/api/export.csv">Stáhnout CSV</a>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="label">Download</div>
      <div id="dl" class="metric">—</div>
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

  <footer>Auto-refresh každých 30 s • Prahy lze změnit přes parametry URL: <code>?dlow=200&uplow=200</code></footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
const qs = new URLSearchParams(location.search);
const DLOW = Number(qs.get('dlow')||200);
const ULOW = Number(qs.get('uplow')||200);
let chart;

function cls(r){
  if ((r.packet_loss_pct||0) > 5 || (r.download_mbps||0) < 5) return 'bad';
  if ((r.packet_loss_pct||0) > 1 || (r.jitter_ms||0) > 30 || (r.ping_ms||0) > 80 ||
      (r.download_mbps||0) < DLOW*0.5 || (r.upload_mbps||0) < ULOW*0.5) return 'warn';
  return 'ok';
}
function fmt(n,u){return (n==null || Number.isNaN(n)) ? '—' : (Number(n).toFixed(1)+' '+u)}

async function load(){
  const r = await fetch('/api/results?limit=120');
  const data = await r.json();
  const arr = data.results || [];
  if (!arr.length) return;

  const last = arr[arr.length-1];
  document.getElementById('dl').textContent = fmt(last.download_mbps,'Mb/s');
  document.getElementById('ul').textContent = fmt(last.upload_mbps,'Mb/s');
  document.getElementById('lat').textContent =
    `${fmt(last.ping_ms,'ms')} • ${fmt(last.jitter_ms,'ms')} • ${(last.packet_loss_pct||0).toFixed(2)}%`;
  document.getElementById('updated').textContent = 'Naposledy: ' + new Date(last.ts).toLocaleString();

  const pill = document.getElementById('statusPill'); const c = cls(last);
  pill.className = 'pill '+c;
  pill.textContent = c==='ok' ? 'Stav: OK' : c==='warn' ? 'Stav: Degradace' : 'Stav: Výpadek';

  const labels = arr.map(x => new Date(x.ts).toLocaleTimeString());
  const dl = arr.map(x => x.download_mbps);
  const ul = arr.map(x => x.upload_mbps);

  const el = document.getElementById('chart');
  if (!chart){
    chart = new Chart(el, {
      type: 'line',
      data: { labels, datasets: [
        { label: 'Download (Mb/s)', data: dl },
        { label: 'Upload (Mb/s)', data: ul }
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
    chart.update('none');
  }
}

document.getElementById('runBtn').onclick = async () => {
  const b = document.getElementById('runBtn');
  b.disabled = true; b.textContent = 'Měřím…';
  try { await fetch('/run-now', {method:'POST'}); await load(); }
  catch(e){ alert('Měření se nepovedlo: '+e); }
  finally { b.disabled=false; b.textContent='Spustit měření';}
};

load(); setInterval(load, 30000);
</script>
</body>
</html>
    """

@app.get("/metrics")
def metrics():
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

@app.get("/api/results")
def recent(limit: int = 50):
    arr = list(_recent)[-limit:]
    return {"results": arr}

@app.get("/api/export.csv")
def export_csv():
    try:
        with open(CSV_PATH, "rb") as f:
            data = f.read()
        return Response(
            data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=results.csv"},
        )
    except FileNotFoundError:
        return Response(
            "ts,download_mbps,upload_mbps,ping_ms,jitter_ms,packet_loss_pct\n",
            media_type="text/csv",
        )

@app.get("/health")
def health():
    try:
        socket.gethostbyname("google.com")
        return {"status": "ok"}
    except Exception:
        return {"status": "degraded"}

@app.post("/run-now")
def run_now():
    """Spuštění měření okamžitě."""
    res = measure_once()
    persist(res)
    _recent.append(res)
    return JSONResponse({"status": "ok", "result": res})

# =========================
# Hlavní spouštěč
# =========================
if __name__ == "__main__":
    ensure_csv_header()
    init_sqlite()

    import threading
    th = threading.Thread(target=loop, daemon=True)
    th.start()

    uvicorn.run(app, host=BIND, port=PORT)
