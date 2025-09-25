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
# Konfigurace z ENV proměnných (s rozumnými defaulty)
# =========================
TZ = os.getenv("TZ", "Europe/Prague")
INTERVAL_SEC = int(os.getenv("IQM_INTERVAL_SEC", "300"))  # perioda měření
WARMUP_RUNS = int(os.getenv("IQM_WARMUP_RUNS", "1"))  # počet warm-up běhů (ignorují se)
RETRY_MAX = int(os.getenv("IQM_RETRY_MAX", "3"))
CSV_PATH = os.getenv("IQM_CSV_PATH", "/app/data/results.csv")
SQLITE_PATH = os.getenv("IQM_SQLITE_PATH", "/app/data/results.sqlite")
USE_SQLITE = os.getenv("IQM_USE_SQLITE", "0") == "1"
TARGETS = [t.strip() for t in os.getenv("IQM_TARGETS", "8.8.8.8,1.1.1.1").split(",") if t.strip()]
BIND = os.getenv("IQM_BIND", "0.0.0.0")
PORT = int(os.getenv("IQM_PORT", "5001"))

# Preferujeme oficiální Ookla CLI binárku; fallback je python speedtest-cli
SPEEDTEST_BIN = os.getenv("IQM_SPEEDTEST_BIN", "speedtest")
# Volitelné extra parametry pro Ookla CLI (např. "--server-id 12345")
SPEEDTEST_EXTRA = shlex.split(os.getenv("IQM_SPEEDTEST_ARGS", ""))

# =========================
# Prometheus registry a metriky
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
    # jednoduché bucketování, ať jsou vidět i rychlé/krátké běhy
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 240),
)

# =========================
# Aplikace a in-memory cache posledních výsledků
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
# Pomocné funkce pro perzistenci
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


# =========================
# Měření rychlosti – Ookla CLI s tichým souhlasem; fallback speedtest-cli (HTTPS)
# =========================
def run_speedtest() -> Dict[str, float]:
    """
    Vrátí dict s download/upload (Mb/s) a ping (ms).
    1) Preferuje oficiální Ookla CLI (binárka `speedtest`), se silent consent přepínači.
    2) Fallback: python speedtest-cli s secure=True (HTTPS), což často řeší 403 od CF.
    """
    try:
        # Ookla CLI s JSON výstupem + tichý souhlas s licencí/GDPR
        cmd = [SPEEDTEST_BIN, "--format=json", "--progress=no", "--accept-license", "--accept-gdpr", *SPEEDTEST_EXTRA]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=240)
        data = json.loads(out.decode("utf-8", errors="ignore"))
        down = data.get("download", {}).get("bandwidth")
        up = data.get("upload", {}).get("bandwidth")
        ping = data.get("ping", {}).get("latency")

        # bandwidth je v bajtech/s → *8/1e6 => Mb/s
        down_mbps = (down * 8 / 1e6) if isinstance(down, (int, float)) else None
        up_mbps = (up * 8 / 1e6) if isinstance(up, (int, float)) else None
        ping_ms = float(ping) if isinstance(ping, (int, float)) else None
        if down_mbps is None or up_mbps is None or ping_ms is None:
            raise RuntimeError("Ookla CLI: nekompletní výstup")

        return {"download_mbps": down_mbps, "upload_mbps": up_mbps, "ping_ms": ping_ms}

    except Exception as cli_err:
        # Fallback – python speedtest-cli
        try:
            import speedtest  # type: ignore

            s = speedtest.Speedtest(secure=True)  # HTTPS (pomáhá proti 403)
            s.get_best_server()
            down = s.download() / 1e6  # b/s → Mb/s
            up = s.upload() / 1e6
            ping = float(s.results.ping)
            return {"download_mbps": down, "upload_mbps": up, "ping_ms": ping}
        except Exception as e:
            raise RuntimeError(f"Speedtest failed: {e}") from cli_err


# =========================
# Ping/jitter/packet loss
# =========================
def ping_stats(target: str, count: int = 10) -> Dict[str, float]:
    """
    Vrátí loss (%) a jitter (ms) pro daný target, pomocí systémového 'ping'.
    Používáme: ping -n -c {count} -i 0.2 target
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
        # řádky s time=
        if " time=" in line:
            # formáty: "... time=7.12 ms"
            try:
                after = line.split(" time=")[1]
                val = after.split(" ")[0].strip()
                ms = float(val)
                times.append(ms)
                rx += 1
            except Exception:
                pass

        # závěrečné statistiky (locale-safe přístup – hledáme substrings)
        if "packets transmitted" in line and "received" in line:
            # např.: "10 packets transmitted, 10 received, 0% packet loss, time 2003ms"
            try:
                parts = line.replace("%", "").split(",")
                # "10 packets transmitted"
                tx = int(parts[0].split()[0])
                # " 10 received"
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

    # robustnější metrika: medián, NaN ignorujeme
    jitter_vals = [j for j in jitters if not math.isnan(j)]
    if jitter_vals:
        jitter = statistics.median(jitter_vals)
    else:
        jitter = 0.0
    loss = statistics.median(losses) if losses else 100.0
    return {"jitter_ms": float(jitter), "loss_pct": float(loss)}


# =========================
# Jeden běh měření (s retries + backoff + jitter)
# =========================
def measure_once() -> Dict[str, Any]:
    attempt, backoff = 0, 1.5
    last_err: Optional[Exception] = None

    while attempt <= RETRY_MAX:
        try:
            t0 = time.perf_counter()
            # speedtest (download/upload/ping)
            st = run_speedtest()
            # ping/jitter/loss – agregace přes více targetů
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

            # aktualizace metrik
            dt = time.perf_counter() - t0
            update_metrics(res, dt)
            return res

        except Exception as e:
            last_err = e
            attempt += 1
            if attempt > RETRY_MAX:
                break
            # exponenciální backoff + malý jitter
            time.sleep(backoff + (0.2 * attempt))
            backoff *= 2

    raise RuntimeError(f"Measurement failed after retries: {last_err}")


def persist(res: Dict[str, Any]) -> None:
    ensure_csv_header()
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                res["ts"],
                res["download_mbps"],
                res["upload_mbps"],
                res["ping_ms"],
                res["jitter_ms"],
                res["packet_loss_pct"],
            ]
        )

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
    # Warm-up běhy – ignorujeme výsledky
    for _ in range(WARMUP_RUNS):
        try:
            _ = measure_once()
        except Exception:
            pass
        time.sleep(2)

    # Hlavní smyčka
    while True:
        try:
            res = measure_once()
            persist(res)
            _recent.append(res)
        except Exception:
            errors_total.inc()
        time.sleep(INTERVAL_SEC)


# =========================
# FastAPI endpointy
# =========================
@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return """
    <html>
      <head><title>Internet Quality Monitor</title></head>
      <body style="font-family:system-ui; max-width:720px; margin:40px auto; line-height:1.6">
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


@app.post("/run-now")
def run_now():
    """
    Spustí okamžité měření (užitečné pro testování, CI, validaci nasazení).
    """
    t0 = time.perf_counter()
    res = measure_once()
    persist(res)
    dt = time.perf_counter() - t0
    _recent.append(res)
    return JSONResponse({"status": "ok", "duration_s": round(dt, 2), "result": res})


# =========================
# Hlavní spouštěč
# =========================
if __name__ == "__main__":
    # Init CSV/SQLite
    ensure_csv_header()
    init_sqlite()

    # Měřící smyčka ve vlákně
    import threading

    th = threading.Thread(target=loop, daemon=True)
    th.start()

    # HTTP server
    uvicorn.run(app, host=BIND, port=PORT)
