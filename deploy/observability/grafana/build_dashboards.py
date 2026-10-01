"""Build the Obseum platform dashboards (Grafana org "Obseum", folder "Obseum platform").

    python deploy/observability/grafana/build_dashboards.py

writes dashboards/*.json; install.sh copies them into the existing Grafana's provisioning. Edit
here, not in the JSON. Datasources: Prometheus-Platform (uid obs-prometheus, ours) and the
existing Loki-Obseum (tenant "Obseum", uid below, reused as it is).
"""

import json
from pathlib import Path

PROM = {"type": "prometheus", "uid": "obs-prometheus"}
LOKI = {"type": "loki", "uid": "P148A5550C732AD4B"}  # Loki-Obseum (existing, org Obseum)
OUT = Path(__file__).parent / "dashboards"
GRAFANA = "https://grafana.obseum.cloud"
APP_STACKS = ["personalos", "kb", "nexus-process-pilot", "litellm", "langfuse"]

_id = 0


def _next() -> int:
    global _id
    _id += 1
    return _id


def target(expr: str, legend: str = "", ds=PROM, instant: bool = False, ref: str = "A") -> dict:
    t = {"datasource": ds, "expr": expr, "refId": ref, "legendFormat": legend}
    if ds is PROM:
        t.update({"range": not instant, "instant": instant})
    else:
        t.update({"queryType": "range"})
    return t


def panel(kind: str, title: str, targets: list, x: int, y: int, w: int, h: int, unit: str = "", **extra) -> dict:
    p = {"id": _next(), "type": kind, "title": title, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "datasource": targets[0]["datasource"] if targets else PROM, "targets": targets,
         "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []}, "options": {}}
    for k, v in extra.items():
        if k == "thresholds":
            p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": v}
            p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
        elif k == "defaults":
            p["fieldConfig"]["defaults"].update(v)
        else:
            p[k] = v
    return p


def steps(warn: float, crit: float) -> list:
    return [{"color": "green", "value": None}, {"color": "orange", "value": warn}, {"color": "red", "value": crit}]


def ts(title, targets, x, y, w, h, unit="", stack=False, **extra):
    p = panel("timeseries", title, targets, x, y, w, h, unit, **extra)
    p["fieldConfig"]["defaults"]["custom"] = {"lineWidth": 1, "fillOpacity": 10 if not stack else 60,
                                              "stacking": {"mode": "normal" if stack else "none"},
                                              "showPoints": "never"}
    p["options"] = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}}
    return p


def stat(title, expr, x, y, w=4, h=4, unit="percentunit", warn=0.8, crit=0.9, decimals=1):
    p = panel("stat", title, [target(expr, instant=True)], x, y, w, h, unit, thresholds=steps(warn, crit),
              defaults={"decimals": decimals})
    p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "area", "textMode": "value"}
    return p


def bars(title, expr, x, y, w, h, unit, legend="{{container}}"):
    p = panel("bargauge", title, [target(expr, legend, instant=True)], x, y, w, h, unit,
              thresholds=[{"color": "blue", "value": None}])
    p["options"] = {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "minVizHeight": 10}
    return p


def table(title, expr, x, y, w, h, unit="", legend=""):
    p = panel("table", title, [target(expr, legend, instant=True)], x, y, w, h, unit)
    p["targets"][0]["format"] = "table"
    p["transformations"] = [{"id": "organize", "options": {"excludeByName": {"Time": True, "__name__": True,
                                                                               "container_id": True}}}]
    return p


def logs(title, expr, x, y, w, h):
    p = panel("logs", title, [target(expr, ds=LOKI)], x, y, w, h)
    p["options"] = {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True,
                    "prettifyLogMessage": False, "dedupStrategy": "none"}
    return p


def text(title, md, x, y, w, h):
    p = panel("text", title, [], x, y, w, h)
    p.pop("datasource")
    p["options"] = {"mode": "markdown", "content": md}
    return p


