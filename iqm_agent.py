import os, csv, time, json, math, socket, sqlite3, statistics, subprocess
from datetime import datetime, timezone
from collections import deque
from typing import Optional, Dict, Any, List
from fastapi.responses import HTMLResponse, JSONResponse


from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
import uvicorn

# ====== Konfigurace (ENV s defaulty) ======
TZ = os.getenv("TZ", "Europe/Prague")
INTERVAL_SEC = int(os.getenv("IQM_INTERVAL_SEC", "300"))         # perioda měření
WARMUP_RUNS = int(os.getenv("IQM_WARMUP_RUNS", "1"))             # počet warm-up běhů (ignorují se)
RETRY_MAX = int(os.getenv("IQM_RETRY_MAX", "3"))
CSV_PATH = os.getenv("IQM_CSV_PATH", "/app/data/results.csv")
SQLITE_PATH = os.getenv("IQM_SQLITE_PATH", "/app/data/results.sqlite")
USE_SQLITE = os.getenv("IQM_USE_SQLITE", "0") == "1"
TARGETS = os.getenv("IQM_TARGETS", "8.8.8.8,1.1.1.1").split(",")  # pro ping/packet-loss
BIND = os.getenv("IQM_BIND", "0.0.0.0")
PORT = int(os.getenv("IQM_PORT", "5001"))
SPEEDTEST_BIN = os.getenv("IQM_SPEEDTEST_BIN", "speedtest")      # ookla-cli nebo speedtest-cli

# ====== Prometheus registry ======
registry = CollectorRegistry()
runs_total = Counter("iqm_runs_total", "Počet spuštění měření", registry=registry)
errors_total = Counter("iqm_errors_total", "Počet chyb měření", registry=registry)
download_g = Gauge("iqm_download_mbps", "Download Mbps", registry=registry)
upload_g = Gauge("iqm_upload_mbps", "Upload Mbps", registry=registry)
ping_g = Gauge("iqm_ping_ms", "Ping ms", registry=registry)
jitter_g = Gauge("iqm_jitter_ms", "Jitter ms", registry=registry)
loss_g = Gauge("iqm_packet_loss_pct", "Packet loss %", registry=registry)
duration_h = Histogram("iqm_run_duration_seconds", "Délka běhu měření (s)", registry=registry, buckets=(0.5,1,2,5,10,20,30,60,120))

