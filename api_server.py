#!/usr/bin/env python3
"""
Backend API server for service status, logs, restart, and config.

Endpoints:
  GET  /api/status              → Aggregated status of all services (Online/Offline/Last ran)
  GET  /api/service/<name>/logs  → Recent logs for a service
  POST /api/service/<name>/restart → Trigger service restart
  GET  /api/service/<name>/config → Config file content for a service

Uses the health check infrastructure from health_check.py.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Health check module path ──
HEALTH_CHECK_PATH = os.path.join(
    os.path.expanduser("~/.hermes/kanban/boards/home-server/workspaces/t_b7412a55"),
    "health_check.py"
)
if os.path.exists(HEALTH_CHECK_PATH):
    sys.path.insert(0, os.path.dirname(HEALTH_CHECK_PATH))
    import health_check as hc
else:
    print(f"WARNING: health_check.py not found at {HEALTH_CHECK_PATH}", file=sys.stderr)
    hc = None

# ── Service definitions with log, restart, and config mappings ──

HERMES_HOME = os.path.expanduser("~/.hermes")
HERMES_LOGS = os.path.join(HERMES_HOME, "logs")
DASHBOARD_CMD = [
    os.path.join(HERMES_HOME, "hermes-agent/venv/bin/python"),
    "-m", "hermes_cli.main", "dashboard",
    "--host", "0.0.0.0", "--port", "9119", "--no-open",
]

SERVICE_DETAILS = {
    "hermes_dashboard": {
        "display_name": "Hermes Dashboard",
        "log_method": "file",
        "log_paths": [
            os.path.join(HERMES_LOGS, "gateway.log"),
            os.path.join(HERMES_LOGS, "agent.log"),
            os.path.join(HERMES_LOGS, "errors.log"),
        ],
        "restart_method": "process",
        "restart_cmd": DASHBOARD_CMD,
        "restart_process_arg": "hermes_cli.main dashboard",
        "config_method": "file",
        "config_paths": [
            os.path.join(HERMES_HOME, "config.yaml"),
        ],
        "port": 9119,
    },
    "ollama": {
        "display_name": "Ollama",
        "log_method": "journalctl",
        "log_journal_unit": "ollama",
        "restart_method": "systemctl",
        "restart_unit": "ollama",
        "config_method": "file",
        "config_paths": [
            "/etc/systemd/system/ollama.service.d/override.conf",
            "/etc/systemd/system/ollama.service",
        ],
        "port": 11434,
    },
    "cloudflare_tunnel": {
        "display_name": "Cloudflare Tunnel",
        "log_method": "journalctl",
        "log_journal_unit": "cloudflared",
        "restart_method": "systemctl",
        "restart_unit": "cloudflared",
        "config_method": "file",
        "config_paths": [
            "/etc/cloudflared/config.yml",
        ],
        "port": 20241,
    },
    "searxng": {
        "display_name": "SearXNG",
        "log_method": "journalctl",
        "log_journal_unit": None,  # Docker container — try journalctl for containerd
        "restart_method": "docker",
        "restart_container": "searxng",
        "restart_note": "Docker container — restart may require Docker daemon access",
        "config_method": "unknown",
        "config_paths": [],
        "config_note": "SearXNG runs in a Docker container. Config is inside the container filesystem.",
        "port": 8080,
    },
    "llm_router": {
        "display_name": "LLM Router",
        "log_method": "journalctl",
        "log_journal_unit": None,  # Docker container
        "restart_method": "docker",
        "restart_container": "llm-router",
        "restart_note": "Docker container — restart may require Docker daemon access",
        "config_method": "unknown",
        "config_paths": [],
        "config_note": "LLM Router runs in a Docker container. Config is inside the container filesystem.",
        "port": None,
    },
    "github_backup": {
        "display_name": "GitHub Backup (cron)",
        "log_method": "cron",
        "log_job_ids": ["283f21414061", "f5581a23f291"],
        "restart_method": "cron_trigger",
        "restart_job_ids": ["283f21414061", "f5581a23f291"],
        "restart_note": "Cron jobs — 'restart' triggers a manual run of both backup jobs",
        "config_method": "cron",
        "config_paths": [],
        "config_note": "Cron jobs managed by Hermes cron system.",
        "port": None,
    },
}


def service_status_string(healthy, svc_type):
    """Map health check result to a user-friendly status string."""
    if svc_type == "cron":
        return "Last ran OK" if healthy else "Last run failed"
    return "Online" if healthy else "Offline"


def check_all_services():
    """Run health checks for all services and return enriched status."""
    if hc is None:
        return {"status": "error", "error": "health_check module not available"}

    results = hc.check_all()
    now = datetime.now(timezone.utc).isoformat()

    enriched = {
        "status": results["status"],
        "timestamp": now,
        "services": {},
    }

    base_url = "http://localhost:9091"

    for key, svc in results["services"].items():
        details = SERVICE_DETAILS.get(key, {})
        status_str = service_status_string(svc["healthy"], svc["type"])

        enriched["services"][key] = {
            "name": svc["name"],
            "status": status_str,
            "healthy": svc["healthy"],
            "detail": svc["detail"],
            "type": svc["type"],
            "port": details.get("port"),
            "restart_available": details.get("restart_method") not in ("docker", "unknown"),
            "actions": {
                "logs": f"{base_url}/api/service/{key}/logs",
                "restart": f"{base_url}/api/service/{key}/restart",
                "config": f"{base_url}/api/service/{key}/config",
            },
        }

    return enriched


def get_service_logs(service_name):
    """Get recent logs for a service. Returns (data, status_code) tuple."""
    details = SERVICE_DETAILS.get(service_name)
    if not details:
        return {"error": f"Unknown service: {service_name}"}, 404

    method = details.get("log_method", "unknown")
    logs_text = ""
    log_sources = []

    try:
        if method == "journalctl":
            unit = details.get("log_journal_unit")
            if unit:
                result = subprocess.run(
                    ["journalctl", "-u", unit, "--no-pager", "-n", "100"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    logs_text = result.stdout.strip()
                    log_sources.append(f"journalctl -u {unit}")
                else:
                    logs_text = f"No journal entries found for {unit}."
            else:
                logs_text = f"No journal unit configured for {service_name}. Service runs in Docker — logs unavailable from host without Docker access."

        elif method == "file":
            paths = details.get("log_paths", [])
            for path in paths:
                if os.path.exists(path):
                    try:
                        with open(path, "r") as f:
                            content = f.read()
                        # Get last ~50 lines per file to keep response manageable
                        lines = content.split("\n")
                        recent = "\n".join(lines[-50:]) if len(lines) > 50 else content
                        logs_text += f"\n=== {os.path.basename(path)} ===\n{recent}\n"
                        log_sources.append(path)
                    except Exception as e:
                        logs_text += f"\n=== {os.path.basename(path)} ===\nError reading: {e}\n"
            if not logs_text:
                logs_text = "No log files found."

        elif method == "cron":
            # Cron logs are fetched via Hermes cronjob tool — not available as subprocess
            logs_text = (
                "Cron job logs are managed by the Hermes cron system.\n"
                "Use 'cronjob list' to see job status and last-run timestamps.\n\n"
                "Job IDs:\n"
            )
            for jid in details.get("log_job_ids", []):
                logs_text += f"  - {jid}\n"

        else:
            logs_text = f"Log retrieval method '{method}' not implemented for {service_name}."

    except Exception as e:
        logs_text = f"Error retrieving logs: {e}"

    return {
        "service": service_name,
        "name": details.get("display_name", service_name),
        "sources": log_sources,
        "logs": logs_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, 200


def restart_service(service_name):
    """Trigger a service restart."""
    details = SERVICE_DETAILS.get(service_name)
    if not details:
        return {"error": f"Unknown service: {service_name}"}, 404

    method = details.get("restart_method", "unknown")

    try:
        if method == "systemctl":
            unit = details.get("restart_unit")
            if not unit:
                return {"error": f"No systemd unit configured for {service_name}"}, 500

            result = subprocess.run(
                ["systemctl", "restart", unit],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                return {
                    "service": service_name,
                    "name": details.get("display_name", service_name),
                    "action": "restart",
                    "result": "success",
                    "message": f"systemctl restart {unit} completed successfully",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }, 200
            else:
                stderr_lower = result.stderr.lower()
                if "access denied" in stderr_lower or "authentication" in stderr_lower:
                    return {
                        "service": service_name,
                        "name": details.get("display_name", service_name),
                        "action": "restart",
                        "result": "auth_required",
                        "message": f"systemctl restart {unit} requires elevated privileges",
                        "detail": result.stderr.strip(),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, 403
                return {
                    "service": service_name,
                    "name": details.get("display_name", service_name),
                    "action": "restart",
                    "result": "failed",
                    "message": f"systemctl restart {unit} failed",
                    "detail": result.stderr.strip(),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }, 500

        elif method == "process":
            # Kill existing process, then restart
            proc_arg = details.get("restart_process_arg")
            if proc_arg:
                # Kill existing
                subprocess.run(
                    ["pkill", "-f", proc_arg],
                    capture_output=True, text=True, timeout=5,
                )
                time.sleep(1)

            # Start new process
            cmd = details.get("restart_cmd", [])
            if cmd:
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return {
                    "service": service_name,
                    "name": details.get("display_name", service_name),
                    "action": "restart",
                    "result": "success",
                    "message": f"Process restarted: {' '.join(cmd)}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }, 200
            else:
                return {"error": f"No restart command configured for {service_name}"}, 500

        elif method == "docker":
            container = details.get("restart_container", service_name)
            return {
                "service": service_name,
                "name": details.get("display_name", service_name),
                "action": "restart",
                "result": "unavailable",
                "message": f"Docker container restart not available from this context. Container: {container}",
                "note": details.get("restart_note", "Docker daemon access required."),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, 503

        elif method == "cron_trigger":
            return {
                "service": service_name,
                "name": details.get("display_name", service_name),
                "action": "restart",
                "result": "unavailable",
                "message": "Cron jobs cannot be restarted — they run on schedule. Use 'cronjob run' to trigger manually.",
                "job_ids": details.get("restart_job_ids", []),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, 200

        else:
            return {"error": f"Restart method '{method}' not implemented for {service_name}"}, 501

    except Exception as e:
        return {
            "service": service_name,
            "name": details.get("display_name", service_name),
            "action": "restart",
            "result": "error",
            "message": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, 500


def get_service_config(service_name):
    """Get config file content for a service. Returns (data, status_code) tuple."""
    details = SERVICE_DETAILS.get(service_name)
    if not details:
        return {"error": f"Unknown service: {service_name}"}, 404

    method = details.get("config_method", "unknown")
    configs = {}

    if method == "file":
        paths = details.get("config_paths", [])
        for path in paths:
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        configs[path] = f.read()
                except Exception as e:
                    configs[path] = f"Error reading: {e}"
            else:
                configs[path] = "File not found"
    elif method == "unknown":
        pass  # configs stays empty

    return {
        "service": service_name,
        "name": details.get("display_name", service_name),
        "configs": configs,
        "note": details.get("config_note") if not configs else "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, 200


def save_service_config(service_name, path, content):
    """Save config content to a service's config file. Returns (data, status_code) tuple."""
    details = SERVICE_DETAILS.get(service_name)
    if not details:
        return {"error": f"Unknown service: {service_name}"}, 404

    method = details.get("config_method", "unknown")
    if method != "file":
        return {
            "error": f"Config editing not supported for {service_name}",
            "note": details.get("config_note", f"Config method '{method}' does not support editing."),
        }, 400

    valid_paths = details.get("config_paths", [])
    if not valid_paths:
        return {"error": f"No config paths configured for {service_name}"}, 400

    if path not in valid_paths:
        return {
            "error": f"Invalid config path '{path}' for {service_name}",
            "valid_paths": valid_paths,
        }, 400

    # Write the file
    try:
        with open(path, "w") as f:
            f.write(content)
    except PermissionError:
        return {
            "error": f"Permission denied writing to {path}",
            "service": service_name,
            "name": details.get("display_name", service_name),
        }, 403
    except Exception as e:
        return {
            "error": f"Failed to write config: {e}",
            "service": service_name,
        }, 500

    return {
        "service": service_name,
        "name": details.get("display_name", service_name),
        "path": path,
        "result": "saved",
        "message": f"Config saved to {path}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, 200


# ── HTTP Request Handler ──

class APIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the service API."""

    def _send_json(self, data, status=200):
        """Send a JSON response with CORS headers."""
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_path(self):
        """Parse the request path into components."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        return path, parse_qs(parsed.query)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        path, params = self._parse_path()

        # GET /api/status
        if path == "/api/status" or path == "" or path == "/":
            data = check_all_services()
            self._send_json(data)

        # GET /api/service/<name>/logs
        elif path.startswith("/api/service/") and path.endswith("/logs"):
            service_name = path[len("/api/service/"):-len("/logs")]
            data, status = get_service_logs(service_name)
            self._send_json(data, status)

        # GET /api/service/<name>/config
        elif path.startswith("/api/service/") and path.endswith("/config"):
            service_name = path[len("/api/service/"):-len("/config")]
            data, status = get_service_config(service_name)
            self._send_json(data, status)

        # GET /api/service/<name> (service detail)
        elif path.startswith("/api/service/"):
            service_name = path[len("/api/service/"):]
            # Return status for a single service
            all_data = check_all_services()
            svc = all_data["services"].get(service_name)
            if svc:
                details = SERVICE_DETAILS.get(service_name, {})
                svc["logs_url"] = f"/api/service/{service_name}/logs"
                svc["restart_url"] = f"/api/service/{service_name}/restart"
                svc["config_url"] = f"/api/service/{service_name}/config"
                self._send_json(svc)
            else:
                self._send_json({"error": f"Unknown service: {service_name}"}, 404)

        # GET /api/health (backward compat with health check server)
        elif path == "/api/health" or path == "/health":
            if hc:
                data = hc.check_all()
                status = 200 if data["status"] == "healthy" else 503
                self._send_json(data, status)
            else:
                self._send_json({"error": "health check not available"}, 500)

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        """Handle POST requests."""
        path, params = self._parse_path()

        # POST /api/service/<name>/restart
        if path.startswith("/api/service/") and path.endswith("/restart"):
            service_name = path[len("/api/service/"):-len("/restart")]
            data, status = restart_service(service_name)
            self._send_json(data, status)

        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self):
        """Handle PUT requests — config editing."""
        path, params = self._parse_path()

        # PUT /api/service/<name>/config
        if path.startswith("/api/service/") and path.endswith("/config"):
            service_name = path[len("/api/service/"):-len("/config")]
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self._send_json({"error": "Missing request body"}, 400)
                return
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
                config_path = body.get("path", "")
                content = body.get("content", "")
                if not config_path:
                    self._send_json({"error": "Missing 'path' in request body"}, 400)
                    return
                data, status = save_service_config(service_name, config_path, content)
                self._send_json(data, status)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON body"}, 400)
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        """Log to stderr so we can debug."""
        import sys as _sys
        print(format % args, file=_sys.stderr)


def main():
    """Start the API server."""
    port = 9091
    if len(sys.argv) > 2 and sys.argv[1] == "--port":
        port = int(sys.argv[2])
    elif len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    server = HTTPServer(("127.0.0.1", port), APIHandler)
    print(f"Service API server listening on http://127.0.0.1:{port}")
    print(f"Endpoints:")
    print(f"  GET  http://127.0.0.1:{port}/api/status")
    print(f"  GET  http://127.0.0.1:{port}/api/service/<name>/logs")
    print(f"  POST http://127.0.0.1:{port}/api/service/<name>/restart")
    print(f"  GET  http://127.0.0.1:{port}/api/service/<name>/config")
    print(f"  GET  http://127.0.0.1:{port}/api/health")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
