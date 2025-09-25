FROM python:3.12-slim

WORKDIR /app

# systémové nástroje: ping + curl + certifikáty + gnupg (repo pro Ookla)
RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping curl ca-certificates gnupg \
 && rm -rf /var/lib/apt/lists/*

# instalace Ookla speedtest CLI
RUN set -eux; \
    curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash; \
    apt-get update; \
    apt-get install -y --no-install-recommends speedtest; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

EXPOSE 5001
CMD ["python", "iqm_agent.py"]
