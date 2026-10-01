"""HTML overviews for the owner (T-416): an agent sends structured data with the
report_html tool, PersonalOS renders it server-side from one fixed template.

No HTML or script from the agent ever reaches the page: every field is plain
text, escaped by Jinja's autoescape, and the charts are SVG drawn here from
numbers. The page lives at /reports/<uuid> behind the web app's login (the same
session cookie as the rest of the UI); there is no public or token link.
"""

import json
import os
import sqlite3
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import audit, comments, tasks
from .config import Settings, get_settings
from .core import Ctx, now_iso
from .db import connect

Status = Literal["green", "amber", "red"]
CSP = "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"


class Kpi(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    value: str = Field(max_length=40)
    delta: str | None = Field(default=None, max_length=40)
    status: Status | None = None

    @field_validator("value", "delta", mode="before")
    @classmethod
    def _text(cls, v):
        return None if v is None else str(v)


class Series(BaseModel):
    name: str = Field(max_length=60)
    values: list[float] = Field(max_length=24)


class Chart(BaseModel):
    type: Literal["bar", "line"]
    title: str = Field(max_length=120)
    labels: list[str] = Field(min_length=1, max_length=24)
    series: list[Series] = Field(min_length=1, max_length=4)

    @field_validator("labels")
    @classmethod
    def _labels(cls, v):
        return [str(x)[:30] for x in v]


class Section(BaseModel):
    heading: str = Field(max_length=120)
    bullets: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("bullets")
    @classmethod
    def _bullets(cls, v):
        return [str(x)[:400] for x in v]


class Report(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=600)
    status: Status
    kpis: list[Kpi] = Field(default_factory=list, max_length=5)
    charts: list[Chart] = Field(default_factory=list, max_length=2)
    sections: list[Section] = Field(default_factory=list, max_length=6)
    next: list[str] = Field(default_factory=list, max_length=8)
    asks: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("next", "asks")
    @classmethod
    def _items(cls, v):
        return [str(x)[:400] for x in v]


def validate(data: dict) -> Report:
    """The report, or tasks.Invalid naming the first broken field (e.g. 6 KPIs)."""
    try:
        report = Report.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        where = ".".join(str(p) for p in err["loc"])
        raise tasks.Invalid(f"report_html: {where}: {err['msg']}") from e
    for ch in report.charts:
        for s in ch.series:
            if len(s.values) != len(ch.labels):
                raise tasks.Invalid(f"report_html: chart '{ch.title}': series '{s.name}' needs "
                                    f"{len(ch.labels)} values, one per label")
    return report


def url_for(report_id: str) -> str:
    base = (os.environ.get("POS_PUBLIC_URL") or "").rstrip("/")
    return f"{base}/reports/{report_id}"


def create(conn: sqlite3.Connection, ctx: Ctx, data: dict, task_id: int | None = None) -> dict:
    """Store a report (the caller commits); with a task, its link goes to the task's activity."""
    report = validate(data)
    if task_id is not None:
        tasks._row(conn, ctx, task_id)  # read access to the task
    rid = str(uuid.uuid4())
    conn.execute("INSERT INTO html_reports (id, agent_id, task_id, title, payload, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (rid, ctx.actor_id, task_id, report.title, report.model_dump_json(), now_iso()))
    url = url_for(rid)
    if task_id is not None:
        comments.add(conn, ctx, task_id, f"HTML přehled: [{report.title}]({url})", kind="system")
    audit.log(conn, ctx, "html_report_create", "html_report", None, report_id=rid, task_id=task_id)
    return {"id": rid, "url": url}


# ------------------------------------------------------------- charts

PALETTE = ("#2563eb", "#f59e0b", "#10b981", "#ef4444")
W, H, LEFT, RIGHT, TOP, BOTTOM = 600, 260, 48, 12, 12, 48


def _fmt(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") if abs(v) >= 100 or v == int(v) else f"{v:.2f}".rstrip("0").rstrip(".")


def chart_geometry(chart: Chart) -> dict:
    """Numbers for the SVG in the template: axis ticks, bars or line points."""
    values = [v for s in chart.series for v in s.values]
    lo, hi = min(0.0, *values), max(0.0, *values)
    if hi == lo:
        hi = lo + 1
    pw, ph = W - LEFT - RIGHT, H - TOP - BOTTOM

    def y(v: float) -> float:
        return round(TOP + ph * (hi - v) / (hi - lo), 1)

    n = len(chart.labels)
    slot = pw / n
    ticks = [{"y": y(lo + (hi - lo) * i / 4), "text": _fmt(lo + (hi - lo) * i / 4)} for i in range(5)]
    labels = [{"x": round(LEFT + slot * (i + 0.5), 1), "text": t} for i, t in enumerate(chart.labels)]
    out = {"w": W, "h": H, "left": LEFT, "right": W - RIGHT, "zero": y(0), "ticks": ticks, "labels": labels,
           "label_y": H - BOTTOM + 18, "bars": [], "lines": [],
           "legend": [{"name": s.name, "color": PALETTE[i]} for i, s in enumerate(chart.series)]}
    if chart.type == "bar":
        bw = slot * 0.8 / len(chart.series)
        for si, s in enumerate(chart.series):
            for i, v in enumerate(s.values):
                top, bottom = sorted((y(v), y(0)))
                out["bars"].append({"x": round(LEFT + slot * (i + 0.1) + bw * si, 1), "y": top,
                                    "w": round(bw, 1), "h": round(max(bottom - top, 0.5), 1),
                                    "color": PALETTE[si], "title": f"{s.name} {chart.labels[i]}: {_fmt(v)}"})
    else:
        for si, s in enumerate(chart.series):
            pts = [(round(LEFT + slot * (i + 0.5), 1), y(v)) for i, v in enumerate(s.values)]
            out["lines"].append({"points": " ".join(f"{px},{py}" for px, py in pts), "color": PALETTE[si],
                                 "dots": [{"x": px, "y": py, "title": f"{s.name} {chart.labels[i]}: "
                                           f"{_fmt(s.values[i])}"} for i, (px, py) in enumerate(pts)]})
    return out


# ------------------------------------------------------------- page

STATUS_TEXT = {"green": "V pořádku", "amber": "Pozor", "red": "Problém"}
STATUS_COLOR = {"green": "#16a34a", "amber": "#d97706", "red": "#dc2626"}

TEMPLATE = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{{ r.title }}</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#111827;background:#f3f4f6}
main{max-width:860px;margin:0 auto;padding:16px}
header{display:flex;gap:12px;align-items:flex-start;margin-bottom:12px}
.light{flex:none;width:22px;height:22px;border-radius:50%;margin-top:6px}
h1{font-size:1.4rem;margin:0}
.meta{color:#6b7280;font-size:.85rem}
.summary{font-size:1.05rem;margin:4px 0 16px}
.badge{display:inline-block;padding:1px 10px;border-radius:999px;color:#fff;font-size:.85rem;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.kpi{background:#fff;border-radius:10px;padding:12px;border-top:4px solid #d1d5db}
.kpi .label{color:#6b7280;font-size:.85rem}
.kpi .value{font-size:1.5rem;font-weight:700}
.kpi .delta{font-size:.85rem;color:#374151}
.card{background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:14px}
h2{font-size:1.1rem;margin:0 0 8px}
ul{margin:0;padding-left:1.2em}
li{margin:2px 0}
svg{width:100%;height:auto;display:block}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:.85rem;margin-top:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
.asks{border-left:4px solid #2563eb}
@media (max-width:600px){.kpis{grid-template-columns:repeat(2,1fr)}main{padding:10px}h1{font-size:1.2rem}}
</style>
</head>
<body>
<main>
<header>
<span class="light" style="background:{{ color[r.status] }}" title="{{ status_text[r.status] }}"></span>
<div>
<h1>{{ r.title }}</h1>
<div class="meta"><span class="badge" style="background:{{ color[r.status] }}">{{ status_text[r.status] }}</span>
 {{ author }} · {{ created }}{% if task_ref %} · {{ task_ref }}{% endif %}</div>
</div>
</header>
{% if r.summary %}<p class="summary">{{ r.summary }}</p>{% endif %}
{% if r.kpis %}<section class="kpis">
{% for k in r.kpis %}<div class="kpi"{% if k.status %} style="border-top-color:{{ color[k.status] }}"{% endif %}>
<div class="label">{{ k.label }}</div><div class="value">{{ k.value }}</div>
{% if k.delta %}<div class="delta">{{ k.delta }}</div>{% endif %}
</div>{% endfor %}
</section>{% endif %}
{% for c in charts %}<section class="card">
<h2>{{ c.title }}</h2>
<svg viewBox="0 0 {{ c.g.w }} {{ c.g.h }}" role="img" aria-label="{{ c.title }}">
{% for t in c.g.ticks %}<line x1="{{ c.g.left }}" x2="{{ c.g.right }}" y1="{{ t.y }}" y2="{{ t.y }}" stroke="#e5e7eb"/>
<text x="{{ c.g.left - 6 }}" y="{{ t.y + 4 }}" text-anchor="end" font-size="12" fill="#6b7280">{{ t.text }}</text>
{% endfor %}<line x1="{{ c.g.left }}" x2="{{ c.g.right }}" y1="{{ c.g.zero }}" y2="{{ c.g.zero }}" stroke="#9ca3af"/>
{% for b in c.g.bars %}<rect x="{{ b.x }}" y="{{ b.y }}" width="{{ b.w }}" height="{{ b.h }}" fill="{{ b.color }}" rx="2"><title>{{ b.title }}</title></rect>
{% endfor %}{% for l in c.g.lines %}<polyline points="{{ l.points }}" fill="none" stroke="{{ l.color }}" stroke-width="2.5"/>
{% for d in l.dots %}<circle cx="{{ d.x }}" cy="{{ d.y }}" r="3.5" fill="{{ l.color }}"><title>{{ d.title }}</title></circle>
{% endfor %}{% endfor %}{% for t in c.g.labels %}<text x="{{ t.x }}" y="{{ c.g.label_y }}" text-anchor="middle" font-size="12" fill="#374151">{{ t.text }}</text>
{% endfor %}</svg>
{% if c.g.legend|length > 1 %}<div class="legend">{% for s in c.g.legend %}<span><i style="background:{{ s.color }}"></i>{{ s.name }}</span>{% endfor %}</div>{% endif %}
</section>{% endfor %}
{% for s in r.sections %}<section class="card"><h2>{{ s.heading }}</h2>
{% if s.bullets %}<ul>{% for b in s.bullets %}<li>{{ b }}</li>{% endfor %}</ul>{% endif %}
</section>{% endfor %}
{% if r.next %}<section class="card"><h2>Co dál</h2><ul>{% for b in r.next %}<li>{{ b }}</li>{% endfor %}</ul></section>{% endif %}
{% if r.asks %}<section class="card asks"><h2>Co potřebuju od Davida</h2><ul>{% for b in r.asks %}<li>{{ b }}</li>{% endfor %}</ul></section>{% endif %}
</main>
</body>
</html>
"""

_env = Environment(autoescape=True)
_template = _env.from_string(TEMPLATE)


def render(report: Report, author: str = "", created: str = "", task_ref: str | None = None) -> str:
    charts = [{"title": c.title, "g": chart_geometry(c)} for c in report.charts]
    return _template.render(r=report, charts=charts, color=STATUS_COLOR, status_text=STATUS_TEXT,
                            author=author, created=created[:16].replace("T", " "), task_ref=task_ref)


# ------------------------------------------------------------- route

router = APIRouter(tags=["reports"])


def _signed_in(request: Request, settings: Settings) -> bool:
    return not settings.password or bool(request.session.get("user") or request.session.get("actor_id"))


@router.get("/reports/{report_id}", response_class=HTMLResponse, include_in_schema=False)
def view(report_id: str, request: Request, settings: Settings = Depends(get_settings)):
    """The overview page, only with the web app's session; without it, off to the login."""
    if not _signed_in(request, settings):
        return RedirectResponse("/", status_code=303)
    try:
        uuid.UUID(report_id)
    except ValueError as e:
        raise HTTPException(404, "no such report") from e
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT h.*, a.name AS author FROM html_reports h "
                           "LEFT JOIN actors a ON a.id = h.agent_id WHERE h.id = ?", (report_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "no such report")
    report = Report.model_validate(json.loads(row["payload"]))
    task_ref = tasks.display_id(row["task_id"]) if row["task_id"] else None
    html = render(report, author=row["author"] or "", created=row["created_at"], task_ref=task_ref)
    return HTMLResponse(html, headers={"Content-Security-Policy": CSP, "Cache-Control": "private, no-store",
                                       "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
