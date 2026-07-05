#!/usr/bin/env python3
"""Agent Inbox server — serves the inbox frontend page on port 8001.
   Talks to the inbox API on port 8000. Matches devmclovin dark theme."""

import http.server
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
SERVE_DIR = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.rstrip("/") or "/"

        if path == "/" or path == "/inbox":
            filepath = os.path.join(SERVE_DIR, "inbox.html")
            if os.path.isfile(filepath):
                with open(filepath) as f:
                    content = f.read().encode()
                self._respond(200, "text/html", content)
            else:
                self._respond(404, "text/plain", b"Inbox page not found")
        elif path == "/health":
            self._respond(200, "text/plain", b"ok")
        else:
            self._respond(404, "text/plain", b"Not Found")

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Inbox server → http://127.0.0.1:{PORT}")
    server.serve_forever()
