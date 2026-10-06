"""Evidence for a hand-in: an agent that says "hotovo / nasazeno / odesláno / opraveno" shows it.

Prod 2026-10: agents handed results in saying "hotovo" or "nasazeno" with nothing behind it, quoted
goal numbers that contradicted the recorded metrics, and named facts nobody could find. A hand-in
by an agent that claims the work is done must carry at least one piece of evidence:

- a commit sha (`git cat-file -e <sha>^{commit}` in the repos the backend can see, or a deploy row),
- a sent message (an `outbound:*` audit line: its id, its message id, or simply a send the task did),
- a URL that answers 2xx/3xx (a quick HEAD; public hosts only),
- a file or note id (`soubor #12`, `/api/files/12`, `poznámka 7`),
- a test output (pos.verification.TESTS_PASS_RE: "12 passed").

Each item is checked cheaply; the checks that leave the process (git, HTTP) share a ~6 s budget and
fail open: what cannot be checked (no repo here, a private GitHub repo, a timeout) is
"unverifiable", which still counts as evidence. Only a claim with no evidence at all, or evidence
that verifiably fails (an unknown sha in a reachable repo, a 404, a failed send, failing tests), is
flagged. Besides that, a number the result states for a goal (its metric or title followed by a
number) that differs from the goal's recorded `current` is a contradiction, flagged the same way.

A flagged hand-in still goes through (like pos.verification's nudge, never a block): the agent gets
a "Doložte …" platform note in the complete_task answer (it can add the evidence with task_comment
in the same run), the task gets a system comment, the audit log a `handin_evidence_missing` /
`handin_evidence_failed` line, and pos.review_policy.decide never auto-accepts it. People are never
gated, nor are review work items (source `review:`; their evidence is the verdict), routine checks
from a schedule and triaged mail (their summary is the result).

POS_EVIDENCE_CHECKS=0 turns the git and HTTP checks off (everything they would check is then
unverifiable); POS_EVIDENCE_REPOS (paths separated by os.pathsep or commas) names the git
checkouts to look shas up in (default: this repo when it is a git checkout).
"""

import contextlib
import contextvars
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import audit
from .core import Ctx

BUDGET_S = 6.0          # all git and HTTP checks of one hand-in together
URL_TIMEOUT_S = 5.0
GIT_TIMEOUT_S = 2.0
MAX_EACH = 5            # at most this many shas and URLs are checked

# Claims of a delivered result. Strong ones always need evidence; the plain "done" only where the
# result is not itself the deliverable (a document, a routine check, a triaged mail).
STRONG_RE = re.compile(r"\b(?:nasazen[oaáýé]?|deployed|odeslán[oaáýé]?|odeslal[ai]?|sent|opraven[oaáýé]?|"
                       r"fixed|merged|mergnut[oaáýé]?|zamergován[oaáýé]?|publikován[oaáýé]?|published|"
                       r"released|vydán[oaáýé]?)\b", re.IGNORECASE)
WEAK_RE = re.compile(r"\b(?:hotov[oaáýé]?|done|dokončen[oaáýé]?|completed|splněn[oaáýé]?)\b", re.IGNORECASE)
NOT_A_CLAIM_RE = re.compile(r"definition of done|done when|to-?do", re.IGNORECASE)
NEGATION_RE = re.compile(r"(?:\b(?:not|nothing|never|no|wasn't|isn't|haven't|hasn't|ne|není|nebyl[oa]?|nic|"
                         r"nikdy)\s+(?:\w+\s+)?)$", re.IGNORECASE)

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'`|]+", re.IGNORECASE)
# A commit sha: lowercase hex with a digit and a letter, not a part of a uuid, a path or a chunk id.
SHA_RE = re.compile(r"(?<![\w\-/.])([0-9a-f]{7,40})(?![\w\-/]|:c\d)")
OUTBOUND_RE = re.compile(r"\b(?:outbound|odchozí(?:\s+zpráv[ayě])?|audit)\s*(?:id\s*)?[#:]?\s*(\d{1,9})\b",
                         re.IGNORECASE)
