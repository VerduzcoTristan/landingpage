#!/usr/bin/env python3
"""Standalone runbook server — serves runbooks on port 3009.
Independent of server.py to avoid concurrency conflicts.
"""
import http.server
import sys
sys.path.insert(0, "/home/hermes/devmclovin-landing")
from runbook_data import runbooks_page

PORT = 3009

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/runbooks":
            content = runbooks_page().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(302)
            self.send_header("Location", "/runbooks")
            self.end_headers()

    def log_message(self, format, *args):
        pass  # quiet

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print(f"Runbook server listening on 127.0.0.1:{port}")
    server.serve_forever()
