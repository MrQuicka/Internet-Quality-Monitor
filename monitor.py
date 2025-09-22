@app.route('/')
def dashboard():
    """Jednoduché webové rozhraní"""
    # Načteme HTML z externího souboru nebo použijeme embedded verzi
    html_path = '/app/dashboard.html'
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            return f.read()
    else:
        # Zde by byl celý HTML kód z artifaktu settings-page-html
        # Pro zkrácení používám placeholder
        return """
        <!DOCTYPE html>
        <html>
        <head><title>Internet Monitor</title></head>
        <body>
            <h1>Internet Monitor</h1>
            <p>Pro plné rozhraní zkopírujte dashboard.html do /app/</p>
            <a href="/api/status">API Status</a>
        </body>
        </html>
        """

def run_scheduler():
    """Běh plánovače v samostatném vlákně"""
    while True:
        schedule.run_pending()
        time.sleep(1)

def run_flask():
    """#!/usr/bin/env python3
import os
import time
import schedule
import threading
import logging
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template_string, request, send_file
from flask_cors import CORS
import speedtest
import ping3
import pymysql
from sqlalchemy import create_engine, Column, Integer, Float, DateTime, String, Boolean, desc
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import requests
import statistics
import yaml
import json
import csv
from io import StringIO, BytesIO
import hashlib
from functools import wraps

# Nastavení logování
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Načtení konfigurace
def load_config():
    config_path = '/app/config.yaml'
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    else:
        # Default konfigurace pokud soubor neexistuje
        return {
            'general': {'test_interval': 5, 'auto_test_enabled': True},
            'tests': {
                'speed_test_enabled': True,
                'speed_test_interval': 6,
                'ping': {'enabled': True, 'count': 10, 'timeout': 2}
            },
            'ping_targets': [
                {'name': 'Google DNS', 'host': '8.8.8.8', 'enabled': True},
                {'name': 'Cloudflare DNS', 'host': '1.1.1.1', 'enabled': True},
                {'name': 'Seznam.cz', 'host': '77.75.74.176', 'enabled': True}
            ],
            'web': {'port': 5000, 'dashboard_refresh': 30, 'auth': {'enabled': False}}
        }

def save_config(config):
    config_path = '/app/config.yaml'
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
    logger.info("Konfigurace uložena")

# Globální konfigurace
CONFIG = load_config()

# Databázové připojení
DB_HOST = os.getenv('DB_HOST', 'db')
DB_PORT = os.getenv('DB_PORT', '3306')
DB_USER = os.getenv('DB_USER', 'dochazka_user')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'dochazka_pass')
DB_NAME = os.getenv('DB_NAME', 'dochazka')

DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# SQLAlchemy setup
Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
Session = sessionmaker(bind=engine)

# Flask aplikace
app = Flask(__name__)
CORS(app)

# Databázový model
class InternetMetric(Base):
    __tablename__ = 'internet_metrics'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    ping_google = Column(Float)
    ping_cloudflare = Column(Float)
    ping_seznam = Column(Float)
    download_speed = Column(Float)  # Mbps
    upload_speed = Column(Float)    # Mbps
    packet_loss = Column(Float)     # procenta
    jitter = Column(Float)           # ms
    is_online = Column(Boolean, default=True)
    isp_name = Column(String(100))
    server_location = Column(String(100))
    external_ip = Column(String(50))

class NetworkMonitor:
    def __init__(self):
        self.config = CONFIG
        self.test_count = 0
        self.load_ping_targets()
        self.create_tables()
        
    def load_ping_targets(self):
    def load_ping_targets(self):
        """Načtení ping cílů z konfigurace"""
        self.test_hosts = {}
        for target in self.config.get('ping_targets', []):
            if target.get('enabled', True):
                # Vytvoření bezpečného klíče pro název
                key = target['name'].lower().replace(' ', '_').replace('.', '_')
                self.test_hosts[key] = target['host']

    def create_tables(self):
        """Vytvoření tabulek v databázi"""
        try:
            Base.metadata.create_all(engine)
            logger.info("Databázové tabulky úspěšně vytvořeny/zkontrolovány")
        except Exception as e:
            logger.error(f"Chyba při vytváření tabulek: {e}")
    
    def measure_ping(self, host, count=None):
        """Měření pingu s výpočtem jitteru"""
        if count is None:
            count = self.config.get('tests', {}).get('ping', {}).get('count', 10)
        timeout = self.config.get('tests', {}).get('ping', {}).get('timeout', 2)
        interval = self.config.get('tests', {}).get('ping', {}).get('interval', 0.1)
        
        pings = []
        lost = 0
        
        for _ in range(count):
            try:
                result = ping3.ping(host, timeout=timeout)
                if result is None:
                    lost += 1
                else:
                    pings.append(result * 1000)  # převod na ms
            except Exception:
                lost += 1
            time.sleep(interval)
        
        if pings:
            avg_ping = statistics.mean(pings)
            jitter = statistics.stdev(pings) if len(pings) > 1 else 0
            packet_loss = (lost / count) * 100
            return avg_ping, jitter, packet_loss
        return None, None, 100
    
    def get_external_ip(self):
        """Získání externí IP adresy"""
        try:
            response = requests.get('https://api.ipify.org?format=json', timeout=5)
            return response.json()['ip']
        except:
            return None
    
    def run_speed_test(self):
        """Spuštění testu rychlosti"""
        try:
            logger.info("Spouštím test rychlosti internetu...")
            st = speedtest.Speedtest()
            st.get_best_server()
            
            # Test downloadu
            download = st.download() / 1_000_000  # převod na Mbps
            # Test uploadu
            upload = st.upload() / 1_000_000  # převod na Mbps
            
            # Získání informací o serveru
            server = st.results.server
            isp_info = st.results.client
            
            return {
                'download': round(download, 2),
                'upload': round(upload, 2),
                'server_location': f"{server['name']}, {server['country']}",
                'isp_name': isp_info.get('isp', 'Unknown')
            }
        except Exception as e:
            logger.error(f"Chyba při testu rychlosti: {e}")
            return None
    
    def run_test(self):
        """Hlavní testovací funkce"""
        logger.info("Spouštím kompletní test připojení...")
        self.test_count += 1
        
        metrics = {
            'timestamp': datetime.utcnow(),
            'is_online': False
        }
        
        # Test pingů pokud je povolen
        if self.config.get('tests', {}).get('ping', {}).get('enabled', True):
            ping_results = {}
            jitters = []
            packet_losses = []
            
            for name, host in self.test_hosts.items():
                ping, jitter, loss = self.measure_ping(host)
                if ping is not None:
                    ping_results[f'ping_{name}'] = round(ping, 2)
                    jitters.append(jitter)
                    packet_losses.append(loss)
                    metrics['is_online'] = True
            
            metrics.update(ping_results)
            
            if jitters:
                metrics['jitter'] = round(statistics.mean(jitters), 2)
            if packet_losses:
                metrics['packet_loss'] = round(statistics.mean(packet_losses), 2)
        
        # Test rychlosti (pouze pokud je povolen a je správný interval)
        speed_test_enabled = self.config.get('tests', {}).get('speed_test_enabled', True)
        speed_test_interval = self.config.get('tests', {}).get('speed_test_interval', 6)
        
        if metrics['is_online'] and speed_test_enabled:
            if self.test_count % speed_test_interval == 0:
                speed_results = self.run_speed_test()
                if speed_results:
                    metrics.update(speed_results)
            else:
                logger.info(f"Přeskakuji test rychlosti (test {self.test_count}/{speed_test_interval})")
            
            # Externí IP
            metrics['external_ip'] = self.get_external_ip()
        
        # Uložení do databáze
        self.save_metrics(metrics)
        
        # Kontrola prahových hodnot pro alarmy
        self.check_alerts(metrics)
        
        logger.info(f"Test dokončen: Download: {metrics.get('download', 'N/A')} Mbps, "
                   f"Upload: {metrics.get('upload', 'N/A')} Mbps, "
                   f"Ping: {metrics.get('ping_google_dns', metrics.get('ping_google', 'N/A'))} ms")
        
        return metrics
    
    def check_alerts(self, metrics):
        """Kontrola prahových hodnot pro alarmy"""
        if not self.config.get('alerts', {}).get('enabled', False):
            return
        
        alerts = self.config.get('alerts', {})
        alert_messages = []
        
        # Kontrola rychlosti downloadu
        if metrics.get('download') and metrics['download'] < alerts.get('download_threshold', 10):
            alert_messages.append(f"Nízká rychlost downloadu: {metrics['download']} Mbps")
        
        # Kontrola rychlosti uploadu
        if metrics.get('upload') and metrics['upload'] < alerts.get('upload_threshold', 5):
            alert_messages.append(f"Nízká rychlost uploadu: {metrics['upload']} Mbps")
        
        # Kontrola pingu
        for key, value in metrics.items():
            if key.startswith('ping_') and value and value > alerts.get('ping_threshold', 50):
                alert_messages.append(f"Vysoký ping {key}: {value} ms")
        
        # Kontrola packet loss
        if metrics.get('packet_loss') and metrics['packet_loss'] > alerts.get('packet_loss_threshold', 5):
            alert_messages.append(f"Vysoká ztráta paketů: {metrics['packet_loss']}%")
        
        if alert_messages:
            logger.warning(f"ALARMY: {', '.join(alert_messages)}")
    
    def save_metrics(self, metrics):
        """Uložení metrik do databáze"""
        session = Session()
        try:
            metric = InternetMetric(**metrics)
            session.add(metric)
            session.commit()
            logger.info("Metriky úspěšně uloženy do databáze")
        except Exception as e:
            logger.error(f"Chyba při ukládání do databáze: {e}")
            session.rollback()
        finally:
            session.close()

# Globální instance monitoru
monitor = NetworkMonitor()

# Flask routes
@app.route('/api/status')
def get_status():
    """Získání aktuálního stavu"""
    session = Session()
    try:
        latest = session.query(InternetMetric).order_by(desc(InternetMetric.timestamp)).first()
        if latest:
            # Dynamické získání všech ping hodnot
            ping_data = {}
            for attr in dir(latest):
                if attr.startswith('ping_') and not attr.startswith('_'):
                    value = getattr(latest, attr)
                    if value is not None:
                        ping_data[attr] = value
            
            return jsonify({
                'online': latest.is_online,
                'timestamp': latest.timestamp.isoformat(),
                'download': latest.download_speed,
                'upload': latest.upload_speed,
                'ping': latest.ping_google_dns or latest.ping_google or next(iter(ping_data.values()), None),
                'ping_data': ping_data,
                'packet_loss': latest.packet_loss,
                'jitter': latest.jitter,
                'isp': latest.isp_name,
                'external_ip': latest.external_ip
            })
        return jsonify({'error': 'Žádná data k dispozici'}), 404
    finally:
        session.close()

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    """Získání nebo aktualizace konfigurace"""
    global CONFIG, monitor
    
    if request.method == 'GET':
        return jsonify(CONFIG)
    
    elif request.method == 'POST':
        new_config = request.json
        CONFIG = new_config
        save_config(CONFIG)
        
        # Reload ping targets
        monitor.config = CONFIG
        monitor.load_ping_targets()
        
        # Přeplánování testů s novým intervalem
        schedule.clear()
        test_interval = CONFIG.get('general', {}).get('test_interval', 5)
        if CONFIG.get('general', {}).get('auto_test_enabled', True):
            schedule.every(test_interval).minutes.do(monitor.run_test)
            logger.info(f"Testy přeplánovány na interval {test_interval} minut")
        
        return jsonify({'status': 'success', 'message': 'Konfigurace uložena'})

@app.route('/api/export/<format>')
def export_data(format):
    """Export dat v různých formátech"""
    session = Session()
    try:
        # Získání dat za posledních 7 dní
        since = datetime.utcnow() - timedelta(days=7)
        metrics = session.query(InternetMetric).filter(
            InternetMetric.timestamp >= since
        ).order_by(desc(InternetMetric.timestamp)).all()
        
        if format == 'csv':
            output = StringIO()
            if metrics:
                # Dynamické získání všech sloupců
                fieldnames = [c.name for c in InternetMetric.__table__.columns]
                writer = csv.DictWriter(output, fieldnames=fieldnames)
                writer.writeheader()
                
                for m in metrics:
                    row = {}
                    for field in fieldnames:
                        value = getattr(m, field)
                        if isinstance(value, datetime):
                            row[field] = value.isoformat()
                        else:
                            row[field] = value
                    writer.writerow(row)
            
            output.seek(0)
            return send_file(
                BytesIO(output.getvalue().encode('utf-8')),
                mimetype='text/csv',
                as_attachment=True,
                download_name=f'internet_monitor_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            )
        
        elif format == 'json':
            data = []
            for m in metrics:
                record = {}
                for c in InternetMetric.__table__.columns:
                    value = getattr(m, c.name)
                    if isinstance(value, datetime):
                        record[c.name] = value.isoformat()
                    else:
                        record[c.name] = value
                data.append(record)
            
            return send_file(
                BytesIO(json.dumps(data, indent=2).encode('utf-8')),
                mimetype='application/json',
                as_attachment=True,
                download_name=f'internet_monitor_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
            )
        
        else:
            return jsonify({'error': 'Nepodporovaný formát'}), 400
    
    finally:
        session.close()

@app.route('/api/cleanup', methods=['POST'])
def cleanup_database():
    """Manuální vyčištění databáze"""
    session = Session()
    try:
        retention_days = CONFIG.get('database', {}).get('retention_days', 90)
        if retention_days > 0:
            cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
            deleted = session.query(InternetMetric).filter(
                InternetMetric.timestamp < cutoff_date
            ).delete()
            session.commit()
            return jsonify({
                'status': 'success',
                'deleted': deleted,
                'message': f'Smazáno {deleted} starých záznamů'
            })
        else:
            return jsonify({
                'status': 'info',
                'message': 'Čištění databáze je vypnuto'
            })
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/history/<period>')
def get_history(period):
    """Získání historie (hour, day, week, month)"""
    session = Session()
    try:
        from datetime import timedelta
        
        periods = {
            'hour': timedelta(hours=1),
            'day': timedelta(days=1),
            'week': timedelta(days=7),
            'month': timedelta(days=30)
        }
        
        if period not in periods:
            return jsonify({'error': 'Neplatná perioda'}), 400
        
        since = datetime.utcnow() - periods[period]
        metrics = session.query(InternetMetric).filter(
            InternetMetric.timestamp >= since
        ).order_by(desc(InternetMetric.timestamp)).all()
        
        data = []
        for m in metrics:
            data.append({
                'timestamp': m.timestamp.isoformat(),
                'download': m.download_speed,
                'upload': m.upload_speed,
                'ping': m.ping_google,
                'packet_loss': m.packet_loss,
                'jitter': m.jitter,
                'online': m.is_online
            })
        
        return jsonify(data)
    finally:
        session.close()

@app.route('/api/statistics')
def get_statistics():
    """Získání statistik"""
    session = Session()
    try:
        from datetime import timedelta
        from sqlalchemy import func
        
        # Statistiky za posledních 24 hodin
        since = datetime.utcnow() - timedelta(days=1)
        
        stats = session.query(
            func.avg(InternetMetric.download_speed).label('avg_download'),
            func.min(InternetMetric.download_speed).label('min_download'),
            func.max(InternetMetric.download_speed).label('max_download'),
            func.avg(InternetMetric.upload_speed).label('avg_upload'),
            func.min(InternetMetric.upload_speed).label('min_upload'),
            func.max(InternetMetric.upload_speed).label('max_upload'),
            func.avg(InternetMetric.ping_google).label('avg_ping'),
            func.avg(InternetMetric.packet_loss).label('avg_packet_loss'),
            func.count(InternetMetric.id).label('total_tests'),
            func.sum(InternetMetric.is_online).label('online_tests')
        ).filter(InternetMetric.timestamp >= since).first()
        
        uptime = (stats.online_tests / stats.total_tests * 100) if stats.total_tests > 0 else 0
        
        return jsonify({
            'download': {
                'avg': round(stats.avg_download or 0, 2),
                'min': round(stats.min_download or 0, 2),
                'max': round(stats.max_download or 0, 2)
            },
            'upload': {
                'avg': round(stats.avg_upload or 0, 2),
                'min': round(stats.min_upload or 0, 2),
                'max': round(stats.max_upload or 0, 2)
            },
            'ping': round(stats.avg_ping or 0, 2),
            'packet_loss': round(stats.avg_packet_loss or 0, 2),
            'uptime': round(uptime, 2),
            'total_tests': stats.total_tests
        })
    finally:
        session.close()

@app.route('/api/test')
def trigger_test():
    """Manuální spuštění testu"""
    result = monitor.run_test()
    return jsonify(result)

@app.route('/')
def dashboard():
    """Jednoduché webové rozhraní"""
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Internet Monitor</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f0f0f0; }
            .container { max-width: 1200px; margin: 0 auto; }
            .card { background: white; padding: 20px; margin: 10px 0; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .status { display: flex; justify-content: space-between; flex-wrap: wrap; }
            .metric { flex: 1; min-width: 150px; margin: 10px; text-align: center; }
            .metric .value { font-size: 2em; font-weight: bold; color: #333; }
            .metric .label { color: #666; margin-top: 5px; }
            .online { color: #4CAF50; }
            .offline { color: #f44336; }
            button { background: #2196F3; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; }
            button:hover { background: #1976D2; }
            #chart { height: 400px; margin-top: 20px; }
        </style>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body>
        <div class="container">
            <h1>Monitor kvality internetového připojení</h1>
            
            <div class="card">
                <h2>Aktuální stav</h2>
                <div id="currentStatus" class="status">
                    <div class="metric">
                        <div class="value">--</div>
                        <div class="label">Status</div>
                    </div>
                    <div class="metric">
                        <div class="value">-- Mbps</div>
                        <div class="label">Download</div>
                    </div>
                    <div class="metric">
                        <div class="value">-- Mbps</div>
                        <div class="label">Upload</div>
                    </div>
                    <div class="metric">
                        <div class="value">-- ms</div>
                        <div class="label">Ping</div>
                    </div>
                    <div class="metric">
                        <div class="value">-- %</div>
                        <div class="label">Packet Loss</div>
                    </div>
                </div>
                <button onclick="runTest()">Spustit test nyní</button>
            </div>
            
            <div class="card">
                <h2>Historie (posledních 24 hodin)</h2>
                <canvas id="chart"></canvas>
            </div>
            
            <div class="card">
                <h2>Statistiky (24h)</h2>
                <div id="statistics" class="status"></div>
            </div>
        </div>
        
        <script>
            let chart = null;
            
            async function loadStatus() {
                try {
                    const response = await fetch('/api/status');
                    const data = await response.json();
                    
                    const statusDiv = document.getElementById('currentStatus');
                    statusDiv.innerHTML = `
                        <div class="metric">
                            <div class="value ${data.online ? 'online' : 'offline'}">${data.online ? 'Online' : 'Offline'}</div>
                            <div class="label">Status</div>
                        </div>
                        <div class="metric">
                            <div class="value">${data.download || '--'} Mbps</div>
                            <div class="label">Download</div>
                        </div>
                        <div class="metric">
                            <div class="value">${data.upload || '--'} Mbps</div>
                            <div class="label">Upload</div>
                        </div>
                        <div class="metric">
                            <div class="value">${data.ping || '--'} ms</div>
                            <div class="label">Ping</div>
                        </div>
                        <div class="metric">
                            <div class="value">${data.packet_loss || '--'} %</div>
                            <div class="label">Packet Loss</div>
                        </div>
                    `;
                } catch (e) {
                    console.error('Error loading status:', e);
                }
            }
            
            async function loadHistory() {
                try {
                    const response = await fetch('/api/history/day');
                    const data = await response.json();
                    
                    const ctx = document.getElementById('chart').getContext('2d');
                    
                    if (chart) {
                        chart.destroy();
                    }
                    
                    chart = new Chart(ctx, {
                        type: 'line',
                        data: {
                            labels: data.map(d => new Date(d.timestamp).toLocaleTimeString()),
                            datasets: [{
                                label: 'Download (Mbps)',
                                data: data.map(d => d.download),
                                borderColor: 'rgb(75, 192, 192)',
                                tension: 0.1
                            }, {
                                label: 'Upload (Mbps)',
                                data: data.map(d => d.upload),
                                borderColor: 'rgb(255, 99, 132)',
                                tension: 0.1
                            }, {
                                label: 'Ping (ms)',
                                data: data.map(d => d.ping),
                                borderColor: 'rgb(54, 162, 235)',
                                tension: 0.1,
                                yAxisID: 'y1'
                            }]
                        },
                        options: {
                            responsive: true,
                            maintainAspectRatio: false,
                            scales: {
                                y: {
                                    type: 'linear',
                                    display: true,
                                    position: 'left',
                                    title: {
                                        display: true,
                                        text: 'Rychlost (Mbps)'
                                    }
                                },
                                y1: {
                                    type: 'linear',
                                    display: true,
                                    position: 'right',
                                    title: {
                                        display: true,
                                        text: 'Ping (ms)'
                                    },
                                    grid: {
                                        drawOnChartArea: false
                                    }
                                }
                            }
                        }
                    });
                } catch (e) {
                    console.error('Error loading history:', e);
                }
            }
            
            async function loadStatistics() {
                try {
                    const response = await fetch('/api/statistics');
                    const data = await response.json();
                    
                    const statsDiv = document.getElementById('statistics');
                    statsDiv.innerHTML = `
                        <div class="metric">
                            <div class="value">${data.uptime}%</div>
                            <div class="label">Uptime</div>
                        </div>
                        <div class="metric">
                            <div class="value">${data.download.avg} Mbps</div>
                            <div class="label">Průměr Download</div>
                        </div>
                        <div class="metric">
                            <div class="value">${data.upload.avg} Mbps</div>
                            <div class="label">Průměr Upload</div>
                        </div>
                        <div class="metric">
                            <div class="value">${data.ping} ms</div>
                            <div class="label">Průměr Ping</div>
                        </div>
                        <div class="metric">
                            <div class="value">${data.total_tests}</div>
                            <div class="label">Počet testů</div>
                        </div>
                    `;
                } catch (e) {
                    console.error('Error loading statistics:', e);
                }
            }
            
            async function runTest() {
                alert('Test byl spuštěn. Může trvat až minutu...');
                try {
                    await fetch('/api/test');
                    setTimeout(() => {
                        loadStatus();
                        loadHistory();
                        loadStatistics();
                    }, 2000);
                } catch (e) {
                    console.error('Error running test:', e);
                }
            }
            
            // Načtení dat při startu
            loadStatus();
            loadHistory();
            loadStatistics();
            
            // Aktualizace každých 30 sekund
            setInterval(() => {
                loadStatus();
                loadHistory();
                loadStatistics();
            }, 30000);
        </script>
    </body>
    </html>
    ''')

def run_scheduler():
    """Běh plánovače v samostatném vlákně"""
    while True:
        schedule.run_pending()
        time.sleep(1)

def run_flask():
    """Běh Flask serveru"""
    port = CONFIG.get('web', {}).get('port', 5000)
    debug = CONFIG.get('web', {}).get('debug', False)
    app.run(host='0.0.0.0', port=port, debug=debug)

def setup_scheduler():
    """Nastavení plánovače podle konfigurace"""
    schedule.clear()
    
    if CONFIG.get('general', {}).get('auto_test_enabled', True):
        test_interval = CONFIG.get('general', {}).get('test_interval', 5)
        schedule.every(test_interval).minutes.do(monitor.run_test)
        logger.info(f"Automatické testy naplánovány každých {test_interval} minut")
    
    # Databázové čištění
    if CONFIG.get('database', {}).get('auto_cleanup', False):
        cleanup_time = CONFIG.get('database', {}).get('cleanup_time', '03:00')
        schedule.every().day.at(cleanup_time).do(cleanup_old_data)
        logger.info(f"Automatické čištění databáze naplánováno na {cleanup_time}")

def cleanup_old_data():
    """Automatické čištění starých dat"""
    session = Session()
    try:
        retention_days = CONFIG.get('database', {}).get('retention_days', 90)
        if retention_days > 0:
            cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
            deleted = session.query(InternetMetric).filter(
                InternetMetric.timestamp < cutoff_date
            ).delete()
            session.commit()
            logger.info(f"Automatické čištění: smazáno {deleted} starých záznamů")
    except Exception as e:
        logger.error(f"Chyba při automatickém čištění: {e}")
        session.rollback()
    finally:
        session.close()

if __name__ == '__main__':
    # Počáteční test při spuštění
    logger.info("Starting Internet Monitor...")
    time.sleep(10)  # Čekání na start databáze
    
    # Inicializace monitoru
    monitor = NetworkMonitor()
    
    # První test
    if CONFIG.get('general', {}).get('auto_test_enabled', True):
        logger.info("Spouštím počáteční test...")
        monitor.run_test()
    
    # Nastavení plánovače
    setup_scheduler()
    
    # Spuštění plánovače v samostatném vlákně
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    logger.info(f"Internet Monitor byl spuštěn")
    logger.info(f"Web interface je dostupný na http://localhost:{CONFIG.get('web', {}).get('port', 5000)}")
    
    # Spuštění Flask serveru
    run_flask()