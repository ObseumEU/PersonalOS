"""The only Google client of invoice filing. Tokens stay in the server's environment; no agent sees one.

- Gmail, read-only (`gmail.readonly`, the refresh tokens knowlage uses): search, a message, an attachment.
  POS_GMAIL_TOKEN_<ADDRESS> per mailbox (david.rosko@obseum.cz → POS_GMAIL_TOKEN_DAVID_ROSKO_OBSEUM_CZ).
- Drive, two tokens of the account that owns both folders (david.rosko@obseum.cz):
  POS_GDRIVE_READ_TOKEN (`drive.readonly`: list folders, check parents and existing files) and
  POS_GDRIVE_FILE_TOKEN (`drive.file`: create files and folders; it cannot even see the owner's other files).
- The OAuth client: POS_GOOGLE_CLIENT_ID / POS_GOOGLE_CLIENT_SECRET (knowlage's Google client).

The client has no method that updates, moves or deletes anything on Drive: writing is `create_folder` and
`upload`, both a POST that creates a new file.
"""

import base64
import html
import json
import os
import re
import threading
import time
import uuid

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
READ_TOKEN_ENV = "POS_GDRIVE_READ_TOKEN"
FILE_TOKEN_ENV = "POS_GDRIVE_FILE_TOKEN"
_transport: httpx.BaseTransport | None = None  # tests put a mock transport here
_tokens: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


class GoogleError(Exception):
    pass


def token_env(address: str) -> str:
    return "POS_GMAIL_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", address.upper())


def mailboxes() -> list[str]:
    raw = os.environ.get("POS_INVOICE_MAILBOXES", "david.rosko@obseum.cz,rosko.dav@gmail.com")
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def configured() -> dict:
    """Which parts are set up (names only, never values)."""
    client = bool(os.environ.get("POS_GOOGLE_CLIENT_ID") and os.environ.get("POS_GOOGLE_CLIENT_SECRET"))
    return {"client": client,
            "gmail": {a: bool(os.environ.get(token_env(a))) for a in mailboxes()},
            "drive_read": bool(os.environ.get(READ_TOKEN_ENV)), "drive_file": bool(os.environ.get(FILE_TOKEN_ENV))}


def _client() -> httpx.Client:
    return httpx.Client(transport=_transport, timeout=60, follow_redirects=True)


def _access_token(env: str) -> str:
    with _lock:
        cached = _tokens.get(env)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
    refresh = os.environ.get(env)
    cid, secret = os.environ.get("POS_GOOGLE_CLIENT_ID"), os.environ.get("POS_GOOGLE_CLIENT_SECRET")
    if not (refresh and cid and secret):
        raise GoogleError(f"Google access is not set up on this server ({env} or the OAuth client missing)")
    with _client() as c:
        r = c.post(TOKEN_URL, data={"client_id": cid, "client_secret": secret, "refresh_token": refresh,
                                    "grant_type": "refresh_token"})
    if r.status_code != 200:
        err = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("error")
        raise GoogleError(f"Google refused the refresh token {env} ({r.status_code} {err or ''})".strip())
    data = r.json()
    with _lock:
        _tokens[env] = (data["access_token"], time.time() + float(data.get("expires_in", 3000)))
    return data["access_token"]


def _call(env: str, method: str, url: str, **kw) -> httpx.Response:
    if method not in ("GET", "POST"):
        raise GoogleError(f"{method} is never used (create-only client)")
    headers = {"Authorization": f"Bearer {_access_token(env)}", **kw.pop("headers", {})}
    try:
        with _client() as c:
            r = c.request(method, url, headers=headers, **kw)
    except httpx.HTTPError as e:
        raise GoogleError(f"Google is unreachable: {e}"[:300]) from e
    if r.status_code >= 400:
        raise GoogleError(f"Google {method} {url.split('?')[0].rsplit('/', 2)[-2:]} → {r.status_code}: "
                          f"{r.text[:300]}")
    return r


# ------------------------------------------------------------------ Gmail (read-only)

def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _html_text(raw: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>|<[^>]+>", " ", re.sub(r"<(br|/p|/div|/tr|/li)\b[^>]*>", "\n", raw,
                                                                 flags=re.I), flags=re.I | re.S)
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


