# The sandbox manager (docs/SANDBOX.md): the control API the PersonalOS api calls, and the egress
# proxy that is the sandboxes' only way out. It holds the Docker socket; nothing else here does.
FROM python:3.12-slim
RUN pip install --no-cache-dir "docker>=7.1"
WORKDIR /app
COPY manager.py ./
EXPOSE 8200 3128
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/health')"
CMD ["python", "-u", "manager.py"]
