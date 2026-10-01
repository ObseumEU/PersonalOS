# The agents' own computer (docs/SANDBOX.md): one container per agent, made from this image by the
# sandbox manager (ops/sandbox). Root inside, anything may be installed; the isolation is outside
# (no capabilities beyond file ownership, no host mounts, an internal network whose only way out is
# the manager's egress proxy, which refuses the LAN).
#
# Built on the server once (and when this file changes):
#   docker build -t pos-agent-sandbox:latest ops/sandbox/image
FROM python:3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=Europe/Prague
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git curl wget ca-certificates jq sqlite3 unzip zip xz-utils file less nano vim-tiny \
        procps psmisc tree rsync tini \
        graphviz ffmpeg imagemagick pandoc poppler-utils ghostscript \
        fonts-dejavu fonts-liberation fonts-noto-color-emoji \
        chromium nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Python: data, charts, documents, the web.
RUN pip install --no-cache-dir \
        pandas numpy scipy matplotlib seaborn plotly kaleido==0.2.1 networkx graphviz pydot \
        pillow openpyxl xlsxwriter python-docx python-pptx reportlab pypdf markdown jinja2 \
        requests httpx beautifulsoup4 lxml pyyaml tabulate rich ipython

# Mermaid CLI (mmdc) on Debian's Chromium (no bundled browser download).
ENV PUPPETEER_SKIP_DOWNLOAD=1 PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
RUN npm install -g --no-audit --no-fund @mermaid-js/mermaid-cli@11 \
    && printf '{"executablePath":"/usr/bin/chromium","args":["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"]}\n' \
        > /etc/puppeteer.json \
    && printf '#!/bin/sh\nexec /usr/local/bin/mmdc-real -p /etc/puppeteer.json "$@"\n' > /usr/local/bin/mmdc.wrap \
    && mv "$(command -v mmdc)" /usr/local/bin/mmdc-real && mv /usr/local/bin/mmdc.wrap /usr/local/bin/mmdc \
    && chmod +x /usr/local/bin/mmdc

# matplotlib without a display; ImageMagick may write PDF/SVG (the default policy forbids it).
ENV MPLBACKEND=Agg
RUN sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' /etc/ImageMagick-6/policy.xml || true

COPY motd.txt /etc/pos-sandbox.txt
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
