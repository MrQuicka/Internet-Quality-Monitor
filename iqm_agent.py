#!/usr/bin/env python3
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

# Speedtest settings
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
# Speedtest - OPRAVENÁ VERZE
# =========================
def run_speedtest(aggregated_rtt_ms: Optional[float] = None) -> Dict[str, float]:
    """
    Vrátí dict s download/upload (Mb/s) a ping (ms).
    - Pokud ENABLE_SPEEDTEST = False → download/upload = 0 a ping = aggregated_rtt_ms
    - Jinak: použij oficiální Ookla CLI s správným parsováním
    """
    if not ENABLE_SPEEDTEST:
        return {"download_mbps": 0.0, "upload_mbps": 0.0, "ping_ms": float(aggregated_rtt_ms or 0.0)}

    try:
        # Kontrola existence speedtest binary
        which_cmd = subprocess.run(["which", SPEEDTEST_BIN], capture_output=True, text=True)
        if which_cmd.returncode != 0:
            raise RuntimeError(f"Speedtest binary '{SPEEDTEST_BIN}' not found in PATH")

        # Spuštění Ookla speedtest
        cmd = [SPEEDTEST_BIN, "--format=json", "--progress=no", "--accept-license", "--accept-gdpr", *SPEEDTEST_EXTRA]
        
        print(f"Running speedtest command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if result.returncode != 0:
            print(f"Speedtest error (code {result.returncode}): {result.stderr}")
            raise RuntimeError(f"Speedtest failed with code {result.returncode}: {result.stderr}")
        
        # Parse JSON výstupu
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"Failed to parse speedtest JSON: {e}")
            print(f"Raw output: {result.stdout}")
            raise RuntimeError(f"Invalid JSON from speedtest: {e}")
        
        # Ookla speedtest vrací rychlosti v BYTES per second
        # Musíme převést na Mbps (megabits per second)
        down_bytes = data.get("download", {}).get("bandwidth")
        up_bytes = data.get("upload", {}).get("bandwidth")
        ping_val = data.get("ping", {}).get("latency")
        
        # Převod bytes/s na Mbps: (bytes * 8) / 1_000_000
        down_mbps = (down_bytes * 8 / 1_000_000) if isinstance(down_bytes, (int, float)) else None
        up_mbps = (up_bytes * 8 / 1_000_000) if isinstance(up_bytes, (int, float)) else None
        ping_ms = float(ping_val) if isinstance(ping_val, (int, float)) else None
        
        # Debug log
        print(f"Speedtest results: Download={down_mbps:.2f} Mbps, Upload={up_mbps:.2f} Mbps, Ping={ping_ms:.2f} ms")
        
        if down_mbps is None or up_mbps is None or ping_ms is None:
            raise RuntimeError(f"Incomplete speedtest data: down={down_mbps}, up={up_mbps}, ping={ping_ms}")
        
        # Sanity check - pokud je ping přes 1000ms, použij agregovaný ping
        if ping_ms > 1000 and aggregated_rtt_ms is not None:
            print(f"Warning: Speedtest ping too high ({ping_ms}ms), using aggregated ping ({aggregated_rtt_ms}ms)")
            ping_ms = aggregated_rtt_ms
        
        return {"download_mbps": down_mbps, "upload_mbps": up_mbps, "ping_ms": ping_ms}

    except subprocess.TimeoutExpired:
        print("Speedtest timeout after 120 seconds")
        raise RuntimeError("Speedtest timeout")
    except Exception as e:
        print(f"Speedtest error: {e}")
        # Fallback na aggregated ping pokud speedtest selže
        if aggregated_rtt_ms is not None:
            return {"download_mbps": 0.0, "upload_mbps": 0.0, "ping_ms": aggregated_rtt_ms}
        raise RuntimeError(f"Speedtest failed: {e}")