class Gmail:
    def __init__(self, address: str):
        self.address = address.lower()
        self.env = token_env(self.address)

    def get(self, path: str, **params) -> dict:
        return _call(self.env, "GET", f"{GMAIL}/{path}", params=params).json()

    def search(self, query: str, limit: int = 200) -> list[str]:
        ids, page = [], None
        while len(ids) < limit:
            params = {"q": query, "maxResults": min(500, limit)}
            if page:
                params["pageToken"] = page
            data = self.get("messages", **params)
            ids += [m["id"] for m in data.get("messages") or []]
            page = data.get("nextPageToken")
            if not page:
                break
        return ids[:limit]

    def labels(self) -> dict[str, str]:
        return {lab["id"]: lab["name"] for lab in self.get("labels").get("labels") or []}

    def message(self, message_id: str, labels: dict[str, str] | None = None) -> dict:
        m = self.get(f"messages/{message_id}", format="full")
        heads = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers") or []}
        plain, rich, attachments = [], [], []

        def walk(part: dict) -> None:
            body = part.get("body") or {}
            if part.get("filename"):
                attachments.append({"part_id": part.get("partId") or "", "filename": part["filename"],
                                    "mime": part.get("mimeType") or "", "size": body.get("size") or 0,
                                    "attachment_id": body.get("attachmentId") or "",
                                    "_inline": body.get("data") if not body.get("attachmentId") else None})
            elif body.get("data") and part.get("mimeType") == "text/plain":
                plain.append(_b64(body["data"]).decode("utf-8", errors="replace"))
            elif body.get("data") and part.get("mimeType") == "text/html":
                rich.append(_html_text(_b64(body["data"]).decode("utf-8", errors="replace")))
            for sub in part.get("parts") or []:
                walk(sub)

        walk(m.get("payload") or {})
        names = labels or {}
        return {"account": self.address, "message_id": m["id"], "thread_id": m.get("threadId"),
                "subject": heads.get("subject", ""), "sender": heads.get("from", ""), "to": heads.get("to", ""),
                "cc": heads.get("cc", ""), "delivered_to": heads.get("delivered-to", ""),
                "date_header": heads.get("date", ""), "internal_ms": int(m.get("internalDate") or 0),
                "label_ids": m.get("labelIds") or [],
                "labels": [names.get(i, i) for i in m.get("labelIds") or []],
                "body": ("\n\n".join(plain) if plain else "\n\n".join(rich))[:20000],
                "attachments": attachments,
                "url": f"https://mail.google.com/mail/u/?authuser={self.address}#all/{m['id']}"}

    def attachment(self, message_id: str, att: dict) -> bytes:
        if att.get("_inline"):
            return _b64(att["_inline"])
        data = self.get(f"messages/{message_id}/attachments/{att['attachment_id']}")
        return _b64(data["data"])


# ------------------------------------------------------------------ Drive (read + create only)

class Drive:
    """Reads with the drive.readonly token, creates with the drive.file token."""

    FIELDS = "id,name,mimeType,md5Checksum,size,parents,createdTime,webViewLink,trashed"

    def children(self, folder_id: str) -> list[dict]:
        out, page = [], None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed = false", "pageSize": 1000,
                      "fields": f"nextPageToken,files({self.FIELDS})", "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            data = _call(READ_TOKEN_ENV, "GET", f"{DRIVE}/files", params=params).json()
            out += data.get("files") or []
            page = data.get("nextPageToken")
            if not page:
                return out

    def get(self, file_id: str) -> dict:
        return _call(READ_TOKEN_ENV, "GET", f"{DRIVE}/files/{file_id}",
                     params={"fields": self.FIELDS, "supportsAllDrives": "true"}).json()

    def parents(self, file_id: str) -> list[str]:
        return self.get(file_id).get("parents") or []

    def create_folder(self, parent_id: str, name: str) -> dict:
        return _call(FILE_TOKEN_ENV, "POST", f"{DRIVE}/files",
                     params={"fields": self.FIELDS, "supportsAllDrives": "true"},
                     json={"name": name, "mimeType": FOLDER, "parents": [parent_id]}).json()

    def upload(self, parent_id: str, name: str, data: bytes, mime: str, description: str = "") -> dict:
        boundary = "pos" + uuid.uuid4().hex
        meta = json.dumps({"name": name, "parents": [parent_id], "description": description[:2000]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        return _call(FILE_TOKEN_ENV, "POST", UPLOAD,
                     params={"uploadType": "multipart", "fields": self.FIELDS, "supportsAllDrives": "true"},
                     headers={"Content-Type": f"multipart/related; boundary={boundary}"}, content=body).json()
