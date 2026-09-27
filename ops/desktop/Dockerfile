# The desktop sandbox for computer use (tool:computer; docs/BROWSER.md).
# Idle it is one small Python process; a session starts Xvfb, fluxbox and Chromium
# lazily and removes everything when it ends. One session at a time.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11-utils fluxbox chromium xdotool imagemagick python3 python3-websocket \
        fonts-dejavu-core fonts-liberation ca-certificates procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 desktop

COPY desktop_server.py /app/desktop_server.py
USER desktop
ENV DESKTOP_PORT=8100 DESKTOP_ROOT=/tmp/desktop
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=3)"
CMD ["python3", "/app/desktop_server.py"]
