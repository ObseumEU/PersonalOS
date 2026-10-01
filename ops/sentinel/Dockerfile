# pos-sentinel: the standard library plus psycopg (Nexus run numbers) and git (recent commits).
FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sentinel ./sentinel
RUN useradd -r -u 10001 sentinel && mkdir -p /state && chown sentinel /state
USER sentinel
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8097/healthz', timeout=4)"
CMD ["python", "-m", "sentinel"]
