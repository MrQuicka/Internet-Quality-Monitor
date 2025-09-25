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
from io import StringIO

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
            raise RuntimeError(f"Speedtest failed: {e}") from cli_err# =========================
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
# Homer API endpointy
# =========================
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

@app.get("/api/latest")
def api_latest():
    """Get latest measurement result"""
    if not _recent:
        return Response(status_code=204)  # No content
    
    latest = list(_recent)[-1]
    return {
        "ts": latest["ts"],
        "ping_ms": latest.get("ping_ms"),
        "jitter_ms": latest.get("jitter_ms"), 
        "download_mbps": latest.get("download_mbps"),
        "upload_mbps": latest.get("upload_mbps"),
        "packet_loss": latest.get("packet_loss_pct"),
        "online": latest.get("online", True)
    }

@app.get("/api/history")
def api_history():
    """Get measurement history - compatible with dashboard"""
    period = request.args.get('period', 'day')
    limit = min(int(request.args.get('limit', 288)), 1000)
    
    # Use existing results endpoint
    arr = list(_recent)[-limit:]
    
    # Transform data for dashboard compatibility
    data = []
    for item in arr:
        data.append({
            'ts': item['ts'],
            'timestamp': item['ts'],  # Dashboard compatibility
            'ping_ms': item.get('ping_ms'),
            'ping': item.get('ping_ms'),  # Dashboard compatibility
            'jitter_ms': item.get('jitter_ms'),
            'download_mbps': item.get('download_mbps'),
            'download': item.get('download_mbps'),  # Dashboard compatibility
            'upload_mbps': item.get('upload_mbps'), 
            'upload': item.get('upload_mbps'),  # Dashboard compatibility
            'packet_loss': item.get('packet_loss_pct'),
            'online': item.get('online', True)
        })
    
    return data

