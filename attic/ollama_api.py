#!/usr/bin/env python3
"""Standalone Ollama API server — handles model listing, deletion, and benchmark."""
import http.server
import json
import urllib.request
import subprocess
import sys
import re

OLLAMA_HOST = "http://127.0.0.1:11434"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3097


class OllamaHandler(http.server.BaseHTTPRequestHandler):
    def _respond(self, code, body_dict):
        body = json.dumps(body_dict).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.rstrip("/") or "/"
        if path == "/api/ollama/models":
            try:
                req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags")
                req.add_header("Accept", "application/json")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                models = []
                for m in data.get("models", []):
                    size_bytes = m.get("size", 0)
                    if size_bytes >= 1024**3:
                        size_display = f"{size_bytes / 1024**3:.1f} GB"
                    elif size_bytes >= 1024**2:
                        size_display = f"{size_bytes / 1024**2:.0f} MB"
                    elif size_bytes >= 1024:
                        size_display = f"{size_bytes / 1024:.0f} KB"
                    else:
                        size_display = f"{size_bytes} B"
                    models.append({
                        "name": m.get("name", "unknown"),
                        "size": size_display,
                        "sizeBytes": size_bytes,
                    })
                self._respond(200, {"models": models})
            except Exception as e:
                self._respond(500, {"models": [], "error": str(e)})
        elif path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        path = self.path.rstrip("/") or "/"

        if path == "/api/ollama/models/delete":
            try:
                body = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                body = {}
            name = body.get("name", "").strip()
            if not name:
                self._respond(400, {"ok": False, "error": "Missing model name"})
                return
            try:
                result = subprocess.run(
                    ["ollama", "rm", name],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0:
                    self._respond(200, {"ok": True})
                else:
                    err = result.stderr.strip() or result.stdout.strip()
                    err = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', err).strip()
                    err = re.sub(r'\x1b\[[0-9]*[GK]', '', err).strip()
                    self._respond(200, {"ok": False, "error": err or f"exit code {result.returncode}"})
            except FileNotFoundError:
                self._respond(500, {"ok": False, "error": "ollama CLI not found"})
            except subprocess.TimeoutExpired:
                self._respond(500, {"ok": False, "error": "ollama rm timed out"})
            except Exception as e:
                self._respond(500, {"ok": False, "error": str(e)})
        else:
            self._respond(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), OllamaHandler)
    print(f"Ollama API server on port {PORT}", flush=True)
    server.serve_forever()
