"""A tiny HTTP server that records every request (method, target, headers, body) as JSON lines."""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
out = open(sys.argv[2], "a", buffering=1)
class H(BaseHTTPRequestHandler):
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        out.write(json.dumps({"method": self.command, "target": self.path, "headers": dict(self.headers),
                              "body": body.decode("utf-8", "replace")}) + "\n")
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _any
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