# =========================
# Zbývající API endpointy
# =========================
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root() -> str:
    try:
        # Zkus načíst dashboard.html soubor
        dashboard_path = "/app/dashboard.html"
        if os.path.exists(dashboard_path):
            with open(dashboard_path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    
    # Fallback - jednoduchá stránka s odkazy
    return """
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Internet Quality Monitor</title>
    <style>
        body { font-family: system-ui; background: #1a1a1a; color: white; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; }
        h1 { color: #6aa6ff; }
        .card { background: #2a2a2a; padding: 20px; border-radius: 10px; margin: 10px 0; }
        .btn { display: inline-block; background: #6aa6ff; color: white; padding: 10px 20px; 
               text-decoration: none; border-radius: 5px; margin: 5px; }
        .status { font-size: 18px; margin: 10px 0; }
        #status { font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🌐 Internet Quality Monitor</h1>
        <div class="card">
            <h2>API Status</h2>
            <div class="status">Status: <span id="status">Checking...</span></div>
            <div class="status">Posledních testů: <span id="count">-</span></div>
        </div>
        
        <div class="card">
            <h2>🔗 API Endpointy</h2>
            <a href="/api/homer-status" class="btn">Homer Status</a>
            <a href="/api/homer-service-status" class="btn">Service Status</a>
            <a href="/metrics" class="btn">Prometheus Metrics</a>
            <a href="/health" class="btn">Health Check</a>
            <a href="/api/results?limit=10" class="btn">Recent Results</a>
        </div>
        
        <div class="card">
            <h2>⚙️ Actions</h2>
            <button onclick="runTest()" class="btn">🚀 Spustit test</button>
            <span id="testResult" style="margin-left: 10px;"></span>
        </div>
        
        <div class="card">
            <h2>📊 Homer Integration</h2>
            <p>Pro integraci s Homer dashboard použij tyto endpointy:</p>
            <ul>
                <li><strong>Message widget:</strong> <code>http://192.168.0.208:5001/api/homer-status</code></li>
                <li><strong>Service status:</strong> <code>http://192.168.0.208:5001/api/homer-service-status</code></li>
            </ul>
        </div>
    </div>

    <script>
    async function checkStatus() {
        try {
            const response = await fetch('/api/results?limit=5');
            const data = await response.json();
            document.getElementById('status').textContent = '✅ Online';
            document.getElementById('count').textContent = data.results ? data.results.length : 0;
        } catch (e) {
            document.getElementById('status').textContent = '❌ Error';
        }
    }
    
    async function runTest() {
        const btn = event.target;
        const result = document.getElementById('testResult');
        btn.disabled = true;
        btn.textContent = '⏳ Měřím...';
        result.textContent = 'Může trvat až 60 sekund...';
        
        try {
            const response = await fetch('/api/run-now', { method: 'POST' });
            const data = await response.json();
            result.textContent = data.status === 'ok' ? '✅ Test dokončen' : '❌ Test selhal';
            checkStatus();
        } catch (e) {
            result.textContent = '❌ Chyba: ' + e.message;
        } finally {
            setTimeout(() => {
                btn.disabled = false;
                btn.textContent = '🚀 Spustit test';
                result.textContent = '';
            }, 3000);
        }
    }
    
    checkStatus();
    setInterval(checkStatus, 30000);
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

@app.get("/api/results_range")
def results_range(
    last: Optional[str] = Query(None, description="např. 2h, 24h, 7d, 30d"),
    frm: Optional[str] = Query(None, description="ISO 8601"),
    to: Optional[str] = Query(None, description="ISO 8601"),
    limit: int = Query(5000, ge=1, le=200000),
):
    """Vrátí výsledky z CSV/SQLite v daném časovém intervalu (nebo za poslední 'last')."""
    if last:
        dt_to = datetime.now(timezone.utc)
        dt_from = dt_to - _parse_last(last)
    else:
        dt_from = _parse_iso(frm) if frm else datetime.now(timezone.utc) - timedelta(hours=24)
        dt_to = _parse_iso(to) if to else datetime.now(timezone.utc)

    out: List[Dict[str, Any]] = []
    if USE_SQLITE and os.path.exists(SQLITE_PATH):
        with sqlite3.connect(SQLITE_PATH) as conn:
            cur = conn.execute(
                "SELECT ts,download_mbps,upload_mbps,ping_ms,jitter_ms,packet_loss_pct "
                "FROM results WHERE ts>=? AND ts<=? ORDER BY ts ASC LIMIT ?",
                (dt_from.isoformat(), dt_to.isoformat(), limit),
            )
            for ts, d, u, p, j, l in cur.fetchall():
                out.append({"ts": ts, "download_mbps": d, "upload_mbps": u, "ping_ms": p, "jitter_ms": j, "packet_loss_pct": l})
    else:
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, "r") as f:
                r = csv.reader(f)
                header = next(r, None)
                for row in r:
                    try:
                        ts = datetime.fromisoformat(row[0])
                        if dt_from <= ts <= dt_to:
                            out.append(_row_to_obj(row))
                            if len(out) >= limit:
                                break
                    except Exception:
                        pass
    return {"results": out}

@app.get("/api/export.csv")
def export_csv(
    last: Optional[str] = Query(None),
    frm: Optional[str] = Query(None),
    to: Optional[str] = Query(None),
):
    """Export CSV v daném časovém intervalu."""
    if last:
        dt_to = datetime.now(timezone.utc)
        dt_from = dt_to - _parse_last(last)
    else:
        dt_from = _parse_iso(frm) if frm else datetime.now(timezone.utc) - timedelta(hours=24)
        dt_to = _parse_iso(to) if to else datetime.now(timezone.utc)

    rows: List[List[str]] = [["ts","download_mbps","upload_mbps","ping_ms","jitter_ms","packet_loss_pct"]]
    if USE_SQLITE and os.path.exists(SQLITE_PATH):
        with sqlite3.connect(SQLITE_PATH) as conn:
            cur = conn.execute(
                "SELECT ts,download_mbps,upload_mbps,ping_ms,jitter_ms,packet_loss_pct "
                "FROM results WHERE ts>=? AND ts<=? ORDER BY ts ASC",
                (dt_from.isoformat(), dt_to.isoformat()),
            )
            for ts, d, u, p, j, l in cur.fetchall():
                rows.append([ts, str(d), str(u), str(p), str(j), str(l)])
    else:
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, "r") as f:
                r = csv.reader(f)
                header = next(r, None)
                for row in r:
                    try:
                        ts = datetime.fromisoformat(row[0])
                        if dt_from <= ts <= dt_to:
                            rows.append(row)
                    except Exception:
                        pass

    buf = StringIO()
    cw = csv.writer(buf)
    cw.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    filename = f"results_{dt_from.date()}_{dt_to.date()}.csv"
    return Response(data, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.get("/api/config")
def get_config():
    return {
        "interval_sec": INTERVAL_SEC,
        "ping_count": PING_COUNT,
        "ping_interval_s": PING_INTERVAL_S,
        "targets": TARGETS,
        "server_id": _extract_server_id(SPEEDTEST_EXTRA),
        "enable_speedtest": ENABLE_SPEEDTEST,
        "retention_days": RETENTION_DAYS,
    }

def _extract_server_id(extra: List[str]) -> Optional[int]:
    if "--server-id" in extra:
        try:
            idx = extra.index("--server-id")
            return int(extra[idx+1])
        except Exception:
            return None
    return None

@app.post("/api/config")
def set_config(payload: Dict[str, Any] = Body(...)):
    global INTERVAL_SEC, PING_COUNT, PING_INTERVAL_S, TARGETS, SPEEDTEST_EXTRA, ENABLE_SPEEDTEST
    if "interval_sec" in payload:
        INTERVAL_SEC = max(5, int(payload["interval_sec"]))
    if "ping_count" in payload:
        PING_COUNT = max(1, min(50, int(payload["ping_count"])))
    if "ping_interval_s" in payload:
        PING_INTERVAL_S = max(0.01, float(payload["ping_interval_s"]))
    if "targets" in payload and isinstance(payload["targets"], list):
        TARGETS = [str(t).strip() for t in payload["targets"] if str(t).strip()]
    if "enable_speedtest" in payload:
        ENABLE_SPEEDTEST = bool(payload["enable_speedtest"])
    if "server_id" in payload:
        sid = payload["server_id"]
        if sid is None or int(sid) <= 0:
            SPEEDTEST_EXTRA = [x for x in SPEEDTEST_EXTRA if x != "--server-id" and not x.isdigit()]
        else:
            cleaned = []
            skip_next = False
            for x in SPEEDTEST_EXTRA:
                if skip_next: skip_next = False; continue
                if x == "--server-id": skip_next = True; continue
                cleaned.append(x)
            SPEEDTEST_EXTRA = cleaned + ["--server-id", str(int(sid))]
    return get_config()

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
    prune_old_data()
    _recent.append(res)
    return JSONResponse({"status": "ok", "result": res})

# =========================
# Hlavní spouštěč
# =========================
if __name__ == "__main__":
    print("🚀 Spouštím Internet Quality Monitor...")
    ensure_csv_header()
    init_sqlite()

    import threading
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    print(f"📊 Monitoring spuštěn na portu {PORT}")

    uvicorn.run(app, host=BIND, port=PORT) - timedelta(hours=24)