def row(title, y):
    return {"id": _next(), "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


def var_query(name, label, query, ds=PROM, multi=False, include_all=False, default=None, regex=""):
    v = {"name": name, "label": label, "type": "query", "datasource": ds, "refresh": 2, "sort": 1,
         "multi": multi, "includeAll": include_all, "regex": regex,
         "query": {"query": query, "refId": "v"} if ds is PROM else {"label": query.split(",")[0], "type": 1,
                                                                      "stream": "", "refId": "v"}}
    if default:
        v["current"] = {"text": default, "value": default}
    return v


def var_custom(name, label, values, default, multi=True, include_all=False):
    vals = default if isinstance(default, list) else [default]
    return {"name": name, "label": label, "type": "custom", "query": ",".join(values), "multi": multi,
            "includeAll": include_all, "current": {"text": vals, "value": vals},
            "options": [{"text": v, "value": v, "selected": v in vals} for v in values]}


def links():
    return [{"title": t, "url": f"{GRAFANA}/d/{uid}", "type": "link", "icon": "dashboard", "targetBlank": False}
            for t, uid in (("Server overview", "obs-server-overview"), ("Apps", "obs-apps"), ("LLM usage", "obs-llm"),
                           ("PersonalOS výkon", "obs-pos-perf"))]


def dashboard(uid, title, panels, variables, tags, time_from="now-6h", refresh="1m"):
    return {"uid": uid, "title": title, "tags": ["obseum-platform", *tags], "timezone": "browser",
            "schemaVersion": 39, "version": 1, "editable": True, "graphTooltip": 1, "refresh": refresh,
            "time": {"from": time_from, "to": "now"}, "links": links(),
            "templating": {"list": variables}, "annotations": {"list": []}, "panels": panels}


# ------------------------------------------------------------------ Server overview

def server_overview():
    h = 'host="$host"'
    p = [
        stat("CPU", f"host:cpu_used:ratio{{{h}}}", 0, 0),
        stat("Memory", f"host:memory_used:ratio{{{h}}}", 4, 0, warn=0.85, crit=0.95),
        stat("Swap", f"host:swap_used:ratio{{{h}}}", 8, 0, warn=0.8, crit=0.9),
        stat("Disk /", f'host:disk_used:ratio{{{h}, mountpoint="/"}}', 12, 0, warn=0.75, crit=0.85),
        stat("Load / CPU", f"host:load1:per_cpu{{{h}}}", 16, 0, unit="none", warn=1, crit=2, decimals=2),
        stat("Root FS read-only", f'max(node_filesystem_readonly{{{h}, mountpoint="/"}})', 20, 0, unit="none",
             warn=0.5, crit=1, decimals=0),
        row("Host", 4),
        ts("CPU, memory, swap", [target(f"host:cpu_used:ratio{{{h}}}", "cpu"),
                                 target(f"host:memory_used:ratio{{{h}}}", "memory", ref="B"),
                                 target(f"host:swap_used:ratio{{{h}}}", "swap", ref="C")],
           0, 5, 12, 8, "percentunit", defaults={"max": 1, "min": 0}),
        ts("Load", [target(f"max(node_load1{{{h}}})", "load1"), target(f"max(node_load5{{{h}}})", "load5", ref="B"),
                    target(f"max(node_load15{{{h}}})", "load15", ref="C")], 12, 5, 12, 8),
        ts("Disk used", [target(f"host:disk_used:ratio{{{h}}}", "{{mountpoint}}")], 0, 13, 8, 7, "percentunit",
           defaults={"max": 1, "min": 0}),
        ts("Disk I/O", [target(f'sum(rate(node_disk_read_bytes_total{{{h}, device!~"loop.*"}}[5m]))', "read"),
                        target(f'sum(rate(node_disk_written_bytes_total{{{h}, device!~"loop.*"}}[5m]))', "write",
                               ref="B")], 8, 13, 8, 7, "Bps"),
        ts("Network", [target(f"sum(rate(node_network_receive_bytes_total{{{h}}}[5m]))", "rx"),
                       target(f"sum(rate(node_network_transmit_bytes_total{{{h}}}[5m]))", "tx", ref="B")],
           16, 13, 8, 7, "Bps"),
        row("Containers", 20),
        bars("Top 12 containers by memory", f"topk(12, container:memory_working_set_bytes{{{h}}})", 0, 21, 8, 11,
             "bytes"),
        ts("Top 8 containers by CPU (cores)", [target(f"topk(8, container:cpu_cores:rate5m{{{h}}})", "{{container}}")],
           8, 21, 16, 11, "none"),
        ts("Memory by stack", [target(f"sum by (stack) (container:memory_working_set_bytes{{{h}}})", "{{stack}}")],
           0, 32, 12, 9, "bytes", stack=True),
        table("Restarts and OOM kills (last hour)",
              f"container:restarts:1h{{{h}}} > 0 or container:oom_events:1h{{{h}}} > 0", 12, 32, 12, 9),
        row("Health checks (blackbox, from .186)", 41),
        ts("Probes up", [target("probe_success", "{{app}}")], 0, 42, 12, 7, "none",
           defaults={"max": 1, "min": 0}),
        ts("Probe latency", [target("probe_duration_seconds", "{{app}}")], 12, 42, 12, 7, "s"),
    ]
    v = [var_query("host", "Host", "label_values(host:cpu_used:ratio, host)", default="svr03")]
    return dashboard("obs-server-overview", "Server overview", p, v, ["hosts"])


# ------------------------------------------------------------------ Apps

def apps():
    sel = 'host="svr03", stack=~"$stack"'
    p = [
        ts("Error log lines by service (per 5 min)",
           [target(f'sum by (stack, service) (count_over_time({{{sel}, level="error"}} [5m]))', "{{stack}}/{{service}}",
                   ds=LOKI)], 0, 0, 12, 8, "none"),
        ts("HTTP 5xx by service (per 5 min)",
           [target(f'sum by (stack, service) (count_over_time({{{sel}, http="5xx"}} [5m]))', "{{stack}}/{{service}}",
                   ds=LOKI)], 12, 0, 12, 8, "none"),
        ts("Warnings by service (per 5 min)",
           [target(f'sum by (stack, service) (count_over_time({{{sel}, level="warn"}} [5m]))', "{{stack}}/{{service}}",
                   ds=LOKI)], 0, 8, 12, 7, "none"),
        ts("Health checks", [target('probe_success{app=~"personalos|knowlage|nexus-api|nexus-web|litellm|langfuse"}',
                                    "{{app}}")], 12, 8, 6, 7, "none", defaults={"max": 1, "min": 0}),
        table("Nexus runs (last hour)", 'label_replace(nexus_runs_failed, "kind", "agent runs failed", "", "") '
              'or label_replace(nexus_runs_finished, "kind", "agent runs finished", "", "") '
              'or label_replace(nexus_workflow_runs_failed, "kind", "workflows failed", "", "")', 18, 8, 6, 7),
        ts("Memory of the app containers",
           [target('container:memory_working_set_bytes{host="svr03", stack=~"$stack"}', "{{container}}")],
           0, 15, 12, 7, "bytes"),
        ts("CPU of the app containers",
           [target('container:cpu_cores:rate5m{host="svr03", stack=~"$stack"}', "{{container}}")],
           12, 15, 12, 7, "none"),
        logs("Log explorer (stack, level and text from the variables above)",
             f'{{{sel}, level=~"$level"}} |~ "(?i)$search"', 0, 22, 24, 16),
    ]
    v = [var_custom("stack", "Stack", APP_STACKS + ["caddy", "nexus-service-monitor", "host"], APP_STACKS[:4]),
         var_custom("level", "Level", ["error", "warn", "info", "debug"], ["error", "warn"]),
         {"name": "search", "label": "Text", "type": "textbox", "query": "", "current": {"text": "", "value": ""}}]
    return dashboard("obs-apps", "Apps", p, v, ["apps", "logs"])


# ------------------------------------------------------------------ LLM usage

def llm():
    p = [
        stat("Spend (range)", "sum(increase(litellm_spend_metric_total[$__range]))", 0, 0, 6, 4, "currencyUSD",
             warn=5, crit=20, decimals=2),
        stat("Requests (range)", "sum(increase(litellm_proxy_total_requests_metric_total[$__range]))", 6, 0, 6, 4,
             "short", warn=1e9, crit=1e9, decimals=0),
        stat("Failed requests (range)", "sum(increase(litellm_proxy_failed_requests_metric_total[$__range]))",
             12, 0, 6, 4, "short", warn=5, crit=20, decimals=0),
        stat("Tokens (range)", "sum(increase(litellm_total_tokens_metric_total[$__range]))", 18, 0, 6, 4, "short",
             warn=1e12, crit=1e12, decimals=0),
        ts("Spend per hour by model", [target("sum by (model) (increase(litellm_spend_metric_total[1h]))",
                                              "{{model}}")], 0, 4, 12, 8, "currencyUSD"),
        ts("Spend per hour by key", [target("sum by (api_key_alias) (increase(litellm_spend_metric_total[1h]))",
                                            "{{api_key_alias}}")], 12, 4, 12, 8, "currencyUSD"),
        ts("Requests by status", [target("sum by (status_code) (rate(litellm_proxy_total_requests_metric_total[5m])) * 60",
                                         "{{status_code}}")], 0, 12, 12, 8, "reqpm"),
        ts("Failures by status and class",
           [target("sum by (exception_status, exception_class) (increase(litellm_proxy_failed_requests_metric_total[15m]))",
                   "{{exception_status}} {{exception_class}}")], 12, 12, 12, 8, "short"),
        bars("Remaining budget by key (USD)", "litellm_remaining_api_key_budget_metric", 0, 20, 12, 10, "currencyUSD",
             legend="{{api_key_alias}}"),
        ts("Average latency by model",
           [target("sum by (model) (rate(litellm_request_total_latency_metric_sum[5m])) / "
                   "sum by (model) (rate(litellm_request_total_latency_metric_count[5m]))", "{{model}}")],
           12, 20, 12, 10, "s"),
        text("Links", "- **Langfuse** (traces, per-call cost): https://langfuse.obseum.cloud\n"
                      "- **LiteLLM** admin UI (keys, budgets, spend logs): https://litellm.obseum.cloud/ui\n"
                      "- Counters restart with the LiteLLM container; ranges use `increase()`, which "
                      "handles the resets.", 0, 30, 8, 7),
        logs("LiteLLM errors and warnings", '{host="svr03", stack="litellm", level=~"error|warn"}', 8, 30, 16, 7),
    ]
    return dashboard("obs-llm", "LLM usage", p, [], ["llm", "litellm"], time_from="now-24h")


# ------------------------------------------------------------------ PersonalOS výkon

def pos_perf():
    """The API's own metrics (pos.metrics, GET /metrics scraped by svr03's Alloy, job "personalos")
    and the web nginx's JSON access log (request_time, upstream_time) in Loki."""
    j = 'job="personalos"'
    rate = f"sum(rate(pos_http_requests_total{{{j}}}[5m]))"
    err = f'sum(rate(pos_http_requests_total{{{j}, status="5xx"}}[5m]))'
    hq = lambda q, by="": (f"histogram_quantile({q}, sum by (le{by}) "  # noqa: E731
                           f"(rate(pos_http_request_duration_seconds_bucket{{{j}, route=~\"$route\"}}[5m])))")
    nginx = '{host="svr03", stack="personalos", service="web"} | json | path=~"/api/.*" | path!~"/api/(chat/)?stream|/api/worker/next"'
    p = [
        stat("p95 (5 min)", hq(0.95), 0, 0, 4, 4, "s", warn=0.3, crit=0.5, decimals=3),
        stat("p50 (5 min)", hq(0.5), 4, 0, 4, 4, "s", warn=0.15, crit=0.3, decimals=3),
        stat("5xx share (5 min)", f"({err} or vector(0)) / {rate}", 8, 0, 4, 4, "percentunit", warn=0.01, crit=0.02,
             decimals=2),
        stat("Requests / min", f"{rate} * 60", 12, 0, 4, 4, "short", warn=1e9, crit=1e9, decimals=0),
        stat("Open live streams", f"sum(pos_live_subscribers{{{j}}})", 16, 0, 4, 4, "short", warn=1e9, crit=1e9,
             decimals=0),
        stat("SQLite locked (range)", f"sum(increase(pos_sqlite_locked_total{{{j}}}[$__range]))", 20, 0, 4, 4, "short",
             warn=1, crit=10, decimals=0),
        row("Latency by route (API, route templates; streams and the worker long-poll not timed)", 4),
        ts("p95 by route (top 10)", [target(f"topk(10, {hq(0.95, ', route')})", "{{route}}")], 0, 5, 12, 9, "s"),
        ts("p50 by route (top 10)", [target(f"topk(10, {hq(0.5, ', route')})", "{{route}}")], 12, 5, 12, 9, "s"),
        ts("Requests / min by route (top 10)",
           [target(f"topk(10, sum by (route) (rate(pos_http_requests_total{{{j}, route=~\"$route\"}}[5m])) * 60)",
                   "{{route}}")], 0, 14, 12, 8, "reqpm"),
        ts("Error share (5xx, 4xx)",
           [target(f"({err} or vector(0)) / {rate}", "5xx"),
            target(f'(sum(rate(pos_http_requests_total{{{j}, status="4xx"}}[5m])) or vector(0)) / {rate}', "4xx",
                   ref="B")], 12, 14, 12, 8, "percentunit"),
        table("Slowest routes (p95 over the range)",
              f"topk(15, histogram_quantile(0.95, sum by (le, route, method) "
              f"(increase(pos_http_request_duration_seconds_bucket{{{j}}}[$__range]))))", 0, 22, 12, 9, "s"),
        ts("Edge time (nginx request_time, /api)",
           [target(f"quantile_over_time(0.95, {nginx} | unwrap request_time [5m]) by ()", "p95", ds=LOKI),
            target(f"quantile_over_time(0.5, {nginx} | unwrap request_time [5m]) by ()", "p50", ds=LOKI, ref="B")],
           12, 22, 12, 9, "s"),
        row("Agents and work", 31),
        ts("Run outcomes per hour", [target(f"sum by (status) (increase(pos_runs_total{{{j}}}[1h]))", "{{status}}")],
           0, 32, 8, 8, "short", stack=True),
        ts("Runs running", [target(f"sum(pos_runs_running{{{j}}})", "running")], 8, 32, 4, 8, "short"),
        ts("Cost today (USD, UTC day)", [target(f"max(pos_cost_usd_today{{{j}}})", "today")], 12, 32, 6, 8,
           "currencyUSD"),
        bars("Cost per day (USD)", f"max_over_time(max(pos_cost_usd_today{{{j}}})[1d:5m])", 18, 32, 6, 8,
             "currencyUSD", legend="last 24 h peak"),
        ts("Review queue and Čeká na tebe",
           [target(f"max(pos_review_queue{{{j}}})", "in review"),
            target(f"max by (kind) (pos_needs_me{{{j}}})", "needs me: {{kind}}", ref="B")], 0, 40, 12, 8, "short"),
        ts("SQLite lock errors (log lines and failed requests, per 5 min)",
           [target('sum(count_over_time({host="svr03", stack="personalos"} |= "database is locked" [5m]))', "log lines",
                   ds=LOKI),
            target(f"sum(increase(pos_sqlite_locked_total{{{j}}}[5m]))", "failed requests", ref="B")],
           12, 40, 12, 8, "short"),
        logs("PersonalOS errors (api, worker, deployer)",
             '{host="svr03", stack="personalos", level="error"} |~ "(?i)$search"', 0, 48, 24, 12),
    ]
    v = [var_query("route", "Route", f"label_values(pos_http_requests_total{{{j}}}, route)", multi=True,
                   include_all=True, default="$__all"),
         {"name": "search", "label": "Text", "type": "textbox", "query": "", "current": {"text": "", "value": ""}}]
    v[0]["allValue"] = ".*"
    return dashboard("obs-pos-perf", "PersonalOS výkon", p, v, ["personalos", "performance"], time_from="now-24h")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for d in (server_overview(), apps(), llm(), pos_perf()):
        (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                                              newline="\n")
        print("wrote", d["uid"])


if __name__ == "__main__":
    main()
