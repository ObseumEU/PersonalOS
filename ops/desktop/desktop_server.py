"""The desktop sandbox for computer use (docs/BROWSER.md, "Computer use").

A small HTTP service in its own container. Idle, it is only this process
(~15 MB). A session starts the desktop lazily: Xvfb, a window manager
(fluxbox) and Chromium with a fresh profile; it ends with the run (the
worker's MCP server stops it) or after DESKTOP_IDLE_S without an action or
DESKTOP_MAX_S in total, and everything it wrote (the profile, downloads) is
deleted with it. One session at a time (memory on svr03): the next waits in
line (FIFO) for up to its `wait_s`.

    POST   /session                 {"holder": "agent#run", "wait_s": 600} -> {"session": id}
    DELETE /session/<id>
    POST   /session/<id>/action     {"action": "left_click", "coordinate": [x, y], ...}
    GET    /session/<id>/page       the browser's current URL and title
    GET    /session/<id>/inspect?x=&y=   the element under a point, and the focused field
    GET    /health

Every call but /health needs `Authorization: Bearer $DESKTOP_TOKEN`; without a
token configured the service refuses everything (fail closed).
"""

import base64
import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WIDTH, HEIGHT = (int(v) for v in os.environ.get("DESKTOP_SIZE", "1280x800").split("x"))
DISPLAY = os.environ.get("DESKTOP_DISPLAY", ":99")
IDLE_S = float(os.environ.get("DESKTOP_IDLE_S", "300"))
MAX_S = float(os.environ.get("DESKTOP_MAX_S", "1800"))
ROOT = os.environ.get("DESKTOP_ROOT", "/tmp/desktop")
CDP = "http://127.0.0.1:9222"
BROWSER = os.environ.get("DESKTOP_BROWSER", "chromium")
KEYMAP = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "backspace": "BackSpace",
          "tab": "Tab", "space": "space", "delete": "Delete", "up": "Up", "down": "Down", "left": "Left",
          "right": "Right", "pageup": "Page_Up", "pagedown": "Page_Down", "home": "Home", "end": "End",
          "cmd": "super", "win": "super", "meta": "super", "ctrl": "ctrl", "control": "ctrl", "alt": "alt",
          "shift": "shift"}


def run(args: list[str], env: dict | None = None, stdin: bytes | None = None, timeout: float = 30) -> bytes:
    out = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout,
                         env={**os.environ, "DISPLAY": DISPLAY, **(env or {})})
    if out.returncode != 0:
        raise RuntimeError(f"{args[0]}: {out.stderr.decode(errors='replace')[:300]}")
    return out.stdout


def xkey(combo: str) -> str:
    """"ctrl+s" / "Return" / "enter" -> xdotool's names."""
    return "+".join(KEYMAP.get(p.strip().lower(), p.strip()) for p in str(combo).split("+") if p.strip())


