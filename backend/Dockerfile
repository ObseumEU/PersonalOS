FROM python:3.14-slim

# Claude Code CLI for the server's own AI calls (Inbox suggestions); it logs in
# with CLAUDE_CODE_OAUTH_TOKEN. Without it the app falls back to rules.
# The current Claude Code: older versions do not know claude-opus-5-5. The build
# fails below the version verified with it (CLAUDE_MIN).
ARG CLAUDE_MIN=2.1.282
# graphviz: DOT files agents share are drawn to SVG here (pos.file_render).
# git: review packets and the evidence gate read the mounted repositories (POS_REVIEW_REPOS).
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates graphviz git \
    && npm install -g @anthropic-ai/claude-code@latest \
    && rm -rf /var/lib/apt/lists/* \
    && v="$(claude --version | cut -d' ' -f1)" \
    && [ "$(printf '%s\n%s\n' "$CLAUDE_MIN" "$v" | sort -V | head -1)" = "$CLAUDE_MIN" ] \
    || { echo "Claude Code $v is older than $CLAUDE_MIN"; exit 1; }

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# onepassword-sdk: agents' credentials from 1Password (pos.credentials; needs glibc >= 2.32, OpenSSL 3).
# pypdf: text of PDFs (pos.files, and invoice filing reads the invoice's issue date and buyer: pos.invoices).
RUN pip install --no-cache-dir ".[onepassword,pdf]"

ENV POS_DATA_DIR=/data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "pos.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