MESSAGE_ID_RE = re.compile(r"(?:message[\s\-_]?id|msg[\s\-_]?id|id\s+zprávy)\s*[:=#]?\s*<?([^\s<>,;]{3,200})>?",
                           re.IGNORECASE)
FILE_RE = re.compile(r"(?:\b(?:file|soubor[ue]?)\s*(?:id\s*)?[#:]?\s*|/api/files/)(\d{1,9})\b", re.IGNORECASE)
NOTE_RE = re.compile(r"\b(?:note|poznámk[aeyu])\s*(?:id\s*)?[#:]?\s*(\d{1,9})\b", re.IGNORECASE)

# A goal number: the goal's metric or title, a short connector, the number (Czech or English format).
NUMBER = r"(-?\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?)\s*(%?)"
CONNECTOR = (r"\s*(?:[:=\-–—]|\(|je|jsou|má|mame|máme|is|are|at|na|teď|ted|nyní|nyni|aktuálně|aktualne|"
             r"currently|now|celkem|total)?\s*(?:je|jsou|is|are|na|at)?\s*")

MISSING_NOTE = ("Doložte výsledek: v odevzdání tvrdíte, že je hotovo, ale chybí důkaz (sha commitu, id odeslané "
                "zprávy, URL vracející 200, id souboru nebo výstup testů). Doplňte ho ještě v tomto běhu "
                "komentářem k úkolu (task_comment); bez něj výsledek automaticky přijat nebude, posoudí ho "
                "recenzent.")
FAILED_NOTE = ("Doložte výsledek: důkaz v odevzdání neprošel ověřením: {items}. Opravte ho nebo doložte "
               "jiný (sha commitu, id odeslané zprávy, URL vracející 200, id souboru nebo výstup testů) "
               "komentářem k úkolu (task_comment) ještě v tomto běhu.")
MISMATCH_NOTE = ("Opravte čísla: {items}. Čísla o cílech citujte z goal_list (metriky cíle) i se zdrojem; "
                 "opravu napište komentářem k úkolu (task_comment).")
GUIDE = ("- A result that says done (hotovo, nasazeno, odesláno, opraveno, merged …) carries evidence: the "
         "commit sha, the sent message's id, a URL that opens, a file id or the test output. Numbers about a "
         "goal are quoted from goal_list (its current value), with the source.")

FLAG_ACTIONS = ("handin_evidence_missing", "handin_evidence_failed")
_ACTIONS = ("handin_evidence_ok", *FLAG_ACTIONS)

_extra_text: contextvars.ContextVar[str] = contextvars.ContextVar("pos_evidence_extra_text", default="")


@dataclass
class Item:
    kind: str     # sha | url | outbound | file | note | tests | deploy
    ref: str
    status: str   # verified | unverifiable | failed
    why: str = ""

    def label(self) -> str:
        return f"{self.kind} {self.ref}" + (f" ({self.why})" if self.why else "")


@dataclass
class Result:
    claim: str | None                  # the claim found ("nasazeno"), None when the text claims nothing
    items: list[Item] = field(default_factory=list)
    required: bool = True              # a claim without evidence is flagged (not where the text is the result)
    contradictions: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[Item]:
        return [i for i in self.items if i.status == "failed"]

    @property
    def status(self) -> str:
        """verified | missing | failed | contradiction | none (no claim, nothing wrong)."""
        if self.claim and self.failed:
            return "failed"
        if self.claim and not self.items and self.required:
            return "missing"
        if self.contradictions:
            return "contradiction"
        return "verified" if self.claim and self.items else "none"

    def note(self) -> str | None:
        """The "doložte" note for the agent (None when nothing is wrong)."""
        parts = []
        if self.status == "missing":
            parts.append(MISSING_NOTE)
        elif self.claim and self.failed:
            parts.append(FAILED_NOTE.format(items="; ".join(i.label() for i in self.failed)))
        if self.contradictions:
            parts.append(MISMATCH_NOTE.format(items="; ".join(self.contradictions)))
        return " ".join(parts) or None


