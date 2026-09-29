#!/usr/bin/env python3
"""Off-host copy of the nightly backup to Google Drive (stdlib only; runs on svr03's host Python).

Uses PersonalOS's `drive.file` token of david.rosko@obseum.cz (POS_GDRIVE_FILE_TOKEN with the OAuth client
POS_GOOGLE_CLIENT_ID / POS_GOOGLE_CLIENT_SECRET, read from the api's .env; values are never printed).
`drive.file` sees only what this app created, so the folder "PersonalOS zálohy" and the backups in it are
the only things this script can list or trash.

  gdrive.py quota                         # used / limit of the account
  gdrive.py upload FILE [--name NAME]     # into "PersonalOS zálohy" (resumable, 32 MB chunks)
  gdrive.py prune --keep-daily 7 --keep-weekly 8   # trash older pos-backup-* files (Drive empties trash in 30 days)
  gdrive.py list
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_NAME = "PersonalOS zálohy"
NAME = re.compile(r"^pos-backup-(\d{8}T\d{6}Z)-(daily|weekly)\.tar\.gpg$")
CHUNK = 32 * 1024 * 1024


def env_values(path: str, keys: tuple[str, ...]) -> dict[str, str]:
    out = {k: os.environ[k] for k in keys if os.environ.get(k)}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            k, sep, v = line.strip().partition("=")
            if sep and k in keys and k not in out:
                out[k] = v.strip().strip('"').strip("'")
    missing = [k for k in keys if not out.get(k)]
    if missing:
        sys.exit(f"gdrive: missing {', '.join(missing)} (in {path})")
    return out


class Drive:
    def __init__(self, env_file: str):
        cfg = env_values(env_file, ("POS_GOOGLE_CLIENT_ID", "POS_GOOGLE_CLIENT_SECRET", "POS_GDRIVE_FILE_TOKEN"))
        body = urllib.parse.urlencode({"client_id": cfg["POS_GOOGLE_CLIENT_ID"],
                                       "client_secret": cfg["POS_GOOGLE_CLIENT_SECRET"],
                                       "refresh_token": cfg["POS_GDRIVE_FILE_TOKEN"],
                                       "grant_type": "refresh_token"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=60) as r:
                self.token = json.load(r)["access_token"]
        except urllib.error.HTTPError as e:
            sys.exit(f"gdrive: Google refused the refresh token ({e.code})")

    def call(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None,
             raw: bool = False):
        """JSON body, or with raw=True (status, headers, body bytes); 308 is a resumable upload's "go on"."""
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                status, head, body = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            if not (raw and e.code == 308):
                sys.exit(f"gdrive: {method} {url.split('?')[0]} → {e.code}: {e.read()[:300]!r}")
            status, head, body = e.code, e.headers, e.read()
        return (status, head, body) if raw else json.loads(body or b"{}")

    def find(self, q: str) -> list[dict]:
        out, page = [], None
        while True:
            params = {"q": q, "fields": "nextPageToken,files(id,name,size,createdTime)", "pageSize": 1000,
                      "spaces": "drive"}
            if page:
                params["pageToken"] = page
            data = self.call("GET", f"{DRIVE}/files?{urllib.parse.urlencode(params)}")
            out += data.get("files", [])
            page = data.get("nextPageToken")
            if not page:
                return out

    def folder(self) -> str:
        found = self.find(f"name = '{FOLDER_NAME}' and mimeType = '{FOLDER_MIME}' and trashed = false")
        if found:
            return sorted(found, key=lambda f: f["createdTime"])[0]["id"]
        meta = json.dumps({"name": FOLDER_NAME, "mimeType": FOLDER_MIME}).encode()
        return self.call("POST", f"{DRIVE}/files?fields=id", meta, {"Content-Type": "application/json"})["id"]

    def backups(self) -> list[dict]:
        return [f for f in self.find(f"'{self.folder()}' in parents and trashed = false") if NAME.match(f["name"])]

    def upload(self, path: str, name: str) -> dict:
        size = os.path.getsize(path)
        meta = json.dumps({"name": name, "parents": [self.folder()]}).encode()
        start = self.call("POST", f"{UPLOAD}?uploadType=resumable&fields=id,name,size", meta,
                          {"Content-Type": "application/json; charset=UTF-8",
                           "X-Upload-Content-Type": "application/octet-stream",
                           "X-Upload-Content-Length": str(size)}, raw=True)
        session = start[1]["Location"]
        with open(path, "rb") as f:
            offset = 0
            while True:
                chunk = f.read(CHUNK)
                end = offset + len(chunk) - 1
                r = self.call("PUT", session, chunk, {"Content-Length": str(len(chunk)),
                                                      "Content-Range": f"bytes {offset}-{end}/{size}" if chunk
                                                      else f"bytes */{size}"}, raw=True)
                offset += len(chunk)
                if r[0] in (200, 201):
                    done = json.loads(r[2])
                    if int(done.get("size", -1)) != size:
                        sys.exit(f"gdrive: uploaded size {done.get('size')} != {size}")
                    return done
                if not chunk:
                    sys.exit("gdrive: upload ended without a final response")

    def trash(self, file_id: str) -> None:
        self.call("PATCH", f"{DRIVE}/files/{file_id}", json.dumps({"trashed": True}).encode(),
                  {"Content-Type": "application/json"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default="/opt/server/personalos/app/.env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("quota")
    sub.add_parser("list")
    up = sub.add_parser("upload")
    up.add_argument("file")
    up.add_argument("--name")
    pr = sub.add_parser("prune")
    pr.add_argument("--keep-daily", type=int, default=7)
    pr.add_argument("--keep-weekly", type=int, default=8)
    a = ap.parse_args()
    d = Drive(a.env_file)
    if a.cmd == "quota":
        q = d.call("GET", f"{DRIVE}/about?fields=storageQuota")["storageQuota"]
        gb = lambda v: round(int(v) / 1e9, 2) if v is not None else None  # noqa: E731
        print(json.dumps({"limit_gb": gb(q.get("limit")), "usage_gb": gb(q.get("usage")),
                          "drive_gb": gb(q.get("usageInDrive")), "trash_gb": gb(q.get("usageInDriveTrash"))}))
    elif a.cmd == "list":
        for f in sorted(d.backups(), key=lambda f: f["name"]):
            print(f["name"], f.get("size"))
    elif a.cmd == "upload":
        done = d.upload(a.file, a.name or os.path.basename(a.file))
        print(json.dumps({"uploaded": done["name"], "size": int(done["size"])}))
    elif a.cmd == "prune":
        files = sorted(d.backups(), key=lambda f: f["name"], reverse=True)
        keep = {f["id"] for f in [f for f in files if NAME.match(f["name"])[2] == "daily"][:a.keep_daily]}
        keep |= {f["id"] for f in [f for f in files if NAME.match(f["name"])[2] == "weekly"][:a.keep_weekly]}
        gone = [f for f in files if f["id"] not in keep]
        for f in gone:
            d.trash(f["id"])
        print(json.dumps({"kept": len(keep), "trashed": [f["name"] for f in gone]}))


if __name__ == "__main__":
    main()