# =========================
# Ping / jitter / loss - VYLEPŠENÁ VERZE
# =========================
def ping_stats(target: str, count: int, interval_s: float) -> Dict[str, float]:
    """
    Vrátí median RTT (ms), loss (%) a jitter (ms) pro daný target pomocí systémového 'ping'.
    """
    try:
        # Použij -W pro timeout na RPi (kompatibilní s Linux ping)
        cmd = ["ping", "-n", "-c", str(count), "-i", str(interval_s), "-W", "2", target]
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            timeout=20 + int(count * interval_s * 2),
        ).decode(errors="ignore")
    except subprocess.CalledProcessError as e:
        print(f"Ping failed for {target}: {e.output.decode(errors='ignore')}")
        return {"rtt_ms": float("nan"), "loss_pct": 100.0, "jitter_ms": float("nan")}
    except subprocess.TimeoutExpired:
        print(f"Ping timeout for {target}")
        return {"rtt_ms": float("nan"), "loss_pct": 100.0, "jitter_ms": float("nan")}

    times: List[float] = []
    tx = count
    rx = 0
    loss_pct = 100.0

    for line in out.splitlines():
        line = line.strip()
        # Parse ping times
        if " time=" in line:
            try:
                after = line.split(" time=")[1]
                val = after.split(" ")[0].strip()
                ms = float(val)
                times.append(ms)
                rx += 1
            except Exception:
                pass

        # Parse statistics
        if "packets transmitted" in line and "received" in line:
            try:
                parts = line.replace("%", "").split(",")
                tx_str = parts[0].split()[0]
                rx_str = parts[1].split()[0]
                tx = int(tx_str)
                rx = int(rx_str)
                loss_pct = (100.0 * (tx - rx) / tx) if tx > 0 else 100.0
            except Exception:
                pass

    rtt_ms = statistics.median(times) if times else float("nan")
    jitter_ms = statistics.pstdev(times) if len(times) > 1 else 0.0
    
    print(f"Ping to {target}: RTT={rtt_ms:.2f}ms, Loss={loss_pct:.1f}%, Jitter={jitter_ms:.2f}ms")
    
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
        except Exception as e:
            print(f"Exception during ping to {t}: {e}")
            losses.append(100.0)
            jitters.append(float("nan"))
            rtts.append(float("nan"))

    jitter_vals = [j for j in jitters if not math.isnan(j)]
    rtt_vals = [r for r in rtts if not math.isnan(r)]
    
    jitter = statistics.median(jitter_vals) if jitter_vals else 0.0
    loss = statistics.median(losses) if losses else 100.0
    rtt = statistics.median(rtt_vals) if rtt_vals else 0.0
    
    print(f"Aggregated ping: RTT={rtt:.2f}ms, Loss={loss:.1f}%, Jitter={jitter:.2f}ms")
    
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
            
            # Nejdřív ping testy
            pp = aggregate_ping(TARGETS)
            
            # Pak speedtest (pokud je povolen)
            if ENABLE_SPEEDTEST:
                st = run_speedtest(aggregated_rtt_ms=pp["rtt_ms"])
            else:
                st = {"download_mbps": 0.0, "upload_mbps": 0.0, "ping_ms": pp["rtt_ms"]}

            now = datetime.now(timezone.utc).isoformat()
            res = {
                "ts": now,
                "download_mbps": float(st["download_mbps"]),
                "upload_mbps": float(st["upload_mbps"]),
                "ping_ms": float(st["ping_ms"] if ENABLE_SPEEDTEST else pp["rtt_ms"]),
                "jitter_ms": float(pp["jitter_ms"]),
                "packet_loss_pct": float(pp["loss_pct"]),
                "online": True
            }

            dt = time.perf_counter() - t0
            update_metrics(res, dt)
            
            print(f"Measurement complete in {dt:.1f}s: Download={res['download_mbps']:.2f} Mbps, "
                  f"Upload={res['upload_mbps']:.2f} Mbps, Ping={res['ping_ms']:.2f} ms")
            
            return res

        except Exception as e:
            last_err = e
            attempt += 1
            print(f"Measurement attempt {attempt} failed: {e}")
            if attempt > RETRY_MAX:
                break
            time.sleep(backoff + (0.2 * attempt))
            backoff *= 2

    # Pokud všechny pokusy selhaly, vrátíme offline stav
    print(f"All measurement attempts failed. Last error: {last_err}")
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
    print(f"Starting monitoring loop with {WARMUP_RUNS} warmup runs...")
    for i in range(WARMUP_RUNS):
        try:
            print(f"Warmup run {i+1}/{WARMUP_RUNS}")
            _ = measure_once()
        except Exception as e:
            print(f"Warmup run {i+1} failed: {e}")
        time.sleep(2)

    print(f"Starting main monitoring loop with {INTERVAL_SEC}s interval...")
    while True:
        try:
            res = measure_once()
            persist(res)
            prune_old_data()
            _recent.append(res)
        except Exception as e:
            print(f"Error in main loop: {e}")
            errors_total.inc()
        time.sleep(max(5, INTERVAL_SEC))

