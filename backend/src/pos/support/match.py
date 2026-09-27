"""Which project a customer's mail is about, and who develops it.

Candidates are the PersonalOS projects (the `projects` table: name, slug, labels, goal, and whatever links
the rich project pages carry: repository links, domains, knowlage tags) plus the company's own products
that are not projects (PersonalOS, knowlage, Nexus; POS_SUPPORT_PRODUCTS may add or replace them as JSON).
A candidate scores by what the mail (and the knowlage history with that customer) mentions:

- a repository name (owner/name or the bare name)        +5
- a domain of the project, or the sender's domain in it   +5
- a keyword phrase (the name, the slug, extra keywords)   +3
- the classifier's project hint names it                  +4
- a knowlage tag / workspace of the project in the history +2

The best one with at least MIN_SCORE wins; nothing → no project (the Head of Customer Success decides).
The developer: the project team's developer (a member whose role is `developer`), else the CTO's Software
Engineer.
"""

import json
import os
import re
import sqlite3

from .. import roles

MIN_SCORE = 4
GENERIC = {"kniha", "web", "app", "api", "projekt", "project", "test", "data", "report"}

PRODUCTS = [
    {"slug": "personalos", "name": "PersonalOS", "repos": ["ObseumEU/PersonalOS"],
     "keywords": ["personalos", "personal os", "pos.obseum"], "domains": [], "tags": []},
    {"slug": "knowlage", "name": "knowlage", "repos": ["ObseumEU/knowlage-agent"],
     "keywords": ["knowlage", "knowledge agent", "znalostní báze"], "domains": [], "tags": []},
    {"slug": "nexus", "name": "Nexus Process Pilot", "repos": ["ObseumEU/nexus-process-pilot"],
     "keywords": ["nexus process pilot", "nexus"], "domains": [], "tags": []},
]
# What the project pages do not say yet (their links come with the rich project pages): kept here per slug.
KNOWN = {
    "kniha": {"repos": ["ObseumEU/Kniha", "roskodav/web-builder-studio"],
              "keywords": ["rodinné příběhy", "rodinne pribehy", "rodinne-pribehy", "audiokniha"],
              "domains": ["rodinne-pribehy.obseum.cz"], "tags": ["kniha"]},
}
URL_HOST = re.compile(r"https?://([a-z0-9.-]+\.[a-z]{2,})", re.I)


def _fold(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def _strings(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return _strings(parsed) if not isinstance(parsed, str) else [parsed]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)] + [str(k) for k in value]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return [str(value)]


def _from_texts(texts: list[str]) -> dict:
    repos, domains, tags = set(), set(), set()
    for t in texts:
        for m in re.finditer(r"github\.com/([\w.-]+/[\w.-]+)", t):
            repos.add(m.group(1).removesuffix(".git"))
        for m in URL_HOST.finditer(t):
            if "github.com" not in m.group(1).lower():
                domains.add(m.group(1).lower())
        low = t.strip().lower()
        if low.startswith(("repo:", "github:")):
            repos.add(t.split(":", 1)[1].strip())
        elif low.startswith("domain:"):
            domains.add(t.split(":", 1)[1].strip().lower())
        elif low.startswith(("kb:", "knowlage:", "tag:", "workspace:")):
            tags.add(t.split(":", 1)[1].strip().lower())
    return {"repos": sorted(repos), "domains": sorted(domains), "tags": sorted(tags)}


def _extra_products() -> list[dict]:
    raw = os.environ.get("POS_SUPPORT_PRODUCTS", "")
    if not raw:
        return PRODUCTS
    try:
        data = json.loads(raw)
    except ValueError:
        return PRODUCTS
    return [dict(p) for p in data if isinstance(p, dict) and p.get("slug")] or PRODUCTS


