"""The operator's CLI for the server-side credential store (pos.credentials.serverstore). Inside the api container:

    # one field from an env file (the file stays on the host: pipe it in with "-")
    docker exec -i personalos-api-1 python -m pos.credentials put kniha-test-admin \\
        --from-env-file - --key ADMIN_HESLO --basic-user admin < /opt/server/kniha-test/.env
    # a value on stdin (first line), into the field `password` (or --field NAME)
    printf %s "$VALUE" | docker exec -i personalos-api-1 python -m pos.credentials put some-name
    docker exec personalos-api-1 python -m pos.credentials list            # names, fields, times: never values
    docker exec personalos-api-1 python -m pos.credentials check kniha-test-admin   # ok / missing
    docker exec personalos-api-1 python -m pos.credentials delete some-name

--basic-user USER stores `username`, `password` and `authorization` (base64 "USER:value", for a registry entry
with the header 'Authorization: Basic {value}'). Nothing here prints a value. Agents have no way to call it.
"""

import argparse
import sys


def _env_value(text: str, key: str) -> str | None:
    found = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        if k == key:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            found = v
    return found


def _audit(action: str, **detail) -> None:
    """The platform's audit trail (without the value); best effort: the store works without the DB."""
    try:
        from ..business import system_ctx
        from ..config import get_settings
        from ..db import connect
        from .. import audit

        conn = connect(get_settings().db_path)
        try:
            audit.log(conn, system_ctx(conn), action, None, None, via_cli=True, **detail)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - never block the operator on the audit; never print a value
        pass


def main(argv: list[str] | None = None) -> int:
    from . import serverstore as ss

    ap = argparse.ArgumentParser(prog="python -m pos.credentials", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("put", help="store or update an entry (value from an env file or stdin)")
    p.add_argument("name")
    p.add_argument("--from-env-file", dest="env_file", help="an env file (- = stdin) to take --key from")
    p.add_argument("--key", help="the variable in the env file")
    p.add_argument("--field", default="password", help="the field to store the value in (default password)")
    p.add_argument("--basic-user", help="also store username and authorization = base64(user:value)")
    p.add_argument("--source", default="", help="where the value comes from (shown in list)")
    p.add_argument("--replace", action="store_true", help="drop the entry's other fields")
    sub.add_parser("list", help="entries with their field names (never values)")
    c = sub.add_parser("check", help="does an entry (or pos://entry/field) resolve: ok / missing")
    c.add_argument("ref")
    d = sub.add_parser("delete", help="remove an entry")
    d.add_argument("name")
    a = ap.parse_args(argv)

    if a.cmd == "list":
        for m in ss.entries():
            print(f"{m['name']}\tfields={','.join(m['fields'])}\tupdated={m['updated_at']}\tsource={m['source']}")
        return 0
    if a.cmd == "check":
        ref = a.ref if ss.is_ref(a.ref) else None
        if ref is None:
            print("ok" if ss.meta(a.ref) else "missing")
            return 0 if ss.meta(a.ref) else 1
        try:
            ss.resolve(ref)
            print("ok")
            return 0
        except ss.Unavailable as e:
            print(f"missing ({e})")
            return 1
    if a.cmd == "delete":
        ok = ss.delete(a.name)
        print("deleted" if ok else "no such entry")
        if ok:
            _audit("cred_store_delete", name=a.name)
        return 0 if ok else 1

    # put
    if a.env_file:
        if not a.key:
            ap.error("--from-env-file needs --key")
        text = sys.stdin.read() if a.env_file == "-" else open(a.env_file, encoding="utf-8").read()
        value = _env_value(text, a.key)
        if not value:
            print(f"{a.key} is not set in that env file", file=sys.stderr)
            return 2
        source = a.source or f"{'stdin' if a.env_file == '-' else a.env_file}:{a.key}"
    else:
        value = (sys.stdin.readline() or "").rstrip("\r\n")
        if not value:
            print("no value on stdin", file=sys.stderr)
            return 2
        source = a.source or "stdin"
    fields = ss.basic_fields(a.basic_user, value) if a.basic_user else {a.field: value}
    try:
        m = ss.put(a.name, fields, source=source, replace=a.replace)
    except (ValueError, ss.Unavailable) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    del value, fields
    _audit("cred_store_put", name=a.name, fields=m.get("fields"), source=source)
    print(f"stored {a.name}: fields={','.join(m.get('fields') or [])} -> {ss.ref(a.name, '<field>')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