# =========================
# Homer API endpointy
# =========================
@app.get("/api/homer-status")
def homer_status():
    """Endpoint pro Homer message widget s aktuálním stavem připojení"""
    try:
        latest = list(_recent)[-1] if _recent else None
        
        if not latest:
            return {
                "title": "🔄 Internet Monitor",
                "content": "Žádná data k dispozici - spouštím první měření...",
                "style": "is-light"
            }
        
        ping = latest.get('ping_ms', 0)
        download = latest.get('download_mbps', 0) 
        upload = latest.get('upload_mbps', 0)
        loss = latest.get('packet_loss_pct', 0)
        online = latest.get('online', True)
        jitter = latest.get('jitter_ms', 0)
        
        ping_str = f"{ping:.1f}" if ping and ping > 0 else "--"
        download_str = f"{download:.1f}" if download and download > 0 else "--"
        upload_str = f"{upload:.1f}" if upload and upload > 0 else "--"
        loss_str = f"{loss:.1f}" if loss and loss > 0 else "0.0"
        jitter_str = f"{jitter:.1f}" if jitter and jitter > 0 else "--"
        
        if not online:
            return {
                "title": "❌ Internet je offline",
                "content": "Připojení k internetu není k dispozici",
                "style": "is-danger"
            }
        
        # Upravené limity pro 300/300 připojení
        if (loss and loss > 5) or (download and download < 100) or (ping and ping > 100):
            return {
                "title": "🚨 Kritické problémy s připojením",
                "content": f"📥 {download_str} Mbps • 📤 {upload_str} Mbps • 📡 {ping_str} ms • 📦 {loss_str}% ztráta • ⚡ {jitter_str} ms jitter",
                "style": "is-danger"
            }
        
        elif (loss and loss > 1) or (download and download < 250) or (ping and ping > 50) or (jitter and jitter > 20):
            quality_issues = []
            if loss and loss > 1: quality_issues.append(f"ztráty {loss_str}%")
            if download and download < 250: quality_issues.append(f"pomalý DL")
            if ping and ping > 50: quality_issues.append(f"vysoký ping")
            if jitter and jitter > 20: quality_issues.append(f"vysoký jitter")
            
            issues_text = " • ".join(quality_issues) if quality_issues else ""
            return {
                "title": f"⚠️ Zhoršená kvalita připojení" + (f" ({issues_text})" if issues_text else ""),
                "content": f"📥 {download_str} Mbps • 📤 {upload_str} Mbps • 📡 {ping_str} ms • 📦 {loss_str}% • ⚡ {jitter_str} ms",
                "style": "is-warning"
            }
        
        else:
            if download and download >= 290:
                quality = "Vynikající"
                emoji = "🚀"
            elif download and download >= 250:
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
        
        dl = f"{download:.0f}" if download and download > 0 else "--"
        ul = f"{upload:.0f}" if upload and upload > 0 else "--"
        ping_val = f"{ping:.0f}ms" if ping and ping > 0 else "--"
        
        if not online:
            return {
                "status": "offline",
                "subtitle": "🔴 Offline",
                "emoji": "🔴"
            }
        elif (loss and loss > 5) or (download and download < 100) or (ping and ping > 100):
            return {
                "status": "critical",
                "subtitle": f"🔴 Problémy • ↓{dl} ↑{ul} Mbps • {ping_val}",
                "emoji": "🔴"
            }
        elif (loss and loss > 1) or (download and download < 250) or (ping and ping > 50):
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
        return Response(status_code=204)
    
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
def api_history(
    period: str = Query("day", description="hour|day|week|month"),
    limit: int = Query(288, ge=1, le=1000),
):
    """Get measurement history"""
    if "limit" not in {p.split("=")[0] for p in str(period).split("&")}:
        seconds = {
            "hour": 60*60,
            "day": 24*60*60,
            "week": 7*24*60*60,
            "month": 30*24*60*60
        }.get(period, 24*60*60)
        est_points = max(1, int(seconds / max(5, INTERVAL_SEC)))
        limit = min(est_points, 1000)

    arr = list(_recent)[-limit:]

    data = []
    for item in arr:
        data.append({
            "ts": item["ts"],
            "timestamp": item["ts"],
            "ping_ms": item.get("ping_ms"),
            "ping": item.get("ping_ms"),
            "jitter_ms": item.get("jitter_ms"),
            "download_mbps": item.get("download_mbps"),
            "download": item.get("download_mbps"),
            "upload_mbps": item.get("upload_mbps"),
            "upload": item.get("upload_mbps"),
            "packet_loss": item.get("packet_loss_pct"),
            "online": item.get("online", True),
        })
    return data