def catalog(conn: sqlite3.Connection) -> list[dict]:
    """Projects (with their links) and the company's products, as match candidates."""
    out = []
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
    link_cols = [c for c in cols if any(k in c for k in ("repo", "link", "url", "domain", "tag", "knowlage"))]
    sql = "SELECT * FROM projects WHERE status IN ('active', 'paused')" + (
        " AND archived_at IS NULL" if "archived_at" in cols else "")
    for row in conn.execute(sql):
        d = dict(row)
        texts = _strings(d.get("labels")) + [d.get("goal") or "", d.get("definition_of_done") or ""]
        for c in link_cols:
            texts += _strings(d.get(c))
        found = _from_texts(texts)
        extra = KNOWN.get(d["slug"], {})
        out.append({"id": d["id"], "slug": d["slug"], "name": d["name"],
                    "repos": sorted(set(found["repos"]) | set(extra.get("repos", []))),
                    "domains": sorted(set(found["domains"]) | set(extra.get("domains", []))),
                    "tags": sorted(set(found["tags"]) | set(extra.get("tags", [])) | {d["slug"]}),
                    "keywords": sorted({d["name"], d["slug"], *extra.get("keywords", [])})})
    taken = {p["slug"] for p in out}
    for p in _extra_products():
        if p["slug"] not in taken:
            out.append({"id": None, "tags": [], "domains": [], "repos": [], "keywords": [], **p})
    return out


def _mentions(text: str, phrase: str) -> bool:
    phrase = _fold(phrase).strip()
    if len(phrase) < 3:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def score(project: dict, text: str, sender_domain: str, hint: str, history: str) -> tuple[int, list[str]]:
    t, h = _fold(text), _fold(history)
    points, why = 0, []
    for repo in project.get("repos", []):
        name = repo.split("/")[-1]
        if _mentions(t, repo) or (name.lower() not in GENERIC and _mentions(t, name)):
            points += 5
            why.append(f"repo {repo}")
            break
    for d in project.get("domains", []):
        d = d.lower()
        if d in t or (sender_domain and (sender_domain == d or sender_domain.endswith("." + d))):
            points += 5
            why.append(f"doména {d}")
            break
    for k in project.get("keywords", []):
        if _fold(k) not in GENERIC and _mentions(t, k):
            points += 3
            why.append(f"zmínka „{k}“")
            break
    if hint and _fold(hint).strip() in {_fold(project["name"]), _fold(project["slug"])}:
        points += 4
        why.append("klasifikátor")
    if h:
        for tag in project.get("tags", []):
            if tag and tag not in GENERIC and _mentions(h, tag):
                points += 2
                why.append(f"knowlage {tag}")
                break
        else:
            if any(_mentions(h, r) for r in project.get("repos", [])):
                points += 2
                why.append("knowlage repo")
    return points, why


def match(conn: sqlite3.Connection, mail: dict, hint: str = "", history: str = "",
          candidates: list[dict] | None = None) -> dict | None:
    """The best matching project {id, slug, name, repos, score, why} or None."""
    from .classify import domain

    text = f"{mail.get('subject', '')}\n{mail.get('body', '')}"
    sender_domain = domain(mail.get("sender") or "")
    best = None
    for p in candidates if candidates is not None else catalog(conn):
        pts, why = score(p, text, sender_domain, hint, history)
        if pts >= MIN_SCORE and (best is None or pts > best["score"]):
            best = {**p, "score": pts, "why": why}
    return best


def developer_for(conn: sqlite3.Connection, project: dict | None) -> str:
    """The project team's developer, else the Software Engineer."""
    if project and project.get("id"):
        row = conn.execute(
            """SELECT a.name FROM project_members m JOIN actors a ON a.id = m.actor_id
               WHERE m.project_id = ? AND a.archived_at IS NULL AND a.kind IN ('agent', 'ai')
                 AND lower(COALESCE(a.role, '')) = 'developer' ORDER BY a.id LIMIT 1""", (project["id"],)).fetchone()
        if row is not None:
            return row["name"]
        team = conn.execute(
            """SELECT a.team FROM project_members m JOIN actors a ON a.id = m.actor_id
               WHERE m.project_id = ? AND m.role = 'lead' AND a.team IS NOT NULL LIMIT 1""", (project["id"],)).fetchone()
        if team is not None:
            dev = conn.execute("SELECT name FROM actors WHERE team = ? AND lower(COALESCE(role, '')) = 'developer' "
                               "AND archived_at IS NULL ORDER BY id LIMIT 1", (team["team"],)).fetchone()
            if dev is not None:
                return dev["name"]
    return roles.ENGINEER
