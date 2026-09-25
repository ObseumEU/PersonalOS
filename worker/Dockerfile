FROM python:3.12-slim

# Codex CLI (the agent's engine) and git for agents that work on repositories.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git ca-certificates \
    && npm install -g @openai/codex \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY pos_worker ./pos_worker
RUN pip install --no-cache-dir .

ENV CODEX_HOME=/codex WORKER_WORKDIR=/work
VOLUME ["/codex", "/work"]
CMD ["python", "-m", "pos_worker"]