@app.post("/api/run")
def api_run():
    """Run manual test"""
    return run_now()

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root() -> str:
    try:
        dashboard_path = "/app/dashboard.html"
        if os.path.exists(dashboard_path):
            with open(dashboard_path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    
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
        </div>
        
        <div class="card">
            <h2>🔗 API Endpointy</h2>
            <a href="/api/latest" class="btn">Latest Result</a>
            <a href="/api/history" class="btn">History</a>
            <a href="/metrics" class="btn">Prometheus Metrics</a>
        </div>
    </div>

    <script>
    async function checkStatus() {
        try {
            const response = await fetch('/api/latest');
            const data = await response.json();
            document.getElementById('status').textContent = data.online ? '✅ Online' : '❌ Offline';
        } catch (e) {
            document.getElementById('status').textContent = '❌ Error';
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
    """Vrátí výsledky z CSV/SQLite v daném časovém intervalu."""
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
        "general": {
            "test_interval": max(1, int(INTERVAL_SEC // 60)) if isinstance(INTERVAL_SEC, (int, float)) else 5,
            "auto_test_enabled": True,
        },
        "tests": {
            "speed_test_enabled": bool(ENABLE_SPEEDTEST),
            "speed_test_interval": 6,
            "ping": {
                "enabled": True,
                "count": int(PING_COUNT),
            },
        },
        "ping_targets": [
            {"name": h, "host": h, "enabled": True} for h in (TARGETS or [])
        ],
    }

@app.post("/api/config")
def set_config(payload: Dict[str, Any] = Body(...)):
    global INTERVAL_SEC, PING_COUNT, TARGETS, ENABLE_SPEEDTEST, SPEEDTEST_EXTRA

    general = payload.get("general", {})
    if "test_interval" in general:
        try:
            minutes = int(general["test_interval"])
            INTERVAL_SEC = max(5, minutes * 60)
        except Exception:
            pass

    tests = payload.get("tests", {})
    if "speed_test_enabled" in tests:
        ENABLE_SPEEDTEST = bool(tests["speed_test_enabled"])
    
    ping_cfg = tests.get("ping", {})
    if "count" in ping_cfg:
        try:
            PING_COUNT = max(1, min(50, int(ping_cfg["count"])))
        except Exception:
            pass

    if "ping_targets" in payload and isinstance(payload["ping_targets"], list):
        new_targets = []
        for t in payload["ping_targets"]:
            host = str(t.get("host") or "").strip()
            enabled = bool(t.get("enabled", True))
            if host and enabled:
                new_targets.append(host)
        TARGETS = new_targets or ["8.8.8.8"]

    if "server_id" in payload:
        sid = payload["server_id"]
        if sid is None or (isinstance(sid, int) and sid <= 0):
            SPEEDTEST_EXTRA = [x for x in SPEEDTEST_EXTRA if x != "--server-id" and not x.isdigit()]
        else:
            cleaned, skip_next = [], False
            for x in SPEEDTEST_EXTRA:
                if skip_next: 
                    skip_next = False
                    continue
                if x == "--server-id":
                    skip_next = True
                    continue
                cleaned.append(x)
            SPEEDTEST_EXTRA = cleaned + ["--server-id", str(int(sid))]

    return {"status": "success", "config": get_config()}

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
    print(f"📊 Configuration:")
    print(f"   - Interval: {INTERVAL_SEC}s")
    print(f"   - Speedtest: {'Enabled' if ENABLE_SPEEDTEST else 'Disabled'}")
    print(f"   - Targets: {', '.join(TARGETS)}")
    print(f"   - Port: {PORT}")
    
    ensure_csv_header()
    init_sqlite()

    import threading
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    print(f"📊 Monitoring thread started")
    print(f"🌐 Web interface: http://localhost:{PORT}")

    uvicorn.run(app, host=BIND, port=PORT)