class Session:
    def __init__(self, holder: str, launcher=None):
        self.id = secrets.token_hex(8)
        self.holder = holder
        self.dir = os.path.join(ROOT, self.id)
        self.procs: list[subprocess.Popen] = []
        self.started = self.last = time.monotonic()
        self.launcher = launcher or self._launch

    def start(self, url: str = "about:blank") -> None:
        os.makedirs(self.dir, exist_ok=True)
        self.launcher(url)

    def _launch(self, url: str) -> None:
        env = {**os.environ, "DISPLAY": DISPLAY, "HOME": self.dir}
        self.procs.append(subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
        for _ in range(50):
            if subprocess.run(["xdpyinfo"], env=env, capture_output=True).returncode == 0:
                break
            time.sleep(0.1)
        self.procs.append(subprocess.Popen(["fluxbox"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=True))
        self.procs.append(subprocess.Popen([
            BROWSER, "--no-sandbox", "--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage",
            "--disable-gpu", "--disable-extensions", "--renderer-process-limit=2",
            "--js-flags=--max-old-space-size=256", "--password-store=basic",
            f"--user-data-dir={os.path.join(self.dir, 'profile')}", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222", "--window-position=0,0", f"--window-size={WIDTH},{HEIGHT}",
            "--start-maximized", url], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True))
        for _ in range(100):  # ready when the browser answers on its debugging port
            try:
                urllib.request.urlopen(CDP + "/json/version", timeout=1)
                return
            except OSError:
                time.sleep(0.1)

    def stop(self) -> None:
        for p in reversed(self.procs):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                try:
                    p.kill()
                except OSError:
                    pass
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self.procs.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    # --- the page (Chromium's DevTools endpoint, inside this container only)
    def page(self) -> dict:
        try:
            tabs = json.loads(urllib.request.urlopen(CDP + "/json/list", timeout=3).read())
        except OSError:
            return {"url": None, "title": None}
        tab = next((t for t in tabs if t.get("type") == "page"), None) or {}
        return {"url": tab.get("url"), "title": tab.get("title"), "ws": tab.get("webSocketDebuggerUrl")}

    def cdp(self, method: str, params: dict) -> dict:
        """One DevTools call on the current tab (the port listens inside this container only)."""
        import websocket  # python3-websocket

        ws_url = self.page().get("ws")
        if not ws_url:
            return {}
        ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    return msg.get("result") or {}
        finally:
            ws.close()

    def evaluate(self, expression: str):
        got = self.cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return (got.get("result") or {}).get("value")

    def inspect(self, x: int | None, y: int | None) -> dict:
        """The element under a screen point and the focused field, as short labels."""
        expr = """(() => {
          const label = (el) => {
            if (!el) return null;
            const b = el.closest('button, a, input, textarea, select, [role=button], [role=link]') || el;
            const t = (b.getAttribute('aria-label') || b.value || b.innerText || b.title || b.placeholder
                       || b.name || '').toString().trim().slice(0, 80);
            const kind = b.tagName.toLowerCase() + (b.type ? '[' + b.type + ']' : '');
            return (kind + ' ' + t).trim();
          };
          const X = %s, Y = %s;
          let at = null;
          if (X !== null) {
            const top = window.outerHeight - window.innerHeight;
            at = label(document.elementFromPoint(X - window.screenX, Y - window.screenY - top));
          }
          const f = document.activeElement;
          return {at, focused: f && f !== document.body ? label(f) : null};
        })()""" % ("null" if x is None else int(x), "null" if y is None else int(y))
        try:
            got = self.evaluate(expr) or {}
        except Exception:  # noqa: BLE001 - no page yet, a crashed tab: nothing to say
            got = {}
        return {**{k: v for k, v in self.page().items() if k != "ws"}, **got}

    # --- actions (names as in Anthropic's computer use tool)
    def screenshot(self, region: list[int] | None = None) -> str:
        png = run(["import", "-silent", "-window", "root", "png:-"])
        if region:
            x0, y0, x1, y1 = (int(v) for v in region)
            png = run(["convert", "png:-", "-crop", f"{x1 - x0}x{y1 - y0}+{x0}+{y0}", "+repage",
                       "-resize", f"{WIDTH}x{HEIGHT}>", "png:-"], stdin=png)
        return base64.b64encode(png).decode()

    def act(self, a: dict) -> dict:
        self.last = time.monotonic()
        name = str(a.get("action") or "")
        xy = a.get("coordinate")
        mods = [xkey(m) for m in str(a.get("text") or "").split("+") if m] if name.endswith("click") else []
        if name == "screenshot":
            return {"image": self.screenshot(), "width": WIDTH, "height": HEIGHT}
        if name == "zoom":
            return {"image": self.screenshot(a.get("region")), "width": WIDTH, "height": HEIGHT}
        if name == "cursor_position":
            out = run(["xdotool", "getmouselocation", "--shell"]).decode()
            vals = dict(line.split("=", 1) for line in out.split() if "=" in line)
            return {"coordinate": [int(vals.get("X", 0)), int(vals.get("Y", 0))]}
        if xy:
            run(["xdotool", "mousemove", "--sync", str(int(xy[0])), str(int(xy[1]))])
        if name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            button = {"right_click": "3", "middle_click": "2"}.get(name, "1")
            repeat = {"double_click": "2", "triple_click": "3"}.get(name, "1")
            for m in mods:
                run(["xdotool", "keydown", m])
            try:
                run(["xdotool", "click", "--repeat", repeat, "--delay", "80", button])
            finally:
                for m in mods:
                    run(["xdotool", "keyup", m])
        elif name == "left_click_drag":
            sx, sy = a["start_coordinate"]
            ex, ey = a["coordinate"]
            run(["xdotool", "mousemove", "--sync", str(int(sx)), str(int(sy)), "mousedown", "1",
                 "mousemove", "--sync", str(int(ex)), str(int(ey)), "mouseup", "1"])
        elif name == "left_mouse_down":
            run(["xdotool", "mousedown", "1"])
        elif name == "left_mouse_up":
            run(["xdotool", "mouseup", "1"])
        elif name == "mouse_move":
            pass
        elif name == "type":
            # From stdin: the text never shows in a process list.
            run(["xdotool", "type", "--delay", "12", "--file", "-"], stdin=str(a.get("text") or "").encode())
        elif name == "key":
            for _ in range(max(1, min(int(a.get("repeat") or 1), 100))):
                run(["xdotool", "key", "--clearmodifiers", xkey(a.get("text") or "")])
        elif name == "scroll":
            button = {"up": "4", "down": "5", "left": "6", "right": "7"}[str(a.get("scroll_direction") or "down")]
            run(["xdotool", "click", "--repeat", str(max(1, min(int(a.get("scroll_amount") or 3), 30))), button])
        elif name == "wait":
            time.sleep(max(0.0, min(float(a.get("duration") or 1), 30)))
        elif name == "open_url":
            url = str(a.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise ValueError("open_url takes an http(s) URL")
            self.cdp("Page.navigate", {"url": url})
            for _ in range(30):  # until it has loaded (at most ~6 s)
                time.sleep(0.2)
                if self.evaluate("document.readyState") == "complete" and self.page().get("url") != "about:blank":
                    break
        else:
            raise ValueError(f"unknown action {name!r}")
        self.last = time.monotonic()
        return {"ok": True}


class Desktop:
    """At most one session; the next holders wait in line (FIFO)."""

    def __init__(self, launcher=None):
        self.cv = threading.Condition()
        self.session: Session | None = None
        self.line: list[str] = []
        self.launcher = launcher

    def open(self, holder: str, wait_s: float, url: str = "about:blank") -> Session | None:
        ticket = secrets.token_hex(4)
        deadline = time.monotonic() + max(0.0, wait_s)
        with self.cv:
            self.line.append(ticket)
            try:
                while self.session is not None or self.line[0] != ticket:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    self.cv.wait(min(left, 5))
                    self._reap()
                s = Session(holder, self.launcher)
                self.session = s
            finally:
                self.line.remove(ticket)
                self.cv.notify_all()
        try:
            s.start(url)
        except Exception:
            self.close(s.id)
            raise
        return s

    def get(self, sid: str) -> Session | None:
        with self.cv:
            return self.session if self.session and self.session.id == sid else None

    def close(self, sid: str) -> bool:
        with self.cv:
            s = self.session if self.session and self.session.id == sid else None
            if s:
                self.session = None
        if s:
            s.stop()
            with self.cv:
                self.cv.notify_all()
        return bool(s)

    def _reap(self) -> None:
        s = self.session
        now = time.monotonic()
        if s and (now - s.last > IDLE_S or now - s.started > MAX_S):
            self.session = None
            threading.Thread(target=s.stop, daemon=True).start()

    def reaper(self) -> None:
        while True:
            time.sleep(10)
            with self.cv:
                self._reap()
                self.cv.notify_all()

    def state(self) -> dict:
        with self.cv:
            return {"busy": bool(self.session), "holder": self.session.holder if self.session else None,
                    "waiting": len(self.line)}


def make_handler(desktop: Desktop, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # no request log (URLs may carry task details)
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            return bool(token) and secrets.compare_digest(got, f"Bearer {token}")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _route(self, method: str) -> None:
            path = urlparse(self.path)
            parts = [p for p in path.path.split("/") if p]
            if parts == ["health"]:
                return self._send(200, {"ok": True, **desktop.state()})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            try:
                if method == "POST" and parts == ["session"]:
                    b = self._body()
                    s = desktop.open(str(b.get("holder") or "?")[:80], float(b.get("wait_s") or 0),
                                     str(b.get("url") or "about:blank"))
                    if s is None:
                        return self._send(409, {"error": "the desktop is busy", **desktop.state()})
                    return self._send(200, {"session": s.id, "width": WIDTH, "height": HEIGHT})
                if len(parts) >= 2 and parts[0] == "session":
                    s = desktop.get(parts[1])
                    if method == "DELETE" and len(parts) == 2:
                        return self._send(200, {"closed": desktop.close(parts[1])})
                    if s is None:
                        return self._send(404, {"error": "no such session (ended or timed out)"})
                    if method == "POST" and parts[2:] == ["action"]:
                        return self._send(200, s.act(self._body()))
                    if method == "GET" and parts[2:] == ["page"]:
                        return self._send(200, {k: v for k, v in s.page().items() if k != "ws"})
                    if method == "GET" and parts[2:] == ["inspect"]:
                        q = parse_qs(path.query)
                        x = int(q["x"][0]) if "x" in q else None
                        y = int(q["y"][0]) if "y" in q else None
                        return self._send(200, s.inspect(x, y))
                return self._send(404, {"error": "not found"})
            except (ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as e:
                return self._send(400, {"error": str(e)[:300]})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


def main() -> None:
    desktop = Desktop()
    threading.Thread(target=desktop.reaper, daemon=True).start()
    port = int(os.environ.get("DESKTOP_PORT", "8100"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(desktop, os.environ.get("DESKTOP_TOKEN", "")))
    signal.signal(signal.SIGTERM, lambda *_: (desktop.close(desktop.session.id) if desktop.session else None,
                                              os._exit(0)))
    server.serve_forever()


if __name__ == "__main__":
    main()
