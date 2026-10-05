"""An approval as the owner reads it: what it is, why it waits for him, and what the button does.

The stored approval is for the machine (`details` is the agent's JSON: payload, kind, reason in
English). Every place that shows one to the owner (/approvals, the Home "Čeká na tebe" card, /m/needs)
uses `view`, so the card never shows raw JSON:

- an e-mail is Komu / Předmět / Text (the whole text, the card folds it);
- the kind and the reason are said in Czech ("Závazek: smlouva, cenová nabídka…");
- the approve button says what really happens: with e-mail mode "draft" (the default) approving saves
  a draft in his Gmail ("Uložit jako koncept v Gmailu"), with "auto" it sends ("Odeslat").
"""

import json
import re
import sqlite3

KIND_LABELS = {
    "commitment": "Závazek (smlouva, cenová nabídka)",
    "money": "Peníze (platba, nákup)",
    "personal_channel": "Tvůj osobní kanál (LinkedIn, sítě)",
    "ordinary": "Běžná komunikace",
}
KIND_WHY = {
    "commitment": "smlouva, cenová nabídka nebo jiný právní či finanční závazek",
    "money": "platba, nákup nebo cokoli, co stojí peníze mimo schválené rozpočty",
    "personal_channel": "příspěvek na tvém osobním profilu (LinkedIn, osobní sítě)",
}
ACTION_LABELS = {
    "email.send": "E-mail",
    "linkedin.post": "Příspěvek na LinkedIn",
    "payment": "Platba",
    "github.pr": "Pull request na GitHubu",
    "github.comment": "Komentář na GitHubu",
    "github.issue": "Issue na GitHubu",
    "discord.send": "Zpráva na Discordu",
    "command": "Příkaz na serveru",
}


def reason_cs(reason: str) -> str:
    """The guard's English reason in Czech (unknown reasons are returned as they are)."""
    r = (reason or "").strip()
    if not r:
        return ""
    m = re.match(r"marked as (\w+)", r)
    if m:
        why = KIND_WHY.get(m.group(1))
        return f"Agent to označil jako {KIND_LABELS.get(m.group(1), m.group(1)).split(' (')[0].lower()}" + (
            f": {why}." if why else ".")
    m = re.match(r"'(.+)' reads as a contract or a binding offer", r)
    if m:
        return f"„{m.group(1)}“ zní jako smlouva nebo závazná nabídka."
    m = re.match(r"'(.+)' reads as buying or paying", r)
    if m:
        return f"„{m.group(1)}“ zní jako nákup nebo platba."
    if r.startswith("a price with an amount"):
        return "Cena s částkou zní jako cenová nabídka."
    if r.endswith("moves money"):
        return "Jde o platbu."
    if "posts on the owner's personal channel" in r:
        return "Jde o příspěvek na tvém osobním profilu."
    m = re.match(r"'?(.+?)'? is one of the owner's personal channels", r)
    if m:
        return f"{m.group(1)} je tvůj osobní kanál."
    if r.startswith("ordinary outbound work"):
        return "Běžná komunikace."
    return r


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action or "", action or "")


def _details(details) -> dict:
    if isinstance(details, str):
        try:
            details = json.loads(details or "{}")
        except ValueError:
            return {}
    return details if isinstance(details, dict) else {}


def title(action: str, details) -> str:
    """One line for lists ("E-mail: Re: Nabídka", "Příspěvek na LinkedIn")."""
    d = _details(details)
    p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    subject = str(p.get("subject") or d.get("subject") or "").strip()
    base = action_label(action)
    return f"{base}: {subject}" if subject else base


def view(conn: sqlite3.Connection | None, action: str, details, *, status: str = "pending") -> dict:
    """{title, action_label, kind, kind_label, reason, why, email?, text?, notes?, approve_label, approve_hint}."""
    d = _details(details)
    p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    kind = str(d.get("kind") or "")
    out: dict = {
        "title": title(action, d), "action_label": action_label(action), "kind": kind,
        "kind_label": KIND_LABELS.get(kind, ""), "reason": reason_cs(str(d.get("reason") or "")),
        "why": str(d.get("why") or d.get("summary") or ""),
        "notes": str(d.get("notes_for_owner") or ""),
        "approve_label": "Schválit", "approve_hint": "",
    }
    if action == "email.send":
        to = str(p.get("to") or d.get("to") or "").strip()
        out["email"] = {
            "to": to or ("odpověď ve stávajícím vlákně" if p.get("thread_id") else ""),
            "cc": str(p.get("cc") or ""),
            "subject": str(p.get("subject") or d.get("subject") or "").strip()
            or ("Re: (předmět vlákna)" if p.get("thread_id") else ""),
            "body": str(p.get("body") or p.get("text") or d.get("text") or ""),
            "account": str(p.get("account") or ""),
        }
        mode = "draft"
        if conn is not None:
            try:
                from . import outbound_gmail

                mode = outbound_gmail.mode_for(conn, p)
            except Exception:  # noqa: BLE001 - the safe label: nothing goes out by itself
                mode = "draft"
        out["mode"] = mode
        if mode == "auto":
            out["approve_label"], out["approve_hint"] = "Odeslat", "E-mail se po schválení hned odešle."
        else:
            out["approve_label"] = "Uložit jako koncept v Gmailu"
            out["approve_hint"] = "Nic se neodešle: koncept najdeš v Gmailu a odešleš ho sám."
    elif action == "linkedin.post":
        out["text"] = str(d.get("text") or p.get("text") or "")
        out["approve_label"] = "Schválit příspěvek"
        out["approve_hint"] = "Po schválení se příspěvek publikuje (nebo ho dostaneš připravený k publikaci)."
    else:
        text = d.get("text") or p.get("text") or p.get("body")
        if text:
            out["text"] = str(text)
        cmds = d.get("commands")
        if isinstance(cmds, list) and cmds:
            out["commands"] = [str(c) for c in cmds][:20]
    if status != "pending":
        out["approve_label"] = ""
    return out
