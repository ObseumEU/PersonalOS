"""Forward 127.0.0.1:8097 to knowlage, for the PersonalOS API running in Docker
on a PC whose containers cannot reach the LAN (Docker Desktop). The API uses
POS_KNOWLAGE_URL=http://host.docker.internal:8097. On the server the API talks
to knowlage over the proxy network and this is not needed.

    python ops/knowlage-bridge.py [upstream] [port]
Answers stream through unbuffered (knowlage sends Server-Sent Events).
"""

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

UPSTREAM = (sys.argv[1] if len(sys.argv) > 1 else "https://knowlage.obseum.cz").rstrip("/")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8097
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "host", "content-length"}
client = httpx.Client(timeout=httpx.Timeout(20, read=600))


class Bridge(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        try:
            with client.stream(self.command, UPSTREAM + self.path, headers=headers, content=body) as r:
                self.send_response(r.status_code)
                for k, v in r.headers.items():
                    if k.lower() not in HOP and k.lower() != "content-encoding":
                        self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in r.iter_bytes():
                    if chunk:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
        except httpx.HTTPError as e:
            msg = f"knowlage bridge: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _forward

    def log_message(self, *args) -> None:  # quiet
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Bridge).serve_forever()