@contextlib.contextmanager
def extra_text(text: str | None):
    """More of the hand-in than the note (the owner report's content): seen by on_hand_in inside."""
    token = _extra_text.set(text or "")
    try:
        yield
    finally:
        _extra_text.reset(token)


def report_text(rep: dict | None) -> str:
    """The owner-facing text of a report (pos.owner_report), for the evidence and the numbers."""
    if not rep:
        return ""
    parts = [rep.get("takeaway"), rep.get("content"), rep.get("next"), rep.get("verification")]
    for k in ("summary", "changes"):
        v = rep.get(k)
        parts.extend(v if isinstance(v, list) else [v])
    for s in rep.get("sources") or []:
        if isinstance(s, dict):
            parts.append(s.get("link"))
    return "\n".join(str(p) for p in parts if p)


# ------------------------------------------------------------------ the claim

def claim(text: str | None) -> str | None:
    """The done-claim in the text (the word), None when it claims nothing."""
    t = NOT_A_CLAIM_RE.sub(" ", text or "")
    for rx in (STRONG_RE, WEAK_RE):
        for m in rx.finditer(t):
            if not NEGATION_RE.search(t[max(0, m.start() - 30):m.start()]):
                return m.group(0)
    return None


# ------------------------------------------------------------------ extracting

def extract(text: str | None) -> dict[str, list[str]]:
    t = text or ""
    urls = list(dict.fromkeys(u.rstrip(".,;:!?)'\"") for u in URL_RE.findall(t)))
    rest = URL_RE.sub(" ", t)
    shas = [s for s in dict.fromkeys(SHA_RE.findall(rest))
            if re.search(r"\d", s) and re.search(r"[a-f]", s)]
    return {
        "url": urls,
        "sha": shas,
        "outbound": list(dict.fromkeys(OUTBOUND_RE.findall(rest))),
        "message_id": list(dict.fromkeys(m.strip("<>") for m in MESSAGE_ID_RE.findall(t))),
        "file": list(dict.fromkeys(FILE_RE.findall(t))),
        "note": list(dict.fromkeys(NOTE_RE.findall(rest))),
    }


# ------------------------------------------------------------------ checks in the database

def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() \
        is not None


def _detail(raw) -> dict:
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _send_status(detail: dict) -> str:
    """verified for a send that went out (or one whose status the log does not say), failed otherwise."""
    status = str(detail.get("status") or "").lower()
    if status in ("failed", "error", "not_configured", "rejected"):
        return "failed"
    return "verified"


def _outbound_rows(conn: sqlite3.Connection, where: str, args: tuple) -> list:
    return conn.execute(f"SELECT id, entity, entity_id, detail FROM audit_log WHERE action LIKE 'outbound:%' "
                        f"AND {where} ORDER BY id DESC LIMIT 5", args).fetchall()


def _check_outbound_id(conn: sqlite3.Connection, ref: str) -> Item:
    rows = _outbound_rows(conn, "id = ?", (int(ref),))
    if not rows:  # an approval's id is another way to name the send
        rows = _outbound_rows(conn, "entity = 'approval' AND entity_id = ?", (int(ref),))
    if not rows:
        return Item("outbound", ref, "failed", "no such sent message")
    return Item("outbound", ref, _send_status(_detail(rows[0]["detail"])))


def _check_message_id(conn: sqlite3.Connection, ref: str) -> Item:
    if ref.isdigit():
        return _check_outbound_id(conn, ref)
    like = "%" + ref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = _outbound_rows(conn, "detail LIKE ? ESCAPE '\\'", (like,))
    if not rows:
        return Item("outbound", ref, "failed", "no sent message with this id")
    return Item("outbound", ref, _send_status(_detail(rows[0]["detail"])))


