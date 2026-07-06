#!/usr/bin/env python3
"""Standalone Ollama Benchmark API Server
Provides GET /api/ollama/models and POST /api/ollama/benchmark endpoints.
Run on port 3004 to avoid conflict with the main devmclovin landing server.
"""
import http.server
import json
import urllib.request
import urllib.error
import time
import os

PORT = int(os.environ.get("OLLAMA_API_PORT", "3004"))
OLLAMA_BASE = "http://127.0.0.1:11434"


def ollama_models():
    """Fetch installed models from Ollama API."""
    req = urllib.request.Request(f"{OLLAMA_BASE}/api/tags")
    req.add_header("Accept", "application/json")
    try:
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
        return {"models": models}
    except Exception as e:
        return {"models": [], "error": str(e)}


def ollama_benchmark(model, prompt):
    """Stream a generate request to Ollama and measure TTFT + total time."""
    body = json.dumps({"model": model, "prompt": prompt, "stream": True}).encode()
    req = urllib.request.Request(f"{OLLAMA_BASE}/api/generate", data=body)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/x-ndjson")
    try:
        t_start = time.time()
        ttft_ms = None
        total_ms = None
        tokens = 0
        response_text = ""
        with urllib.request.urlopen(req, timeout=60) as resp:
            leftover = b""
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                leftover += chunk
                while b"\n" in leftover:
                    line, leftover = leftover.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line.decode())
                    except Exception:
                        continue
                    if ttft_ms is None:
                        ttft_ms = (time.time() - t_start) * 1000
                    tokens += 1
                    response_text += obj.get("response", "")
                    if obj.get("done", False):
                        total_ms = (time.time() - t_start) * 1000
                        break
        if ttft_ms is None:
            return {"error": "No tokens received", "ttft_ms": 0, "total_ms": 0, "tokens": 0, "response": ""}
        if total_ms is None:
            total_ms = (time.time() - t_start) * 1000
        return {
            "ttft_ms": round(ttft_ms, 1),
            "total_ms": round(total_ms, 1),
            "tokens": tokens,
            "response": response_text.strip(),
        }
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace") if e.fp else str(e)
        return {"error": f"Ollama HTTP {e.code}: {err_body[:200]}", "ttft_ms": 0, "total_ms": 0, "tokens": 0, "response": ""}
    except Exception as e:
        return {"error": str(e), "ttft_ms": 0, "total_ms": 0, "tokens": 0, "response": ""}


class Handler(http.server.BaseHTTPRequestHandler):
    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._respond(204, "text/plain", b"")

    def do_GET(self):
        path = self.path.rstrip("/") or "/"
        if path == "/health":
            self._respond(200, "text/plain", b"ok")
        elif path == "/api/ollama/models":
            self._respond(200, "application/json", json.dumps(ollama_models()).encode())
        else:
            self._respond(404, "text/plain", b"Not Found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        path = self.path.rstrip("/") or "/"

        if path == "/api/ollama/benchmark":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            model = body_data.get("model", "").strip()
            prompt = body_data.get("prompt", "Hello").strip()
            if not model:
                self._respond(400, "application/json", json.dumps({"error": "Missing model name"}).encode())
            else:
                result = ollama_benchmark(model, prompt)
                self._respond(200, "application/json", json.dumps(result).encode())
        else:
            self._respond(404, "text/plain", b"Not Found")

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Ollama API server → http://127.0.0.1:{PORT}")
    server.serve_forever()
