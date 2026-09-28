"""What the sentinel watches and its thresholds.

The defaults describe svr03 (PersonalOS, knowlage, Nexus, LiteLLM, Langfuse).
A JSON file (SENTINEL_CONFIG, default /app/sentinel.json) is merged over them
key by key; URLs and secrets come from the environment.
"""

import copy
import fnmatch
import json
import os

DEFAULTS: dict = {
    "interval_s": 60,
    "state_dir": "/state",
    # container name glob → service
    "services": {
        "personalos-*": "personalos",
        "nexus-process-pilot-*": "nexus",
        "kb-*": "knowlage",
        "litellm*": "litellm",
        "langfuse*": "langfuse",
    },
    # containers whose state (running, restarts, OOM, health) is watched
    "containers": ["personalos-*", "nexus-process-pilot-*", "kb-*", "litellm", "litellm-postgres",
                   "langfuse-web", "langfuse-postgres"],
    # containers whose logs are fingerprinted (and counted for 5xx / 429 / 401 / quota)
    "logs": ["personalos-*", "nexus-process-pilot-api-1", "nexus-process-pilot-runtime-1",
             "nexus-process-pilot-codex-shim-1", "nexus-process-pilot-web-1", "kb-kb-1", "kb-web-1", "kb-caddy-1",
             "litellm", "langfuse-web"],
    "ignore": ["*sentinel*", "*docker-proxy*", "*buildkit*"],
    "http": [
        # public: through the front proxy with SNI (TLS expiry is read on the way)
        {"name": "personalos-web", "service": "personalos", "url": "https://personalos.obseum.cz/",
         "connect": "caddy-proxy:443", "restart": "personalos-web-1"},
        {"name": "knowlage-web", "service": "knowlage", "url": "https://knowlage.obseum.cz/api/health",
         "connect": "caddy-proxy:443"},
        {"name": "nexus-web", "service": "nexus", "url": "https://nexus.obseum.cloud/", "connect": "caddy-proxy:443"},
        {"name": "nexus-api-public", "service": "nexus", "url": "https://nexus-api.obseum.cloud/healthz",
         "connect": "caddy-proxy:443"},
        # internal health endpoints
        {"name": "personalos-api", "service": "personalos", "url": "{pos_url}/api/health",
         "restart": "personalos-api-1"},
        {"name": "knowlage-api", "service": "knowlage", "url": "http://knowlage:8080/api/health", "json_level": "sync"},
        {"name": "nexus-web-internal", "service": "nexus", "url": "http://nexus-web:8080/sign-in",
         "restart": "nexus-process-pilot-web-1"},
        {"name": "nexus-api", "service": "nexus", "url": "http://nexus-api:3001/readyz",
         "restart": "nexus-process-pilot-api-1"},
        {"name": "litellm", "service": "litellm", "url": "http://litellm:4000/health/liveliness", "restart": "litellm"},
        {"name": "langfuse", "service": "langfuse", "url": "http://langfuse-web:3000/api/public/health",
         "restart": "langfuse-web"},
    ],
    "runbook": {
        # stateless containers only: never databases, queues or anything holding data in memory
        "restart_allowlist": ["personalos-web-1", "personalos-api-1", "nexus-process-pilot-web-1",
                              "nexus-process-pilot-api-1", "kb-web-1", "litellm", "langfuse-web"],
        "cooldown_min": 30,
        "grace_s": 150,
    },
    "thresholds": {
        "health_fails": 3,             # consecutive failed checks before an incident
        "down_streak": 3,              # ticks a watched container is not running
        "unhealthy_streak": 3,
        "restart_loop": 3,             # restarts within restart_window_min
        "restart_window_min": 15,
        "tls_days_warn": 14,
        "tls_days_high": 5,
        "disk_pct": 90, "disk_pct_critical": 96,
        "mem_avail_pct": 5,
        "swap_pct": 90,
        "load_per_cpu": 3.0,
        "run_fail_ratio": 0.5, "run_min": 5,   # failed share of finished runs in the last hour
        "window_min": 5,               # the log/status window
        "new_fp_min": 5,               # a new error fingerprint: at least this many in the window
        "spike_factor": 10.0, "spike_min_per_min": 5.0,
        "http_5xx_min": 20, "http_5xx_ratio": 0.2,
        "http_429_min": 20,
        "quota_min": 3,
        "auth_401_min": 100,
        "budget_ratio": 0.9,
        "backup_warn_h": 26, "backup_fail_h": 48,   # age of the newest file in a backup directory
        "warmup_min": 30,              # learn fingerprints without opening incidents after a fresh start
        "quiet_min": 30,               # auto-resolve after this long without a new observation
        "quiet_min_health": 10,
    },
    "apps_every_min": 5,
    "tls_every_min": 60,
    # backup name → directory in the container (read-only mounts, see docs/SENTINEL.md)
    "backups": {"personalos": "/backups/personalos", "knowlage": "/backups/knowlage", "nexus": "/backups/nexus"},
    "backups_every_min": 15,
    "repos": {"personalos": "/repos/personalos", "nexus": "/repos/nexus"},
}


def load(path: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = path or os.environ.get("SENTINEL_CONFIG", "/app/sentinel.json")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    env = os.environ.get
    cfg["pos_url"] = env("SENTINEL_POS_URL", "http://api:8000").rstrip("/")
    cfg["token"] = env("SENTINEL_TOKEN", "")
    cfg["docker_host"] = env("DOCKER_HOST", "unix:///var/run/docker.sock")
    cfg["nexus_dsn"] = env("SENTINEL_NEXUS_DSN", "")
    cfg["knowlage_url"] = env("SENTINEL_KNOWLAGE_URL", "http://knowlage:8080").rstrip("/")
    cfg["litellm_url"] = env("SENTINEL_LITELLM_URL", "http://litellm:4000").rstrip("/")
    cfg["litellm_key"] = env("SENTINEL_LITELLM_KEY", "")
    cfg["test_hook"] = env("SENTINEL_TEST_HOOK", "0") == "1"
    cfg["state_dir"] = env("SENTINEL_STATE_DIR", cfg["state_dir"])
    cfg["listen"] = env("SENTINEL_LISTEN", "0.0.0.0:8097")
    return cfg


def _merge(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def matches(name: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, g) for g in globs)


def service_of(cfg: dict, container: str) -> str:
    for glob, service in cfg["services"].items():
        if fnmatch.fnmatchcase(container, glob):
            return service
    return container
