#!/bin/bash

# Internet Monitor Setup Script
# Tento skript automaticky vytvoří strukturu složek a soubory

echo "🚀 Instalace Internet Monitor"
echo "=============================="

# Vytvoření složek
echo "📁 Vytváření struktury složek..."
mkdir -p internet-monitor/{data,logs,exports}

# Vytvoření souborů
echo "📄 Vytváření souborů..."

# Dockerfile
cat > internet-monitor/Dockerfile << 'EOF'
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    iputils-ping \
    curl \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Run the application
CMD ["python", "-u", "monitor.py"]
EOF

# requirements.txt
cat > internet-monitor/requirements.txt << 'EOF'
speedtest-cli==2.1.3
pymysql==1.1.0
SQLAlchemy==2.0.23
flask==3.0.0
flask-cors==4.0.0
ping3==4.0.4
schedule==1.2.0
requests==2.31.0
python-dotenv==1.0.0
pyyaml==6.0.1
EOF

echo "📝 Vytváření monitor.py (stáhněte z artifaktu)..."
echo "📝 Vytváření config.yaml (stáhněte z artifaktu)..."
echo "📝 Vytváření dashboard.html (stáhněte z artifaktu)..."

# Upozornění
echo ""
echo "⚠️  DŮLEŽITÉ: Musíte ručně zkopírovat následující soubory:"
echo "   1. monitor.py - hlavní aplikace"
echo "   2. config.yaml - konfigurační soubor"
echo "   3. dashboard.html - webové rozhraní"
echo ""
echo "✅ Struktura složek byla vytvořena!"
echo ""
echo "📋 Další kroky:"
echo "   1. Zkopírujte soubory monitor.py, config.yaml a dashboard.html do složky internet-monitor/"
echo "   2. Přidejte službu do vašeho docker-compose.yml"
echo "   3. Spusťte: docker-compose up -d internet_monitor"
echo "   4. Otevřete: http://YOUR_SERVER_IP:5000"
echo ""

# Nastavení oprávnění
chmod -R 755 internet-monitor/
echo "✅ Instalace dokončena!"