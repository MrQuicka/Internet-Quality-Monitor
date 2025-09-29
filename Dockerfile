FROM python:3.12-slim

WORKDIR /app

# Systémové závislosti včetně Ookla speedtest
RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

# Instalace oficiálního Ookla Speedtest CLI
RUN curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash \
    && apt-get update \
    && apt-get install -y speedtest \
    && rm -rf /var/lib/apt/lists/*

# Předběžné přijetí licence Ookla speedtest
RUN speedtest --accept-license --accept-gdpr || true

# Python závislosti
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Aplikační soubory
COPY . ./

# Vytvoření data složky
RUN mkdir -p /app/data

EXPOSE 5001
CMD ["python", "-u", "iqm_agent.py"]