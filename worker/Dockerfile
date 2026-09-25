FROM python:3.12-slim

# Claude Code CLI (default engine), Codex CLI (fallback) and git for repositories.
# The current Claude Code: older versions do not know claude-opus-5-5. The build
# fails below the version verified with it (CLAUDE_MIN).
ARG CLAUDE_MIN=2.1.282
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git ca-certificates \
    && npm install -g @anthropic-ai/claude-code@latest @openai/codex@latest \
    && rm -rf /var/lib/apt/lists/* \
    && v="$(claude --version | cut -d' ' -f1)" \
    && [ "$(printf '%s\n%s\n' "$CLAUDE_MIN" "$v" | sort -V | head -1)" = "$CLAUDE_MIN" ] \
    || { echo "Claude Code $v is older than $CLAUDE_MIN"; exit 1; }

# The guarded browser (browser:use, pos_worker.browser_guard): Playwright MCP with
# its headless Chromium and the system libraries it needs, so it runs in the container.
# (browser_guard adds --isolated --headless; Chromium comes from the Playwright
# version @playwright/mcp itself depends on.)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_MCP="playwright-mcp --browser chromium --no-sandbox"
RUN npm install -g @playwright/mcp@latest \
    && cd "$(npm root -g)/@playwright/mcp" && npx --no-install playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /app
COPY pyproject.toml ./
COPY pos_worker ./pos_worker
RUN pip install --no-cache-dir .

ENV CODEX_HOME=/codex WORKER_WORKDIR=/work
VOLUME ["/codex", "/work"]
CMD ["python", "-m", "pos_worker"]
