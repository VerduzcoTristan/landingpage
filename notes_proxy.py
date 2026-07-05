#!/usr/bin/env python3
"""Tiny proxy server: forwards /notes* to the Notes API on port 8123 with auth."""

import http.server
import json
import os
import urllib.request

NOTES_API = "http://127.0.0.1:8123"
NOTES_TOKEN = os.environ.get("NOTES_API_TOKEN", "notes-secret-token")
PORT = int(os.environ.get("NOTES_PROXY_PORT", "3005"))


class Proxy(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def _proxy(self):
        path = self.path
        url = NOTES_API + path
        req = urllib.request.Request(url, method=self.command)
        req.add_header("Authorization", "Bearer " + NOTES_TOKEN)

        # Forward body for POST/PATCH
        content_type = self.headers.get("Content-Type", "")
        if content_type:
            req.add_header("Content-Type", content_type)

        if self.command in ("POST", "PATCH"):
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                req.data = self.rfile.read(length)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:
            err = json.dumps({"error": f"Notes API unreachable: {str(e)}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Proxy)
    print(f"Notes proxy → http://127.0.0.1:{PORT} (→ {NOTES_API})")
    server.serve_forever()
