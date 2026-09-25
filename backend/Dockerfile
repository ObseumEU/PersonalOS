FROM python:3.12-slim

# Claude Code CLI for the server's own AI calls (Inbox suggestions); it logs in
# with CLAUDE_CODE_OAUTH_TOKEN. Without it the app falls back to rules.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV POS_DATA_DIR=/data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "pos.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
