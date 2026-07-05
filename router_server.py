#!/usr/bin/env python3
"""
Standalone LLM Router Dashboard server.
Serves the dashboard HTML and API endpoint on port 3003.
Independent of the main devmclovin-landing server.
"""
import http.server
import json
import os
import urllib.parse
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from router_metrics import get_router_metrics

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3003


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/router" or path == "/":
            # Serve the dashboard HTML
            dash_path = os.path.join(SCRIPT_DIR, "router-dashboard.html")
            try:
                with open(dash_path) as f:
                    content = f.read().encode()
                self._respond(200, "text/html", content)
            except FileNotFoundError:
                self._respond(404, "text/plain", b"Dashboard not found")

        elif path == "/api/router-metrics":
            window = qs.get("window", ["today"])[0]
            data = get_router_metrics(window=window)
            self._respond(200, "application/json", json.dumps(data).encode())

        elif path == "/health":
            self._respond(200, "text/plain", b"ok")

        else:
            self._respond(404, "text/plain", b"Not Found")

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Router dashboard → http://127.0.0.1:{PORT}/router")
    server.serve_forever()