def _check_row(conn: sqlite3.Connection, kind: str, table: str, ref: str) -> Item:
    if not _table(conn, table):
        return Item(kind, ref, "unverifiable")
    row = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (int(ref),)).fetchone()
    return Item(kind, ref, "verified" if row else "failed", "" if row else f"no {kind} {ref}")


def _task_sends(conn: sqlite3.Connection, task_id: int, since: str | None = None) -> list[Item]:
    """What the platform itself knows the task did: its sends, its deploys, the assignee's messages
    in chat since `since` (a digest "odeslán v DM")."""
    out = []
    t = conn.execute("SELECT assignee_id, created_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if t is not None and t["assignee_id"] and since:
        start = max(since, t["created_at"] or "")
        for table, author in (("chat_messages", "author_id"), ("messages", "from_actor")):
            if _table(conn, table):
                r = conn.execute(f"SELECT id FROM {table} WHERE {author} = ? AND created_at >= ? ORDER BY id DESC "
                                 "LIMIT 1", (t["assignee_id"], start)).fetchone()
                if r:
                    out.append(Item("message", str(r["id"]), "verified", f"posted in {table}"))
    for r in conn.execute("SELECT id, detail FROM audit_log WHERE action LIKE 'outbound:%' AND entity = 'task' "
                          "AND entity_id = ? ORDER BY id DESC LIMIT 3", (task_id,)).fetchall():
        if _send_status(_detail(r["detail"])) == "verified":
            out.append(Item("outbound", str(r["id"]), "verified", "sent by this task"))
    if _table(conn, "deploys"):
        for r in conn.execute("SELECT id, new_sha FROM deploys WHERE task_id = ? AND status = 'ok' "
                              "ORDER BY id DESC LIMIT 1", (task_id,)).fetchall():
            out.append(Item("deploy", r["new_sha"][:10], "verified", "deployed for this task"))
    return out


def _known_url(conn: sqlite3.Connection, url: str) -> bool:
    """A URL a send of ours returned (a GitHub comment, an issue): no request needed."""
    like = "%" + url.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return bool(_outbound_rows(conn, "detail LIKE ? ESCAPE '\\'", (like,)))


def _deployed_sha(conn: sqlite3.Connection, sha: str) -> bool:
    if not _table(conn, "deploys"):
        return False
    return conn.execute("SELECT 1 FROM deploys WHERE new_sha LIKE ? OR old_sha LIKE ? LIMIT 1",
                        (sha + "%", sha + "%")).fetchone() is not None


# ------------------------------------------------------------------ checks outside (git, HTTP)

def _checks_on() -> bool:
    return os.environ.get("POS_EVIDENCE_CHECKS", "1") != "0"


def repos() -> list[Path]:
    raw = os.environ.get("POS_EVIDENCE_REPOS", "")
    if raw.strip():
        return [Path(p.strip()) for p in re.split(r"[,%s]" % re.escape(os.pathsep), raw) if p.strip()]
    from .tools import repo_root

    root = repo_root()
    return [root] if root and (root / ".git").exists() else []


_SUBMODULE_PATH_RE = re.compile(r"^\s*path\s*=\s*(.+?)\s*$", re.MULTILINE)


def with_submodules(paths: list[Path]) -> list[Path]:
    """Each repository and then its checked-out submodules (from .gitmodules): a commit in the Kniha
    web submodule (roskodav/web-builder-studio, /work/kniha/web) is evidence too (prod 2026-10: T-248
    and T-509 were returned for web commits 'not in the repository')."""
    out: list[Path] = []
    for repo in paths:
        repo = Path(repo)
        out.append(repo)
        try:
            text = (repo / ".gitmodules").read_text(encoding="utf-8")
        except OSError:
            continue
        for rel in _SUBMODULE_PATH_RE.findall(text):
            sub = repo / rel.strip()
            if ".." not in Path(rel).parts and (sub / ".git").exists():
                out.append(sub)
    return list(dict.fromkeys(out))


def check_sha(sha: str, paths: list[Path] | None = None) -> Item:
    """verified when a known repo has the commit; failed when every reachable repo lacks it;
    unverifiable when no repo is reachable (no git, no checkout, a timeout)."""
    paths = with_submodules(repos() if paths is None else paths)
    git = shutil.which("git")
    if not git or not paths:
        return Item("sha", sha, "unverifiable", "no repository here")
    looked = 0
    for repo in paths:
        try:
            # safe.directory: the checkouts are mounted from another user's volume (read-only)
            r = subprocess.run([git, "-c", "safe.directory=*", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                               capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return Item("sha", sha, "verified")
        err = (r.stderr or "").lower()
        if "not a git repository" in err or "cannot change to" in err or "no such file" in err:
            continue
        looked += 1
    if not looked:
        return Item("sha", sha, "unverifiable", "no repository reachable")
    return Item("sha", sha, "failed", "no such commit in the repository")


def _public_host(host: str) -> bool:
    """Every address the host resolves to is public (no LAN, loopback, link-local, metadata)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    addrs = {i[4][0] for i in infos}
    try:
        return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)
    except ValueError:
        return False


PRIVATE_404_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")  # a private repo answers 404 to strangers


def check_url(url: str) -> Item:
    import httpx

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return Item("url", url, "unverifiable", "not a web address")
    if not _public_host(parts.hostname):
        return Item("url", url, "unverifiable", "not a public host")
    try:
        with httpx.Client(timeout=URL_TIMEOUT_S, follow_redirects=False,
                          headers={"User-Agent": "PersonalOS evidence check"}) as c:
            r = c.head(url)
            if r.status_code in (405, 501):
                with c.stream("GET", url) as g:
                    code = g.status_code
            else:
                code = r.status_code
    except Exception:  # noqa: BLE001 - a network problem is not the agent's fault: unverifiable
        return Item("url", url, "unverifiable", "no answer")
    if code < 400:
        return Item("url", url, "verified", f"HTTP {code}")
    host = parts.hostname.lower()
    if code in (404, 410) and not any(host == h or host.endswith("." + h) for h in PRIVATE_404_HOSTS):
        return Item("url", url, "failed", f"HTTP {code}")
    if code >= 500:
        return Item("url", url, "failed", f"HTTP {code}")
    return Item("url", url, "unverifiable", f"HTTP {code}")


# ------------------------------------------------------------------ goal numbers

def _num(raw: str) -> float | None:
    try:
        return float(raw.replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _names(g) -> list[str]:
    out = []
    for raw in (g["metric"], g["title"]):
        name = re.sub(r"\s*\(.*?\)\s*", " ", raw or "").strip(" .:")
        if len(name) >= 3 and name.lower() not in (n.lower() for n in out):
            out.append(name)
    return out


def contradictions(conn: sqlite3.Connection, text: str | None) -> list[str]:
    """'<goal> má podle metrik X, v textu je Y' for every goal number the text states that differs
    from the recorded one. Deterministic and narrow: the goal's metric or title followed (only a
    connector like ':', 'je', 'is' in between) by a number. The target and the baseline are fine to
    quote; a percentage is compared with the goal's progress."""
    t = text or ""
    if not t.strip() or not _table(conn, "goals"):
        return []
    try:
        goals = conn.execute("SELECT id, title, metric, current, baseline, target_value, progress FROM goals "
                             "WHERE archived_at IS NULL AND status IN ('active', 'proposed', 'paused') "
                             "AND current IS NOT NULL").fetchall()
    except sqlite3.OperationalError:  # the metric columns are added on first use (pos.goals)
        return []
    out = []
    for g in goals:
        allowed = {float(v) for v in (g["current"], g["baseline"], g["target_value"]) if v is not None}
        for name in _names(g):
            rx = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)" + CONNECTOR + NUMBER, re.IGNORECASE)
            m = rx.search(t)
            if not m:
                continue
            value = _num(m.group(1))
            if value is None or (not m.group(2) and value.is_integer() and 1900 <= value <= 2100
                                 and not any(1900 <= a <= 2100 for a in allowed)):
                continue  # a year, not the metric
            if m.group(2):  # a percentage: the goal's progress (or the metric itself in %)
                from .goals import _measured

                prog = g["progress"] if g["progress"] is not None else _measured(dict(g))
                if (prog is not None and abs(value - prog) < 1) or any(abs(value - a) < 1e-6 for a in allowed):
                    break
            elif any(abs(value - a) <= max(1e-6, abs(a) * 0.005) for a in allowed):
                break
            out.append(f"cíl „{g['title']}“ má podle metrik {_fmt(g['current'])}"
                       f"{' (' + g['metric'] + ')' if g['metric'] else ''}, v textu je {m.group(1)}{m.group(2)}")
            break
    return out


# ------------------------------------------------------------------ the whole check

def check(conn: sqlite3.Connection, text: str | None, task_id: int | None = None, strict: bool = True,
          budget_s: float = BUDGET_S, since: str | None = None) -> Result:
    """Find the claim and the evidence in `text`, verify each item (git and HTTP within `budget_s`).
    `strict`: a claim with no evidence at all is flagged (otherwise only evidence that fails);
    `since`: messages the agent (task's assignee) posted after this count as sent."""
    res = Result(claim(text), required=strict)
    res.contradictions = contradictions(conn, text)
    if not res.claim:
        return res
    res.items = gather(conn, text, task_id, budget_s=budget_s, since=since)
    return res


def gather(conn: sqlite3.Connection, text: str | None, task_id: int | None = None, budget_s: float = BUDGET_S,
           since: str | None = None) -> list[Item]:
    """Every piece of evidence in `text`, each verified (git and HTTP within `budget_s`, failing open);
    with `task_id`, also what the platform knows the task did (its sends, deploys, messages since `since`).
    Shared with pos.delivery (evidence per acceptance criterion)."""
    from .verification import TESTS_FAIL_RE, TESTS_PASS_RE

    found = extract(text)
    items: list[Item] = []
    if TESTS_FAIL_RE.search(text or ""):
        items.append(Item("tests", TESTS_FAIL_RE.search(text).group(0), "failed", "tests failing"))
    elif TESTS_PASS_RE.search(text or ""):
        items.append(Item("tests", TESTS_PASS_RE.search(text).group(0), "verified"))
    for ref in found["outbound"]:
        items.append(_check_outbound_id(conn, ref))
    for ref in found["message_id"]:
        items.append(_check_message_id(conn, ref))
    for ref in found["file"]:
        items.append(_check_row(conn, "file", "files", ref))
    for ref in found["note"]:
        items.append(_check_row(conn, "note", "notes", ref))
    if task_id:
        items.extend(_task_sends(conn, task_id, since))
    shas, urls = [], []
    for sha in found["sha"][:MAX_EACH]:
        if _deployed_sha(conn, sha):
            items.append(Item("sha", sha, "verified", "deployed"))
        else:
            shas.append(sha)
    for url in found["url"][:MAX_EACH]:
        if _known_url(conn, url):
            items.append(Item("url", url, "verified", "returned by a send"))
        else:
            urls.append(url)
    if not _checks_on():
        items += [Item("sha", s, "unverifiable", "checks off") for s in shas]
        items += [Item("url", u, "unverifiable", "checks off") for u in urls]
    elif shas or urls:
        paths = repos()
        jobs = [(Item("sha", s, "unverifiable", "timed out"), check_sha, (s, paths)) for s in shas] + \
               [(Item("url", u, "unverifiable", "timed out"), check_url, (u,)) for u in urls]
        pool = ThreadPoolExecutor(max_workers=min(8, len(jobs)))
        try:
            futures = [pool.submit(fn, *args) for _, fn, args in jobs]
            wait(futures, timeout=budget_s)
            for (fallback, _, _), f in zip(jobs, futures, strict=True):
                try:
                    items.append(f.result(timeout=0) if f.done() else fallback)
                except Exception:  # noqa: BLE001 - fail open
                    items.append(fallback)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    return items


def _gated(conn: sqlite3.Connection, ctx: Ctx, row) -> bool:
    who = conn.execute("SELECT kind FROM actors WHERE id = ?", (ctx.actor_id,)).fetchone()
    if who is None or who["kind"] == "human" or row["assignee_id"] != ctx.actor_id:
        return False
    return not (row["source"] or "").startswith("review:")


def _run_start(conn: sqlite3.Connection, ctx: Ctx) -> str:
    """Since when the agent's own messages count as this hand-in's sends: its run, else the last 6 h."""
    if ctx.run_id:
        r = conn.execute("SELECT started_at FROM runs WHERE id = ?", (ctx.run_id,)).fetchone()
        if r is not None and r["started_at"]:
            return r["started_at"]
    return (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")


def _strict(conn: sqlite3.Connection, row, note: str | None) -> bool:
    """Does a claim need evidence here? Not where the result itself is the deliverable (a document,
    a routine check, a triaged mail): there only evidence that fails is flagged."""
    from . import review_policy

    return not (review_policy.is_routine(conn, row) or review_policy.is_triage(row)
                or review_policy.is_doc(row, note))


def on_hand_in(conn: sqlite3.Connection, ctx: Ctx, row, note: str | None) -> str | None:
    """An agent handed a result in (`row` before the change): check its claim and its numbers.
    Returns the "doložte" note for the agent, None when the hand-in is fine. Never raises."""
    try:
        if not _gated(conn, ctx, row):
            return None
        text = "\n".join(x for x in (note, _extra_text.get()) if x)
        started = time.monotonic()
        res = check(conn, text, row["id"], strict=_strict(conn, row, note), since=_run_start(conn, ctx))
        detail = {"status": res.status, **({"claim": res.claim} if res.claim else {}),
                  "items": [i.label() + f" = {i.status}" for i in res.items][:12],
                  **({"contradictions": res.contradictions} if res.contradictions else {}),
                  "ms": round(1000 * (time.monotonic() - started))}
        msg = res.note()
        if msg:
            action = "handin_evidence_missing" if res.status == "missing" else "handin_evidence_failed"
            audit.log(conn, ctx, action, "task", row["id"], **detail)
            from . import comments

            comments.log(conn, ctx, row["id"], f"PersonalOS: {msg}", "system")
            return msg
        if res.status == "verified" or flagged(conn, row["id"]):
            audit.log(conn, ctx, "handin_evidence_ok", "task", row["id"], **detail)
        return None
    except Exception:  # noqa: BLE001 - a check must never fail the hand-in
        return None


def flagged(conn: sqlite3.Connection, task_id: int) -> str:
    """Why the task's latest hand-in lacks evidence ('' when it does not): the review policy never
    auto-accepts such a result."""
    r = conn.execute(f"SELECT action FROM audit_log WHERE entity = 'task' AND entity_id = ? AND action IN "
                     f"({', '.join('?' * len(_ACTIONS))}) ORDER BY id DESC LIMIT 1",
                     (task_id, *_ACTIONS)).fetchone()
    if r is None or r["action"] == "handin_evidence_ok":
        return ""
    return "the done-claim lacks evidence" if r["action"] == "handin_evidence_missing" \
        else "its evidence or numbers did not check out"


def nudges(conn: sqlite3.Connection, actor_id: int) -> list[str]:
    """A line for the agent's next prompts when its recent hand-ins were flagged."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    n = conn.execute(f"SELECT COUNT(*) FROM audit_log WHERE actor_id = ? AND at >= ? AND action IN "
                     f"({', '.join('?' * len(FLAG_ACTIONS))})", (actor_id, since, *FLAG_ACTIONS)).fetchone()[0]
    if not n:
        return []
    return [f"In the last 7 days {n} of your hand-ins said done without evidence that checked out (or with "
            "goal numbers that contradicted the metrics). A done result carries the commit sha, the sent "
            "message's id, a URL that opens, a file id or the test output; goal numbers come from goal_list."]