# ====== App & cache ======
app = FastAPI(title="Internet Quality Monitor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_recent: deque[Dict[str, Any]] = deque(maxlen=500)

def ensure_csv_header():
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts","download_mbps","upload_mbps","ping_ms","jitter_ms","packet_loss_pct"])

def init_sqlite():
    if not USE_SQLITE:
        return
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS results(
            ts TEXT PRIMARY KEY,
            download_mbps REAL,
            upload_mbps REAL,
            ping_ms REAL,
            jitter_ms REAL,
            packet_loss_pct REAL
        )
        """)

def run_speedtest() -> Dict[str, float]:
    """
    Použijeme externí binárku 'speedtest' (Ookla) nebo python balík 'speedtest-cli' jako fallback.
    Vrací dict s download/upload/ping.
    """
    try:
        # Ookla CLI s JSON výstupem
        out = subprocess.check_output([SPEEDTEST_BIN, "--format=json", "--progress=no"], stderr=subprocess.STDOUT, timeout=240)
        data = json.loads(out.decode("utf-8", errors="ignore"))
        down = data["download"]["bandwidth"] * 8 / 1e6 if "download" in data and "bandwidth" in data["download"] else None
        up = data["upload"]["bandwidth"] * 8 / 1e6 if "upload" in data and "bandwidth" in data["upload"] else None
        ping = float(data["ping"]["latency"]) if "ping" in data and "latency" in data["ping"] else None
        return {"download_mbps": down, "upload_mbps": up, "ping_ms": ping}
    except Exception:
        # Fallback na python speedtest-cli
        try:
            import speedtest
            s = speedtest.Speedtest()
            s.get_best_server()
            down = s.download() / 1e6
            up = s.upload() / 1e6
            ping = s.results.ping
            return {"download_mbps": down, "upload_mbps": up, "ping_ms": ping}
        except Exception as e:
            raise RuntimeError(f"Speedtest failed: {e}")

def ping_stats(target: str, count: int = 10) -> Dict[str, float]:
    """
    Vrátí loss (%) a jitter (ms) pro daný target pomocí systémového 'ping'.
    """
    # Linux: ping -n -c 10 -i 0.2 target
    out = subprocess.check_output(["ping", "-n", "-c", str(count), "-i", "0.2", target], stderr=subprocess.STDOUT, timeout=20).decode()
    # Parse řádků s 'time=' a závěrečných statistik
    times: List[float] = []
    received = 0
    for line in out.splitlines():
        if "time=" in line:
            try:
                ms = float(line.split("time=")[1].split(" ")[0])
                times.append(ms)
                received += 1
            except Exception:
                pass
        if "packets transmitted" in line and "received" in line:
            parts = line.split(",")
            tx = int(parts[0].split(" ")[0])
            rx = int(parts[1].strip().split(" ")[0])
            received = rx
            loss_pct = 100.0 * (tx - rx) / tx if tx > 0 else 100.0
    jitter_ms = statistics.pstdev(times) if len(times) > 1 else 0.0
    return {"loss_pct": loss_pct, "jitter_ms": jitter_ms}

def aggregate_ping(targets: List[str]) -> Dict[str, float]:
    losses, jitters = [], []
    for t in targets:
        try:
            s = ping_stats(t)
            losses.append(s["loss_pct"])
            jitters.append(s["jitter_ms"])
        except Exception:
            losses.append(100.0); jitters.append(float("nan"))
    # střední hodnota, NaN ignorujeme
    jitter_vals = [j for j in jitters if not math.isnan(j)]
    jitter = statistics.median(jitter_vals) if jitter_vals else 0.0
    loss = statistics.median(losses) if losses else 100.0
    return {"packet_loss_pct": loss, "jitter_ms": jitter}

def measure_once() -> Dict[str, Any]:
    """
    Jeden běh s retry + exponenciální backoff + jitter.
    """
    attempt, backoff = 0, 1.5
    start = time.perf_counter()
    last_err: Optional[Exception] = None
    while attempt <= RETRY_MAX:
        try:
            st = run_speedtest()
            pp = aggregate_ping(TARGETS)
            now = datetime.now(timezone.utc).isoformat()
            res = {
                "ts": now,
                "download_mbps": float(st["download_mbps"]),
                "upload_mbps": float(st["upload_mbps"]),
                "ping_ms": float(st["ping_ms"]),
                "jitter_ms": float(pp["jitter_ms"]),
                "packet_loss_pct": float(pp["packet_loss_pct"]),
            }
            return res
        except Exception as e:
            last_err = e
            attempt += 1
            if attempt > RETRY_MAX:
                break
            time.sleep(backoff + (0.2 * attempt))  # malý jitter
            backoff *= 2
    raise RuntimeError(f"Measurement failed after retries: {last_err}")

def persist(res: Dict[str, Any]):
    ensure_csv_header()
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.writer(f); w.writerow([res["ts"], res["download_mbps"], res["upload_mbps"], res["ping_ms"], res["jitter_ms"], res["packet_loss_pct"]])
    if USE_SQLITE:
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO results(ts,download_mbps,upload_mbps,ping_ms,jitter_ms,packet_loss_pct) VALUES(?,?,?,?,?,?)",
                (res["ts"], res["download_mbps"], res["upload_mbps"], res["ping_ms"], res["jitter_ms"], res["packet_loss_pct"])
            )

def update_metrics(res: Dict[str, Any], duration_s: float):
    download_g.set(res["download_mbps"])
    upload_g.set(res["upload_mbps"])
    ping_g.set(res["ping_ms"])
    jitter_g.set(res["jitter_ms"])
    loss_g.set(res["packet_loss_pct"])
    duration_h.observe(duration_s)
    runs_total.inc()

def loop():
    # Warm-up běhy
    for _ in range(WARMUP_RUNS):
        try:
            _ = measure_once()
        except Exception:
            pass
        time.sleep(2)
    # Hlavní smyčka
    while True:
        t0 = time.perf_counter()
        try:
            res = measure_once()
            persist(res)
            dt = time.perf_counter() - t0
            update_metrics(res, dt)
            _recent.append(res)
        except Exception:
            errors_total.inc()
        time.sleep(INTERVAL_SEC)

# ====== FastAPI ======
@app.get("/metrics")
def metrics():
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

@app.get("/api/results")
def recent(limit: int = 50):
    arr = list(_recent)[-limit:]
    return {"results": arr}

@app.get("/health")
def health():
    try:
        socket.gethostbyname("google.com")
        return {"status": "ok"}
    except Exception:
        return {"status": "degraded"}
    
@app.get("/", response_class=HTMLResponse)
def root():
    return """
    <html>
      <head><title>Internet Quality Monitor</title></head>
      <body style="font-family:system-ui; max-width:720px; margin:40px auto; line-height:1.5">
        <h1>Internet Quality Monitor</h1>
        <p>API běží. Užitečné odkazy:</p>
        <ul>
          <li><a href="/health">/health</a></li>
          <li><a href="/metrics">/metrics</a> (Prometheus)</li>
          <li><a href="/api/results?limit=20">/api/results?limit=20</a></li>
        </ul>
      </body>
    </html>
    """

@app.post("/run-now")
def run_now():
    """Spusť okamžité měření (užitečné pro test)."""
    t0 = time.perf_counter()
    res = measure_once()
    persist(res)
    dt = time.perf_counter() - t0
    update_metrics(res, dt)
    _recent.append(res)
    return JSONResponse({"status": "ok", "duration_s": round(dt, 2), "result": res})

if __name__ == "__main__":
    # Init DB a CSV
    ensure_csv_header()
    init_sqlite()

    # Spuštění měřící smyčky v separátním vlákně
    import threading
    th = threading.Thread(target=loop, daemon=True)
    th.start()

    # HTTP server
    uvicorn.run(app, host=BIND, port=PORT)
