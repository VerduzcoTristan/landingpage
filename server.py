#!/usr/bin/env python3
"""Landing page server for devmclovin.com — dark mode, morning briefings, Hermes link."""

import http.server
import html
import json
import os
import re
import glob
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from runbook_data import runbooks_page

PORT = 3002
BRIEFING_DIR = Path(os.path.expanduser("~/.hermes/cron/output/7dc1d641173d"))
SITE_DIR = Path(__file__).parent

# ── Model Comparison DB ──
# The price-comparison DB lives in a separate repo that may be absent/moved.
# fetch_models() is the SINGLE read path for both /models and /api/models and
# never crashes the server on a missing DB — it returns an error string instead.
MODELS_DB = os.path.expanduser("~/projects/model-price-comparison/models.db")


def fetch_models() -> tuple[list[dict], str | None]:
    """Return (rows, error). error is a message when the models DB is unavailable
    so callers can render a friendly state instead of raising at import/route time."""
    try:
        conn = sqlite3.connect(MODELS_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT model_id, name, provider, input_price_per_mtok, "
            "output_price_per_mtok, context_window, best_use_case, "
            "preferred_role, notes FROM models ORDER BY provider, name"
        ).fetchall()
        conn.close()
    except Exception as e:
        return [], f"Models DB unavailable: {e}"
    return [{
        "model_id": r["model_id"], "name": r["name"], "provider": r["provider"],
        "input_price_per_mtok": r["input_price_per_mtok"],
        "output_price_per_mtok": r["output_price_per_mtok"],
        "context_window": r["context_window"],
        "best_use_case": r["best_use_case"],
        "preferred_role": r["preferred_role"], "notes": r["notes"] or "",
    } for r in rows], None

# ── Auth helpers (Cloudflare Access) ──
def is_authenticated(handler) -> bool:
    """Check Cloudflare Access JWT or localhost bypass."""
    client_ip = handler.client_address[0] if hasattr(handler, "client_address") else ""
    if client_ip in ("127.0.0.1", "::1"):
        return True
    cf_email = handler.headers.get("Cf-Access-Authenticated-User-Email", "")
    return bool(cf_email)

_UNAUTH_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>403 — devmclovin</title><style>
:root{--bg:#0d1117;--text:#e6edf3;--text-muted:#8b949e;--border:#30363d;--accent:#7c3aed}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{text-align:center;padding:3rem 2rem;max-width:420px}
.card h1{font-size:4rem;color:var(--accent);margin-bottom:0.5rem}
.card p{color:var(--text-muted);line-height:1.6}
</style></head><body><div class="card"><h1>403</h1><p>You must be authenticated to access this page.</p></div></body></html>"""

# ── Briefing Archive (DB-backed) ──
sys.path.insert(0, os.path.expanduser("~/.hermes/tools"))
from briefing_archive import BriefingArchive

_ARCHIVE: BriefingArchive | None = None

def _get_archive() -> BriefingArchive:
    global _ARCHIVE
    if _ARCHIVE is None:
        _ARCHIVE = BriefingArchive()
    return _ARCHIVE

# ── Predefined categories (display order) ──
CATEGORY_ORDER = ["AI", "coding", "security", "homelab", "finance", "GitHub"]
CATEGORY_COLORS = {
    "AI":        ("#a855f7", "#e9d5ff"),  # purple
    "coding":    ("#06b6d4", "#cffafe"),  # cyan
    "security":  ("#ef4444", "#fee2e2"),  # red
    "homelab":   ("#f59e0b", "#fef3c7"),  # amber
    "finance":   ("#10b981", "#d1fae5"),  # emerald
    "GitHub":    ("#6366f1", "#e0e7ff"),  # indigo
    "general":   ("#6b7280", "#f3f4f6"),  # gray
}

# ── New feature paths ──
QUICKLINKS_FILE = Path(os.path.expanduser("~/.devmclovin/quicklinks.json"))
CRON_JOBS_FILE = Path(os.path.expanduser("~/.hermes/cron/jobs.json"))
CRON_OUTPUT_DIR = Path(os.path.expanduser("~/.hermes/cron/output"))
KANBAN_DB = Path(os.path.expanduser("~/.hermes/kanban.db"))
BRIEFING_DB = Path(os.path.expanduser("~/.hermes/data/briefings.db"))
IMPACT_CACHE_DIR = Path(os.path.expanduser("~/.devmclovin/impacts"))
BOOKMARKS_FILE = Path(os.path.expanduser("~/.devmclovin/bookmarks.json"))
TUNING_DIR = Path(os.path.expanduser("~/.devmclovin/model-tuning"))
TUNING_EXAMPLES_FILE = TUNING_DIR / "examples.json"
TUNING_CONFIG_DIR = TUNING_DIR / "configs"
TUNING_DATASET_DIR = TUNING_DIR / "datasets"
HF_DOWNLOAD_DIR = Path(os.path.expanduser("~/models/huggingface"))
LLM_LAB_DIR = TUNING_DIR / "lab"
LLM_LAB_EVALS_FILE = LLM_LAB_DIR / "eval_suites.json"
LLM_LAB_TRACES_FILE = LLM_LAB_DIR / "traces.json"
LLM_LAB_ARENA_FILE = LLM_LAB_DIR / "arena.json"
LLM_LAB_ROUTER_FILE = LLM_LAB_DIR / "router.json"
LLM_LAB_PROMPTS_FILE = LLM_LAB_DIR / "prompt_library.json"
LLM_LAB_PRESETS_FILE = LLM_LAB_DIR / "settings_presets.json"
LLM_LAB_RUNS_FILE = LLM_LAB_DIR / "runs.json"

# ── GitHub projects cache ──
_GITHUB_CACHE: dict = {"data": None, "ts": 0, "username": None}
_OPENROUTER_CACHE: dict = {"data": None, "ts": 0}
_CACHE_TTL = 300  # 5 minutes
# ── System status cache ──
_SYS_CACHE: dict = {"data": None, "ts": 0}
# ── Link Health Check ──
_LINK_HEALTH_CACHE: dict = {}
_LINK_HEALTH_TTL = 600  # 10 minutes
_INTERNAL_DOMAINS = os.environ.get(
    "INTERNAL_DOMAINS",
    "devmclovin.com,localhost,127.0.0.1,puzzlelabs.app,ssh.devmclovin.com"
).split(",")
# ── Cloudflare tunnel cache ──
_CF_TUNNEL_CACHE: dict = {"data": None, "ts": 0}

def _run_check(cmd, timeout=3):
    # Run a shell command and return stripped stdout or empty string.
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

# ── Bookmark helpers ──

def _load_bookmarks() -> dict:
    """Load the bookmarks JSON file, returning {'saved': [], 'read_later': []}."""
    if BOOKMARKS_FILE.exists():
        try:
            data = json.loads(BOOKMARKS_FILE.read_text())
            if isinstance(data, dict) and "saved" in data and "read_later" in data:
                return data
        except Exception:
            pass
    return {"saved": [], "read_later": []}

def _save_bookmarks(data: dict):
    """Write bookmarks to disk atomically."""
    BOOKMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOOKMARKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.rename(BOOKMARKS_FILE)

def _story_id(date_str: str, title: str, source_url: str) -> str:
    """Generate a stable ID for a story."""
    import hashlib
    raw = f"{date_str}|{title}|{source_url}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def _find_bookmark(bookmarks: dict, sid: str):
    """Find a bookmark by story_id across both lists. Returns (list_ref, index) or (None, -1)."""
    for key in ("saved", "read_later"):
        for i, bm in enumerate(bookmarks[key]):
            if bm.get("id") == sid:
                return bookmarks[key], i, key
    return None, -1, None

def _toggle_bookmark(sid: str, story: dict, bookmark_type: str) -> dict:
    """Toggle a bookmark on/off. Returns the updated bookmarks dict."""
    bookmarks = _load_bookmarks()
    target_list, idx, current_type = _find_bookmark(bookmarks, sid)
    if target_list is not None and current_type == bookmark_type:
        # Already in this list — remove it (toggle off)
        target_list.pop(idx)
    else:
        # Remove from any other list first
        if target_list is not None:
            target_list.pop(idx)
        # Add to the requested list
        entry = {
            "id": sid,
            "title": story.get("title", ""),
            "source_name": story.get("source_name", ""),
            "source_url": story.get("source_url", ""),
            "body": story.get("body", ""),
            "date": story.get("date", ""),
            "saved_at": datetime.utcnow().isoformat(),
        }
        bookmarks[bookmark_type].append(entry)
    _save_bookmarks(bookmarks)
    return bookmarks

def _is_bookmarked(sid: str, bookmark_type: str) -> bool:
    """Check if a story is bookmarked in the given type."""
    bookmarks = _load_bookmarks()
    _, _, current_type = _find_bookmark(bookmarks, sid)
    return current_type == bookmark_type

def get_system_status():
    # Return system status checks. Cached for 2 min.
    global _SYS_CACHE
    now = time.time()
    if _SYS_CACHE["data"] is not None and (now - _SYS_CACHE["ts"]) < 120:
        return _SYS_CACHE["data"]

    result = {"ts": now}

    # Server
    result["server"] = {
        "status": "online", "label": "Server", "metric": "serving",
        "icon": "\U0001f5a5\ufe0f", "action_url": "#", "action_label": "Active",
    }
    try:
        pid = os.getpid()
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read().split()
        if len(stat) > 21:
            with open("/proc/uptime") as uf:
                sys_uptime = float(uf.read().split()[0])
            clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            proc_uptime = sys_uptime - (int(stat[21]) / clk_tck)
            if proc_uptime > 86400:
                result["server"]["metric"] = f"up {proc_uptime/86400:.0f}d"
            elif proc_uptime > 3600:
                result["server"]["metric"] = f"up {proc_uptime/3600:.0f}h"
            else:
                result["server"]["metric"] = f"up {proc_uptime/60:.0f}m"
    except Exception:
        pass

    # Hermes agent
    hermes_pid = _run_check(["pgrep", "-f", "hermes-agent"], timeout=2)
    if hermes_pid:
        result["hermes"] = {
            "status": "online", "label": "Hermes", "metric": "running",
            "icon": "\U0001f916", "action_url": "https://hermes.devmclovin.com",
            "action_label": "Open TUI \u2192",
        }
    else:
        result["hermes"] = {
            "status": "offline", "label": "Hermes", "metric": "not running",
            "icon": "\U0001f916", "action_url": "#", "action_label": "Down",
        }

    # Router
    ping_out = _run_check(["ping", "-c", "1", "-W", "2", "192.168.50.1"], timeout=3)
    if ping_out and "1 received" in ping_out:
        m = re.search(r"time=([\d.]+)\s*ms", ping_out)
        if m:
            result["router"] = {
                "status": "online", "label": "Router", "metric": f"{m.group(1)}ms",
                "icon": "\U0001f4e1", "action_url": "http://192.168.50.1",
                "action_label": "Admin \u2192",
            }
        else:
            result["router"] = {
                "status": "online", "label": "Router", "metric": "reachable",
                "icon": "\U0001f4e1", "action_url": "http://192.168.50.1",
                "action_label": "Admin \u2192",
            }
    else:
        result["router"] = {
            "status": "offline", "label": "Router", "metric": "unreachable",
            "icon": "\U0001f4e1", "action_url": "#", "action_label": "Check \u2192",
        }

    # Cloudflare tunnel
    tunnel_pid = _run_check(["pgrep", "-f", "cloudflared"], timeout=2)
    if tunnel_pid:
        result["tunnel"] = {
            "status": "online", "label": "Tunnel", "metric": "active",
            "icon": "\U0001f512", "action_url": "https://dash.cloudflare.com",
            "action_label": "Dash \u2192",
        }
    else:
        result["tunnel"] = {
            "status": "offline", "label": "Tunnel", "metric": "not running",
            "icon": "\U0001f512", "action_url": "#", "action_label": "Down",
        }

    # OpenRouter spend
    or_data = get_openrouter_data()
    if or_data.get("ok"):
        balance = or_data.get("balance", 0) or 0
        usage = or_data.get("total_usage", 0) or 0
        remaining = balance - usage
        result["spend"] = {
            "status": "online" if remaining > 0 else "warning",
            "label": "Credits", "metric": f"${remaining:.2f}",
            "icon": "\U0001f4b0", "action_url": "https://openrouter.ai/activity",
            "action_label": "OpenRouter \u2192",
        }
    else:
        result["spend"] = {
            "status": "warning", "label": "Credits", "metric": "N/A",
            "icon": "\U0001f4b0", "action_url": "#", "action_label": "Configure \u2192",
        }

    _SYS_CACHE = {"data": result, "ts": now}
    return result


def services_status_row() -> str:
    """Render a live service status table that fetches from /api/status."""
    return '<div class="section-title">📡 Services <a href="/status" style="font-size:0.75rem;font-weight:400;color:var(--text-muted);margin-left:0.5rem;text-decoration:none">view all →</a></div>' + \
           '<div class="services-status-panel" id="services-panel">' + \
           '<div class="services-loading" id="services-loading">Loading service status...</div>' + \
           '<div class="services-error" id="services-error" style="display:none"></div>' + \
           '</div>' + \
           '<script>' + \
           '(function(){' + \
           'fetch("/api/status").then(function(r){' + \
           'if(!r.ok)throw new Error("Server returned "+r.status);return r.json()' + \
           '}).then(function(data){' + \
           'var svcs=data.services||{};' + \
           'var names=["hermes_dashboard","ollama","cloudflare_tunnel","searxng","llm_router","github_backup"];' + \
           'var icons={hermes_dashboard:"\U0001f5a5\ufe0f",ollama:"\U0001f999",cloudflare_tunnel:"\U0001f310",searxng:"\U0001f50d",llm_router:"\U0001f500",github_backup:"\U0001f4e6"};' + \
           'var h="<div class=dashboard-grid>";' + \
           'names.forEach(function(k){var s=svcs[k];if(!s)return;' + \
           'var dot=s.healthy?"green":"red";var icon=icons[k]||"\u26ab";' + \
           'h+="<div class=briefing-card style=padding:1.25rem;min-width:240px>"+' + \
           '"<div style=display:flex;align-items:center;gap:0.75rem;margin-bottom:0.75rem>"+' + \
           '"<span style=font-size:1.6rem;line-height:1>"+icon+"</span>"+' + \
           '"<div><div style=font-weight:600;font-size:0.95rem;color:var(--text)>"+s.name+"</div>"+' + \
           '"<div style=display:flex;align-items:center;gap:0.35rem;margin-top:0.25rem>"+' + \
           '"<span class=status-dot "+dot+"></span>"+' + \
           '"<span style=font-size:0.8rem;color:var(--text-muted)>"+s.status+"</span></div></div></div>"+' + \
           '"<div style=font-size:0.78rem;color:var(--text-muted);margin-bottom:0.75rem;padding-left:0.25rem>"+s.detail+"</div>";' + \
           'if(s.actions){h+="<div style=display:flex;gap:0.5rem;flex-wrap:wrap;border-top:1px solid var(--border);padding-top:0.75rem;margin-top:0.25rem>";' + \
           'if(s.actions.logs)h+="<a href="+s.actions.logs.replace("http://localhost:9091","")+" class=card-action>\U0001f4c4 Logs</a>";' + \
           'if(s.actions.restart)h+="<button class=card-action data-url="+s.actions.restart+" onclick=fetch(this.dataset.url,{method:\'POST\'}) style=background:none;border:none;cursor:pointer;font-size:0.8rem>\U0001f504 Restart</button>";' + \
           'h+="</div>"}' + \
           'h+="</div>"});h+="</div>";' + \
           'var p=document.getElementById("services-panel");if(p)p.innerHTML=h' + \
           '}).catch(function(e){' + \
           'var ld=document.getElementById("services-loading");if(ld)ld.style.display="none";' + \
           'var err=document.getElementById("services-error");if(err){err.style.display="block";' + \
           'err.textContent="Service status unavailable — API server is not responding.";}' + \
           '});' + \
           '})()</script>'

def _load_env_var(name: str) -> str | None:
    """Read a variable from the Hermes .env file."""
    env_path = Path(os.path.expanduser("~/.hermes/.env"))
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _load_openrouter_key() -> str | None:
    """Read OPENROUTER_API_KEY from Hermes .env (uncommented)."""
    env_path = Path(os.path.expanduser("~/.hermes/.env"))
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("OPENROUTER_API_KEY="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _openrouter_api(path: str) -> dict | None:
    """Call the OpenRouter REST API. Returns parsed JSON or None on failure."""
    key = _load_openrouter_key()
    if not key:
        return None
    req = urllib.request.Request(
        f"https://openrouter.ai/api/v1{path}",
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "devmclovin-spending",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def get_openrouter_data() -> dict:
    """Return OpenRouter spending data. Cached for 5 min."""
    global _OPENROUTER_CACHE
    now = time.time()
    if _OPENROUTER_CACHE["data"] is not None and (now - _OPENROUTER_CACHE["ts"]) < _CACHE_TTL:
        return _OPENROUTER_CACHE["data"]

    result = {
        "ok": False, "balance": None, "total_usage": None,
        "models": [], "recent": [], "daily": [], "error": None,
    }

    key = _load_openrouter_key()
    if not key:
        result["error"] = "OpenRouter API key not configured"
        _OPENROUTER_CACHE = {"data": result, "ts": now}
        return result

    credits = _openrouter_api("/credits")
    if credits and isinstance(credits, dict):
        data = credits.get("data", credits)
        result["balance"] = data.get("total_credits")
        result["total_usage"] = data.get("total_usage")

    key_info = _openrouter_api("/key")
    if key_info and isinstance(key_info, dict):
        kd = key_info.get("data", key_info)
        result["key_label"] = kd.get("label", "")
        result["key_usage"] = kd.get("usage", 0)
        result["key_usage_monthly"] = kd.get("usage_monthly", 0)

    gen = _openrouter_api("/generation")
    models_map: dict[str, dict] = {}
    daily_map: dict[str, dict] = {}
    recent_list = []

    if gen and isinstance(gen, dict):
        gen_data = gen.get("data", gen)
        entries = gen_data if isinstance(gen_data, list) else gen_data.get("data", [])
        if isinstance(entries, list):
            for entry in entries:
                model = entry.get("model", "unknown")
                cost = entry.get("total_cost", 0) or 0
                prompt_tok = entry.get("tokens_prompt", 0) or 0
                comp_tok = entry.get("tokens_completion", 0) or 0
                created = entry.get("created_at", "") or ""

                if model not in models_map:
                    models_map[model] = {"name": model, "requests": 0, "cost": 0.0,
                                          "prompt_tokens": 0, "completion_tokens": 0}
                m = models_map[model]
                m["requests"] += 1
                m["cost"] += cost
                m["prompt_tokens"] += prompt_tok
                m["completion_tokens"] += comp_tok

                if created:
                    day = created[:10]
                    if day not in daily_map:
                        daily_map[day] = {"date": day, "cost": 0.0, "requests": 0}
                    daily_map[day]["cost"] += cost
                    daily_map[day]["requests"] += 1

                recent_list.append({
                    "model": model, "cost": cost,
                    "tokens": prompt_tok + comp_tok, "when": created,
                })

    result["models"] = sorted(models_map.values(), key=lambda x: x["cost"], reverse=True)
    result["daily"] = sorted(daily_map.values(), key=lambda x: x["date"], reverse=True)[:14]
    result["recent"] = sorted(recent_list, key=lambda x: x["when"], reverse=True)[:20]
    result["ok"] = True

    _OPENROUTER_CACHE = {"data": result, "ts": now}
    return result


def _load_github_token() -> str | None:
    """Read GITHUB_READ_TOKEN from the Hermes .env file."""
    env_path = Path(os.path.expanduser("~/.hermes/.env"))
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("GITHUB_READ_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _github_api(path: str) -> dict | list | None:
    """Call the GitHub REST API. Returns parsed JSON or None on failure."""
    token = _load_github_token()
    if not token:
        return None
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "devmclovin-landing",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None



# ── Cloudflare Tunnel API ──

def _load_cf_credentials() -> tuple:
    """Load CF_API_TOKEN, CF_ACCOUNT_ID, CF_TUNNEL_ID."""
    api_token = os.environ.get("CF_API_TOKEN") or _load_env_var("CF_API_TOKEN")
    account_id = os.environ.get("CF_ACCOUNT_ID") or _load_env_var("CF_ACCOUNT_ID")
    tunnel_id = os.environ.get("CF_TUNNEL_ID") or _load_env_var("CF_TUNNEL_ID")
    return api_token, account_id, tunnel_id


def get_cloudflare_tunnel_data() -> dict:
    """Fetch Cloudflare tunnel report with 5-min caching."""
    global _CF_TUNNEL_CACHE
    now = time.time()
    if _CF_TUNNEL_CACHE["data"] is not None and (now - _CF_TUNNEL_CACHE["ts"]) < _CACHE_TTL:
        return _CF_TUNNEL_CACHE["data"]
    api_token, account_id, tunnel_id = _load_cf_credentials()
    if not api_token or not account_id:
        result = {"ok": False, "data": None,
            "error": "CF_API_TOKEN and CF_ACCOUNT_ID must be set.",
            "checked_at": now, "account_id": account_id, "tunnel_id": tunnel_id}
        _CF_TUNNEL_CACHE = {"data": result, "ts": now}
        return result
    try:
        import asyncio
        _dir = os.path.dirname(os.path.abspath(__file__))
        if _dir not in sys.path: sys.path.insert(0, _dir)
        from cloudflare_api import get_full_report
        report = asyncio.run(get_full_report(
            api_token=api_token, account_id=account_id,
            tunnel_id=tunnel_id or None))
        data = {"ok": True, "data": {
            "tunnel_id": report.tunnel_id,
            "tunnel_name": report.status.tunnel_name,
            "is_up": report.status.is_up,
            "connections": [{"connection_id": c.connection_id,
                "client_id": c.client_id, "arch": c.arch,
                "version": c.version, "origin_ip": c.origin_ip,
                "opened_at": c.opened_at} for c in report.status.connections],
            "hostnames": [{"hostname": h.hostname, "service": h.service}
                for h in report.config.hostnames],
            "port_mappings": [{"protocol": pm.protocol, "host": pm.host,
                "port": pm.port} for pm in report.port_mappings],
            "access_policies": {
                "total_policies": report.access_policies.total_policies,
                "policies": [{"policy_id": p.policy_id, "name": p.name,
                    "decision": p.decision, "include_count": p.include_count,
                    "exclude_count": p.exclude_count,
                    "require_count": p.require_count}
                    for p in report.access_policies.policies],
                "types_breakdown": report.access_policies.types_breakdown},
            "last_reconnect_at": report.reconnect.last_reconnect_at,
            "connection_count": report.reconnect.connection_count},
            "error": None, "checked_at": now,
            "account_id": account_id, "tunnel_id": report.tunnel_id}
    except Exception as e:
        data = {"ok": False, "data": None, "error": str(e),
            "checked_at": now, "account_id": account_id, "tunnel_id": tunnel_id}
    _CF_TUNNEL_CACHE = {"data": data, "ts": now}
    return data


def get_github_repos() -> tuple[list[dict], str]:
    """Return (repos sorted by most-recently-updated, github_username). Cached for 5 min."""
    global _GITHUB_CACHE
    now = time.time()
    if _GITHUB_CACHE["data"] is not None and (now - _GITHUB_CACHE["ts"]) < _CACHE_TTL:
        return _GITHUB_CACHE["data"], _GITHUB_CACHE["username"]

    repos = []
    username = _GITHUB_CACHE.get("username") or ""

    if not username:
        user = _github_api("/user")
        if user and isinstance(user, dict):
            username = user.get("login", "")

    all_repos = _github_api("/user/repos?sort=updated&direction=desc&per_page=50&type=owner")
    if all_repos and isinstance(all_repos, list):
        for r in all_repos:
            repos.append({
                "name": r.get("name", ""),
                "full_name": r.get("full_name", ""),
                "description": (r.get("description") or "").strip(),
                "language": r.get("language") or "",
                "stars": r.get("stargazers_count", 0),
                "updated_at": r.get("updated_at", ""),
                "html_url": r.get("html_url", ""),
                "private": r.get("private", False),
                "fork": r.get("fork", False),
            })

    _GITHUB_CACHE = {"data": repos, "ts": now, "username": username}
    return repos, username


def simple_md_to_html(text: str) -> str:
    """Convert a subset of markdown to HTML — enough for the briefing format."""
    text = re.sub(r'^#### (.+)$', r'<h4>\1</h4>', text, flags=re.MULTILINE)
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$', r'<h1>\1</h1>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r'^---$', r'<hr>', text, flags=re.MULTILINE)
    text = re.sub(r'\n\n', '</p><p>', text)
    text = re.sub(r'\n', '<br>', text)
    return f'<p>{text}</p>'


# ── Shared nav assets (single source of truth for the top nav) ──────────────
# NAV_CSS is a faithful copy of every nav-related rule in BASE_CSS. html_page()
# emits {NAV_CSS}{BASE_CSS} (duplicate-but-identical rules on server pages), and
# the six template pages receive NAV_CSS via the __SITE_NAV_CSS__ placeholder so
# the injected nav is styled everywhere. NAV_JS holds the dropdown close-behaviour
# handlers, embedded by html_page and injected into templates via __SITE_NAV_JS__.
NAV_CSS = """
.skip-link {
    position: absolute;
    top: -100px;
    left: 1rem;
    background: var(--accent);
    color: #fff;
    padding: 0.5rem 1rem;
    border-radius: 0 0 6px 6px;
    z-index: 200;
    font-size: 0.9rem;
    font-weight: 600;
    text-decoration: none;
    transition: top 0.2s;
}
.skip-link:focus { top: 0; }

nav {
    background: var(--bg-nav);
    border-bottom: 1px solid var(--border);
    padding: 0 2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 60px;
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(10px);
    flex-wrap: wrap;
}
nav .logo {
    font-size: 1.25rem;
    font-weight: 700;
    color: var(--text);
    text-decoration: none;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
nav .logo span { color: var(--accent); }
nav .links {
    display: flex;
    gap: 1.5rem;
    align-items: center;
    flex-wrap: wrap;
}
nav .links a {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.9rem;
    transition: color 0.2s;
    padding: 0.5rem 0;
    white-space: nowrap;
}
nav .links a:hover,
nav .links a.active { color: var(--text); }
nav .links a.hermes-btn {
    background: var(--accent);
    color: #fff;
    padding: 0.4rem 1rem;
    border-radius: 6px;
    font-weight: 600;
    transition: background 0.2s, box-shadow 0.2s;
}
nav .links a.hermes-btn:hover {
    background: var(--accent-hover);
    box-shadow: 0 0 20px var(--accent-glow);
}
.nav-dropdown {
    position: relative;
    display: flex;
    align-items: center;
}
.nav-more-summary {
    color: var(--text-muted);
    font-size: 0.9rem;
    cursor: pointer;
    padding: 0.5rem 0;
    white-space: nowrap;
    list-style: none;
    user-select: none;
    transition: color 0.2s;
}
.nav-more-summary::-webkit-details-marker { display: none; }
.nav-more-summary::after { content: " ▾"; font-size: 0.7rem; }
.nav-more-summary:hover,
.nav-more-summary.active { color: var(--text); }
.nav-dropdown-menu {
    position: absolute;
    top: 100%;
    right: 0;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.4rem 0;
    min-width: 230px;
    z-index: 150;
    box-shadow: 0 8px 30px rgba(0,0,0,0.4);
    display: flex;
    flex-direction: column;
}
.nav-menu-label {
    padding: 0.35rem 1rem 0.25rem;
    color: var(--text-muted);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    opacity: 0.75;
}
.nav-dropdown-menu a {
    color: var(--text-muted) !important;
    text-decoration: none;
    font-size: 0.85rem !important;
    padding: 0.5rem 1rem !important;
    transition: background 0.15s, color 0.15s;
    white-space: nowrap;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
}
.nav-dropdown-menu a:hover {
    background: rgba(124,58,237,0.1);
    color: var(--text) !important;
}
.nav-dropdown-menu a.active { color: var(--accent-hover) !important; }
.nav-item-main { color: inherit; font-weight: 600; }
.nav-item-hint {
    color: #9aa4b2;
    font-size: 0.7rem;
    line-height: 1.25;
}
.nav-dropdown-menu a:hover .nav-item-hint { color: var(--text-muted); }

a:focus-visible,
button:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible,
.category-pill:focus-visible,
.category-tab:focus-visible,
.bm-btn:focus-visible,
.link-card:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
    border-radius: 4px;
}

footer {
    text-align: center;
    padding: 2rem;
    color: var(--text-muted);
    font-size: 0.8rem;
    border-top: 1px solid var(--border);
    margin-top: 3rem;
}
.footer-nav {
    display: flex;
    justify-content: center;
    gap: 1.5rem;
    margin-top: 0.5rem;
    flex-wrap: wrap;
}
.footer-nav a {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.78rem;
}
.footer-nav a:hover { color: var(--accent-hover); }

@media (min-width: 481px) and (max-width: 900px) {
    nav { padding: 0 1.25rem; }
    nav .links { gap: 1rem; }
    nav .links a { font-size: 0.82rem; }
}
@media (max-width: 480px) {
    nav {
        padding: 0 0.75rem;
        height: auto;
        min-height: 52px;
    }
    nav .links {
        flex-wrap: nowrap;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        gap: 0.75rem;
        padding-bottom: 0.25rem;
        scrollbar-width: none;
    }
    nav .links::-webkit-scrollbar { display: none; }
    nav .links a { font-size: 0.8rem; padding: 0.45rem 0; }
    nav .logo { font-size: 1.05rem; }
    .footer-nav { gap: 1rem; }
}
"""

NAV_JS = """
document.addEventListener('DOMContentLoaded', function() {
    var dropdowns = Array.prototype.slice.call(document.querySelectorAll('details.nav-dropdown'));
    dropdowns.forEach(function(dropdown) {
        dropdown.addEventListener('toggle', function() {
            if (dropdown.open) {
                dropdowns.forEach(function(other) {
                    if (other !== dropdown) other.open = false;
                });
            }
        });
    });
    document.addEventListener('click', function(e) {
        if (!e.target.closest('details.nav-dropdown')) {
            dropdowns.forEach(function(dropdown) { dropdown.open = false; });
        }
    });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            dropdowns.forEach(function(dropdown) { dropdown.open = false; });
        }
    });
});
"""


def render_nav(active: str = "home") -> str:
    """Single source of truth for the top nav (see section 3 of the redesign plan).
    Returns the full <nav>…</nav> block: used by html_page() and injected verbatim
    into the six template pages so the nav is byte-identical everywhere."""
    top_links = [
        ("/", "Home", "home"),
        ("/briefings", "Briefings", "briefings"),
        ("/projects", "Projects", "projects"),
        ("/portfolio", "Portfolio", "portfolio"),
        ("/status", "Status", "status"),
        ("/hermes", "Hermes", "hermes"),
    ]
    links = ""
    for href, label, key in top_links:
        cls = 'active' if active == key else ''
        links += f'<a href="{href}" class="{cls}">{label}</a>'

    tool_keys = {"notes", "inbox", "runbooks", "cron", "models",
                 "model-tuning", "llm-lab", "disk-cleanup", "tunnel", "logs"}
    items = [
        ("__label__", "Daily", ""),
        ("/notes", "Notes", "Personal notes", "notes"),
        ("/inbox", "Inbox", "Agent intake queue", "inbox"),
        ("/runbooks", "Runbooks", "Copy-paste server fixes", "runbooks"),
        ("__label__", "Ops", ""),
        ("/cron", "Cron Jobs", "Schedules and outputs", "cron"),
        ("/models", "Models", "LLM pricing and local models", "models"),
        ("/model-tuning", "Tuning", "Fine-tune datasets and HF pulls", "model-tuning"),
        ("/llm-lab", "LLM Lab", "Evals, traces, arena, GGUF pulls", "llm-lab"),
        ("/disk-cleanup", "Disk", "Storage usage and cleanup", "disk-cleanup"),
        ("/tunnel", "Tunnel", "Cloudflare routes", "tunnel"),
        ("/logs", "Logs", "Server and router journals", "logs"),
    ]
    summary_cls = 'nav-more-summary' + (' active' if active in tool_keys else '')
    links += '<details class="nav-dropdown"><summary class="' + summary_cls + '">Tools</summary><div class="nav-dropdown-menu">'
    for item in items:
        if item[0] == "__label__":
            links += '<div class="nav-menu-label">' + item[1] + '</div>'
            continue
        href, label, hint, key = item
        cls = 'active' if active == key else ''
        links += '<a href="' + href + '" class="' + cls + '"><span class="nav-item-main">' + label + '</span><span class="nav-item-hint">' + hint + '</span></a>'
    links += '</div></details>'
    links += '<a href="https://ssh.devmclovin.com" class="hermes-btn">SSH</a>'

    return ('<nav>'
            '<a href="/" class="logo" aria-label="devmclovin home">dev<span>mclovin</span></a>'
            '<div class="links">' + links + '</div>'
            '</nav>')


def inject_nav(page_html: str, active: str) -> str:
    """Fill the NavInjection placeholders in a template page so it shares the exact
    same nav, nav CSS and nav JS as server-rendered pages (kills nav drift)."""
    return (page_html
            .replace("__SITE_NAV_CSS__", NAV_CSS)
            .replace("__SITE_NAV_JS__", NAV_JS)
            .replace("__SITE_NAV__", render_nav(active)))


BASE_CSS = """
:root {
    --bg: #0d1117;
    --bg-card: #161b22;
    --bg-nav: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --accent: #7c3aed;
    --accent-hover: #8b5cf6;
    --accent-glow: rgba(124, 58, 237, 0.3);
    --green: #3fb950;
    --orange: #d2991d;
    --red: #f85149;
    --blue: #58a6ff;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    min-height: 100vh;
}

.skip-link {
    position: absolute;
    top: -100px;
    left: 1rem;
    background: var(--accent);
    color: #fff;
    padding: 0.5rem 1rem;
    border-radius: 0 0 6px 6px;
    z-index: 200;
    font-size: 0.9rem;
    font-weight: 600;
    text-decoration: none;
    transition: top 0.2s;
}
.skip-link:focus {
    top: 0;
}

nav {
    background: var(--bg-nav);
    border-bottom: 1px solid var(--border);
    padding: 0 2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 60px;
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(10px);
    flex-wrap: wrap;
}

nav .logo {
    font-size: 1.25rem;
    font-weight: 700;
    color: var(--text);
    text-decoration: none;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

nav .logo span {
    color: var(--accent);
}

nav .links {
    display: flex;
    gap: 1.5rem;
    align-items: center;
    flex-wrap: wrap;
}

nav .links a {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.9rem;
    transition: color 0.2s;
    padding: 0.5rem 0;
    white-space: nowrap;
}

nav .links a:hover,
nav .links a.active {
    color: var(--text);
}

nav .links a.hermes-btn {
    background: var(--accent);
    color: #fff;
    padding: 0.4rem 1rem;
    border-radius: 6px;
    font-weight: 600;
    transition: background 0.2s, box-shadow 0.2s;
}

nav .links a.hermes-btn:hover {
    background: var(--accent-hover);
    box-shadow: 0 0 20px var(--accent-glow);
}

/* ── Nav dropdown (More menu) ── */
.nav-dropdown {
    position: relative;
    display: flex;
    align-items: center;
}
.nav-more-summary {
    color: var(--text-muted);
    font-size: 0.9rem;
    cursor: pointer;
    padding: 0.5rem 0;
    white-space: nowrap;
    list-style: none;
    user-select: none;
    transition: color 0.2s;
}
.nav-more-summary::-webkit-details-marker { display: none; }
.nav-more-summary::after {
    content: " ▾";
    font-size: 0.7rem;
}
.nav-more-summary:hover,
.nav-more-summary.active {
    color: var(--text);
}
.nav-dropdown-menu {
    position: absolute;
    top: 100%;
    right: 0;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.4rem 0;
    min-width: 160px;
    z-index: 150;
    box-shadow: 0 8px 30px rgba(0,0,0,0.4);
    display: flex;
    flex-direction: column;
}
.nav-dropdown-menu a {
    color: var(--text-muted) !important;
    text-decoration: none;
    font-size: 0.85rem !important;
    padding: 0.5rem 1rem !important;
    transition: background 0.15s, color 0.15s;
    white-space: nowrap;
}
.nav-dropdown-menu a:hover {
    background: rgba(124,58,237,0.1);
    color: var(--text) !important;
}
.nav-dropdown-menu a.active {
    color: var(--accent-hover) !important;
}

.container {
    max-width: 1100px;
    margin: 0 auto;
    padding: 2rem;
}

/* ── Hero ── */
.hero {
    text-align: center;
    padding: 1.5rem 0 1rem;
}

.hero h1 {
    font-size: 2.5rem;
    font-weight: 800;
    background: linear-gradient(135deg, var(--accent-hover), #a78bfa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 0.5rem;
}

.hero p {
    color: var(--text-muted);
    font-size: 1.1rem;
}
/* ── System Overview collapsible ── */
.system-overview {
    margin: 2rem 0;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--bg-card);
    overflow: hidden;
}
.system-overview > summary {
    padding: 1rem 1.5rem;
    list-style: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 1.1rem;
    font-weight: 700;
    color: var(--text);
    user-select: none;
}
.system-overview > summary::-webkit-details-marker { display: none; }
.system-overview > summary::after {
    content: "▸";
    font-size: 0.85rem;
    color: var(--text-muted);
    transition: transform 0.2s;
    margin-left: 1rem;
}
.system-overview[open] > summary::after {
    transform: rotate(90deg);
}
.system-overview-body {
    padding: 0 1.5rem 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
}

/* ── System summary cards (compact, inside overview) ── */
.sys-summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 0.75rem;
}
.sys-summary-card {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
    text-decoration: none;
    color: var(--text);
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    transition: border-color 0.2s, transform 0.2s;
}
.sys-summary-card:hover {
    border-color: var(--accent);
    transform: translateY(-1px);
}
.sys-summary-icon {
    font-size: 1.3rem;
    line-height: 1;
    flex-shrink: 0;
}
.sys-summary-info {
    flex: 1;
    min-width: 0;
}
.sys-summary-label {
    font-weight: 600;
    font-size: 0.85rem;
    margin-bottom: 0.15rem;
}
.sys-summary-metric {
    font-size: 0.78rem;
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}


/* ── Organized navigation + page hubs ── */
.nav-dropdown-menu {
    min-width: 230px;
}
.nav-menu-label {
    padding: 0.35rem 1rem 0.25rem;
    color: var(--text-muted);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    opacity: 0.75;
}
.nav-dropdown-menu a {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
}
.nav-item-main {
    color: inherit;
    font-weight: 600;
}
.nav-item-hint {
    color: #9aa4b2;
    font-size: 0.7rem;
    line-height: 1.25;
}
.nav-dropdown-menu a:hover .nav-item-hint {
    color: var(--text-muted);
}
.page-toolbar a {
    color: var(--accent-hover);
    text-decoration: none;
    font-size: 0.8rem;
    border: 1px solid rgba(124,58,237,0.3);
    border-radius: 999px;
    padding: 0.25rem 0.55rem;
    background: rgba(124,58,237,0.08);
}
.page-toolbar a:hover {
    border-color: var(--accent);
    color: var(--text);
}
.toolbox-panel {
    margin: 1rem 0 1.5rem;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--bg-card);
    overflow: hidden;
}
.toolbox-panel > summary {
    list-style: none;
    cursor: pointer;
    padding: 0.9rem 1.1rem;
    font-weight: 700;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.toolbox-panel > summary::-webkit-details-marker { display: none; }
.toolbox-panel > summary::after { content: "▸"; color: var(--text-muted); transition: transform 0.2s; }
.toolbox-panel[open] > summary::after { transform: rotate(90deg); }
.toolbox-panel-body { padding: 0 1rem 1rem; }
.page-toolbar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.65rem;
    margin: 0 0 1rem;
}
.page-search,
.page-select {
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 8px;
    padding: 0.55rem 0.75rem;
    font: inherit;
    font-size: 0.88rem;
}
.page-search { min-width: 240px; flex: 1; }
.page-select { min-width: 160px; }
.lc-details {
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
    padding: 0.45rem 0;
}
.lc-details > summary {
    list-style: none;
    cursor: pointer;
    color: var(--text-muted);
    font-size: 0.78rem;
    font-weight: 600;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.lc-details > summary::-webkit-details-marker { display: none; }
.lc-details > summary::after { content: "▸"; transition: transform 0.2s; }
.lc-details[open] > summary::after { transform: rotate(90deg); }
.lc-details .lc-info-grid {
    border: 0;
    padding: 0.6rem 0 0.15rem;
}


/* ── Compact landing status strip ── */
.landing-status-strip {
    margin: 1rem 0 1.25rem;
    border: 1px solid var(--border);
    border-radius: 14px;
    background: rgba(22,27,34,0.82);
    overflow: hidden;
}
.landing-status-strip > summary {
    list-style: none;
    cursor: pointer;
    padding: 0.65rem 0.9rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    font-size: 0.82rem;
    color: var(--text-muted);
}
.landing-status-strip > summary::-webkit-details-marker { display: none; }
.status-summary-left { display: inline-flex; align-items: center; gap: 0.5rem; min-width: 0; }
.status-summary-title { color: var(--text); font-weight: 700; white-space: nowrap; }
.status-summary-meta { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-summary-right { display: inline-flex; align-items: center; gap: 0.5rem; white-space: nowrap; }
.status-mini-pill { display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.12rem 0.5rem; border: 1px solid var(--border); border-radius: 999px; background: var(--bg); color: var(--text-muted); font-size: 0.72rem; }
.status-mini-pill.ok { color: var(--green); border-color: rgba(63,185,80,0.35); }
.status-mini-pill.warn { color: var(--orange); border-color: rgba(210,153,29,0.35); }
.status-expand-hint { color: var(--accent-hover); font-size: 0.72rem; }
.landing-status-body { border-top: 1px solid var(--border); padding: 0.6rem 0.75rem 0.75rem; }
.status-mini-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.45rem; }
.status-mini-service { display:flex; align-items:center; justify-content:space-between; gap:0.5rem; padding:0.45rem 0.55rem; border:1px solid var(--border); border-radius:10px; background:var(--bg); color:var(--text); font-size:0.78rem; }
.status-mini-service span:first-child { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.status-mini-service small { color: var(--text-muted); font-size: 0.68rem; white-space: nowrap; }

/* ── Homepage: page-head row (replaces .hero) ── */
.page-head { display:flex; align-items:baseline; justify-content:space-between; gap:1rem; flex-wrap:wrap; margin:1.25rem 0 1rem; }
.page-head h1 { font-size:1.5rem; font-weight:700; color:var(--text); }
.page-date { color:var(--text-muted); font-size:0.9rem; white-space:nowrap; }

/* ── Status strip health dot ── */
.status-strip-dot { display:inline-block; width:10px; height:10px; border-radius:50%; background:var(--text-muted); flex-shrink:0; }
.status-strip-dot.green { background:var(--green); }
.status-strip-dot.red { background:var(--red); }
.status-strip-dot.amber { background:var(--orange); }

/* ── Homepage section-title row (title left, action link right) ── */
.section-head { display:flex; align-items:baseline; justify-content:space-between; gap:1rem; flex-wrap:wrap; margin:2rem 0 0.5rem; }
.section-head h2 { font-size:1.05rem; font-weight:600; color:var(--text); }
.section-head a { color:var(--accent-hover); text-decoration:none; font-size:0.82rem; white-space:nowrap; }
.section-head a:hover { color:var(--text); }

/* ── Today's Briefing: vertical list (home only, no horizontal scroll) ── */
.briefing-home { border:1px solid var(--border); border-radius:12px; background:var(--bg-card); overflow:hidden; }
.briefing-home-row { display:flex; align-items:flex-start; gap:0.6rem; padding:0.6rem 0.85rem; border-top:1px solid var(--border); }
.briefing-home-row:first-child { border-top:none; }
.briefing-home-row .bh-badge { flex-shrink:0; margin-top:0.1rem; }
.briefing-home-row .bh-main { min-width:0; flex:1; }
.briefing-home-row .bh-title { color:var(--text); text-decoration:none; font-size:0.9rem; font-weight:500; }
.briefing-home-row .bh-title:hover { color:var(--accent-hover); }
.briefing-home-row .bh-impact { color:var(--text-muted); font-size:0.8rem; margin-top:0.15rem; line-height:1.4; }

/* ── Compact hub rows (label + inline chips) ── */
.hub-rows { display:flex; flex-direction:column; gap:0.5rem; margin:0.5rem 0 0; }
.hub-row { display:flex; align-items:center; gap:0.6rem; flex-wrap:wrap; }
.hub-row-label { display:inline-flex; align-items:center; gap:0.4rem; font-weight:600; font-size:0.85rem; color:var(--text); min-width:9rem; }
.hub-chips { display:flex; flex-wrap:wrap; gap:0.4rem; }
.hub-chip { color:var(--accent-hover); text-decoration:none; font-size:0.8rem; border:1px solid var(--border); border-radius:999px; padding:0.35rem 0.7rem; background:var(--bg-card); }
.hub-chip:hover { border-color:var(--accent); color:var(--text); }
@media (max-width:480px) { .hub-row-label { min-width:100%; } }

/* ── Logs tabs (Server | Router) ── */
.logs-tabs { display:flex; gap:1.25rem; border-bottom:1px solid var(--border); margin:0.75rem 0 1rem; }
.logs-tab { color:var(--text-muted); text-decoration:none; font-size:0.9rem; padding:0.4rem 0.1rem; border-bottom:2px solid transparent; margin-bottom:-1px; }
.logs-tab:hover { color:var(--text); }
.logs-tab.active { color:var(--text); border-bottom-color:var(--accent); }

/* ── Briefing archive cards + subnav ── */
.briefing-subnav { display:flex; flex-wrap:wrap; gap:0.5rem; justify-content:center; margin:-0.35rem 0 1rem; }
.briefing-subnav a { color:var(--text-muted); text-decoration:none; border:1px solid var(--border); background:var(--bg-card); border-radius:999px; padding:0.35rem 0.8rem; font-size:0.82rem; }
.briefing-subnav a:hover, .briefing-subnav a.active { color:var(--text); border-color:var(--accent); background:rgba(124,58,237,0.12); }
.briefing-archive-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap:0.9rem; margin-top:0.75rem; }
.briefing-archive-card { display:flex; flex-direction:column; gap:0.65rem; min-height:190px; padding:1rem; background:linear-gradient(180deg, rgba(124,58,237,0.09), rgba(22,27,34,0.98) 38%); border:1px solid var(--border); border-radius:14px; color:var(--text); text-decoration:none; transition:border-color .2s, transform .2s, box-shadow .2s; }
.briefing-archive-card:hover { border-color:var(--accent); transform:translateY(-2px); box-shadow:0 8px 26px rgba(124,58,237,0.13); }
.briefing-card-topline { display:flex; align-items:center; justify-content:space-between; gap:0.75rem; }
.briefing-date-chip { color:var(--accent-hover); font-weight:700; font-size:0.82rem; }
.briefing-count-chip { color:var(--text-muted); border:1px solid var(--border); border-radius:999px; padding:0.12rem 0.45rem; font-size:0.7rem; white-space:nowrap; }
.briefing-top-story { font-size:0.96rem; font-weight:700; line-height:1.35; }
.briefing-preview-list { list-style:none; padding:0; margin:0; display:flex; flex-direction:column; gap:0.35rem; }
.briefing-preview-list li { color:var(--text-muted); font-size:0.78rem; line-height:1.35; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.briefing-preview-list li::before { content:"• "; color:var(--accent); }
.briefing-card-footer { margin-top:auto; color:var(--accent-hover); font-size:0.78rem; }

/* ── Project command center polish ── */
.project-hero-card { margin:1rem 0 1.2rem; padding:1.25rem; border:1px solid var(--border); border-radius:18px; background: radial-gradient(circle at top left, rgba(124,58,237,0.22), transparent 32%), linear-gradient(180deg, rgba(22,27,34,0.98), rgba(13,17,23,0.72)); }
.project-stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:0.65rem; margin-top:1rem; }
.project-stat { border:1px solid var(--border); background:rgba(13,17,23,0.65); border-radius:12px; padding:0.75rem; }
.project-stat-value { font-size:1.35rem; font-weight:800; color:var(--text); line-height:1; }
.project-stat-label { color:var(--text-muted); font-size:0.72rem; margin-top:0.25rem; text-transform:uppercase; letter-spacing:.06em; }
.project-count { color:var(--text-muted); font-size:0.82rem; margin-left:auto; }
.launcher-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap:1rem; margin-bottom:2rem; padding:0.5rem 0 1rem; }
.launcher-card { position:relative; overflow:hidden; background:linear-gradient(180deg, rgba(22,27,34,0.98), rgba(13,17,23,0.82)); border:1px solid var(--border); border-radius:16px; padding:1rem; transition:border-color .2s, transform .2s, box-shadow .2s; display:flex; flex-direction:column; gap:0.65rem; }
.launcher-card::before { content:""; position:absolute; inset:0 0 auto 0; height:3px; background:linear-gradient(90deg,var(--accent),#58a6ff,transparent); opacity:.7; }
.launcher-card:hover { border-color:var(--accent); transform:translateY(-2px); box-shadow:0 10px 30px rgba(124,58,237,0.14); }
.lc-header { display:flex; align-items:flex-start; justify-content:space-between; gap:0.75rem; }
.lc-repo-name { font-size:1.05rem; font-weight:800; color:var(--text); flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.lc-repo-name a { color:var(--text); text-decoration:none; }
.lc-repo-name a:hover { color:var(--accent-hover); }
.lc-meta-row { display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap; font-size:0.72rem; color:var(--text-muted); }
.lc-actions { display:flex; gap:0.45rem; margin-top:auto; padding-top:0.35rem; flex-wrap:wrap; }
.lc-btn { flex:1 1 42%; display:inline-flex; align-items:center; justify-content:center; gap:0.28rem; padding:0.42rem 0.5rem; border-radius:8px; font-size:0.72rem; font-weight:600; text-decoration:none; cursor:pointer; border:1px solid var(--border); background:var(--bg); color:var(--text-muted); transition:border-color .15s,color .15s,background .15s; white-space:nowrap; }
.lc-btn:hover { border-color:var(--accent); color:var(--text); background:rgba(124,58,237,0.08); }

.lc-btn.hide-project-btn { color: var(--orange); border-color: rgba(210,153,29,0.28); }
.lc-btn.hide-project-btn:hover { color: var(--text); border-color: var(--orange); background: rgba(210,153,29,0.1); }
.launcher-card.project-hidden { opacity: 0.48; filter: grayscale(0.45); border-style: dashed; }
.launcher-card.project-hidden::after { content: "Hidden"; position: absolute; top: 0.62rem; right: 0.75rem; color: var(--orange); border: 1px solid rgba(210,153,29,0.35); background: rgba(210,153,29,0.12); border-radius: 999px; padding: 0.12rem 0.45rem; font-size: 0.65rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }
.launcher-card.project-hidden .hide-project-btn { color: var(--green); border-color: rgba(63,185,80,0.35); }
@media (max-width:640px) { .launcher-grid,.briefing-archive-grid { grid-template-columns:1fr; } .project-count { flex-basis:100%; margin-left:0; } .landing-status-strip > summary { align-items:flex-start; flex-direction:column; } }

/* ── Mini section title (used inside overview) ── */
.section-title-mini {
    font-size: 1rem;
    font-weight: 700;
    margin: 0.5rem 0 0.75rem;
    color: var(--text);
}

.tunnel-table{width:100%;border-collapse:collapse;font-size:.85rem;margin:.5rem 0}
.tunnel-table th{text-align:left;color:var(--text-muted);font-weight:500;padding:.4rem .5rem;border-bottom:1px solid var(--border);font-size:.75rem;text-transform:uppercase;letter-spacing:.03em}
.tunnel-table td{padding:.4rem .5rem;border-bottom:1px solid rgba(48,54,61,.5)}
.tunnel-table tr:last-child td{border-bottom:none}
.tunnel-table code{font-size:.8rem;color:var(--text-muted)}
.tunnel-table a{color:var(--accent-hover);text-decoration:none}
.tunnel-table a:hover{text-decoration:underline}
.badge{display:inline-block;background:var(--bg-card);border:1px solid var(--border);border-radius:4px;padding:.1rem .4rem;font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted)}
.tunnel-status-card .status-dot{animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.tunnel-status-card,.tunnel-hostnames-card,.tunnel-ports-card,.tunnel-policies-card{min-width:260px}
.section-title {
    font-size: 1.4rem;
    font-weight: 700;
    margin: 2rem 0 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
}

/* ── Briefing cards ── */
.briefing-header {
    margin-bottom: 1.5rem;
}

.briefing-header .date {
    color: var(--text-muted);
    font-size: 0.85rem;
}

.briefing-header h2 {
    font-size: 1.3rem;
    color: var(--accent-hover);
    margin-top: 0.25rem;
}

.briefing-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 1rem;
    overflow: visible;
    padding: 0.5rem 0.25rem 1rem;
    margin-bottom: 2rem;
}

.briefing-grid::-webkit-scrollbar {
    display: none;
}

.briefing-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
    transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s;
    display: flex;
    flex-direction: column;
    flex: 0 0 320px;
    scroll-snap-align: start;
}

.briefing-card:hover {
    border-color: var(--accent);
    transform: translateY(-2px);
    box-shadow: 0 4px 20px rgba(124, 58, 237, 0.15);
}

.briefing-card .card-num {
    display: inline-block;
    background: var(--accent);
    color: #fff;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    text-align: center;
    line-height: 24px;
    font-size: 0.75rem;
    font-weight: 700;
    margin-bottom: 0.6rem;
}

.briefing-card h3 {
    font-size: 1rem;
    margin-bottom: 0.6rem;
    line-height: 1.4;
    color: var(--text);
    flex-shrink: 0;
}

.briefing-card .card-summary {
    color: var(--text-muted);
    font-size: 0.88rem;
    line-height: 1.5;
    flex: 1;
}

.briefing-card .card-impact {
    color: var(--accent-hover);
    font-size: 0.82rem;
    font-style: italic;
    line-height: 1.4;
    margin-bottom: 0.5rem;
    padding-left: 0.25rem;
    border-left: 2px solid var(--accent);
    flex-shrink: 0;
}

.briefing-card .card-source {
    margin-top: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border);
    font-size: 0.78rem;
    color: var(--text-muted);
    flex-shrink: 0;
}

.briefing-card .card-source a {
    color: var(--accent-hover);
    text-decoration: none;
    font-weight: 500;
}

.briefing-card .card-source a:hover {
    text-decoration: underline;
}

/* ── Category badges on cards ── */
.briefing-card .card-categories {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-top: 0.6rem;
    flex-shrink: 0;
}
.category-badge {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 10px;
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    white-space: nowrap;
}
/* ── Category filter tabs ── */
.category-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin: 1rem 0 1.5rem;
}
.category-tab {
    padding: 0.35rem 0.85rem;
    border-radius: 16px;
    border: 1px solid var(--border);
    background: var(--bg-card);
    color: var(--text-muted);
    font-size: 0.8rem;
    font-weight: 500;
    cursor: pointer;
    text-decoration: none;
    transition: all 0.2s;
    white-space: nowrap;
}
.category-tab:hover {
    border-color: var(--accent);
    color: var(--text);
}
.category-tab.active {
    border-color: var(--accent);
    background: var(--accent);
    color: #fff;
}
.category-tab .tab-count {
    font-size: 0.68rem;
    opacity: 0.75;
    margin-left: 0.2rem;
}

/* ── Repo cards ── */
.repo-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
    transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s;
    display: flex;
    flex-direction: column;
    flex: 0 0 280px;
    scroll-snap-align: start;
}

.repo-card:hover {
    border-color: var(--accent);
    transform: translateY(-2px);
    box-shadow: 0 4px 20px rgba(124, 58, 237, 0.15);
}

.repo-card h3 {
    font-size: 1rem;
    margin-bottom: 0.5rem;
    line-height: 1.4;
    flex-shrink: 0;
}

.repo-card h3 a {
    color: var(--text);
    text-decoration: none;
}

.repo-card h3 a:hover {
    color: var(--accent-hover);
}

.repo-card .repo-desc {
    color: var(--text-muted);
    font-size: 0.85rem;
    line-height: 1.5;
    flex: 1;
    margin-bottom: 0.75rem;
}

.repo-card .repo-meta {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    font-size: 0.75rem;
    color: var(--text-muted);
    flex-shrink: 0;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border);
}

.repo-card .repo-meta .lang {
    display: flex;
    align-items: center;
    gap: 0.35rem;
}

.repo-lang-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
}

.repo-card .repo-meta .stars {
    display: flex;
    align-items: center;
    gap: 0.25rem;
}

.repo-badge {
    font-size: 0.65rem;
    padding: 0.15rem 0.4rem;
    border-radius: 4px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
}

.repo-badge.private {
    background: rgba(210, 153, 29, 0.15);
    color: var(--orange);
}

.repo-badge.fork {
    background: rgba(139, 148, 158, 0.12);
    color: var(--text-muted);
}

/* ── Horizontal scroll arrows ── */
.briefing-scroll {
    position: relative;
    overflow: visible;
}

/* De-scrolled: cards wrap in a grid (.briefing-grid); arrows hidden. */
.scroll-arrow { display: none; }

.briefing-list a {
    display: block;
    padding: 0.75rem 1rem;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 0.5rem;
    color: var(--text);
    text-decoration: none;
    transition: border-color 0.2s, background 0.2s;
}

.briefing-list a:hover {
    border-color: var(--accent);
    background: #1c2333;
}

.briefing-list a .brief-date {
    font-weight: 600;
}

.briefing-list a .brief-meta {
    color: var(--text-muted);
    font-size: 0.85rem;
}

.briefing-list a .brief-titles {
    list-style: none;
    margin-top: 0.5rem;
    padding: 0;
}

.briefing-list a .brief-titles li {
    color: var(--text-muted);
    font-size: 0.82rem;
    line-height: 1.5;
    padding: 0.15rem 0;
}

.briefing-list a .brief-titles li::before {
    content: "▸ ";
    color: var(--accent);
}

/* ── Search bar ── */
.search-bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 1rem 0 1.5rem;
}
.search-bar input {
    flex: 1;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.65rem 1rem;
    font-size: 0.95rem;
    color: var(--text);
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s;
}
.search-bar input:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
}
.search-bar input::placeholder {
    color: var(--text-muted);
}
.search-bar .search-icon {
    position: absolute;
    right: 1rem;
    color: var(--text-muted);
    pointer-events: none;
    font-size: 0.9rem;
}
.search-input-wrap {
    position: relative;
    flex: 1;
}
.search-clear {
    position: absolute;
    right: 0.65rem;
    top: 50%;
    transform: translateY(-50%);
    background: none;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    font-size: 1rem;
    padding: 0.2rem 0.4rem;
    display: none;
}
.search-clear.visible {
    display: block;
}
.search-clear:hover {
    color: var(--text);
}
.search-status {
    color: var(--text-muted);
    font-size: 0.8rem;
    margin-bottom: 0.5rem;
    display: none;
}
.search-results {
    display: none;
}
.search-results.active {
    display: block;
}
.search-result-item {
    display: block;
    padding: 0.85rem 1rem;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 0.5rem;
    color: var(--text);
    text-decoration: none;
    transition: border-color 0.2s, background 0.2s;
}
.search-result-item:hover {
    border-color: var(--accent);
    background: #1c2333;
}
.search-result-item .sr-title {
    font-weight: 600;
    margin-bottom: 0.3rem;
}
.search-result-item .sr-meta {
    color: var(--text-muted);
    font-size: 0.8rem;
    margin-bottom: 0.3rem;
}
.search-result-item .sr-snippet {
    color: var(--text-muted);
    font-size: 0.82rem;
    line-height: 1.5;
}
.search-result-item .sr-snippet mark {
    background: rgba(124, 58, 237, 0.25);
    color: var(--accent-hover);
    padding: 0 2px;
    border-radius: 2px;
}
.search-results-placeholder {
    display: none;
    text-align: center;
    padding: 2rem 0;
    color: var(--text-muted);
}
.search-results-placeholder.active {
    display: block;
}
.search-results-empty {
    display: none;
    text-align: center;
    padding: 1.5rem 0;
    color: var(--text-muted);
}
.search-results-empty.active {
    display: block;
}

.cta-button {
    display: inline-block;
    background: var(--accent);
    color: #fff;
    padding: 0.85rem 2rem;
    border-radius: 10px;
    font-size: 1.1rem;
    font-weight: 600;
    text-decoration: none;
    transition: background 0.2s, transform 0.2s;
}
.cta-button:hover {
    background: var(--accent-hover);
    transform: translateY(-2px);
}

/* ── Accessibility: keyboard focus ── */
a:focus-visible,
button:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible,
.category-pill:focus-visible,
.category-tab:focus-visible,
.bm-btn:focus-visible,
.link-card:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
    border-radius: 4px;
}

/* ── Tablet (481–900px) ── */
@media (min-width: 481px) and (max-width: 900px) {
    .container { padding: 1.5rem; }
    .hero h1 { font-size: 2rem; }
    .hero p { font-size: 1rem; }
    .hero { padding: 2rem 0 1.5rem; }
    nav { padding: 0 1.25rem; }
    nav .links { gap: 1rem; }
    nav .links a { font-size: 0.82rem; }
    .briefing-card,
    .repo-card,
    .spending-mini { flex: 0 0 280px; }
    .quicklinks-grid { grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }
    .command-center { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
    .section-title { font-size: 1.2rem; margin: 1.5rem 0 0.75rem; }
    .scroll-arrow.left { left: -36px; }
    .scroll-arrow.right { right: -36px; }
    .sys-summary-grid { grid-template-columns: repeat(2, 1fr); }
    .system-overview-body { padding: 0 1rem 1rem; }
}

.empty-state {
    text-align: center;
    padding: 3rem;
    color: var(--text-muted);
}

.empty-state svg {
    width: 64px;
    height: 64px;
    margin-bottom: 1rem;
    opacity: 0.4;
}

.services-loading { color: var(--text-muted); padding: 1rem 0; font-size: 0.9rem; }
.services-error { color: #f85149; padding: 1rem 0; font-size: 0.9rem;
    background: rgba(248,81,73,0.08); border-radius: 8px; padding: 1rem; }

.coming-soon {
    background: var(--bg-card);
    border: 1px dashed var(--border);
    border-radius: 12px;
    padding: 2rem;
    text-align: center;
    color: var(--text-muted);
    margin: 2rem 0;
}

.coming-soon h3 {
    color: var(--text);
    margin-bottom: 0.5rem;
}

/* ── Spending dashboard ── */
.spending-metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin-bottom: 2rem;
}

.spending-metric {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
    text-align: center;
}

.spending-metric .metric-label {
    color: var(--text-muted);
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.5rem;
}

.spending-metric .metric-value {
    font-size: 1.8rem;
    font-weight: 700;
    color: var(--text);
}

.spending-metric .metric-value.positive { color: var(--green); }
.spending-metric .metric-value.negative { color: #f85149; }

.spending-metric .metric-sub {
    color: var(--text-muted);
    font-size: 0.78rem;
    margin-top: 0.25rem;
}

.model-table {
    width: 100%;
    border-collapse: collapse;
    margin: 1.5rem 0;
}

.model-table th {
    text-align: left;
    color: var(--text-muted);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.75rem 0.5rem;
    border-bottom: 1px solid var(--border);
}

.model-table td {
    padding: 0.6rem 0.5rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.88rem;
}

.model-table tr:hover td {
    background: rgba(124, 58, 237, 0.05);
}

.model-table .model-name {
    font-weight: 600;
    max-width: 240px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.model-table .cost-cell {
    font-variant-numeric: tabular-nums;
    text-align: right;
}

/* Daily bar chart */
.daily-bars {
    display: flex;
    align-items: flex-end;
    gap: 4px;
    height: 120px;
    margin: 1rem 0 2rem;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--border);
}

.daily-bar-wrapper {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    min-width: 0;
}

.daily-bar {
    width: 100%;
    max-width: 32px;
    background: var(--accent);
    border-radius: 4px 4px 0 0;
    min-height: 2px;
    transition: background 0.2s;
    cursor: pointer;
}

.daily-bar:hover {
    background: var(--accent-hover);
}

.daily-bar-label {
    color: var(--text-muted);
    font-size: 0.6rem;
    margin-top: 0.35rem;
    white-space: nowrap;
    transform: rotate(-45deg);
    transform-origin: top left;
    margin-left: 8px;
}

.recent-table {
    width: 100%;
    border-collapse: collapse;
}

.recent-table th {
    text-align: left;
    color: var(--text-muted);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.5rem 0.5rem;
    border-bottom: 1px solid var(--border);
}

.recent-table td {
    padding: 0.5rem 0.5rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.82rem;
    font-variant-numeric: tabular-nums;
}

.recent-table .when-cell {
    color: var(--text-muted);
    white-space: nowrap;
}

.loading-skeleton {
    background: linear-gradient(90deg, var(--bg-card) 25%, #1c2333 50%, var(--bg-card) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
    border-radius: 8px;
}

@keyframes shimmer {
    0% { background-position: -200% 0; }
    100% { background-position: 200% 0; }
}

/* ── Home page spending mini cards ── */
.spending-mini {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
    transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s;
    display: flex;
    flex-direction: column;
    flex: 0 0 240px;
    scroll-snap-align: start;
    min-height: 120px;
}

.spending-mini:hover {
    border-color: var(--accent);
    transform: translateY(-2px);
    box-shadow: 0 4px 20px rgba(124, 58, 237, 0.15);
}

.spend-mini-label {
    color: var(--text-muted);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.5rem;
}

.spend-mini-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 0.35rem;
}

.spend-mini-value.positive { color: var(--green); }
.spend-mini-value.negative { color: #f85149; }

.spend-mini-sub {
    color: var(--text-muted);
    font-size: 0.78rem;
    margin-top: auto;
}

/* ── Quick Links Grid ── */
.quicklinks-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 1rem;
    margin-bottom: 2rem;
}

.link-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
    text-decoration: none;
    color: var(--text);
    display: flex;
    align-items: flex-start;
    gap: 1rem;
    transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s;
}

.link-card:hover {
    border-color: var(--accent);
    transform: translateY(-2px);
    box-shadow: 0 4px 20px rgba(124, 58, 237, 0.15);
}

.link-card .link-emoji {
    font-size: 1.5rem;
    line-height: 1;
    flex-shrink: 0;
    width: 40px;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(124, 58, 237, 0.12);
    border-radius: 10px;
}

.link-card .link-info {
    flex: 1;
    min-width: 0;
}

.link-card .link-label {
    font-weight: 600;
    font-size: 0.95rem;
    margin-bottom: 0.2rem;
}

.link-card .link-desc {
    font-size: 0.78rem;
    color: var(--text-muted);
    line-height: 1.4;
}

/* ── Link Health Status Dots ── */
.link-health-dot {
    width: 10px; height: 10px; border-radius: 50%;
    flex-shrink: 0; margin-left: auto; align-self: center;
    transition: background 0.3s;
}
.link-health-dot.ok {
    background: var(--green);
    box-shadow: 0 0 6px rgba(63, 185, 80, 0.5);
}
.link-health-dot.error {
    background: #f85149;
    box-shadow: 0 0 6px rgba(248, 81, 73, 0.5);
    animation: health-pulse 1.5s ease-in-out infinite;
}
.link-health-dot.unknown {
    background: var(--text-muted);
    animation: health-pulse 1.5s ease-in-out infinite;
}
@keyframes health-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
.link-card.link-health-error {
    border-left: 3px solid #f85149;
}

/* ── Category Filter Bar ── */
.category-filter {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 1.25rem;
}

.category-pill {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 100px;
    padding: 0.45rem 1rem;
    font-size: 0.82rem;
    font-weight: 500;
    color: var(--text-muted);
    cursor: pointer;
    transition: all 0.2s;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    white-space: nowrap;
}

.category-pill:hover {
    border-color: var(--accent);
    color: var(--text);
}

.category-pill.active {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
}

.category-pill.empty {
    opacity: 0.4;
    cursor: default;
    pointer-events: none;
}

.pill-count {
    background: rgba(255,255,255,0.15);
    border-radius: 100px;
    padding: 0.1rem 0.45rem;
    font-size: 0.7rem;
    font-weight: 600;
    min-width: 1.2em;
    text-align: center;
}

.category-pill.active .pill-count {
    background: rgba(255,255,255,0.25);
}

.link-card.filtered-out {
    display: none;
}

/* ── Cron Status Table ── */
.cron-table {
    width: 100%;
    border-collapse: collapse;
    margin: 1.5rem 0;
}

.cron-table th {
    text-align: left;
    color: var(--text-muted);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.75rem 0.5rem;
    border-bottom: 1px solid var(--border);
}

.cron-table td {
    padding: 0.75rem 0.5rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.88rem;
}

.cron-table tr:hover td {
    background: rgba(124, 58, 237, 0.05);
}

.cron-table a {
    color: var(--accent-hover);
    text-decoration: none;
    font-weight: 500;
}

.cron-table a:hover {
    text-decoration: underline;
}

.status-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 0.5rem;
    vertical-align: middle;
}

.status-dot.green  { background: var(--green); }
.status-dot.orange { background: var(--orange); }
.status-dot.red    { background: var(--red); }

.status-badge {
    display: inline-block;
    font-size: 0.72rem;
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    font-weight: 600;
    text-transform: uppercase;
}

.status-badge.ok     { background: rgba(63, 185, 80, 0.12); color: var(--green); }
.status-badge.error  { background: rgba(248, 81, 73, 0.12); color: var(--red); }
.status-badge.never  { background: rgba(210, 153, 29, 0.12); color: var(--orange); }
.status-badge.paused { background: rgba(139, 148, 158, 0.12); color: var(--text-muted); }

/* ── Cron output list ── */
.output-list {
    margin: 1.5rem 0;
}

.output-list .output-item {
    display: block;
    padding: 0.75rem 1rem;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 0.5rem;
    color: var(--text);
    text-decoration: none;
    transition: border-color 0.2s, background 0.2s;
}

.output-list .output-item:hover {
    border-color: var(--accent);
    background: #1c2333;
}

.output-item .output-date {
    font-weight: 600;
}

.output-item .output-size {
    color: var(--text-muted);
    font-size: 0.82rem;
    margin-left: 0.75rem;
}

/* ── Cron output preview ── */
.output-preview {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
    margin: 1.5rem 0;
    white-space: pre-wrap;
    font-family: 'SF Mono', 'Fira Code', 'Fira Mono', Menlo, Consolas, monospace;
    font-size: 0.82rem;
    line-height: 1.6;
    max-height: 600px;
    overflow-y: auto;
    color: var(--text);
}

@media (max-width: 900px) {
}

@media (max-width: 500px) {
}

/* ── Page header with back link ── */
.page-back {
    padding: 1rem 0 0;
}

.page-back a {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.9rem;
}

.page-back a:hover {
    color: var(--accent-hover);
}

footer {
    text-align: center;
    padding: 2rem;
    color: var(--text-muted);
    font-size: 0.8rem;
    border-top: 1px solid var(--border);
    margin-top: 3rem;
}

.footer-nav {
    display: flex;
    justify-content: center;
    gap: 1.5rem;
    margin-top: 0.5rem;
    flex-wrap: wrap;
}

.footer-nav a {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.78rem;
}

.footer-nav a:hover {
    color: var(--accent-hover);
}

@media (max-width: 640px) {
    nav { padding: 0 1rem; }
    .container { padding: 1rem; }
    .hero h1 { font-size: 1.8rem; }
    .quicklinks-grid { grid-template-columns: 1fr; }
    .category-pill { padding: 0.35rem 0.7rem; font-size: 0.75rem; }
}


/* ── Command Center ── */
.command-center {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 0.75rem;
    margin-bottom: 2rem;
}

.cc-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

.cc-icon {
    font-size: 1.2rem;
}

.cc-label {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    font-weight: 600;
}

.cc-metric {
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text);
}

.cc-timestamp {
    font-size: 0.65rem;
    color: var(--text-muted);
}

.cc-action {
    font-size: 0.72rem;
    color: var(--accent-hover);
    font-weight: 500;
    margin-top: auto;
}

.cc-action:hover {
    text-decoration: underline;
}

/* ── Status badges extended ── */
.status-badge.online  { background: rgba(63, 185, 80, 0.12); color: var(--green); }
.status-badge.offline { background: rgba(248, 81, 73, 0.12); color: var(--red); }
.status-badge.warning { background: rgba(210, 153, 29, 0.12); color: var(--orange); }
.status-badge.stale   { background: rgba(210, 153, 29, 0.12); color: var(--orange); }
.status-badge.running { background: rgba(63, 185, 80, 0.12); color: var(--green); }
.status-badge.failed  { background: rgba(248, 81, 73, 0.12); color: var(--red); }

.status-dot.online  { background: var(--green); }
.status-dot.offline { background: var(--red); }
.status-dot.warning { background: var(--orange); }
.status-dot.stale   { background: var(--orange); }

/* ── Dashboard responsive grid ── */
.dashboard-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 1rem;
    margin-bottom: 2rem;
    padding: 0.5rem 0 1rem;
}

@media (max-width: 640px) {
    .dashboard-grid {
        grid-template-columns: 1fr;
    }
}

/* ── Section timestamp ── */
.section-timestamp {
    font-size: 0.72rem;
    color: #9aa4b2;
    margin: -0.5rem 0 0.75rem;
    font-style: italic;
}

/* Long/preformatted output never forces the page to scroll horizontally. */
pre { overflow-x: auto; }

/* ── Card action link ── */
.card-action {
    display: block;
    margin-top: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border);
    font-size: 0.78rem;
    color: var(--accent-hover);
    font-weight: 500;
    text-decoration: none;
}

.card-action:hover {
    text-decoration: underline;
}

/* ── Confirmation Modal ── */
.confirm-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0, 0, 0, 0.75);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10000;
    animation: confirmFadeIn 0.15s ease;
    backdrop-filter: blur(4px);
}
@keyframes confirmFadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
}
.confirm-modal {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 2rem 2rem 1.5rem;
    max-width: 480px;
    width: 90vw;
    box-shadow: 0 12px 50px rgba(0, 0, 0, 0.6);
    animation: confirmSlideIn 0.2s ease;
}
@keyframes confirmSlideIn {
    from { transform: translateY(-20px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
}
.confirm-modal .confirm-icon {
    text-align: center;
    font-size: 2.5rem;
    margin-bottom: 0.75rem;
    line-height: 1;
}
.confirm-modal .confirm-title {
    font-size: 1.2rem;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 0.5rem;
    text-align: center;
}
.confirm-modal .confirm-desc {
    color: var(--text-muted);
    font-size: 0.9rem;
    line-height: 1.5;
    margin-bottom: 1.25rem;
    text-align: center;
}
.confirm-modal .confirm-input-row {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1rem;
}
.confirm-modal .confirm-input-row input {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.6rem 0.8rem;
    color: var(--text);
    font-size: 0.9rem;
    outline: none;
    transition: border-color 0.2s;
}
.confirm-modal .confirm-input-row input:focus {
    border-color: var(--accent);
}
.confirm-modal .confirm-buttons {
    display: flex;
    gap: 0.75rem;
    justify-content: flex-end;
}
.confirm-btn {
    padding: 0.6rem 1.5rem;
    border-radius: 8px;
    font-size: 0.9rem;
    font-weight: 600;
    cursor: pointer;
    border: none;
    transition: background 0.2s, transform 0.1s;
}
.confirm-btn:active {
    transform: scale(0.97);
}
.confirm-btn.cancel {
    background: var(--bg);
    color: var(--text-muted);
    border: 1px solid var(--border);
}
.confirm-btn.cancel:hover {
    background: #1c2333;
    color: var(--text);
}
.confirm-btn.danger {
    background: var(--red);
    color: #fff;
}
.confirm-btn.danger:hover {
    background: #d73a49;
}
.confirm-btn.danger:disabled {
    background: #5a1d28;
    color: #8b949e;
    cursor: not-allowed;
}

/* ── Command Shortcuts Panel ── */
.shortcuts-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 1rem; }
.shortcut-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 1.25rem; text-decoration: none; color: var(--text); display: flex; flex-direction: column; align-items: center; text-align: center; gap: 0.6rem; transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s; cursor: pointer; }
.shortcut-card:hover { border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 4px 20px rgba(124, 58, 237, 0.15); }
.shortcut-card .sc-label { font-size: 0.9rem; font-weight: 600; line-height: 1.3; color: var(--text); }
.shortcut-card .sc-hint { font-size: 0.75rem; color: var(--text-muted); }
.shortcut-card.destructive { border-color: rgba(210, 153, 29, 0.3); }
.shortcut-card.destructive:hover { border-color: var(--orange); }
.shortcut-card .sc-status { font-size: 0.75rem; margin-top: 0.25rem; }
.shortcut-card .sc-status.success { color: var(--green); }
.shortcut-card .sc-status.error { color: #f85149; }
.shortcut-card.loading { opacity: 0.7; pointer-events: none; }

/* ── Models Section ── */
.models-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
    min-width: 280px;
    max-width: 400px;
}
.models-card h3 {
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--accent);
    margin-bottom: 0.85rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
}
.models-list {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    margin-bottom: 1rem;
}
.model-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem 0.75rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 0.85rem;
}
.model-item .model-name { font-weight: 500; }
.model-item .model-size {
    color: var(--text-muted);
    font-size: 0.8rem;
}
.model-delete-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    font-size: 0.85rem;
    padding: 0.1rem 0.35rem;
    border-radius: 4px;
    line-height: 1;
    transition: color 0.2s, background 0.2s;
    flex-shrink: 0;
}
.model-delete-btn:hover {
    color: #f85149;
    background: rgba(248,81,73,0.1);
}
.model-bench-btn {
    background: transparent;
    border: 1px solid rgba(88,166,255,0.27);
    color: var(--blue);
    cursor: pointer;
    font-size: 0.72rem;
    padding: 0.15rem 0.5rem;
    border-radius: 5px;
    font-family: inherit;
    flex-shrink: 0;
    white-space: nowrap;
    transition: all 0.15s;
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
}
.model-bench-btn:hover { background: rgba(88,166,255,0.1); }
.model-bench-btn:disabled {
    color: var(--text-muted);
    border-color: var(--border);
    cursor: not-allowed;
    opacity: 0.5;
}
.model-bench-spinner {
    width: 10px;
    height: 10px;
    border: 2px solid rgba(88,166,255,0.27);
    border-top: 2px solid var(--blue);
    border-radius: 50%;
    animation: bench-spin 0.6s linear infinite;
    display: inline-block;
}
@keyframes bench-spin { to { transform: rotate(360deg); } }
.model-bench-result {
    font-size: 0.7rem;
    color: var(--green);
    padding: 0.15rem 0.4rem;
    border-radius: 4px;
    background: rgba(63,185,80,0.1);
    flex-shrink: 0;
    white-space: nowrap;
    cursor: default;
    position: relative;
}
.model-bench-result-error {
    font-size: 0.7rem;
    color: var(--red);
    padding: 0.15rem 0.4rem;
    border-radius: 4px;
    background: rgba(248,81,73,0.1);
    flex-shrink: 0;
    white-space: nowrap;
    cursor: default;
    position: relative;
}
.model-bench-tooltip {
    position: absolute;
    bottom: calc(100% + 6px);
    right: 0;
    background: #1c2333;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.5rem 0.65rem;
    font-size: 0.72rem;
    color: var(--text);
    white-space: nowrap;
    z-index: 10;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    pointer-events: none;
}
.pull-form {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
}
.pull-form input {
    flex: 1;
    padding: 0.5rem 0.75rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-size: 0.85rem;
    outline: none;
    font-family: inherit;
}
.pull-form input:focus { border-color: var(--accent); }
.pull-form button {
    padding: 0.5rem 1rem;
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 6px;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s;
    white-space: nowrap;
    font-family: inherit;
}
.pull-form button:hover { background: var(--accent-hover); }
.pull-form button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}
.pull-progress {
    margin-top: 0.5rem;
    padding: 0.75rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
}
.pull-progress-bar {
    height: 4px;
    background: var(--border);
    border-radius: 2px;
    margin-bottom: 0.5rem;
    overflow: hidden;
}
.pull-progress-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 2px;
    transition: width 0.3s ease;
}
.pull-progress-text {
    color: var(--text-muted);
    font-size: 0.8rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
.pull-progress-done { color: var(--green); }
.pull-progress-error { color: var(--red); }
@keyframes pull-spin { to { transform: rotate(360deg); } }
.pull-spinner {
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 2px solid transparent;
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: pull-spin 0.8s linear infinite;
}
.models-hint {
    color: var(--text-muted);
    font-size: 0.75rem;
    font-style: italic;
    margin-top: 0.35rem;
}
/* ── Bookmarks ── */
.bm-empty {
    text-align: center;
    padding: 3rem 1rem;
    color: var(--text-muted);
    font-size: 1.1rem;
}
.bm-empty p { margin: 0; }
.bm-section h3 {
    font-size: 1.1rem;
    margin: 2rem 0 0.75rem;
    padding-bottom: 0.35rem;
    border-bottom: 1px solid var(--border);
}
.bm-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem 1.25rem;
    margin-bottom: 0.75rem;
}
.bm-card h4 {
    margin: 0 0 0.35rem;
    font-size: 1rem;
}
.bm-card h4 a { color: inherit; text-decoration: none; }
.bm-card h4 a:hover { color: var(--accent); }
.bm-meta {
    color: var(--text-muted);
    font-size: 0.8rem;
    margin-bottom: 0.35rem;
}
.bm-body {
    font-size: 0.85rem;
    color: var(--text);
    margin-bottom: 0.5rem;
    line-height: 1.4;
}
.bm-remove {
    background: transparent;
    color: var(--red, #e0556a);
    border: 1px solid var(--red, #e0556a);
    border-radius: 4px;
    padding: 0.2rem 0.7rem;
    font-size: 0.75rem;
    cursor: pointer;
    transition: all 0.15s;
}
.bm-remove:hover { background: var(--red, #e0556a); color: #fff; }
/* Bookmark toggle buttons on article cards */
.bm-btn-row {
    display: flex;
    gap: 0.4rem;
    margin-top: 0.5rem;
}
.bm-btn {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.25rem 0.65rem;
    font-size: 0.75rem;
    cursor: pointer;
    color: var(--text-muted);
    transition: all 0.15s;
}
.bm-btn:hover { border-color: var(--accent); color: var(--accent); }
.bm-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }

/* ── Mobile phones (375-480px) ── */
@media (max-width: 480px) {
    /* 3. Container padding reduced */
    .container { padding: 1rem; }

    /* 6. Nav bar — horizontal scroll instead of wrapping */
    nav {
        padding: 0 0.75rem;
        height: auto;
        min-height: 52px;
    }
    nav .links {
        flex-wrap: nowrap;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        gap: 0.75rem;
        padding-bottom: 0.25rem;
        scrollbar-width: none;
    }
    nav .links::-webkit-scrollbar { display: none; }
    nav .links a { font-size: 0.8rem; padding: 0.45rem 0; }
    nav .logo { font-size: 1.05rem; }

    /* 11. Hero h1 sizing */
    .hero h1 { font-size: 1.8rem; }
    .hero p { font-size: 0.95rem; }
    .hero { padding: 2rem 0 1rem; }

    /* 2. Card fixed widths → viewport-relative */
    .briefing-card,
    .repo-card,
    .spending-mini {
        flex: 0 0 calc(100vw - 2rem);
        max-width: calc(100vw - 2rem);
    }
    .tunnel-status-card,
    .tunnel-hostnames-card,
    .tunnel-ports-card,
    .tunnel-policies-card {
        min-width: 0;
    }
    .models-card {
        min-width: 0;
        max-width: 100%;
    }

    /* 4. Scroll arrows — hide on mobile (off-screen otherwise) */
    .scroll-arrow.left,
    .scroll-arrow.right {
        display: none;
    }

    /* 10. Touch swipe for scrollable card rows */
    .briefing-grid {
        touch-action: pan-x;
    }

    /* 5. Grid minmax fix — single column on phone */
    .quicklinks-grid {
        grid-template-columns: 1fr;
    }
    .dashboard-grid {
        grid-template-columns: 1fr;
    }
    .spending-metrics {
        grid-template-columns: 1fr;
    }
    .shortcuts-grid {
        grid-template-columns: 1fr;
    }

    /* 7. Command center — 2-wide then stack */
    .command-center {
        grid-template-columns: repeat(2, 1fr);
    }

    /* 8. Tap targets — min 44px for all interactive elements */
    button,
    .bm-btn,
    .bm-remove,
    .category-pill,
    .category-tab,
    .confirm-btn,
    .model-delete-btn,
    .model-bench-btn,
    .link-card {
        min-height: 44px;
    }
    .link-card {
        padding: 0.75rem 1rem;
    }
    .category-pill {
        padding: 0.5rem 0.85rem;
    }
    .category-tab {
        padding: 0.45rem 0.85rem;
    }
    .confirm-btn {
        padding: 0.7rem 1.5rem;
    }
    .bm-btn {
        padding: 0.4rem 0.75rem;
    }

    /* 9. Tables — horizontal scroll */
    .cron-table,
    .model-table,
    .tunnel-table,
    .recent-table {
        display: block;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
    }
    .cron-table tbody,
    .model-table tbody,
    .tunnel-table tbody,
    .recent-table tbody {
        white-space: nowrap;
    }

    /* 13. No horizontal overflow on body */
    body {
        overflow-x: hidden;
    }

    /* Section titles */
    .section-title {
        font-size: 1.15rem;
        margin: 1.5rem 0 0.75rem;
    }

    /* Card action links */
    .card-action {
        font-size: 0.82rem;
        min-height: 44px;
        display: flex;
        align-items: center;
    }

    /* Footer */
    footer {
        padding: 1.5rem 1rem;
    }

    /* Search bar */
    .search-bar {
        flex-direction: column;
    }
    .search-input-wrap {
        width: 100%;
    }

    /* Category filter */
    .category-filter {
        gap: 0.35rem;
    }
    .category-tabs {
        gap: 0.35rem;
    }

    /* Confirm modal */
    .confirm-modal {
        width: 95vw;
        padding: 1.5rem;
    }
    .confirm-modal .confirm-buttons {
        flex-direction: column;
    }
    .confirm-btn {
        width: 100%;
    }

    /* Spending metric values */
    .spending-metric .metric-value {
        font-size: 1.4rem;
    }

    /* CTA button */
    .cta-button {
        padding: 0.7rem 1.5rem;
        font-size: 1rem;
    }

    /* Footer nav */
    .footer-nav { gap: 1rem; }

    /* Daily bars chart */
    .daily-bars {
        height: 80px;
    }
    .daily-bar {
        max-width: 20px;
    }

    /* Page back link */
    .page-back {
        padding: 0.5rem 0 0;
    }

    /* System Overview — full width, tighter padding */
    .system-overview > summary {
        padding: 0.75rem 1rem;
        font-size: 1rem;
    }
    .system-overview-body {
        padding: 0 1rem 1rem;
    }
    .sys-summary-grid {
        grid-template-columns: 1fr;
    }
}

"""
# ═══════════════════════════════════════════════════════════════
#  Quick Links
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════

def _esc(text: str) -> str:
    """Minimal HTML-escape for attribute-safe rendering."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") \
               .replace('"', "&quot;").replace("'", "&#39;")


# Valid bookmark categories — referenced by load_quick_links() and UI components
BOOKMARK_CATEGORIES = [
    "AI / models",
    "Homelab",
    "Coding",
    "Cloudflare",
    "GitHub",
    "Docs",
    "Dashboards",
]


def load_quick_links() -> list[dict]:
    """Load quick links from config file, returning sensible defaults if missing.

    Each link dict has these fields:
        label       (str, required) — display name
        url         (str, required) — target URL
        emoji       (str, optional) — icon, defaults to '🔗'
        description (str, optional) — hover/subtitle text, defaults to ''
        category    (str, optional) — one of BOOKMARK_CATEGORIES, defaults to ''
        healthStatus(str, optional) — 'ok' | 'error' | 'unknown', defaults to 'unknown'
    """
    defaults = [
        {"label": "OpenRouter", "url": "https://openrouter.ai/activity", "emoji": "🤖",
         "description": "AI model usage and spending dashboard",
         "category": "AI / models", "healthStatus": "unknown"},
        {"label": "GitHub", "url": "https://github.com/VerduzcoTristan", "emoji": "💻",
         "description": "All projects and repositories",
         "category": "GitHub", "healthStatus": "unknown"},
        {"label": "Cloudflare", "url": "https://dash.cloudflare.com", "emoji": "☁️",
         "description": "DNS, tunnels, and domain management",
         "category": "Cloudflare", "healthStatus": "unknown"},
        {"label": "Linear", "url": "https://linear.app", "emoji": "📋",
         "description": "Project and task management",
         "category": "Dashboards", "healthStatus": "unknown"},
        {"label": "Hermes Docs", "url": "https://hermes-agent.nousresearch.com/docs", "emoji": "📘",
         "description": "Hermes Agent configuration and reference",
         "category": "Docs", "healthStatus": "unknown"},
    ]
    if not QUICKLINKS_FILE.exists():
        return defaults
    try:
        links = json.loads(QUICKLINKS_FILE.read_text())
        if isinstance(links, list) and links:
            return links
    except (json.JSONDecodeError, Exception):
        pass
    return defaults


# ── Link Health Check helpers ──

def _is_internal_link(url: str) -> bool:
    """Check if a URL's hostname matches any internal domain (exact or subdomain)."""
    from urllib.parse import urlparse
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower()
        for domain in _INTERNAL_DOMAINS:
            domain = domain.strip().lower()
            if not domain:
                continue
            if hostname == domain or hostname.endswith("." + domain):
                return True
        return False
    except Exception:
        return False


def _check_single_link(url: str, method: str = "HEAD") -> tuple[str, str | None]:
    """Check a single URL. Returns (status, error_message).
    Tries HEAD first, falls back to GET on 405/501."""
    import urllib.request
    import urllib.error
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for attempt_method in (method, "GET"):
        try:
            req = urllib.request.Request(url, method=attempt_method)
            req.add_header("User-Agent", "devmclovin-link-checker/1.0")
            resp = urllib.request.urlopen(req, timeout=8, context=ctx)
            if attempt_method == "GET":
                resp.read(4096)
            resp.close()
            return ("ok", None)
        except urllib.error.HTTPError as e:
            if attempt_method == method and e.code in (405, 501):
                continue
            resp_body = ""
            try:
                resp_body = e.read(4096).decode("utf-8", errors="replace")
            except Exception:
                pass
            err = f"HTTP {e.code}"
            if resp_body:
                err += f": {resp_body[:120]}"
            return ("error", err)
        except urllib.error.URLError as e:
            return ("error", str(e.reason))
        except Exception as e:
            return ("error", str(e))
    return ("error", "No valid method")


def get_link_health(url: str) -> dict:
    """Get cached link health. Returns {status: 'ok'|'error'|'external', error: str|None}."""
    if not _is_internal_link(url):
        return {"status": "external", "error": None}

    cached = _LINK_HEALTH_CACHE.get(url)
    if cached and (time.time() - cached["ts"]) < _LINK_HEALTH_TTL:
        return cached

    status, err = _check_single_link(url)
    result = {"status": status, "error": err, "ts": time.time()}
    _LINK_HEALTH_CACHE[url] = result
    return result


def quick_links_row() -> str:
    """Render a grid of quick-link cards with category filter bar and health indicators."""
    links = load_quick_links()
    if not links:
        return ""

    # ── Category counts ──
    cat_counts: dict[str, int] = {}
    for link in links:
        cat = link.get("category", "") or ""
        if cat:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
    total = len(links)

    # ── Category filter bar ──
    html = '<div class="section-title">🔗 Quick Links</div>'
    html += '<div class="category-filter">'
    # "All" pill — always active by default
    html += f'<button class="category-pill active" onclick="filterByCategory(&#39;all&#39;, this)" data-cat="all">All<span class="pill-count">{total}</span></button>'

    for cat in BOOKMARK_CATEGORIES:
        count = cat_counts.get(cat, 0)
        if count == 0:
            continue
        html += f'<button class="category-pill" onclick="filterByCategory(&#39;{_esc(cat)}&#39;, this)" data-cat="{_esc(cat)}">{_esc(cat)}<span class="pill-count">{count}</span></button>'

    html += '</div>'

    # ── Link cards grid ──
    html += '<div class="quicklinks-grid">'
    for link in links:
        url = link["url"]
        health = get_link_health(url)
        status = health["status"]
        error_msg = health.get("error", "")
        cat = link.get("category", "") or ""

        card_class = "link-card"
        if status == "error":
            card_class += " link-health-error"

        html += f'<a href="{_esc(url)}" target="_blank" rel="noopener" class="{card_class}" data-category="{_esc(cat)}">'
        html += f'<span class="link-emoji">{_esc(link.get("emoji", "🔗"))}</span>'
        html += '<span class="link-info">'
        html += f'<div class="link-label">{_esc(link["label"])}</div>'
        html += f'<div class="link-desc">{_esc(link.get("description", ""))}</div>'
        html += '</span>'

        if status == "external":
            pass
        elif status == "ok":
            html += '<span class="link-health-dot ok" title="Reachable"></span>'
        elif status == "error":
            err_text = _esc(error_msg) if error_msg else "Unreachable"
            html += f'<span class="link-health-dot error" title="{err_text}"></span>'
        else:
            html += '<span class="link-health-dot unknown" title="Checking..."></span>'

        html += '</a>'
    html += '</div>'
    return html


# ═══════════════════════════════════════════════════════════════
#  Cron Job Status Viewer
# ═══════════════════════════════════════════════════════════════

def load_cron_jobs() -> list[dict]:
    """Load cron jobs from jobs.json. Returns empty list on failure."""
    if not CRON_JOBS_FILE.exists():
        return []
    try:
        data = json.loads(CRON_JOBS_FILE.read_text())
        return data.get("jobs", [])
    except (json.JSONDecodeError, Exception):
        return []


def _cron_output_files(job_id: str) -> list[Path]:
    """Return most recent output files for a cron job, sorted newest first."""
    out_dir = CRON_OUTPUT_DIR / job_id
    if not out_dir.is_dir():
        return []
    files = sorted(out_dir.glob("*.md"), reverse=True)
    return files[:20]


def _cron_status_dot(job: dict) -> str:
    """Return a coloured status dot + label for a cron job."""
    last_status = job.get("last_status", "")
    last_run = job.get("last_run_at")
    paused = job.get("paused_at") is not None

    if paused:
        return '<span class="status-dot orange"></span> Paused'
    if last_run is None:
        return '<span class="status-dot orange"></span> Never run'
    if last_status == "ok":
        return '<span class="status-dot green"></span> OK'
    if last_status == "error":
        return '<span class="status-dot red"></span> Error'
    return '<span class="status-dot orange"></span> Unknown'


def _format_schedule(job: dict) -> str:
    """Format the schedule display for a cron job."""
    sched = job.get("schedule", {})
    display = sched.get("display", "") if isinstance(sched, dict) else ""
    if not display:
        display = job.get("schedule_display", "")
    return display


def _format_iso_time(iso_str: str | None) -> str:
    """Format an ISO timestamp for display."""
    if not iso_str:
        return "—"
    try:
        ts = iso_str.replace("+00:00", "").replace("Z", "")
        # Strip microseconds if present
        if "." in ts:
            ts = ts[:19]
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, Exception):
        return iso_str[:19] if len(iso_str) >= 19 else iso_str
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def cron_page() -> str:
    """Render the /cron status dashboard."""
    jobs = load_cron_jobs()
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>Cron Jobs</h1></div>'

    if not jobs:
        body += '<div class="empty-state"><p>No cron jobs configured.</p></div>'
        return html_page("Cron Jobs", body, active_nav="cron")

    body += '<table class="cron-table">'
    body += '<thead><tr><th>Name</th><th>Schedule</th><th>Last Run</th><th>Next Run</th><th>Status</th></tr></thead>'
    body += '<tbody>'

    for job in jobs:
        name = job.get("name", job.get("id", ""))
        job_id = job.get("id", "")
        schedule = _format_schedule(job)
        last_run = _format_iso_time(job.get("last_run_at"))
        next_run = _format_iso_time(job.get("next_run_at"))
        status_html = _cron_status_dot(job)

        body += '<tr>'
        body += f'<td><a href="/cron/{job_id}">{name}</a></td>'
        body += f'<td><code>{schedule}</code></td>'
        body += f'<td>{last_run}</td>'
        body += f'<td>{next_run}</td>'
        body += f'<td>{status_html}</td>'
        body += '</tr>'

    body += '</tbody></table>'

    return html_page("Cron Jobs", body, active_nav="cron")


def cron_job_detail_page(job_id: str) -> tuple[int, str]:
    """Render a detail page for a single cron job showing recent outputs.
    Returns (status_code, html) so the handler can 404 an unknown job id."""
    jobs = load_cron_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)

    body = '<div class="page-back"><a href="/cron">← Back to all cron jobs</a></div>'

    if not job:
        body += f'<div class="empty-state" style="margin-top:2rem"><p>Cron job {html.escape(job_id)} not found.</p></div>'
        return 404, html_page(f"Cron — {job_id}", body, active_nav="cron")

    name = job.get("name", job_id)
    schedule = _format_schedule(job)
    last_status = job.get("last_status", "unknown")
    last_run = _format_iso_time(job.get("last_run_at"))
    next_run = _format_iso_time(job.get("next_run_at"))

    body += f'<h2 style="margin-top:1rem">{name}</h2>'
    body += '<div style="margin:1rem 0;display:flex;gap:2rem;flex-wrap:wrap">'
    body += f'<div><span style="color:var(--text-muted)">Schedule:</span> <code>{schedule}</code></div>'
    body += f'<div><span style="color:var(--text-muted)">Last run:</span> {last_run}</div>'
    body += f'<div><span style="color:var(--text-muted)">Next run:</span> {next_run}</div>'
    body += f'<div><span style="color:var(--text-muted)">Status:</span> {_cron_status_dot(job)}</div>'
    body += '</div>'

    # List recent output files
    files = _cron_output_files(job_id)
    if not files:
        body += '<div class="empty-state"><p>No output files yet.</p></div>'
    else:
        body += '<div class="section-title">Recent Outputs</div>'
        body += '<div class="output-list">'
        for f in files:
            fname = f.name
            try:
                fsize = f.stat().st_size
                if fsize > 1024 * 1024:
                    size_str = f"{fsize / (1024 * 1024):.1f} MB"
                elif fsize > 1024:
                    size_str = f"{fsize / 1024:.1f} KB"
                else:
                    size_str = f"{fsize} B"
            except Exception:
                size_str = "?"

            # Parse date from filename: YYYY-MM-DD_HH-MM-SS.md
            dt_display = fname.replace(".md", "").replace("_", " ")
            body += f'<a href="/cron/{job_id}/{fname}" class="output-item">'
            body += f'<span class="output-date">{dt_display}</span>'
            body += f'<span class="output-size">{size_str}</span>'
            body += '</a>'
        body += '</div>'

    return 200, html_page(f"Cron — {name}", body, active_nav="cron")


def cron_output_preview_page(job_id: str, filename: str) -> str:
    """Render a preview of a specific cron output file."""
    file_path = CRON_OUTPUT_DIR / job_id / filename
    body = f'<div class="page-back"><a href="/cron/{job_id}">← Back to {job_id}</a></div>'

    if not file_path.exists():
        body += '<div class="empty-state" style="margin-top:2rem"><p>Output file not found.</p></div>'
        return html_page("Cron Output", body, active_nav="hermes")

    try:
        raw = file_path.read_text()
    except Exception:
        raw = "Error reading output file."

    body += f'<h2 style="margin-top:1rem">{filename}</h2>'
    body += f'<div class="output-preview">{raw}</div>'

    return html_page(f"Cron — {filename}", body, active_nav="hermes")


# ═══════════════════════════════════════════════════════════════
#  Kanban Board Dashboard
# ═══════════════════════════════════════════════════════════════

def _kanban_db() -> sqlite3.Connection | None:
    """Open a read-only connection to the kanban database. Returns None on failure."""
    if not KANBAN_DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(KANBAN_DB))
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def kanban_create(title: str, body: str = "", assignee: str = "") -> str | None:
    import uuid, time
    conn = _kanban_db()
    if not conn: return None
    try:
        task_id = str(uuid.uuid4())[:8]
        now = int(time.time())
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, created_at, started_at, workspace_kind) "
            "VALUES (?,?,?,?,'running',0,'web',?,?,'scratch')",
            (task_id, title, body, assignee, now, now)
        )
        conn.commit(); return task_id
    except: return None
    finally: conn.close()

def kanban_move(task_id: str, new_status: str) -> bool:
    import time
    conn = _kanban_db()
    if not conn: return False
    try:
        now = int(time.time())
        if new_status == "running": conn.execute("UPDATE tasks SET status=?, started_at=? WHERE id=?", (new_status, now, task_id))
        elif new_status == "done": conn.execute("UPDATE tasks SET status=?, completed_at=? WHERE id=?", (new_status, now, task_id))
        else: conn.execute("UPDATE tasks SET status=? WHERE id=?", (new_status, task_id))
        conn.commit(); return True
    except: return False
    finally: conn.close()

def kanban_comment(task_id: str, author: str, body: str) -> bool:
    import time
    conn = _kanban_db()
    if not conn: return False
    try:
        now = int(time.time())
        conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)", (task_id, author, body, now))
        conn.commit(); return True
    except: return False
    finally: conn.close()

def load_kanban_tasks() -> dict[str, list[dict]]:
    """Load tasks grouped by status. Returns {status: [task_dict, ...]}.

    Returns empty dict if the database can't be read or schema differs.
    """
    conn = _kanban_db()
    if not conn:
        return {}

    try:
        rows = conn.execute("""
            SELECT id, title, body, assignee, status, priority, created_by,
                   created_at, started_at, completed_at
            FROM tasks
            WHERE status IN ('ready', 'running', 'blocked', 'done')
               OR (status = 'todo' AND assignee IS NOT NULL)
            ORDER BY priority DESC, created_at DESC
        """).fetchall()
    except sqlite3.Error:
        conn.close()
        return {}

    # Also fetch comments
    try:
        comments_raw = conn.execute("""
            SELECT task_id, author, body, created_at
            FROM task_comments
            ORDER BY created_at ASC
        """).fetchall()
    except sqlite3.Error:
        comments_raw = []

    conn.close()

    # Index comments by task_id
    comments_by_task: dict[str, list[dict]] = {}
    for c in comments_raw:
        task_id = c["task_id"]
        if task_id not in comments_by_task:
            comments_by_task[task_id] = []
        comments_by_task[task_id].append({
            "author": c["author"],
            "body": c["body"],
            "created_at": c["created_at"],
        })

    grouped: dict[str, list[dict]] = {"ready": [], "running": [], "blocked": [], "done": []}

    for row in rows:
        status = row["status"]
        # Map 'todo' (with assignee) to 'ready'
        if status == "todo":
            status = "ready"
        if status not in grouped:
            continue

        task = dict(row)
        task["comments"] = comments_by_task.get(task["id"], [])
        grouped[status].append(task)

    return grouped




def load_kanban_tasks_full() -> list[dict]:
    """Load all kanban tasks with full detail: runs, events, comments, links.
    Returns a flat list of task dicts ready for JSON serialization."""
    conn = _kanban_db()
    if not conn:
        return []

    try:
        rows = conn.execute("""
            SELECT id, title, body, assignee, status, priority, created_by,
                   created_at, started_at, completed_at, workspace_kind,
                   workspace_path, branch_name, tenant, consecutive_failures,
                   worker_pid, last_heartbeat_at, last_failure_error,
                   max_retries, block_kind
            FROM tasks
            WHERE status NOT IN ('archived')
            ORDER BY priority DESC, created_at DESC
        """).fetchall()
    except sqlite3.Error:
        conn.close()
        return []

    # Comments
    try:
        comments_raw = conn.execute(
            "SELECT task_id, author, body, created_at FROM task_comments ORDER BY created_at ASC"
        ).fetchall()
    except sqlite3.Error:
        comments_raw = []
    comments_by_task: dict[str, list[dict]] = {}
    for c in comments_raw:
        comments_by_task.setdefault(c["task_id"], []).append({
            "author": c["author"], "body": c["body"], "at": c["created_at"]
        })

    # Runs
    try:
        runs_raw = conn.execute(
            "SELECT task_id, id, profile, status, outcome, started_at, ended_at, summary, metadata, error "
            "FROM task_runs ORDER BY started_at DESC"
        ).fetchall()
    except sqlite3.Error:
        runs_raw = []
    runs_by_task: dict[str, list[dict]] = {}
    for r in runs_raw:
        runs_by_task.setdefault(r["task_id"], []).append({
            "n": r["id"],
            "profile": r["profile"],
            "outcome": r["outcome"] or "unknown",
            "startedAt": r["started_at"],
            "endedAt": r["ended_at"],
            "summary": r["summary"],
            "metadata": json.loads(r["metadata"]) if r["metadata"] else None,
            "error": r["error"],
        })

    # Events
    try:
        events_raw = conn.execute(
            "SELECT task_id, kind, payload, created_at FROM task_events ORDER BY created_at ASC"
        ).fetchall()
    except sqlite3.Error:
        events_raw = []
    events_by_task: dict[str, list[dict]] = {}
    for e in events_raw:
        try:
            payload = json.loads(e["payload"]) if e["payload"] else {}
        except (ValueError, TypeError):
            payload = {}
        events_by_task.setdefault(e["task_id"], []).append({
            "kind": e["kind"], "at": e["created_at"], "payload": payload
        })

    # Links
    try:
        links_raw = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
    except sqlite3.Error:
        links_raw = []
    parents_by_task: dict[str, list[str]] = {}
    children_by_task: dict[str, list[str]] = {}
    for lk in links_raw:
        parents_by_task.setdefault(lk["child_id"], []).append(lk["parent_id"])
        children_by_task.setdefault(lk["parent_id"], []).append(lk["child_id"])

    conn.close()

    # Build task list
    tasks: list[dict] = []
    for row in rows:
        tid = row["id"]
        status = row["status"]
        # Map 'todo' to 'todo' (the mock has a todo column)
        task = {
            "id": tid,
            "status": status,
            "title": row["title"],
            "priority": row["priority"] or 0,
            "tenant": row["tenant"] or "",
            "assignee": row["assignee"] or None,
            "workspace": (row["workspace_kind"] + (":" + row["workspace_path"] if row["workspace_path"] else "")) if row["workspace_kind"] else "scratch",
            "createdBy": row["created_by"] or "",
            "createdAt": row["created_at"],
            "body": row["body"] or "",
            "parents": parents_by_task.get(tid, []),
            "children": children_by_task.get(tid, []),
            "comments": comments_by_task.get(tid, []),
            "runs": runs_by_task.get(tid, []),
            "events": events_by_task.get(tid, []),
            "log": "",
        }
        if row["started_at"]:
            task["runStartedAt"] = row["started_at"]
        if row["last_heartbeat_at"]:
            task["lastHeartbeatAt"] = row["last_heartbeat_at"]
        if row["worker_pid"]:
            task["pid"] = row["worker_pid"]
        if row["consecutive_failures"]:
            task["failures"] = row["consecutive_failures"]
        if row["last_failure_error"]:
            task["blockReason"] = row["last_failure_error"]
        if row["block_kind"]:
            task["blockKind"] = row["block_kind"]
        if row["max_retries"]:
            task["maxRetries"] = row["max_retries"]
        if row["completed_at"]:
            task["completedAt"] = row["completed_at"]
        tasks.append(task)

    return tasks

def _kanban_age(epoch: int | None) -> str:
    """Return a human-friendly age string from a Unix epoch timestamp."""
    if not epoch:
        return ""
    delta = int(time.time() - epoch)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _get_ollama_models():
    """Fetch installed models from local Ollama API with normalized metadata."""
    import urllib.request
    try:
        req = urllib.request.Request('http://localhost:11434/api/tags')
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        models = []
        for m in data.get('models', []):
            details = m.get('details') or {}
            size = int(m.get('size') or 0)
            models.append({
                'name': m.get('name') or m.get('model') or 'unknown',
                'model': m.get('model') or m.get('name') or 'unknown',
                'size': size,
                'size_gb': round(size / (1024 ** 3), 2) if size else 0,
                'modified_at': m.get('modified_at', ''),
                'digest': m.get('digest', ''),
                'details': details,
                'family': details.get('family', ''),
                'families': details.get('families') or [],
                'parameter_size': details.get('parameter_size', ''),
                'quantization_level': details.get('quantization_level', ''),
                'format': details.get('format', ''),
            })
        return models
    except Exception:
        return []


def _llama_cli_path():
    """Find a user-local or PATH llama.cpp CLI binary."""
    import shutil
    candidates = [
        shutil.which('llama-cli'),
        shutil.which('main'),
        os.path.expanduser('~/llama.cpp/build/bin/llama-cli'),
        os.path.expanduser('~/llama.cpp/build/bin/main'),
        os.path.expanduser('~/.local/bin/llama-cli'),
    ]
    for c in candidates:
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return ''


def _gguf_registry_path() -> Path:
    return Path(os.path.expanduser('~/.devmclovin/gguf-folders.json'))


def _gguf_default_dirs() -> list[str]:
    dirs = [
        os.path.expanduser('~/models'),
        os.path.expanduser('~/gguf'),
        os.path.expanduser('~/Downloads'),
        os.path.expanduser('~/projects'),
        '/models',
    ]
    return [d for d in dirs if os.path.isdir(d)]


def _gguf_registry_data() -> dict:
    path = _gguf_registry_path()
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _gguf_write_registry(data: dict) -> None:
    path = _gguf_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _gguf_load_dirs() -> list[str]:
    data = _gguf_registry_data()
    configured = [str(x) for x in data.get('folders', []) if isinstance(x, str)]
    removed = {os.path.abspath(os.path.expanduser(str(x))) for x in data.get('removed', []) if isinstance(x, str)}
    seen = set()
    out = []
    for d in configured + _gguf_default_dirs():
        expanded = os.path.abspath(os.path.expanduser(d))
        if expanded in removed:
            continue
        if expanded not in seen and os.path.isdir(expanded):
            seen.add(expanded)
            out.append(expanded)
    return out


def _gguf_save_dir(folder: str) -> dict:
    expanded = os.path.abspath(os.path.expanduser(folder or ''))
    if not expanded or not os.path.isdir(expanded):
        return {'ok': False, 'error': 'Folder does not exist or is not readable'}
    data = _gguf_registry_data()
    folders = [os.path.abspath(os.path.expanduser(str(x))) for x in data.get('folders', []) if isinstance(x, str)]
    removed = [os.path.abspath(os.path.expanduser(str(x))) for x in data.get('removed', []) if isinstance(x, str)]
    if expanded not in folders:
        folders.insert(0, expanded)
    removed = [x for x in removed if x != expanded]
    _gguf_write_registry({'folders': folders, 'removed': removed})
    return {'ok': True, 'folders': _gguf_load_dirs()}


def _gguf_remove_dir(folder: str) -> dict:
    expanded = os.path.abspath(os.path.expanduser(folder or ''))
    data = _gguf_registry_data()
    folders = [os.path.abspath(os.path.expanduser(str(x))) for x in data.get('folders', []) if isinstance(x, str)]
    removed = [os.path.abspath(os.path.expanduser(str(x))) for x in data.get('removed', []) if isinstance(x, str)]
    folders = [x for x in folders if x != expanded]
    if expanded not in removed:
        removed.append(expanded)
    _gguf_write_registry({'folders': folders, 'removed': removed})
    return {'ok': True, 'folders': _gguf_load_dirs()}



def _gguf_quant_from_name(name: str) -> str:
    m = re.search(r'(?i)(?:^|[-_.])(IQ\d(?:_[A-Z0-9]+)?|Q\d(?:_[A-Z0-9]+)*)', name)
    return m.group(1).upper() if m else ''


def _scan_gguf_models(limit: int = 300) -> list[dict]:
    """Scan configured folders for GGUF model files."""
    models: list[dict] = []
    seen = set()
    for folder in _gguf_load_dirs():
        try:
            base = Path(folder)
            for p in base.rglob('*.gguf'):
                if len(models) >= limit:
                    return models
                try:
                    rp = str(p.resolve())
                    if rp in seen:
                        continue
                    seen.add(rp)
                    stat = p.stat()
                    lower = p.name.lower()
                    is_projector = lower.startswith('mmproj') or 'mmproj' in lower
                    models.append({
                        'id': 'gguf:' + rp,
                        'backend': 'gguf',
                        'name': p.name,
                        'path': rp,
                        'folder': str(p.parent),
                        'size': stat.st_size,
                        'size_gb': round(stat.st_size / (1024 ** 3), 2),
                        'modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        'quantization_level': _gguf_quant_from_name(p.name),
                        'format': 'gguf',
                        'is_projector': is_projector,
                    })
                except OSError:
                    continue
        except Exception:
            continue
    models.sort(key=lambda m: (m.get('is_projector', False), m.get('name', '').lower()))
    return models


def _gguf_health() -> dict:
    models = _scan_gguf_models()
    return {
        'runtime': 'llama.cpp',
        'llama_cli': _llama_cli_path(),
        'available': bool(_llama_cli_path()),
        'folders': _gguf_load_dirs(),
        'model_count': len(models),
        'models': models[:25],
        'note': '' if _llama_cli_path() else 'Install llama.cpp / llama-cli to run GGUF files. Scanning still works.',
    }


def _gguf_generate(payload: dict) -> dict:
    """Run a local GGUF with llama.cpp CLI."""
    import subprocess
    llama_cli = _llama_cli_path()
    model_id = (payload.get('model') or payload.get('path') or '').strip()
    model_path = model_id[len('gguf:'):] if model_id.startswith('gguf:') else model_id
    model_path = os.path.abspath(os.path.expanduser(model_path))
    if not llama_cli:
        return {'ok': False, 'backend': 'gguf', 'model': model_id, 'error': 'llama-cli is not installed or not on PATH', 'debug': {'folders': _gguf_load_dirs()}}
    if not model_path.endswith('.gguf') or not os.path.exists(model_path):
        return {'ok': False, 'backend': 'gguf', 'model': model_id, 'error': 'GGUF file not found: ' + model_path}

    system = payload.get('system') or ''
    prompt = payload.get('prompt') or ''
    if not prompt.strip():
        return {'ok': False, 'backend': 'gguf', 'model': 'gguf:' + model_path, 'error': 'Prompt is empty'}
    full_prompt = (('System:\n' + system.strip() + '\n\n') if system.strip() else '') + 'User:\n' + prompt.strip() + '\n\nAssistant:\n'
    options = payload.get('options') or {}
    timeout = max(5, min(int(payload.get('timeout') or 180), 1200))
    cmd = [llama_cli, '-m', model_path, '-p', full_prompt, '--no-display-prompt']
    if options.get('num_predict') not in ('', None):
        cmd += ['-n', str(int(options.get('num_predict')))]
    if options.get('num_ctx') not in ('', None):
        cmd += ['-c', str(int(options.get('num_ctx')))]
    if options.get('temperature') not in ('', None):
        cmd += ['--temp', str(options.get('temperature'))]
    if options.get('top_p') not in ('', None):
        cmd += ['--top-p', str(options.get('top_p'))]
    if options.get('top_k') not in ('', None):
        cmd += ['--top-k', str(int(options.get('top_k')))]
    if options.get('min_p') not in ('', None):
        cmd += ['--min-p', str(options.get('min_p'))]
    if options.get('repeat_penalty') not in ('', None):
        cmd += ['--repeat-penalty', str(options.get('repeat_penalty'))]
    if options.get('seed') not in ('', None):
        cmd += ['--seed', str(int(options.get('seed')))]
    if options.get('n_threads') not in ('', None):
        cmd += ['-t', str(int(options.get('n_threads')))]
    if options.get('n_gpu_layers') not in ('', None):
        cmd += ['-ngl', str(int(options.get('n_gpu_layers')))]
    lora_path = os.path.abspath(os.path.expanduser(str(payload.get('lora_path') or options.get('lora_path') or '').strip())) if (payload.get('lora_path') or options.get('lora_path')) else ''
    if lora_path:
        if not os.path.exists(lora_path):
            return {'ok': False, 'backend': 'gguf', 'model': 'gguf:' + model_path, 'error': 'LoRA adapter not found: ' + lora_path}
        cmd += ['--lora', lora_path]

    t_start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        total_ms = (time.time() - t_start) * 1000
        output = (proc.stdout or '').strip()
        # Some llama.cpp builds echo the prompt. Keep the assistant suffix when present.
        if 'Assistant:' in output:
            output = output.split('Assistant:', 1)[-1].strip()
        tokens = len(re.findall(r'\S+', output))
        result = {
            'ok': proc.returncode == 0,
            'backend': 'gguf',
            'model': 'gguf:' + model_path,
            'model_name': os.path.basename(model_path),
            'response': output,
            'ttft_ms': None,
            'total_ms': round(total_ms, 1),
            'tokens': tokens,
            'tokens_per_sec': round(tokens / (total_ms / 1000), 2) if total_ms and tokens else None,
            'created_at': datetime.utcnow().isoformat() + 'Z',
            'settings': {'system': system, 'prompt': prompt, 'options': options, 'timeout': timeout, 'lora_path': lora_path},
            'debug': {'command': cmd, 'returncode': proc.returncode, 'stderr': (proc.stderr or '')[-4000:]},
        }
        if proc.returncode != 0:
            result['error'] = (proc.stderr or proc.stdout or ('llama-cli exited ' + str(proc.returncode)))[-1000:]
        return result
    except subprocess.TimeoutExpired as e:
        return {'ok': False, 'backend': 'gguf', 'model': 'gguf:' + model_path, 'error': 'llama-cli timed out after ' + str(timeout) + 's', 'debug': {'command': cmd, 'stdout': (e.stdout or '')[-2000:] if isinstance(e.stdout, str) else '', 'stderr': (e.stderr or '')[-2000:] if isinstance(e.stderr, str) else ''}}
    except Exception as e:
        return {'ok': False, 'backend': 'gguf', 'model': 'gguf:' + model_path, 'error': str(e), 'debug': {'command': cmd}}


def _ollama_health():
    """Return diagnostics for the local Ollama runtime used by the playground."""
    import shutil
    import urllib.request, urllib.error
    info = {
        'localhost_only': True,
        'base_url': 'http://127.0.0.1:11434',
        'api': {'available': False},
        'cli': shutil.which('ollama') or '',
        'model_count': 0,
        'loaded_models': [],
    }
    try:
        req = urllib.request.Request('http://127.0.0.1:11434/api/version')
        with urllib.request.urlopen(req, timeout=3) as resp:
            version = json.loads(resp.read())
        info['api'] = {'available': True, 'version': version.get('version', '')}
    except Exception as e:
        info['api'] = {'available': False, 'error': str(e)}
    models = _get_ollama_models()
    info['model_count'] = len(models)
    try:
        req = urllib.request.Request('http://127.0.0.1:11434/api/ps')
        with urllib.request.urlopen(req, timeout=3) as resp:
            ps = json.loads(resp.read())
        info['loaded_models'] = ps.get('models', [])
    except Exception as e:
        info['loaded_models_error'] = str(e)
    info['gguf'] = _gguf_health()
    return info


def _generate_any(payload: dict) -> dict:
    model = (payload.get('model') or '').strip()
    if model.startswith('ollama:'):
        one = dict(payload)
        one['model'] = model[len('ollama:'):]
        result = _ollama_generate(one)
        result['backend'] = 'ollama'
        result['model'] = 'ollama:' + one['model']
        return result
    if model.startswith('gguf:') or model.endswith('.gguf') or os.path.isabs(model):
        return _gguf_generate(payload)
    result = _ollama_generate(payload)
    result['backend'] = 'ollama'
    result['model'] = 'ollama:' + model if model else model
    return result


def _ollama_generate(payload):
    """Run one Ollama generation with timings, final API metrics, and debug data."""
    import urllib.request, urllib.error
    model = (payload.get('model') or '').strip()
    prompt = payload.get('prompt') or ''
    system = payload.get('system') or ''
    if not model:
        return {'ok': False, 'error': 'Missing model name'}
    if not prompt.strip():
        return {'ok': False, 'error': 'Prompt is empty'}

    options_in = payload.get('options') or {}
    allowed_options = {
        'temperature', 'top_p', 'top_k', 'min_p', 'num_predict', 'num_ctx',
        'repeat_penalty', 'seed', 'stop', 'mirostat', 'mirostat_eta', 'mirostat_tau'
    }
    options = {}
    for key, value in options_in.items():
        if key not in allowed_options or value in ('', None):
            continue
        options[key] = value

    request_body = {
        'model': model,
        'prompt': prompt,
        'stream': True,
        'options': options,
    }
    if system.strip():
        request_body['system'] = system
    if payload.get('template'):
        request_body['template'] = payload.get('template')
    if payload.get('format'):
        request_body['format'] = payload.get('format')

    timeout = int(payload.get('timeout') or 120)
    timeout = max(5, min(timeout, 600))
    req = urllib.request.Request('http://127.0.0.1:11434/api/generate', data=json.dumps(request_body).encode())
    req.add_header('Content-Type', 'application/json')
    req.add_header('Accept', 'application/x-ndjson')

    t_start = time.time()
    ttft_ms = None
    chunks = 0
    response_text = ''
    final_obj = {}
    raw_events = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            leftover = b''
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                leftover += chunk
                while b'\n' in leftover:
                    line, leftover = leftover.split(b'\n', 1)
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line.decode())
                    except Exception:
                        raw_events.append({'parse_error': line.decode(errors='replace')[:500]})
                        continue
                    if len(raw_events) < 80:
                        raw_events.append(obj)
                    token_piece = obj.get('response', '')
                    if token_piece and ttft_ms is None:
                        ttft_ms = (time.time() - t_start) * 1000
                    if token_piece:
                        chunks += 1
                        response_text += token_piece
                    if obj.get('done', False):
                        final_obj = obj
                        break
        total_ms = (time.time() - t_start) * 1000
        eval_count = final_obj.get('eval_count') or chunks
        eval_duration_ns = final_obj.get('eval_duration') or 0
        tokens_per_sec = None
        if eval_count and eval_duration_ns:
            tokens_per_sec = eval_count / (eval_duration_ns / 1e9)
        elif eval_count and total_ms:
            tokens_per_sec = eval_count / (total_ms / 1000)
        return {
            'ok': True,
            'model': model,
            'response': response_text.strip(),
            'ttft_ms': round(ttft_ms, 1) if ttft_ms is not None else None,
            'total_ms': round(total_ms, 1),
            'chunks': chunks,
            'tokens': eval_count,
            'tokens_per_sec': round(tokens_per_sec, 2) if tokens_per_sec else None,
            'prompt_eval_count': final_obj.get('prompt_eval_count'),
            'eval_count': final_obj.get('eval_count'),
            'load_duration_ms': round((final_obj.get('load_duration') or 0) / 1e6, 1) if final_obj else None,
            'prompt_eval_duration_ms': round((final_obj.get('prompt_eval_duration') or 0) / 1e6, 1) if final_obj else None,
            'eval_duration_ms': round((final_obj.get('eval_duration') or 0) / 1e6, 1) if final_obj else None,
            'total_duration_ms': round((final_obj.get('total_duration') or 0) / 1e6, 1) if final_obj else None,
            'created_at': datetime.utcnow().isoformat() + 'Z',
            'settings': {'system': system, 'prompt': prompt, 'options': options, 'timeout': timeout},
            'debug': {'request': request_body, 'final': final_obj, 'raw_events': raw_events},
        }
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors='replace') if e.fp else str(e)
        return {'ok': False, 'model': model, 'error': f'Ollama HTTP {e.code}: {err_body[:1000]}', 'debug': {'request': request_body}}
    except Exception as e:
        return {'ok': False, 'model': model, 'error': str(e), 'debug': {'request': request_body}}


def _ollama_benchmark(model, prompt):
    """Backward-compatible benchmark endpoint backed by the richer generator."""
    result = _ollama_generate({'model': model, 'prompt': prompt, 'options': {'num_predict': 64}, 'timeout': 90})
    if not result.get('ok'):
        return {'error': result.get('error', 'Benchmark failed'), 'ttft_ms': 0, 'total_ms': 0, 'tokens': 0, 'response': ''}
    return {
        'ttft_ms': result.get('ttft_ms') or 0,
        'total_ms': result.get('total_ms') or 0,
        'tokens': result.get('tokens') or 0,
        'tokens_per_sec': result.get('tokens_per_sec'),
        'response': result.get('response', ''),
    }


def _ollama_compare(payload):
    """Run the same prompt/settings against selected models sequentially."""
    models = payload.get('models') or []
    if not isinstance(models, list) or not models:
        return {'ok': False, 'error': 'Select at least one model', 'results': []}
    max_models = min(len(models), 6)
    shared = {
        'prompt': payload.get('prompt') or '',
        'system': payload.get('system') or '',
        'options': payload.get('options') or {},
        'timeout': payload.get('timeout') or 120,
        'format': payload.get('format') or '',
        'lora_path': payload.get('lora_path') or '',
    }
    run_id = 'run-' + datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    results = []
    for model in models[:max_models]:
        one = dict(shared)
        one['model'] = str(model)
        results.append(_generate_any(one))
    return {'ok': True, 'run_id': run_id, 'created_at': datetime.utcnow().isoformat() + 'Z', 'results': results}


def models_section() -> str:
    """Server-rendered model list with pull input and vanilla JS SSE streaming."""
    models = _get_ollama_models()
    total_gb = sum(m.get('size', 0) for m in models) / 1e9

    body = '<a href=/models style=text-decoration:none;color:inherit><div class=section-title style=margin-top:2rem>🤖 Installed Models</div></a>'
    body += '<div class=section-timestamp>Total: ' + f'{total_gb:.1f} GB across {len(models)} models' + '</div>'

    body += '<div class=models-card>'
    body += '<h3>📦 Installed Models</h3>'

    if models:
        body += '<div class=models-list id=models-list>'
        for m in models:
            name = m.get('name', 'unknown')
            size_gb = m.get('size', 0) / 1e9
            escName = name.replace("'", "\\'")
            escDQ = name.replace('"', '&quot;')
            body += '<div class=model-item><span class=model-name>' + name + '</span>'
            body += '<span class=model-size>' + f'{size_gb:.1f} GB' + '</span>'
            body += '<button class=model-bench-btn data-model-name="' + escDQ + '" title="Benchmark ' + name + '">⚡ Benchmark</button>'
            body += '<button class=model-delete-btn data-name="' + escDQ + '" title="Delete ' + name + '">🗑</button>'
            body += '</div>'
        body += '</div>'
    else:
        body += '<div class=models-list id=models-list>'
        body += '<div class=model-item><span class=model-name style=color:var(--text-muted)>No models installed</span></div>'
        body += '</div>'

    # Pull form with SSE streaming
    body += '<div class=pull-form id=pull-form>'
    body += '<input type=text id=pull-model-name placeholder="e.g. phi4-mini" autocomplete=off>'
    body += '<button id=pull-btn onclick=startPull()>Pull</button>'
    body += '<button id=cancel-btn onclick=cancelPull() style="background:var(--red);display:none">Cancel</button>'
    body += '</div>'
    body += '<div class=pull-progress id=pull-progress style=display:none>'
    body += '<div class=pull-progress-bar><div class=pull-progress-fill id=pull-progress-fill style=width:0%></div></div>'
    body += '<div class=pull-progress-text id=pull-progress-text></div>'
    body += '</div>'
    body += '<div class=models-hint id=models-hint>Enter a model name and click Pull to download</div>'
    body += '</div>'

    # JavaScript for model management (pull, refresh, delete)
    body += '<script>'
    body += 'onModelDelete = function(name) {'
    body += '  fetch("/api/ollama/models/delete", {'
    body += '    method: "POST",'
    body += '    headers: {"Content-Type": "application/json"},'
    body += '    body: JSON.stringify({name: name})'
    body += '  })'
    body += '    .then(function(r) { return r.json(); })'
    body += '    .then(function(data) {'
    body += '      if (data.ok) {'
    body += '        refreshModels();'
    body += '        fetch("/api/ollama/models")'
    body += '          .then(function(r2) { return r2.json(); })'
    body += '          .then(function(d2) {'
    body += '            var el = document.querySelector(".section-timestamp");'
    body += '            if (el && d2.models) {'
    body += '              var gb = 0;'
    body += '              for (var k = 0; k < d2.models.length; k++) gb += d2.models[k].size / 1e9;'
    body += '              el.textContent = "Total: " + gb.toFixed(1) + " GB across " + d2.models.length + " models";'
    body += '            }'
    body += '          });'
    body += '        showConfirmDialog({'
    body += '          title: "Model Deleted",'
    body += '          description: "Model \\"" + name + "\\" has been deleted.",'
    body += '          icon: "✅",'
    body += '          labelConfirm: "OK",'
    body += '          labelCancel: null'
    body += '        });'
    body += '      } else {'
    body += '        showConfirmDialog({'
    body += '          title: "Delete Failed",'
    body += '          description: data.error || "Failed to delete model",'
    body += '          icon: "❌",'
    body += '          labelConfirm: "OK",'
    body += '          labelCancel: null'
    body += '        });'
    body += '      }'
    body += '    })'
    body += '    .catch(function(e) {'
    body += '      showConfirmDialog({'
    body += '        title: "Delete Error",'
    body += '        description: "Network error: " + (e.message || "unknown"),'
    body += '        icon: "❌",'
    body += '        labelConfirm: "OK",'
    body += '        labelCancel: null'
    body += '      });'
    body += '    });'
    body += '};'
    body += '\n'
    body += '// ── Benchmark handler ──'
    body += '\n'
    body += 'var benchRunning = {};'
    body += '\n'
    body += 'var benchResults = {};'
    body += '\n'
    body += 'function formatMs(ms) {'
    body += '\n'
    body += '  if (ms == null) return \"—\";'
    body += '\n'
    body += '  if (ms < 1000) return Math.round(ms) + \" ms\";'
    body += '\n'
    body += '  return (ms / 1000).toFixed(1) + \" s\";'
    body += '\n'
    body += '}'
    body += '\n'
    body += 'function runBenchmark(modelName, btn) {'
    body += '\n'
    body += '  if (benchRunning[modelName]) return;'
    body += '\n'
    body += '  benchRunning[modelName] = true;'
    body += '\n'
    body += '  delete benchResults[modelName];'
    body += '\n'
    body += '  btn.disabled = true;'
    body += '\n'
    body += '  btn.innerHTML = \'<span class=\"model-bench-spinner\"></span> …\';'
    body += '\n'
    body += '  var startTime = performance.now();'
    body += '\n'
    body += '  fetch(\"/api/ollama/benchmark\", {'
    body += '\n'
    body += '    method: \"POST\",'
    body += '\n'
    body += '    headers: {\"Content-Type\": \"application/json\"},'
    body += '\n'
    body += '    body: JSON.stringify({model: modelName, prompt: \"Hello\"}),'
    body += '\n'
    body += '    signal: AbortSignal.timeout(30000)'
    body += '\n'
    body += '  })'
    body += '\n'
    body += '    .then(function(r) {'
    body += '\n'
    body += '      var endTime = performance.now();'
    body += '\n'
    body += '      if (!r.ok) return r.text().then(function(t) { throw new Error(t || \"HTTP \" + r.status); });'
    body += '\n'
    body += '      return r.json().then(function(data) { return {data: data, endTime: endTime}; });'
    body += '\n'
    body += '    })'
    body += '\n'
    body += '    .then(function(result) {'
    body += '\n'
    body += '      var data = result.data;'
    body += '\n'
    body += '      var networkMs = result.endTime - startTime - (data.total_ms || 0);'
    body += '\n'
    body += '      benchResults[modelName] = {'
    body += '\n'
    body += '        ttft_ms: data.ttft_ms,'
    body += '\n'
    body += '        total_ms: data.total_ms,'
    body += '\n'
    body += '        tokens: data.tokens,'
    body += '\n'
    body += '        networkMs: Math.max(0, Math.round(networkMs))'
    body += '\n'
    body += '      };'
    body += '\n'
    body += '      renderBenchResult(modelName, btn);'
    body += '\n'
    body += '    })'
    body += '\n'
    body += '    .catch(function(e) {'
    body += '\n'
    body += '      benchResults[modelName] = {error: e.message || \"Benchmark failed\"};'
    body += '\n'
    body += '      renderBenchResult(modelName, btn);'
    body += '\n'
    body += '    });'
    body += '\n'
    body += '}'
    body += '\n'
    body += 'function renderBenchResult(modelName, btn) {'
    body += '\n'
    body += '  benchRunning[modelName] = false;'
    body += '\n'
    body += '  var r = benchResults[modelName];'
    body += '\n'
    body += '  var span = document.createElement(\"span\");'
    body += '\n'
    body += '  if (r.error) {'
    body += '\n'
    body += '    span.className = \"model-bench-result-error\";'
    body += '\n'
    body += '    span.textContent = \"⚠️ Failed\";'
    body += '\n'
    body += '    span.title = r.error;'
    body += '\n'
    body += '  } else {'
    body += '\n'
    body += '    span.className = \"model-bench-result\";'
    body += '\n'
    body += '    span.textContent = formatMs(r.ttft_ms || r.total_ms);'
    body += '\n'
    body += '    span.addEventListener(\"mouseenter\", function() {'
    body += '\n'
    body += '      var tip = document.createElement(\"span\");'
    body += '\n'
    body += '      tip.className = \"model-bench-tooltip\";'
    body += '\n'
    body += '      tip.innerHTML = \"TTFT: \" + formatMs(r.ttft_ms) + \"<br>Total: \" + formatMs(r.total_ms) + \"<br>Tokens: \" + (r.tokens || \"—\") + \"<br>Network: \" + formatMs(r.networkMs);'
    body += '\n'
    body += '      span.appendChild(tip);'
    body += '\n'
    body += '    });'
    body += '\n'
    body += '    span.addEventListener(\"mouseleave\", function() {'
    body += '\n'
    body += '      var tip = span.querySelector(\".model-bench-tooltip\");'
    body += '\n'
    body += '      if (tip) tip.remove();'
    body += '\n'
    body += '    });'
    body += '\n'
    body += '  }'
    body += '\n'
    body += '  btn.replaceWith(span);'
    body += '\n'
    body += '}'
    body += '\n'
    body += '// ── Delegated click handler for benchmark buttons ──'
    body += '\n'
    body += 'document.addEventListener(\"click\", function(e) {'
    body += '\n'
    body += '  var btn = e.target.closest(\".model-bench-btn\");'
    body += '\n'
    body += '  if (!btn || btn.disabled) return;'
    body += '\n'
    body += '  e.stopPropagation();'
    body += '\n'
    body += '  var modelName = btn.getAttribute(\"data-model-name\");'
    body += '\n'
    body += '  if (modelName) runBenchmark(modelName, btn);'
    body += '\n'
    body += '});'
    body += '\n'
    body += '</script>'
    return body



def hermes_page() -> str:
    """Consolidated Hermes dashboard."""
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>Hermes</h1>'
    body += '<p>AI agent dashboard — cron, briefings.</p></div>'
    body += '<div style="text-align:center;margin:0 0 2rem 0">'
    body += '<a href="https://hermes.devmclovin.com" target="_blank" rel="noopener" class="cta-button">🚀 Open Hermes Web UI →</a></div>'

    # ── Command Shortcuts Panel ──
    body += '<details class="toolbox-panel"><summary>⚡ Admin Actions</summary><div class="toolbox-panel-body">'
    body += '<div class="shortcuts-grid">'
    sc_cards = [
        ("Restart Dashboard", "safe", "restart-dashboard", "Restart Hermes Dashboard"),
        ("Restart LLM Router", "destructive", "restart-router", "Restart Cloudflare tunnel"),
        ("Pull Latest Repo", "safe", "pull-github", "Git pull hermes-agent repo"),
        ("Run Backup", "destructive", "run-backup", "Trigger backup scripts"),
        ("Server Logs", "link", "/logs", "View systemd journal"),
        ("Router Logs", "link", "/logs/router", "View LLM router logs"),
        ("Docker Status", "link", "/status", "Containers + disk"),
    ]
    for label, kind, endpoint, hint in sc_cards:
        if kind == "safe":
            body += '<div class="shortcut-card" onclick="safeAction(this,\'' + label + '\',\'' + endpoint + '\')"><span class="sc-label">' + label + '</span><span class="sc-hint">' + hint + '</span></div>'
        elif kind == "destructive":
            body += '<div class="shortcut-card destructive" onclick="destructiveAction(this,\'' + label + '\',\'' + endpoint + '\')"><span class="sc-label">' + label + '</span><span class="sc-hint">' + hint + '</span></div>'
        elif kind == "external":
            body += '<a href="' + endpoint + '" target="_blank" rel="noopener" class="shortcut-card"><span class="sc-label">' + label + '</span><span class="sc-hint">' + hint + '</span></a>'
        else:
            body += '<a href="' + endpoint + '" class="shortcut-card"><span class="sc-label">' + label + '</span><span class="sc-hint">' + hint + '</span></a>'
    body += '</div></div></details>'

    # ── Cron Jobs (grid) ──
    body += '<a href=/cron style=text-decoration:none;color:inherit><div class="section-title">⏰ Cron Jobs</div></a>'
    jobs = load_cron_jobs()
    if jobs:
        body += '<div class="section-timestamp">Last updated: ' + datetime.now().strftime('%Y-%m-%d %H:%M UTC') + '</div>'
        body += '<div class="dashboard-grid">'
        for job in jobs[:4]:
            name = job.get("name", job.get("id", ""))
            job_id = job.get("id", "")
            schedule = _format_schedule(job)
            last_run = _format_iso_time(job.get("last_run_at"))
            next_run = _format_iso_time(job.get("next_run_at"))
            status = job.get("last_status", "unknown")
            enabled = job.get("enabled", True)
            sd = "⏸️" if not enabled else ("🟢" if status == "ok" else ("🔴" if status == "error" else "⚪"))
            body += '<a href=/cron/' + job_id + ' class="briefing-card" style="text-decoration:none;color:inherit">'
            body += '<div class="card-num" style="font-size:1.5rem;margin-bottom:0.5rem">' + sd + '</div>'
            body += '<h3 style="margin-bottom:0.4rem">' + name + '</h3>'
            body += '<div class="card-summary" style="font-size:0.8rem;color:var(--text-muted)">Schedule: <code style="font-size:0.75rem">' + schedule + '</code></div>'
            body += '<div class="card-source" style="margin-top:0.3rem">Last: ' + last_run + '</div>'
            if next_run and next_run != chr(0x2014):
                body += '<div class="card-source">Next: ' + next_run + '</div>'
            body += '<span class="card-action">View details →</span>'
            body += '</a>'
        body += '</div>'
        if len(jobs) > 4:
            body += '<p class="section-timestamp"><a href=/cron style="color:var(--accent-hover)">View all ' + str(len(jobs)) + ' cron jobs →</a></p>'
    else:
        body += '<div class="empty-state"><p>No cron jobs configured.</p></div>'

    # ── Installed Models moved to a dedicated page to keep Hermes scannable ──
    body += '<a href=/models style=text-decoration:none;color:inherit><div class="section-title">📦 Models</div></a>'
    body += '<div class="dashboard-grid">'
    body += '<a href=/models class="briefing-card" style="text-decoration:none;color:inherit">'
    body += '<div class="card-num">🤖</div><h3>Model Console</h3>'
    body += '<div class="card-summary">Compare pricing, view installed Ollama models, pull new models, and run benchmarks.</div>'
    body += '<span class="card-action">Open models →</span></a></div>'

    # ── Recent Briefings (grid) ──
    body += '<a href=/briefings style=text-decoration:none;color:inherit><div class="section-title" style="margin-top:2rem">📰 Recent Briefings</div></a>'
    files = sorted(glob.glob(str(BRIEFING_DIR / "*.md")), reverse=True)[:4]
    if files:
        body += '<div class="section-timestamp">Last updated: ' + datetime.now().strftime('%Y-%m-%d %H:%M UTC') + '</div>'
        body += '<div class="dashboard-grid">'
        for f in files:
            fname = Path(f).stem
            date_part = fname[:10]
            body += '<a href=/briefing/' + date_part + ' class="briefing-card" style="text-decoration:none;color:inherit">'
            body += '<div class="card-num">📅</div><h3>' + date_part + '</h3>'
            try:
                raw = Path(f).read_text()
                stories, _ = parse_briefing_stories(raw)
                if stories:
                    body += '<div class="card-summary">' + stories[0]["title"][:80] + '...</div>'
                    body += '<div class="card-source">' + str(len(stories)) + ' stories</div>'
            except:
                body += '<div class="card-summary">-</div>'
            body += '<span class="card-action">Read full briefing →</span>'
            body += '</a>'
        body += '</div>'
    else:
        body += '<div class="empty-state"><p>No briefings found.</p></div>'

    return html_page("Hermes", body, active_nav="hermes")

# ═══════════════════════════════════════════════════════════════
#  HTML helpers
# ═══════════════════════════════════════════════════════════════


def html_page(title: str, body: str, active_nav: str = "home", extra_head: str = "") -> str:
    site_nav = render_nav(active_nav)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {extra_head}
    <title>{title} — devmclovin</title>
    <style>{NAV_CSS}{BASE_CSS}
</style>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚀</text></svg>">
    <script>

    // ── Nav dropdown behavior: close menus when clicking off them ──
    {NAV_JS}
    // ── Confirmation dialog for destructive actions ──
    function showConfirmDialog(opts) {{
        var overlay = document.createElement('div');
        overlay.className = 'confirm-overlay';
        overlay.id = '__confirm_dlg';

        var title = (opts && opts.title) || 'Confirm Action';
        var desc  = (opts && opts.description) || 'Are you sure you want to proceed?';
        var icon  = (opts && opts.icon) || '⚠️';
        var onConfirm = (opts && opts.onConfirm) || null;

        var requireInput = !!(opts && opts.requireConfirmText);
        var confirmText = (opts && opts.confirmText) || 'CONFIRM';
        var labelConfirm = (opts && opts.labelConfirm) || 'Confirm';
        var labelCancel  = (opts && opts.labelCancel) || 'Cancel';

        var inputRow = '';
        if (requireInput) {{
            inputRow = '<div class=confirm-input-row>' +
                '<input type=text id=__confirm_input placeholder="Type \\\"' +
                confirmText.replace(/"/g, '&quot;') + '\\\" to confirm" autocomplete=off>' +
                '</div>';
        }}

        overlay.innerHTML =
            '<div class=confirm-modal>' +
            '<div class=confirm-icon>' + icon + '</div>' +
            '<div class=confirm-title>' + title + '</div>' +
            '<div class=confirm-desc>' + desc + '</div>' +
            inputRow +
            '<div class=confirm-buttons>' +
            '<button class="confirm-btn cancel" id=__confirm_cancel>' + labelCancel + '</button>' +
            '<button class="confirm-btn danger" id=__confirm_ok' +
            (requireInput ? ' disabled' : '') + '>' + labelConfirm + '</button>' +
            '</div>' +
            '</div>';

        document.body.appendChild(overlay);

        var inputEl = document.getElementById('__confirm_input');
        var okBtn   = document.getElementById('__confirm_ok');

        function close() {{
            var el = document.getElementById('__confirm_dlg');
            if (el) el.remove();
        }}

        document.getElementById('__confirm_cancel').onclick = close;
        overlay.addEventListener('click', function(e) {{
            if (e.target === overlay) close();
        }});

        function doConfirm() {{
            close();
            if (onConfirm) onConfirm();
        }}

        if (requireInput && inputEl) {{
            inputEl.oninput = function() {{
                okBtn.disabled = (inputEl.value !== confirmText);
            }};
            inputEl.addEventListener('keydown', function(e) {{
                if (e.key === 'Enter' && inputEl.value === confirmText) doConfirm();
            }});
            inputEl.focus();
        }}

        okBtn.onclick = doConfirm;
    }}


    // ── Safe action: POST to proxy, show loading/result on card ──
    function safeAction(cardEl, label, endpoint) {{
        var originalHTML = cardEl.innerHTML;
        cardEl.innerHTML = '<span class=sc-label>' + label + '</span><span class=sc-status>⏳ Running...</span>';
        cardEl.classList.add('loading');
        fetch('/api/proxy/commands/' + endpoint, {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: '{{}}'}})
            .then(function(r) {{ return r.json().then(function(data) {{ return {{ok: r.ok, data: data}}; }}); }})
            .then(function(result) {{
                cardEl.classList.remove('loading');
                if (result.ok && result.data.success !== false) {{
                    cardEl.innerHTML = '<span class=sc-label>' + label + '</span><span class=\"sc-status success\">✅ Done</span>';
                }} else {{
                    var err = (result.data && (result.data.error || result.data.detail)) || 'Request failed';
                    cardEl.innerHTML = '<span class=sc-label>' + label + '</span><span class=\"sc-status error\">❌ ' + err.substring(0,40) + '</span>';
                }}
                setTimeout(function() {{ cardEl.innerHTML = originalHTML; cardEl.classList.remove('loading'); }}, 3500);
            }})
            .catch(function(e) {{
                cardEl.classList.remove('loading');
                cardEl.innerHTML = '<span class=sc-label>' + label + '</span><span class=\"sc-status error\">❌ Connection failed</span>';
                setTimeout(function() {{ cardEl.innerHTML = originalHTML; cardEl.classList.remove('loading'); }}, 3500);
            }});
    }}

    // ── Destructive action: confirm then POST ──
    function destructiveAction(cardEl, label, endpoint) {{
        showConfirmDialog({{
            title: label,
            description: 'This action may cause a brief service disruption. Proceed?',
            icon: '⚠️',
            requireConfirmText: true,
            confirmText: 'CONFIRM',
            onConfirm: function() {{ safeAction(cardEl, label, endpoint); }}
        }});
    }}

    // ── Category filter for Quick Links ──
    function filterByCategory(cat, btn) {{
        // Update active pill
        document.querySelectorAll('.category-pill').forEach(function(p) {{
            p.classList.remove('active');
        }});
        btn.classList.add('active');

        // Show/hide link cards
        document.querySelectorAll('.link-card[data-category]').forEach(function(card) {{
            if (cat === 'all' || card.getAttribute('data-category') === cat) {{
                card.classList.remove('filtered-out');
            }} else {{
                card.classList.add('filtered-out');
            }}
        }});
    }}

    // ── Bookmark toggle ──
    function toggleBookmark(btn, sid, date, title, url, srcName, body, btype) {{
        var formData = new URLSearchParams();
        formData.append('id', sid);
        formData.append('type', btype);
        formData.append('title', title);
        formData.append('source_name', srcName);
        formData.append('source_url', url);
        formData.append('body', body);
        formData.append('date', date);
        fetch('/bookmarks/toggle', {{method: 'POST', headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}, body: formData.toString()}})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                if (data.ok) {{
                    if (data.active) {{
                        btn.classList.add('active');
                        if (btype === 'saved') {{
                            btn.textContent = '⭐ Saved';
                        }} else {{
                            btn.textContent = '📌 Saved for Later';
                        }}
                    }} else {{
                        btn.classList.remove('active');
                        if (btype === 'saved') {{
                            btn.textContent = '⭐ Save';
                        }} else {{
                            btn.textContent = '📌 Read Later';
                        }}
                    }}
                }}
            }})
            .catch(function(e) {{ console.error('Bookmark toggle failed:', e); }});
    }}
    </script>
</head>
<body>
    <a href="#main-content" class="skip-link">Skip to main content</a>
    {site_nav}
    <main id="main-content">
    <div class="container">
        {body}
    </div>
    </main>
    <footer>
        <p>devmclovin.com</p>
        <nav class="footer-nav" aria-label="Footer links">
            <a href="/">Home</a>
            <a href="/hermes">Hermes</a>
            <a href="/status">Status</a>
            <a href="/projects">Projects</a>
            <a href="/briefings">Briefings</a>
        </nav>
    </footer>
</body>
</html>"""


def first_sentence(text: str) -> str:
    """Extract a good one-sentence summary. Splits on sentence-ending punctuation
    followed by a capital letter, which naturally skips abbreviations like 'PHerc.' or 'U.S.'."""
    text = text.strip()
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    first = sentences[0].strip()
    if len(first) < 80 and len(sentences) > 1:
        first = first + ' ' + sentences[1].strip()
    return first


def _extract_impact(body_lines: list[str]) -> str:
    """Extract a one-line 'why this matters' impact statement from the body.
    Checks for explicit 'Impact:' prefix first; falls back to the last sentence."""
    for line in body_lines:
        m = re.match(r'^Impact:\s*(.+)', line.strip())
        if m:
            return m.group(1).strip()
    # Fallback: extract the last sentence from the joined text
    full = " ".join(body_lines).strip()
    if not full:
        return ""
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', full)
    last = sentences[-1].strip()
    if not last:
        last = sentences[-2].strip() if len(sentences) > 1 else ""
    return last


# ── Impact generation for briefing stories ──

def _openrouter_chat(messages: list[dict], model: str = "google/gemini-2.5-flash-lite") -> str | None:
    """Call OpenRouter chat completions API. Returns response text or None."""
    key = _load_openrouter_key()
    if not key:
        return None
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 256,
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "devmclovin-landing",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _load_impacts_cache(date_key: str) -> dict[str, str]:
    """Load cached impacts for a briefing date. Returns {title: impact} dict."""
    cache_file = IMPACT_CACHE_DIR / f"{date_key}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_impacts_cache(date_key: str, impacts: dict[str, str]) -> None:
    """Save impacts cache for a briefing date."""
    IMPACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = IMPACT_CACHE_DIR / f"{date_key}.json"
    cache_file.write_text(json.dumps(impacts, ensure_ascii=False, indent=2))


def _generate_impacts_via_llm(stories: list[dict]) -> dict[str, str]:
    """Use OpenRouter LLM to generate one-line impact statements for stories.
    Returns {title: impact} dict. Falls back to empty impacts on failure."""
    if not stories:
        return {}

    # Build a prompt with all stories in one API call
    story_text = ""
    for i, s in enumerate(stories, 1):
        summary = first_sentence(s["body"]).replace("<br>", " ")
        story_text += f"{i}. TITLE: {s['title']}\n   SUMMARY: {summary}\n\n"

    prompt = (
        "For each news story below, write a ONE-LINE 'why this matters' impact statement "
        "(max 20 words). Make each statement specific, concrete, and insightful — "
        "explain the real-world consequence or significance. "
        "Respond with ONLY a JSON object mapping story numbers to impact strings. "
        "Format: {\"1\": \"impact text\", \"2\": \"impact text\", ...}\n\n"
        f"{story_text}"
    )

    messages = [
        {"role": "system", "content": "You are a news analyst. Respond with ONLY valid JSON, no other text."},
        {"role": "user", "content": prompt},
    ]

    response = _openrouter_chat(messages)
    if not response:
        return {}

    # Parse the JSON response
    try:
        # Strip markdown code fences if present
        response = response.strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[1] if "\n" in response else response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()
        result = json.loads(response)
    except json.JSONDecodeError:
        return {}

    # Map numeric keys to story titles
    impacts: dict[str, str] = {}
    for i, s in enumerate(stories, 1):
        key = str(i)
        if key in result and isinstance(result[key], str):
            impacts[s["title"]] = result[key].strip()

    return impacts


def _get_story_impacts(stories: list[dict], date_str: str) -> None:
    """Ensure every story has an impact. Loads from cache, generates via LLM if missing."""
    if not stories:
        return

    # Extract date key (e.g. "2026-06-28" from "Sunday, June 28, 2026")
    date_key = date_str
    try:
        from datetime import datetime as _dt
        for fmt in ("%A, %B %d, %Y", "%Y-%m-%d"):
            try:
                dt = _dt.strptime(date_str, fmt)
                date_key = dt.strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
    except Exception:
        pass

    # Load cache
    cached = _load_impacts_cache(date_key)

    # Fill from cache where available
    missing = []
    for s in stories:
        if s["title"] in cached:
            s["impact"] = cached[s["title"]]
        else:
            missing.append(s)

    # Generate missing impacts via LLM
    if missing and any(not s["impact"] for s in missing):
        try:
            new_impacts = _generate_impacts_via_llm(missing)
            for title, impact in new_impacts.items():
                cached[title] = impact
            _save_impacts_cache(date_key, cached)
            # Apply to stories
            for s in missing:
                if s["title"] in cached:
                    s["impact"] = cached[s["title"]]
        except Exception:
            pass  # graceful degradation — cards show without impact


# ── Category rendering helpers ──

def category_badge_html(categories_str: str) -> str:
    """Render small colored category badges from a comma-separated string."""
    if not categories_str or categories_str == "general":
        return ""
    cats = [c.strip() for c in categories_str.split(",") if c.strip()]
    if not cats:
        return ""
    html = '<div class="card-categories">'
    for c in cats:
        bg, fg = CATEGORY_COLORS.get(c, ("#6b7280", "#f3f4f6"))
        html += f'<span class="category-badge" style="background:{bg};color:{fg}">{c}</span>'
    html += '</div>'
    return html


def _category_filter_html(active_category: str = "All", counts: dict | None = None) -> str:
    """Render horizontal category filter tabs. 'active_category' of 'All' means no filter."""
    html = '<div class="category-tabs">'
    all_cls = 'active' if active_category == "All" else ''
    all_count = sum(counts.values()) if counts else ""
    html += f'<a href="/briefings" class="category-tab {all_cls}">All'
    if all_count:
        html += f'<span class="tab-count">{all_count}</span>'
    html += '</a>'
    for cat in CATEGORY_ORDER:
        cls = 'active' if active_category == cat else ''
        cnt = counts.get(cat, 0) if counts else ""
        html += f'<a href="/briefings?category={cat}" class="category-tab {cls}">{cat}'
        if cnt:
            html += f'<span class="tab-count">{cnt}</span>'
        html += '</a>'
    html += '</div>'
    return html


def briefing_card_from_db(articles: list[dict], date_str: str, show_date: bool = True) -> str:
    """Render a horizontally-scrollable briefing card grid from DB-format articles."""
    if not articles:
        return '<div class="empty-state"><p>No articles found.</p></div>'

    html = '<div class="briefing-header">'
    if show_date:
        html += f'<div class="date">{date_str}</div>'
    html += '<h2>📰 Morning Briefing</h2>'
    html += '</div>'
    html += '<div class="briefing-scroll">'
    html += '<button class="scroll-arrow left" onclick="this.nextElementSibling.scrollBy({left:-340,behavior:\'smooth\'})">◂</button>'
    html += '<div class="briefing-grid">'
    for a in articles:
        title = a.get("title", "Untitled")
        summary = a.get("summary", "")
        source = a.get("source_name", "")
        url = a.get("source_url", "")
        categories = a.get("categories", "")
        position = a.get("position", 0)

        html += '<div class="briefing-card">'
        html += f'<span class="card-num">{position}</span>'
        html += f'<h3>{title}</h3>'
        if summary:
            first = first_sentence(summary)
            html += f'<div class="card-summary">{first}</div>'
        html += category_badge_html(categories)
        html += '<div class="card-source">'
        if url:
            html += f'<a href="{url}" target="_blank" rel="noopener">{source}</a>'
        else:
            html += source
        html += '</div>'
        # Bookmark buttons
        sid = _story_id(date_str, title, url)
        saved_active = ' active' if _is_bookmarked(sid, 'saved') else ''
        rl_active = ' active' if _is_bookmarked(sid, 'read_later') else ''
        saved_label = '⭐ Saved' if _is_bookmarked(sid, 'saved') else '⭐ Save'
        rl_label = '📌 Saved for Later' if _is_bookmarked(sid, 'read_later') else '📌 Read Later'
        import html as _html
        args_saved = ','.join(["'"+_html.escape(str(x), quote=False)+"'" for x in [sid, date_str, title, url, source, (summary or '')[:500], 'saved']])
        args_rl = ','.join(["'"+_html.escape(str(x), quote=False)+"'" for x in [sid, date_str, title, url, source, (summary or '')[:500], 'read_later']])
        html += f'<div class="bm-btn-row"><button class="bm-btn saved-btn{saved_active}" onclick="toggleBookmark(this,{args_saved})">{saved_label}</button> <button class="bm-btn read-later-btn{rl_active}" onclick="toggleBookmark(this,{args_rl})">{rl_label}</button></div>'
        html += '</div>'
    html += '</div>'
    html += '<button class="scroll-arrow right" onclick="this.previousElementSibling.scrollBy({left:340,behavior:\'smooth\'})">▸</button>'
    html += '</div>'
    return html


def _render_briefing_date(full_date: str | None, iso_date: str) -> str:
    """Convert ISO or full date to a display-friendly string."""
    if full_date:
        return full_date
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return dt.strftime("%A, %B %d, %Y")
    except ValueError:
        return iso_date


def parse_briefing_stories(raw_md: str) -> tuple[list[dict], str]:
    """Parse the cron output markdown into structured stories, skipping skill/prompt preamble."""
    lines = raw_md.split("\n")
    briefing_start = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("MORNING BRIEFING"):
            briefing_start = i

    if briefing_start == -1:
        return [], ""

    date_str = ""
    for line in lines[briefing_start:briefing_start + 10]:
        m = re.match(r"MORNING BRIEFING\s*[—–-]\s*(.+)", line.strip())
        if m:
            date_str = m.group(1).strip()
            break

    stories = []
    current_story = None
    body_lines = []

    for line in lines[briefing_start:]:
        stripped = line.strip()
        m = re.match(r'^(\*{0,2})(\d+)\.\s+(.+?)(\*{0,2})$', stripped)
        if m:
            if current_story:
                current_story["body"] = "<br>".join(body_lines)
                stories.append(current_story)
            current_story = {"title": m.group(3).strip("*"), "source_name": "", "source_url": "", "body": "", "impact": ""}
            body_lines = []
            continue

        if current_story is None:
            continue

        src = re.match(r'^Source:\s+(.+?)\s+[—–-]+\s+(https?://\S+)', stripped)
        if src:
            current_story["source_name"] = src.group(1).strip()
            current_story["source_url"] = src.group(2).strip()
            continue

        if stripped:
            body_lines.append(stripped)

    if current_story:
        current_story["body"] = "<br>".join(body_lines)
        stories.append(current_story)

    # Validate: if date is the template placeholder or no story has a source URL,
    # this is a template being parsed, not real content
    if date_str == "Day, Month DD, YYYY" or not any(s.get("source_url") for s in stories):
        return [], ""

    return stories, date_str


def briefing_card(stories: list[dict], date_str: str) -> str:
    if not stories:
        return '<div class="empty-state"><p>No briefing available for today yet. Check back after 7am UTC.</p></div>'

    # Populate impact statements (cached, generated via LLM if needed)
    _get_story_impacts(stories, date_str)

    html = '<div class="briefing-header">'
    html += f'<div class="date">{date_str}</div>'
    html += '<h2>📰 Morning Briefing</h2>'
    html += '<div class="section-timestamp">Generated: ' + date_str + '</div>'
    html += '</div>'
    html += '<div class="dashboard-grid">'
    for i, s in enumerate(stories, 1):
        summary = first_sentence(s["body"])
        html += '<div class="briefing-card">'
        html += f'<span class="card-num">{i}</span>'
        html += f'<h3>{s["title"]}</h3>'
        if s.get("impact"):
            html += f'<div class="card-impact">💡 {s["impact"]}</div>'
        html += f'<div class="card-summary">{summary}</div>'
        html += '<div class="card-source">'
        if s["source_url"]:
            html += f'<a href="{s["source_url"]}" target="_blank" rel="noopener">{s["source_name"]}</a>'
        else:
            html += s["source_name"]
        html += '</div>'
        if s["source_url"]:
            html += f'<a href="{s["source_url"]}" target="_blank" rel="noopener" class="card-action">Read full article →</a>'
        # Bookmark buttons
        sid = _story_id(date_str, s["title"], s.get("source_url", ""))
        saved_active = ' active' if _is_bookmarked(sid, 'saved') else ''
        rl_active = ' active' if _is_bookmarked(sid, 'read_later') else ''
        saved_label = '⭐ Saved' if _is_bookmarked(sid, 'saved') else '⭐ Save'
        rl_label = '📌 Saved for Later' if _is_bookmarked(sid, 'read_later') else '📌 Read Later'
        import html as _html
        args_saved = ','.join(["'"+_html.escape(str(x), quote=False)+"'" for x in [sid, date_str, s["title"], s.get("source_url", ""), s.get("source_name", ""), s.get("body", "")[:500], 'saved']])
        args_rl = ','.join(["'"+_html.escape(str(x), quote=False)+"'" for x in [sid, date_str, s["title"], s.get("source_url", ""), s.get("source_name", ""), s.get("body", "")[:500], 'read_later']])
        html += f'<div class="bm-btn-row"><button class="bm-btn saved-btn{saved_active}" onclick="toggleBookmark(this,{args_saved})">{saved_label}</button> <button class="bm-btn read-later-btn{rl_active}" onclick="toggleBookmark(this,{args_rl})">{rl_label}</button></div>'
        html += '</div>'
    html += '</div>'
    return html

# ── GitHub language colours ──
_LANG_COLORS = {
    "Python": "#3572A5", "JavaScript": "#f1e05a", "TypeScript": "#3178c6",
    "Go": "#00ADD8", "Rust": "#dea584", "Java": "#b07219",
    "Ruby": "#701516", "C": "#555555", "C++": "#f34b7d", "C#": "#178600",
    "HTML": "#e34c26", "CSS": "#563d7c", "Shell": "#89e051",
    "Swift": "#F05138", "Kotlin": "#A97BFF", "PHP": "#4F5D95",
    "Vue": "#41b883", "Svelte": "#ff3e00", "Jupyter Notebook": "#DA5B0B",
    "Dockerfile": "#384d54", "Makefile": "#427819", "Lua": "#000080",
    "HCL": "#844FBA", "Elixir": "#6e4a7e", "Scala": "#c22d40",
}


def _lang_color(lang: str) -> str:
    return _LANG_COLORS.get(lang, "#8b949e")


def _relative_time(iso_str: str) -> str:
    """Convert ISO 8601 timestamp to a friendly relative string."""
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return iso_str[:10]
    delta = datetime.utcnow() - dt
    mins = int(delta.total_seconds() / 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h ago"
    days = hrs // 24
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


# ── Cloudflare Tunnel Monitor UI ──

def _cf_dashboard_url(account_id, path=""):
    if not account_id: return "https://one.dash.cloudflare.com/"
    return f"https://one.dash.cloudflare.com/{account_id}/{path}" if path else f"https://one.dash.cloudflare.com/{account_id}"

def _cf_timestamp(iso_str):
    if not iso_str: return "Never"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        if delta.days > 0: return f"{delta.days}d ago"
        if delta.seconds >= 3600: return f"{delta.seconds//3600}h ago"
        if delta.seconds >= 60: return f"{delta.seconds//60}m ago"
        return "just now"
    except: return iso_str[:19] if iso_str else "Never"

def cloudflare_tunnel_page():
    cf = get_cloudflare_tunnel_data()
    account_id = cf.get("account_id", "")
    tid = cf.get("tunnel_id") or (cf.get("data", {}) or {}).get("tunnel_id", "")
    dash = _cf_dashboard_url(account_id, "networks/tunnels") if account_id else "https://one.dash.cloudflare.com/"
    import time as _t
    checked = cf.get("checked_at", 0)
    ct = "never"
    if checked:
        from datetime import datetime, timezone
        ct = datetime.fromtimestamp(checked, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>🌐 Cloudflare Tunnel Monitor</h1>'
    body += f'<p style="color:var(--text-muted)">Last checked: {ct} <a href="{dash}" target="_blank" rel="noopener" style="color:var(--accent-hover);font-size:0.85rem">Zero Trust →</a></p></div>'

    if not cf.get("ok"):
        err = cf.get("error", "Unknown error")
        if not account_id:
            # Credentials not configured — show a clean info page, not a warning
            body += '<div class="empty-state" style="margin-top:2rem;padding:3rem 2rem;text-align:center;border:1px dashed var(--border);border-radius:12px">'
            body += '<div style="font-size:3rem;margin-bottom:1rem">🔒</div>'
            body += '<h2 style="color:var(--text);margin-bottom:0.5rem">Cloudflare Tunnel Monitoring</h2>'
            body += '<p style="color:var(--text-muted);max-width:500px;margin:0 auto 1.5rem">Monitor your Cloudflare Tunnel — status, hostnames, connections, port mappings, and access policies — all from the API.</p>'
            body += '<p style="color:var(--text-muted);font-size:0.85rem">Set <code>CF_API_TOKEN</code> and <code>CF_ACCOUNT_ID</code> in <code>~/.hermes/.env</code> to enable.</p>'
            body += '</div>'
        else:
            body += f'<div class="empty-state" style="margin-top:2rem"><p>⚠️ Unable to load tunnel data: {err}</p></div>'
        return html_page("Tunnel Monitor", body, active_nav="tunnel")

    d = cf["data"]
    is_up = d["is_up"]; sc = "var(--green)" if is_up else "#f85149"
    st = "✅ UP" if is_up else "❌ DOWN"

    body += '<div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:2rem">'
    body += f'<div class="briefing-card" style="flex:1;min-width:200px"><div class="card-title">Tunnel Status</div>'
    body += f'<div style="font-size:1.5rem;font-weight:700;color:{sc};margin:0.5rem 0">{st}</div>'
    body += f'<div class="card-meta">Name: {d["tunnel_name"]}</div>'
    body += f'<div class="card-meta">ID: <code>{tid[:16]}...</code></div>'
    body += f'<div class="card-meta">Connections: {len(d["connections"])}</div>'
    body += f'<div class="card-meta">Last reconnect: {_cf_timestamp(d["last_reconnect_at"])}</div></div>'
    ap = d["access_policies"]
    body += f'<div class="briefing-card" style="flex:1;min-width:200px"><div class="card-title">Access Policies</div>'
    body += f'<div style="font-size:1.5rem;font-weight:700;color:var(--accent);margin:0.5rem 0">{ap["total_policies"]}</div>'
    body += '<div class="card-meta">Total policies on account</div>'
    for dec, cnt in sorted(ap.get("types_breakdown", {}).items()): body += f'<div class="card-meta">{dec}: {cnt}</div>'
    access = _cf_dashboard_url(account_id, "access/policies") if account_id else "#"
    body += f'<a href="{access}" target="_blank" rel="noopener" class="card-action">Manage Policies →</a></div></div>'

    body += '<div class="section-title">🔗 Public Hostnames</div>'
    hostnames = d["hostnames"]
    if hostnames:
        body += '<table class="tunnel-table" style="width:100%"><thead><tr><th>Hostname</th><th>Origin Service</th><th>Zero Trust</th></tr></thead><tbody>'
        apps = _cf_dashboard_url(account_id, "access/apps") if account_id else "#"
        for hn in hostnames: body += f'<tr><td><strong>{hn["hostname"]}</strong></td><td><code>{hn["service"]}</code></td><td><a href="{apps}" target="_blank" rel="noopener" style="color:var(--accent-hover)">Manage →</a></td></tr>'
        body += '</tbody></table>'
    else: body += '<div class="empty-state"><p>No public hostnames configured.</p></div>'

    body += '<div class="section-title" style="margin-top:2rem">📡 Port Mappings</div>'
    ports = d["port_mappings"]
    if ports:
        body += '<table class="tunnel-table" style="width:100%"><thead><tr><th>Protocol</th><th>Host</th><th>Port</th></tr></thead><tbody>'
        for pm in ports: body += f'<tr><td><span class="badge">{pm["protocol"].upper()}</span></td><td>{pm["host"]}</td><td><code>{pm["port"]}</code></td></tr>'
        body += '</tbody></table>'
    else: body += '<div class="empty-state"><p>No port mappings detected.</p></div>'

    body += '<div class="section-title" style="margin-top:2rem">🛡️ Access Policies</div>'
    policies = ap.get("policies", [])
    if policies:
        body += '<table class="tunnel-table" style="width:100%"><thead><tr><th>Name</th><th>Decision</th><th>Include</th><th>Exclude</th><th>Require</th></tr></thead><tbody>'
        colors = {"allow": "var(--green)", "deny": "#f85149", "bypass": "var(--orange)", "non_identity": "var(--text-muted)"}
        for p in policies:
            dc = colors.get(p["decision"], "var(--text-muted)")
            body += f'<tr><td>{p["name"] or "<em>Unnamed</em>"}</td><td><span style="color:{dc};font-weight:600">{p["decision"]}</span></td><td>{p["include_count"]}</td><td>{p["exclude_count"]}</td><td>{p["require_count"]}</td></tr>'
        body += '</tbody></table>'
    else: body += '<div class="empty-state"><p>No access policies found.</p></div>'

    body += '<div class="section-title" style="margin-top:2rem">🔌 Active Connections</div>'
    conns = d["connections"]
    if conns:
        body += '<table class="tunnel-table" style="width:100%"><thead><tr><th>Connection ID</th><th>Origin IP</th><th>Version</th><th>Arch</th><th>Opened</th></tr></thead><tbody>'
        for c in conns: body += f'<tr><td><code>{c["connection_id"][:16]}...</code></td><td>{c["origin_ip"]}</td><td>{c["version"]}</td><td>{c["arch"]}</td><td>{_cf_timestamp(c["opened_at"])}</td></tr>'
        body += '</tbody></table>'
    else: body += '<div class="empty-state"><p>No active connections.</p></div>'

    return html_page("Tunnel Monitor", body, active_nav="tunnel")



def system_summary_row() -> str:
    """Render a compact summary row for system sections (Spend, GitHub, Tunnel)."""
    html = '<div class="section-title-mini">📊 System Overview</div>'
    html += '<div class="sys-summary-grid">'

    # ── OpenRouter Spend summary ──
    or_data = get_openrouter_data()
    if not or_data["error"]:
        bal = or_data.get("balance") or 0
        usage = or_data.get("total_usage") or 0
        rem = bal - usage
        spend_text = f"${rem:.2f} remaining"
    else:
        spend_text = "Not configured"
    html += '<a href="https://openrouter.ai/activity" target="_blank" rel="noopener" class="sys-summary-card">'
    html += '<span class="sys-summary-icon">💰</span>'
    html += '<div class="sys-summary-info">'
    html += '<div class="sys-summary-label">OpenRouter Credits</div>'
    html += f'<div class="sys-summary-metric">{spend_text}</div>'
    html += '</div></a>'

    # ── GitHub summary ──
    repos, username = get_github_repos()
    if repos and username:
        private_n = sum(1 for r in repos if r.get("private"))
        public_n = len(repos) - private_n
        gh_text = f"{len(repos)} repos (@{username})"
    else:
        gh_text = "Token not configured"
    html += f'<a href="/projects" class="sys-summary-card">'
    html += '<span class="sys-summary-icon">📂</span>'
    html += '<div class="sys-summary-info">'
    html += '<div class="sys-summary-label">GitHub Projects</div>'
    html += f'<div class="sys-summary-metric">{gh_text}</div>'
    html += '</div></a>'

    # ── Cloudflare Tunnel summary ──
    cf = get_cloudflare_tunnel_data()
    if cf.get("ok"):
        d = cf["data"]
        status_text = "UP" if d.get("is_up") else "DOWN"
        cf_text = f"Tunnel: {status_text} — {len(d.get('hostnames', []))} hostnames"
    elif cf.get("account_id"):
        cf_text = cf.get("error", "Unavailable")
    else:
        cf_text = "Not configured"
    html += f'<a href="/tunnel" class="sys-summary-card">'
    html += '<span class="sys-summary-icon">🌐</span>'
    html += '<div class="sys-summary-info">'
    html += '<div class="sys-summary-label">Cloudflare Tunnel</div>'
    html += f'<div class="sys-summary-metric">{cf_text}</div>'
    html += '</div></a>'

    html += '</div>'
    return html

def home_hub_html() -> str:
    """Task-oriented homepage hub: one compact row per intent (icon + label + chips)."""
    groups = [
        ("📰", "Read", [
            ("/briefings", "Briefings"), ("/bookmarks", "Saved"), ("/notes", "Notes")
        ]),
        ("🛠️", "Build", [
            ("/projects", "Projects"), ("/hermes", "Hermes"), ("/inbox", "Inbox")
        ]),
        ("📡", "Monitor", [
            ("/status", "Status"), ("/tunnel", "Tunnel"), ("/logs", "Logs")
        ]),
        ("⚙️", "Maintain", [
            ("/cron", "Cron"), ("/disk-cleanup", "Disk"), ("/models", "Models"), ("/model-tuning", "Tuning"), ("/llm-lab", "LLM Lab"), ("/runbooks", "Runbooks")
        ]),
    ]
    html = '<div class="hub-rows" aria-label="Organized site sections">'
    for icon, title, links in groups:
        html += '<div class="hub-row">'
        html += '<span class="hub-row-label"><span>' + icon + '</span><span>' + title + '</span></span>'
        html += '<span class="hub-chips">'
        for href, label in links:
            html += '<a class="hub-chip" href="' + href + '">' + label + '</a>'
        html += '</span></div>'
    html += '</div>'
    return html


def briefing_list_home(articles: list[dict], date_str: str) -> str:
    """Today's stories on the homepage as a plain vertical list (max 5 rows), no
    horizontal scroll and no bookmark toggle. Used ONLY by home_page(); the archive
    page keeps briefing_card_from_db / briefing_card unchanged."""
    rows = articles[:5]
    h = '<div class="briefing-home">'
    for a in rows:
        title = a.get("title", "Untitled")
        url = a.get("source_url", "")
        summary = a.get("summary") or a.get("impact") or a.get("body") or ""
        categories = a.get("categories", "")
        first_cat = ""
        if categories:
            parts = [c.strip() for c in categories.split(",") if c.strip() and c.strip() != "general"]
            first_cat = parts[0] if parts else ""
        h += '<div class="briefing-home-row">'
        if first_cat:
            bg, fg = CATEGORY_COLORS.get(first_cat, ("#6b7280", "#f3f4f6"))
            h += f'<span class="bh-badge category-badge" style="background:{bg};color:{fg}">{html.escape(first_cat)}</span>'
        h += '<div class="bh-main">'
        href = url or f"/briefing/{date_str}"
        target = ' target="_blank" rel="noopener"' if url else ''
        h += f'<a class="bh-title" href="{html.escape(href, quote=True)}"{target}>{html.escape(title)}</a>'
        if summary:
            h += f'<div class="bh-impact">{html.escape(first_sentence(summary))}</div>'
        h += '</div></div>'
    h += '</div>'
    return h

def status_strip() -> str:
    """Topmost homepage element: a 44px health bar. Green dot + 'All systems normal'
    or red dot + 'K services need attention' (auto-opens the details on issues).
    Amber dot + 'Status API unavailable' on fetch failure."""
    return '''<details class="landing-status-strip" id="landing-status-strip">
        <summary>
            <span class="status-summary-left">
                <span class="status-strip-dot" id="status-strip-dot"></span>
                <span class="status-summary-title">Service status</span>
                <span class="status-summary-meta" id="status-summary-meta">Checking services…</span>
            </span>
            <span class="status-summary-right">
                <span class="status-mini-pill" id="status-ok-pill">— OK</span>
                <span class="status-mini-pill" id="status-issue-pill">— issues</span>
                <span class="status-expand-hint">expand</span>
            </span>
        </summary>
        <div class="landing-status-body" id="landing-status-body">
            <div class="services-loading">Loading compact status…</div>
        </div>
    </details>
    <script>
    (function(){
        var names=["hermes_dashboard","ollama","cloudflare_tunnel","searxng","llm_router","github_backup"];
        var dot=document.getElementById('status-strip-dot');
        fetch("/api/status").then(function(r){ if(!r.ok) throw new Error(r.status); return r.json(); })
        .then(function(data){
            var svcs=data.services||{}; var ok=0; var bad=0; var h='<div class="status-mini-grid">';
            names.forEach(function(k){ var s=svcs[k]; if(!s) return; var healthy=!!s.healthy; if(healthy) ok++; else bad++;
                h += '<a class="status-mini-service" href="/status" style="text-decoration:none">' +
                     '<span><span class="status-dot ' + (healthy?'green':'red') + '"></span> ' + s.name + '</span>' +
                     '<small>' + (s.status || (healthy?'Online':'Issue')) + '</small></a>';
            });
            h += '</div><div style="margin-top:.55rem"><a href="/status" class="card-action">Open full status board →</a></div>';
            var meta=document.getElementById('status-summary-meta'); if(meta) meta.textContent = bad ? (bad + ' service' + (bad>1?'s':'') + ' need attention') : 'All systems normal';
            if(dot) dot.classList.add(bad?'red':'green');
            var okP=document.getElementById('status-ok-pill'); if(okP){ okP.textContent=ok+' OK'; okP.classList.add('ok'); }
            var badP=document.getElementById('status-issue-pill'); if(badP){ badP.textContent=bad+' issues'; badP.classList.add(bad?'warn':'ok'); }
            var body=document.getElementById('landing-status-body'); if(body) body.innerHTML=h;
            if(bad>0){ var d=document.getElementById('landing-status-strip'); if(d) d.open=true; }
        }).catch(function(){
            if(dot) dot.classList.add('amber');
            var meta=document.getElementById('status-summary-meta'); if(meta) meta.textContent='Status API unavailable';
            var body=document.getElementById('landing-status-body'); if(body) body.innerHTML='<div class="services-error">Service status unavailable. Open the full status page for static checks.</div><a href="/status" class="card-action">Open full status board →</a>';
        });
    })();
    </script>'''

def home_page() -> str:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    page_date = now.strftime("%A, %B ") + str(now.day)  # cross-platform "no leading zero"

    # 1) Header row (replaces the hero tagline)
    body = ('<div class="page-head"><h1>devmclovin</h1>'
            f'<span class="page-date">{page_date}</span></div>')

    # 2) Status strip (topmost interactive element; auto-opens on issues)
    body += status_strip()

    # 3) Today's Briefing — same 3-level fallback chain as before, rendered as a
    #    vertical list (briefing_list_home) instead of the horizontal-scroll cards.
    archive = _get_archive()
    stories = []            # articles found through any path
    iso_for_links = today   # ISO date used for per-story fallback links
    section_date = now.strftime("%b ") + str(now.day)

    briefing = archive.get_briefing(today)
    if briefing and briefing.get("articles"):
        stories = briefing["articles"]
        iso_for_links = briefing["date"]
    else:
        # Fallback 1: today's raw .md on disk (may not be in DB yet)
        today_files = sorted(BRIEFING_DIR.glob(f"{today}_*.md"), reverse=True)
        if today_files:
            raw = today_files[0].read_text(encoding="utf-8")
            file_stories, _ = parse_briefing_stories(raw)
            if file_stories:
                stories = file_stories
        if not stories:
            # Fallback 2: most recent briefing from DB
            recent = archive.get_briefings(limit=1)
            if recent:
                b = archive.get_briefing(recent[0]["date"])
                if b and b.get("articles"):
                    stories = b["articles"]
                    iso_for_links = b["date"]
                    section_date = _render_briefing_date(b.get("full_date"), b["date"])

    body += ('<div class="section-head"><h2>Today\'s Briefing — '
             + html.escape(section_date) + '</h2>'
             '<a href="/briefings">All briefings →</a></div>')
    if stories:
        body += briefing_list_home(stories, iso_for_links)
    else:
        body += '<div class="empty-state"><p>☕ No briefings found. The morning briefing runs at 7am UTC.</p></div>'

    # 4) Compact hub chips
    body += home_hub_html()

    # 5) System Overview (collapsed secondary section)
    body += '<details class="system-overview">'
    body += '<summary>📊 System Overview</summary>'
    body += '<div class="system-overview-body">'
    body += system_summary_row()
    body += services_status_row()
    body += quick_links_row()
    body += '</div></details>'

    return html_page("devmclovin", body, active_nav="home")
def briefing_subnav_html(active: str = "archive") -> str:
    archive_cls = 'active' if active == 'archive' else ''
    saved_cls = 'active' if active == 'bookmarks' else ''
    return '<div class="briefing-subnav"><a href="/briefings" class="' + archive_cls + '">Archive</a><a href="/bookmarks" class="' + saved_cls + '">Saved / Read Later</a></div>'

def briefings_page(category=None) -> str:
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>Past Briefings</h1><p>Search, filter, and scan the briefing archive.</p></div>'
    body += briefing_subnav_html("archive")
    # ── Category filter counts ──
    archive = _get_archive()
    cat_counts_raw = archive.get_category_counts()
    cat_counts = {c["category"]: c["count"] for c in cat_counts_raw}
    body += _category_filter_html(active_category=category or "All", counts=cat_counts)

    # ── Search bar ──
    body += '''<div class="search-bar">
        <div class="search-input-wrap">
            <input type="text" id="briefing-search" placeholder="Search all articles by title, content, or category…" autocomplete="off">
            <button class="search-clear" id="search-clear" title="Clear search">&times;</button>
        </div>
    </div>
    <div class="search-status" id="search-status"></div>
    <div class="search-results-placeholder" id="search-placeholder">
        <p>🔍 Type above to search across all archived briefings.</p>
    </div>
    <div class="search-results-empty" id="search-empty">
        <p>No articles found matching your search.</p>
    </div>
    <div class="search-results" id="search-results"></div>'''

    briefings = archive.get_briefings(limit=30)
    body += '<div id="briefing-list-wrapper">'
    if not briefings:
        body += '<div class="empty-state"><p>No briefings found.</p></div>'
        body += '</div>'
        return html_page("Briefings", body, active_nav="briefings")

    body += '<div class="briefing-archive-grid">'
    for b in briefings:
        date_part = b["date"]
        try:
            dt = datetime.strptime(date_part, "%Y-%m-%d")
            display_date = dt.strftime("%b %d, %Y")
            weekday = dt.strftime("%A")
        except ValueError:
            display_date = date_part
            weekday = ""

        # Get articles for this briefing (filtered if category is set)
        if category:
            articles = archive.get_articles_by_category(category, start_date=date_part, end_date=date_part, limit=100)
        else:
            articles = archive.get_articles(date_str=date_part)

        count = len(articles)
        if count == 0:
            continue  # skip briefings with no matching articles after filtering

        titles = [a.get("title", "Untitled") for a in articles]
        top_title = html.escape(titles[0]) if titles else "Untitled"
        preview_titles = [html.escape(t) for t in titles[1:4]]
        href = "/briefing/" + date_part + (("?category=" + category) if category else "")

        body += '<a class="briefing-archive-card" href="' + href + '">'
        body += '<div class="briefing-card-topline"><span class="briefing-date-chip">' + html.escape(display_date) + '</span><span class="briefing-count-chip">' + str(count) + ' stories</span></div>'
        body += '<div class="briefing-top-story">' + top_title + '</div>'
        body += '<ul class="briefing-preview-list">'
        for t in preview_titles:
            body += '<li>' + t + '</li>'
        if count > 4:
            body += '<li>+' + str(count - 4) + ' more stories</li>'
        body += '</ul>'
        body += '<div class="briefing-card-footer">' + html.escape(weekday) + ' · Read briefing →</div>'
        body += '</a>'

    body += '</div></div>'

    # ── Search JavaScript ──
    body += '''<script>
(function() {
    var input = document.getElementById("briefing-search");
    var clearBtn = document.getElementById("search-clear");
    var statusEl = document.getElementById("search-status");
    var resultsEl = document.getElementById("search-results");
    var placeholderEl = document.getElementById("search-placeholder");
    var emptyEl = document.getElementById("search-empty");
    var listWrapper = document.getElementById("briefing-list-wrapper");
    var timer = null;
    var currentQuery = "";

    function hideAll() {
        resultsEl.classList.remove("active");
        placeholderEl.classList.remove("active");
        emptyEl.classList.remove("active");
        statusEl.style.display = "none";
    }

    function showPlaceholder() {
        hideAll();
        placeholderEl.classList.add("active");
        listWrapper.style.display = "";
    }

    function showEmpty() {
        hideAll();
        emptyEl.classList.add("active");
        listWrapper.style.display = "none";
    }

    function renderResults(data) {
        hideAll();
        resultsEl.classList.add("active");
        listWrapper.style.display = "none";
        var html = "";
        for (var i = 0; i < data.length; i++) {
            var r = data[i];
            var catsHtml = "";
            if (r.categories && r.categories !== "general") {
                var cats = r.categories.split(",");
                for (var j = 0; j < cats.length; j++) {
                    var c = cats[j].trim();
                    if (c) catsHtml += '<span class="tag-pill">' + c + '</span>';
                }
            }
            html += '<a href="/briefing/' + r.briefing_date + '" class="search-result-item">';
            html += '<div class="sr-title">' + r.title + '</div>';
            html += '<div class="sr-meta">' + r.briefing_date + ' — ' + r.source_name;
            if (catsHtml) html += ' <span style="margin-left:0.5rem">' + catsHtml + '</span>';
            html += '</div>';
            html += '<div class="sr-snippet">' + (r.snippet || r.summary || "").substring(0, 300) + '</div>';
            html += '</a>';
        }
        resultsEl.innerHTML = html;
    }

    function doSearch(q) {
        if (!q || q.trim().length === 0) {
            showPlaceholder();
            clearBtn.classList.remove("visible");
            return;
        }
        clearBtn.classList.add("visible");
        currentQuery = q.trim();
        statusEl.style.display = "block";
        statusEl.textContent = "Searching…";
        fetch("/api/briefings/search?q=" + encodeURIComponent(currentQuery))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (currentQuery !== input.value.trim()) return; // stale
                statusEl.style.display = "none";
                if (!data || data.length === 0) {
                    showEmpty();
                } else {
                    statusEl.style.display = "block";
                    statusEl.textContent = data.length + " result" + (data.length !== 1 ? "s" : "") + " for \u201c" + currentQuery + "\u201d";
                    renderResults(data);
                }
            })
            .catch(function(err) {
                if (currentQuery !== input.value.trim()) return;
                statusEl.style.display = "block";
                statusEl.textContent = "Search error. Try again.";
                console.error(err);
            });
    }

    input.addEventListener("input", function() {
        var q = input.value;
        if (timer) clearTimeout(timer);
        if (!q || q.trim().length === 0) {
            showPlaceholder();
            clearBtn.classList.remove("visible");
            statusEl.style.display = "none";
            return;
        }
        timer = setTimeout(function() { doSearch(q); }, 300);
    });

    input.addEventListener("keydown", function(e) {
        if (e.key === "Escape") {
            input.value = "";
            showPlaceholder();
            clearBtn.classList.remove("visible");
            statusEl.style.display = "none";
            input.blur();
        }
    });

    clearBtn.addEventListener("click", function() {
        input.value = "";
        showPlaceholder();
        clearBtn.classList.remove("visible");
        statusEl.style.display = "none";
        input.focus();
    });

    // Show placeholder on load
    showPlaceholder();
})();
</script>'''

    return html_page("Briefings", body, active_nav="briefings")


def briefing_detail_page(date: str, category: str = "") -> str:
    body = f'<div style="padding-top:1rem"><a href="/briefings'
    if category:
        body += f'?category={category}'
    body += '" style="color:var(--text-muted);text-decoration:none;font-size:0.9rem">← Back to all briefings</a></div>'

    archive = _get_archive()
    briefing = archive.get_briefing(date)

    if not briefing or not briefing.get("articles"):
        body += f'<div class="empty-state" style="margin-top:2rem"><p>No briefing found for {date}.</p></div>'
        return html_page(f"Briefing — {date}", body, active_nav="briefings")

    articles = briefing["articles"]
    if category:
        articles = [a for a in articles if category in (a.get("categories") or "").split(",")]

    date_str = _render_briefing_date(briefing.get("full_date"), date)

    # Show category tabs on detail page too (for quick switching)
    cat_counts_raw = archive.get_category_counts()
    cat_counts = {c["category"]: c["count"] for c in cat_counts_raw}
    body += _category_filter_html(active_category=category or "All", counts=cat_counts)

    body += briefing_card_from_db(articles, date_str, show_date=True)
    return html_page(f"Briefing — {date}", body, active_nav="briefings")


# ═══════════════════════════════════════════════════════════════
#  Logs Pages
# ═══════════════════════════════════════════════════════════════

def _read_server_logs() -> str:
    """Last 100 journalctl lines (server)."""
    import subprocess
    try:
        out = subprocess.run(
            ["journalctl", "--no-pager", "-n", "100", "-o", "short-iso"],
            capture_output=True, text=True, timeout=5
        )
        return out.stdout or "No recent entries."
    except Exception as e:
        return f"Error reading logs: {e}"


def _read_router_logs() -> str:
    """Last 100 lines of the router + gateway log files."""
    import subprocess
    paths = [
        os.path.expanduser("~/.hermes/logs/router.log"),
        os.path.expanduser("~/.hermes/logs/gateway.log"),
    ]
    log_text = ""
    for lp in paths:
        try:
            out = subprocess.run(
                ["tail", "-n", "100", lp],
                capture_output=True, text=True, timeout=5
            )
            if out.stdout.strip():
                log_text += "\n=== " + lp + " ===\n" + out.stdout
        except Exception:
            pass
    return log_text or "No recent entries."


def logs_page(tab: str = "server") -> str:
    """Merged journal viewer with Server / Router tabs (replaces the two pages)."""
    tab = tab if tab in ("server", "router") else "server"
    if tab == "router":
        log_text = _read_router_logs()
        note = "Last 100 lines from router and gateway logs"
    else:
        log_text = _read_server_logs()
        note = "Last 100 lines from journalctl"

    body = '<h1 class="section-title">📋 Logs</h1>'
    body += '<div class="logs-tabs">'
    body += '<a href="/logs" class="logs-tab' + (' active' if tab == "server" else '') + '">Server</a>'
    body += '<a href="/logs?tab=router" class="logs-tab' + (' active' if tab == "router" else '') + '">Router</a>'
    body += '</div>'
    body += '<p class="section-timestamp">' + note + '</p>'
    body += '<pre style="background:var(--bg-card);padding:1rem;border-radius:8px;overflow-x:auto;font-size:0.8rem">' + html.escape(log_text) + "</pre>"
    body += '<p style="margin-top:1rem"><a href=/hermes style="color:var(--accent)">Back to Hermes</a></p>'
    return html_page("Logs", body, active_nav="logs")


# ═══════════════════════════════════════════════════════════════
#  Status Board Page
# ═══════════════════════════════════════════════════════════════

def status_page() -> str:
    """Serve the standalone status-board.html page (with shared nav injected)."""
    status_html = SITE_DIR / "status-board.html"
    if status_html.exists():
        return inject_nav(status_html.read_text(), "status")
    return "<html><body><h1>Status Board Not Found</h1></body></html>"


def portfolio_page() -> str:
    """Serve the standalone portfolio.html page (with shared nav injected)."""
    portfolio_html = SITE_DIR / "portfolio.html"
    if portfolio_html.exists():
        return inject_nav(portfolio_html.read_text(), "portfolio")
    return "<html><body><h1>Portfolio Not Found</h1></body></html>"



def bookmarks_page() -> str:
    bookmarks = _load_bookmarks()
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>📑 Saved Briefings</h1><p>Saved and read-later articles from the briefing archive.</p></div>'
    body += briefing_subnav_html("bookmarks")

    read_later = bookmarks.get("read_later", [])
    saved = bookmarks.get("saved", [])

    if not read_later and not saved:
        body += '<div class="bm-empty"><p>📭 No bookmarks yet. Browse the <a href="/briefings" style="color:var(--accent)">briefings</a> and save articles you want to keep.</p></div>'
        return html_page("Saved Briefings", body, active_nav="briefings")

    # Read Later section first (priority queue)
    if read_later:
        body += '<div class="bm-section"><h3>📌 Read Later</h3>'
        for bm in read_later:
            body += '<div class="bm-card">'
            body += f'<h4><a href="{bm.get("source_url", "#")}" target="_blank" rel="noopener">{bm.get("title", "Untitled")}</a></h4>'
            body += f'<div class="bm-meta">{bm.get("source_name", "")} · {bm.get("date", "")} · saved {bm.get("saved_at", "")[:10]}</div>'
            if bm.get("body"):
                body += f'<div class="bm-body">{first_sentence(bm["body"])}</div>'
            body += f'<form method="POST" action="/bookmarks/remove" style="display:inline"><input type="hidden" name="id" value="{bm.get("id", "")}"><input type="hidden" name="type" value="read_later"><button type="submit" class="bm-remove">Remove</button></form>'
            body += '</div>'
        body += '</div>'

    # Saved section
    if saved:
        body += '<div class="bm-section"><h3>⭐ Saved</h3>'
        for bm in saved:
            body += '<div class="bm-card">'
            body += f'<h4><a href="{bm.get("source_url", "#")}" target="_blank" rel="noopener">{bm.get("title", "Untitled")}</a></h4>'
            body += f'<div class="bm-meta">{bm.get("source_name", "")} · {bm.get("date", "")} · saved {bm.get("saved_at", "")[:10]}</div>'
            if bm.get("body"):
                body += f'<div class="bm-body">{first_sentence(bm["body"])}</div>'
            body += f'<form method="POST" action="/bookmarks/remove" style="display:inline"><input type="hidden" name="id" value="{bm.get("id", "")}"><input type="hidden" name="type" value="saved"><button type="submit" class="bm-remove">Remove</button></form>'
            body += '</div>'
        body += '</div>'

    return html_page("Saved Briefings", body, active_nav="briefings")


def disk_cleanup_page() -> str:
    """Render the disk cleanup / usage display page."""
    disk_css = """<style>
.disk-hero { text-align: center; padding: 2rem 0 1.5rem; }
.disk-hero h1 { font-size: 2.2rem; font-weight: 800; background: linear-gradient(135deg, var(--accent-hover), #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; margin-bottom:0.5rem; }
.disk-hero p { color: var(--text-muted); font-size: 1rem; }
.disk-loading { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 3rem 0; }
.disk-spinner { width: 44px; height: 44px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: disk-spin 0.8s linear infinite; }
.disk-loading-text { margin-top: 1rem; color: var(--text-muted); font-size: 0.9rem; }
@keyframes disk-spin { to { transform: rotate(360deg); } }
.disk-error { display: none; background: var(--bg-card); border: 1px solid var(--red); border-radius: 10px; padding: 1.5rem; text-align: center; margin: 1rem 0; }
.disk-error p { color: var(--red); margin-bottom: 0.75rem; }
.disk-retry { background: var(--accent); color: #fff; border: none; padding: 0.5rem 1.5rem; border-radius: 8px; font-size: 0.9rem; font-weight: 600; cursor: pointer; transition: background 0.2s; }
.disk-retry:hover { background: var(--accent-hover); }
.disk-content { display: none; }
.disk-category { background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 0.75rem; overflow: hidden; transition: border-color 0.2s; }
.disk-category:hover { border-color: var(--accent); }
.disk-cat-header { display: flex; align-items: center; justify-content: space-between; padding: 0.85rem 1rem; cursor: pointer; user-select: none; gap: 0.75rem; }
.disk-cat-header h3 { margin: 0; font-size: 1rem; font-weight: 600; display: flex; align-items: center; gap: 0.5rem; flex: 1; }
.disk-cat-icon { font-size: 1.2rem; }
.disk-cat-count { font-size: 0.75rem; font-weight: 600; background: var(--accent); color: #fff; padding: 0.1rem 0.5rem; border-radius: 10px; }
.disk-cat-count.zero { background: var(--bg); color: var(--text-muted); border: 1px solid var(--border); }
.disk-cat-chevron { font-size: 0.8rem; color: var(--text-muted); transition: transform 0.25s; flex-shrink: 0; }
.disk-category.open .disk-cat-chevron { transform: rotate(180deg); }
.disk-cat-body { display: none; padding: 0 1rem 0.85rem; }
.disk-category.open .disk-cat-body { display: block; }
.disk-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.disk-table th { text-align: left; color: var(--text-muted); font-weight: 500; padding: 0.35rem 0.5rem; border-bottom: 1px solid var(--border); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em; }
.disk-table td { padding: 0.35rem 0.5rem; border-bottom: 1px solid rgba(48,54,61,0.4); }
.disk-table tr:last-child td { border-bottom: none; }
.disk-table .size-col { white-space: nowrap; color: var(--orange); font-weight: 600; font-variant-numeric: tabular-nums; text-align: right; width: 1%; }
.disk-table .path-col { word-break: break-all; }
.disk-warning { background: rgba(210,153,29,0.08); border: 1px solid rgba(210,153,29,0.2); border-radius: 8px; padding: 0.75rem 1rem; color: var(--orange); font-size: 0.85rem; display: flex; align-items: center; gap: 0.5rem; }
.disk-empty { color: var(--text-muted); font-size: 0.85rem; font-style: italic; padding: 0.5rem 0; }
.disk-desc { color: var(--text-muted); font-size: 0.85rem; line-height: 1.5; }
.disk-meta { color: var(--text-muted); font-size: 0.75rem; margin-top: 0.5rem; }
@media (max-width: 640px) {
    .disk-hero h1 { font-size: 1.6rem; }
    .disk-cat-header { padding: 0.7rem 0.85rem; }
    .disk-cat-body { padding: 0 0.85rem 0.7rem; }
}

/* ── Project Launcher Cards ── */
.launcher-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 1.25rem;
    margin-bottom: 2rem;
    padding: 0.5rem 0 1rem;
}
@media (max-width: 640px) {
    .launcher-grid { grid-template-columns: 1fr; }
}
.launcher-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
    transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
}
.launcher-card:hover {
    border-color: var(--accent);
    transform: translateY(-2px);
    box-shadow: 0 4px 20px rgba(124, 58, 237, 0.15);
}
.lc-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
}
.lc-repo-name {
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--text);
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.lc-repo-name a {
    color: var(--text);
    text-decoration: none;
}
.lc-repo-name a:hover {
    color: var(--accent-hover);
}
.lc-meta-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
    font-size: 0.72rem;
    color: var(--text-muted);
}
.lc-info-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.5rem 1rem;
    padding: 0.6rem 0;
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
}
.lc-info-item {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 0;
}
.lc-info-label {
    font-size: 0.62rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
.lc-info-value {
    font-size: 0.8rem;
    color: var(--text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.lc-info-value a {
    color: var(--accent-hover);
    text-decoration: none;
}
.lc-info-value a:hover {
    text-decoration: underline;
}
.lc-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: auto;
    padding-top: 0.5rem;
}
.lc-btn {
    flex: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.3rem;
    padding: 0.4rem 0.5rem;
    border-radius: 6px;
    font-size: 0.72rem;
    font-weight: 500;
    text-decoration: none;
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text-muted);
    transition: border-color 0.15s, color 0.15s, background 0.15s;
    white-space: nowrap;
}
.lc-btn:hover {
    border-color: var(--accent);
    color: var(--text);
    background: #1c2333;
}
.lc-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
}
.lc-btn-icon {
    font-size: 0.85rem;
}
/* ── Toast notifications ── */
.launcher-toast {
    position: fixed;
    bottom: 1.5rem;
    right: 1.5rem;
    z-index: 9999;
    max-width: 400px;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    font-size: 0.85rem;
    font-weight: 500;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    transition: opacity 0.3s, transform 0.3s;
    opacity: 0;
    transform: translateY(10px);
}
.launcher-toast.show {
    opacity: 1;
    transform: translateY(0);
}
.launcher-toast.error {
    background: #da3633;
    color: #fff;
    border: 1px solid #f85149;
}
.launcher-toast.ok {
    background: #3fb950;
    color: #000;
    border: 1px solid #3fb950;
}
/* ── Restart confirmation dialog ── */
.restart-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    z-index: 9998;
    display: flex;
    align-items: center;
    justify-content: center;
}
.restart-dialog {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 2rem;
    max-width: 400px;
    text-align: center;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5);
}
.restart-dialog h3 {
    margin: 0 0 0.75rem;
}
.restart-dialog p {
    color: var(--text-muted);
    margin: 0 0 1.5rem;
    font-size: 0.85rem;
}
.dialog-btn-primary {
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 0.6rem 1.5rem;
    border-radius: 8px;
    font-size: 0.9rem;
    cursor: pointer;
    margin-right: 0.75rem;
    font-weight: 500;
}
.dialog-btn-secondary {
    background: transparent;
    color: var(--text-muted);
    border: 1px solid var(--border);
    padding: 0.6rem 1.5rem;
    border-radius: 8px;
    font-size: 0.9rem;
    cursor: pointer;
}

</style>"""

    body = f"""{disk_css}
<div class="disk-hero">
    <h1>🗑️ Disk Cleanup</h1>
    <p>See what's taking up space on your home server</p>
</div>

<div class="disk-loading" id="disk-loading">
    <div class="disk-spinner"></div>
    <div class="disk-loading-text">Scanning disk usage…</div>
</div>

<div class="disk-error" id="disk-error">
    <p id="disk-error-msg">Failed to load disk usage data.</p>
    <button class="disk-retry" onclick="loadDiskData()">🔄 Retry</button>
</div>

<div class="disk-content" id="disk-content"></div>

<script>
const CATEGORIES = [
    {{key: 'largest_folders', icon: '📁', label: 'Largest Folders', render: renderFolderTable}},
    {{key: 'docker',         icon: '🐳', label: 'Docker',          render: renderDockerCard}},
    {{key: 'ollama_models',  icon: '🦙', label: 'Ollama Models',   render: renderOllamaTable}},
    {{key: 'journal_logs',   icon: '📜', label: 'Journal Logs',    render: renderJournalCard}},
    {{key: 'git_repos',      icon: '📦', label: 'Git Repos',       render: renderGitTable}},
    {{key: 'caches',         icon: '🗑️', label: 'Cache Directories', render: renderCacheTable}},
];

function renderFolderTable(data) {{
    var items = data.folders || [];
    if (!items.length) return '<div class="disk-empty">No folders found.</div>';
    var rows = '';
    for (var i = 0; i < items.length; i++) {{
        rows += '<tr><td class="size-col">' + esc(items[i].size) + '</td><td class="path-col">' + esc(items[i].path) + '</td></tr>';
    }}
    return '<table class="disk-table"><thead><tr><th>Size</th><th>Path</th></tr></thead><tbody>' + rows + '</tbody></table>';
}}

function renderOllamaTable(data) {{
    var items = data.models || [];
    if (!items.length) return '<div class="disk-empty">No Ollama models found.</div>';
    var rows = '';
    for (var i = 0; i < items.length; i++) {{
        rows += '<tr><td>' + esc(items[i].name) + '</td><td class="size-col">' + esc(items[i].size) + '</td></tr>';
    }}
    return '<table class="disk-table"><thead><tr><th>Model</th><th>Size</th></tr></thead><tbody>' + rows + '</tbody></table>';
}}

function renderDockerCard(data) {{
    if (!data.available) {{
        var msg = data.error || 'Docker is not available on this system.';
        return '<div class="disk-warning">⚠️ ' + esc(msg) + '</div>';
    }}
    return '<div class="disk-empty">Docker is available but no disk usage data was returned.</div>';
}}

function renderJournalCard(data) {{
    if (data.raw) {{
        return '<div class="disk-desc">' + esc(data.raw) + '</div>' +
            (data.size ? '<div class="disk-meta">Total: <strong>' + esc(data.size) + '</strong></div>' : '');
    }}
    return '<div class="disk-empty">No journal data available.</div>';
}}

function renderGitTable(data) {{
    var items = data.repos || [];
    if (!items.length) return '<div class="disk-empty">No git repos with significant disk usage found.</div>';
    var rows = '';
    for (var i = 0; i < items.length; i++) {{
        rows += '<tr><td class="size-col">' + esc(items[i].size) + '</td><td class="path-col">' + esc(items[i].path) + '</td></tr>';
    }}
    return '<table class="disk-table"><thead><tr><th>Size</th><th>Path</th></tr></thead><tbody>' + rows + '</tbody></table>';
}}

function renderCacheTable(data) {{
    var items = data.caches || [];
    if (!items.length) return '<div class="disk-empty">No cache directories found.</div>';
    var rows = '';
    for (var i = 0; i < items.length; i++) {{
        rows += '<tr><td class="path-col">' + esc(items[i].path) + '</td><td class="size-col">' + esc(items[i].size) + '</td></tr>';
    }}
    return '<table class="disk-table"><thead><tr><th>Path</th><th>Size</th></tr></thead><tbody>' + rows + '</tbody></table>';
}}

function esc(s) {{
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function getCount(data, key) {{
    if (data.count !== undefined) return data.count;
    if (key === 'largest_folders') return (data.folders || []).length;
    if (key === 'ollama_models') return (data.models || []).length;
    if (key === 'git_repos') return (data.repos || []).length;
    if (key === 'caches') return (data.caches || []).length;
    return 0;
}}

function toggleCategory(header) {{
    var cat = header.parentElement;
    cat.classList.toggle('open');
}}

function loadDiskData() {{
    var loading = document.getElementById('disk-loading');
    var error = document.getElementById('disk-error');
    var content = document.getElementById('disk-content');

    loading.style.display = 'flex';
    error.style.display = 'none';
    content.style.display = 'none';

    fetch('http://127.0.0.1:9099/api/disk-usage')
        .then(function(r) {{
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        }})
        .then(function(data) {{
            loading.style.display = 'none';
            var html = '';
            for (var i = 0; i < CATEGORIES.length; i++) {{
                var cat = CATEGORIES[i];
                var catData = data[cat.key] || {{}};
                var count = getCount(catData, cat.key);
                var countClass = count === 0 ? ' zero' : '';
                var bodyHtml = cat.render(catData);
                html += '<div class="disk-category open">' +
                    '<div class="disk-cat-header" onclick="toggleCategory(this)">' +
                    '<h3><span class="disk-cat-icon">' + cat.icon + '</span>' + cat.label +
                    '<span class="disk-cat-count' + countClass + '">' + count + '</span></h3>' +
                    '<span class="disk-cat-chevron">▼</span>' +
                    '</div>' +
                    '<div class="disk-cat-body">' + bodyHtml + '</div>' +
                    '</div>';
            }}
            if (data._metadata) {{
                html += '<div class="disk-meta" style="text-align:center;padding:1rem 0;">Scanned in ' +
                    (data._metadata.elapsed_s || '?') + 's</div>';
            }}
            content.innerHTML = html;
            content.style.display = 'block';
        }})
        .catch(function(err) {{
            loading.style.display = 'none';
            document.getElementById('disk-error-msg').textContent =
                'Failed to load disk usage data: ' + (err.message || 'Connection refused');
            error.style.display = 'block';
        }});
}}

loadDiskData();
</script>"""

    return html_page("Disk Cleanup", body, active_nav="disk-cleanup")

# ── Project Launcher Config ──

_CONFIG_FILE = Path(os.path.expanduser("~/.devmclovin/project-launcher.json"))

def load_project_configs() -> dict:
    """Load per-project config from JSON file. Returns dict of name -> config."""
    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}

def save_project_configs(configs: dict):
    """Save per-project config to JSON file."""
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(configs, indent=2))

def get_project_launcher_data() -> list[dict]:
    """Combine GitHub repos with local project configs for launcher cards."""
    repos, username = get_github_repos()
    configs = load_project_configs()
    projects = []
    for r in repos:
        name = r["name"]
        cfg = configs.get(name, {})
        projects.append({
            "name": name,
            "github_url": r["html_url"],
            "language": r.get("language"),
            "last_commit": _relative_time(r.get("updated_at", "")) if r.get("updated_at") else None,
            "description": r.get("description") or cfg.get("description", ""),
            "private": r.get("private", False),
            "fork": r.get("fork", False),
            "stars": r.get("stars", 0),
            "service_name": cfg.get("service_name", ""),
            "local_path": cfg.get("local_path", ""),
            "local_url": cfg.get("local_url", ""),
            "docs_url": cfg.get("docs_url", ""),
            "docs_label": cfg.get("docs_label", "Docs"),
        })
    return projects


# ── Project Launcher Backend ──

def projects_page() -> str:
    """Render the project launcher page with wired action buttons."""
    projects = get_project_launcher_data()

    body = '<div style="padding:1rem 0">'
    body += '<div class="project-hero-card">'
    body += '<h1>Project Command Center</h1>'
    body += '<p style="color:var(--text-muted);font-size:0.95rem;margin:0.35rem 0 0">Search, filter, and sort every repo by what you need right now.</p>'
    langs = sorted({p.get("language") for p in projects if p.get("language")})
    private_count = sum(1 for p in projects if p.get("private"))
    service_count = sum(1 for p in projects if p.get("service_name"))
    docs_count = sum(1 for p in projects if p.get("docs_url"))
    body += '<div class="project-stat-grid">'
    for val, label in [(len(projects), "projects"), (private_count, "private"), (len(projects)-private_count, "public"), (service_count, "services"), (len(langs), "languages"), (docs_count, "docs links")]:
        body += '<div class="project-stat"><div class="project-stat-value">' + str(val) + '</div><div class="project-stat-label">' + label + '</div></div>'
    body += '</div></div>'
    body += '<div class="page-toolbar" role="search" aria-label="Project filters">'
    body += '<input class="page-search" id="project-search" type="search" placeholder="Search projects, descriptions, languages…" autocomplete="off">'
    body += '<select class="page-select" id="project-language"><option value="all">All languages</option>'
    for lang_opt in langs:
        body += '<option value="' + _esc(lang_opt) + '">' + _esc(lang_opt) + '</option>'
    body += '</select>'
    body += '<select class="page-select" id="project-visibility"><option value="all">All visibility</option><option value="private">Private</option><option value="public">Public</option></select>'
    body += '<select class="page-select" id="project-kind"><option value="all">All projects</option><option value="service">Has service</option><option value="docs">Has docs</option></select>'
    body += '<select class="page-select" id="project-sort"><option value="name">Sort: name</option><option value="updated">Sort: recently updated</option><option value="stars">Sort: stars</option><option value="language">Sort: language</option></select>'
    body += '<select class="page-select" id="project-hidden"><option value="visible">Hidden: hide</option><option value="show">Show hidden</option><option value="hidden">Hidden only</option></select>'
    body += '<span class="project-count" id="project-count">' + str(len(projects)) + ' shown</span>'
    body += '<a href="#project-grid">Skip to cards</a></div>'

    if not projects:
        body += '<div class="empty-state"><p>No projects found. Set GITHUB_READ_TOKEN in ~/.hermes/.env.</p></div>'
        body += '</div>'
        return html_page("Projects", body, active_nav="projects")

    body += '<div class="launcher-grid" id="project-grid" role="list" aria-label="Project launcher cards">'

    for p in projects:
        name = p["name"]
        gh = p.get("github_url", "#")
        lang = p.get("language")
        last_commit = p.get("last_commit")
        desc = p.get("description", "")
        if len(desc) > 140:
            desc = desc[:137].rsplit(" ", 1)[0] + "…"
        svc = p.get("service_name", "")
        local_url = p.get("local_url", "")
        docs_url = p.get("docs_url", "")
        docs_label = p.get("docs_label", "Docs")
        private = p.get("private", False)
        fork = p.get("fork", False)
        stars = p.get("stars", 0)

        def esc(s):
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

        # Badges
        badges = ""
        if private:
            badges += '<span class="repo-badge private">private</span> '
        if fork:
            badges += '<span class="repo-badge fork">fork</span> '

        search_text = (name + ' ' + desc + ' ' + (lang or '')).lower()
        is_private = "true" if private else "false"
        has_service = "true" if svc else "false"
        has_docs = "true" if docs_url else "false"
        body += '<article class="launcher-card" data-project-id="' + esc(name) + '" data-name="' + esc(search_text) + '" data-title="' + esc(name.lower()) + '" data-lang="' + esc((lang or '').lower()) + '" data-private="' + is_private + '" data-service="' + has_service + '" data-docs="' + has_docs + '" data-stars="' + str(stars or 0) + '" data-updated="' + esc(last_commit or '') + '" role="article" aria-label="Launcher card for ' + esc(name) + '">'

        # Header: name + GitHub link
        body += '<div class="lc-header">'
        body += '<span class="lc-repo-name"><a href="' + esc(gh) + '" target="_blank" rel="noopener">' + badges + esc(name) + '</a></span>'
        body += '</div>'

        # Description
        if desc:
            body += '<div class="repo-desc" style="font-size:0.82rem;margin:0.25rem 0">' + esc(desc) + '</div>'

        # Meta row: language, last commit, stars
        meta_parts = []
        if lang:
            color = _lang_color(lang) if lang in _LANG_COLORS else "#8b949e"
            meta_parts.append('<span class="lang"><span class="repo-lang-dot" style="background:' + color + '"></span>' + esc(lang) + '</span>')
        if last_commit:
            meta_parts.append('<span>' + esc(last_commit) + '</span>')
        if stars:
            meta_parts.append('<span>⭐ ' + str(stars) + '</span>')
        if meta_parts:
            body += '<div class="lc-meta-row">' + ' <span aria-hidden="true">·</span> '.join(meta_parts) + '</div>'

        # Secondary technical details stay available without dominating the card
        body += '<details class="lc-details"><summary>Technical details</summary>'
        body += '<div class="lc-info-grid">'
        body += '<div class="lc-info-item"><span class="lc-info-label">Local URL</span>'
        if local_url:
            body += '<span class="lc-info-value"><a href="' + esc(local_url) + '" target="_blank" rel="noopener">' + esc(local_url) + '</a></span>'
        else:
            body += '<span class="lc-info-value" style="color:var(--text-muted)">—</span>'
        body += '</div>'
        body += '<div class="lc-info-item"><span class="lc-info-label">Docs</span>'
        if docs_url:
            body += '<span class="lc-info-value"><a href="' + esc(docs_url) + '" target="_blank" rel="noopener">' + esc(docs_label) + '</a></span>'
        else:
            body += '<span class="lc-info-value" style="color:var(--text-muted)">—</span>'
        body += '</div>'
        body += '<div class="lc-info-item"><span class="lc-info-label">Service</span>'
        if svc:
            body += '<span class="lc-info-value"><code>' + esc(svc) + '</code></span>'
        else:
            body += '<span class="lc-info-value" style="color:var(--text-muted)">—</span>'
        body += '</div>'
        body += '<div class="lc-info-item"><span class="lc-info-label">Config Path</span>'
        lp = p.get("local_path", "")
        if lp:
            body += '<span class="lc-info-value" style="font-size:0.72rem"><code>' + esc(lp) + '</code></span>'
        else:
            body += '<span class="lc-info-value" style="color:var(--text-muted)">—</span>'
        body += '</div>'
        body += '</div></details>'

        # Action buttons
        body += '<div class="lc-actions" role="group" aria-label="Project actions for ' + esc(name) + '">'

        # Logs button
        body += '<a class="lc-btn" href="/projects/' + name + '/logs" aria-label="View logs for ' + esc(name) + '">'
        body += '<span class="lc-btn-icon" aria-hidden="true">📋</span> Logs</a>'

        # Restart button
        svc_js = svc.replace("'", "\'")
        name_js = name.replace("'", "\'")
        body += '<button class="lc-btn" type="button" onclick="confirmRestart(&#39;' + name_js + '&#39;,&#39;' + svc_js + '&#39;)" aria-label="Restart ' + esc(name) + '">'
        body += '<span class="lc-btn-icon" aria-hidden="true">🔄</span> Restart</button>'

        # GitHub button
        body += '<a class="lc-btn" href="' + esc(gh) + '" target="_blank" rel="noopener" aria-label="' + esc(name) + ' on GitHub">'
        body += '<span class="lc-btn-icon" aria-hidden="true">🐙</span> GitHub</a>'

        # Config button
        body += '<a class="lc-btn" href="/projects/' + name + '/config" aria-label="Edit configuration for ' + esc(name) + '">'
        body += '<span class="lc-btn-icon" aria-hidden="true">⚙</span> Config</a>'

        # Hide/unhide button (client-side preference stored in localStorage)
        body += '<button class="lc-btn hide-project-btn" type="button" data-hide-project="' + esc(name) + '" aria-label="Hide ' + esc(name) + ' from the Project Command Center">'
        body += '<span class="lc-btn-icon" aria-hidden="true">🙈</span> Hide</button>'

        body += '</div>'
        body += '</article>'

    body += '</div>'

    body += """<script>
(function(){
  var STORAGE_KEY = 'devmclovin.hiddenProjects.v1';
  var search = document.getElementById('project-search');
  var lang = document.getElementById('project-language');
  var visibility = document.getElementById('project-visibility');
  var kind = document.getElementById('project-kind');
  var sort = document.getElementById('project-sort');
  var hiddenMode = document.getElementById('project-hidden');
  var count = document.getElementById('project-count');
  var grid = document.getElementById('project-grid');

  function loadHidden(){
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch(e) { return []; }
  }
  function saveHidden(list){
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(list)); } catch(e) {}
  }
  function isHidden(id){ return loadHidden().indexOf(id) !== -1; }
  function setHidden(id, shouldHide){
    var list = loadHidden().filter(function(x){ return x !== id; });
    if (shouldHide) list.push(id);
    saveHidden(list);
  }
  function val(el, fallback){ return (el && el.value || fallback).toLowerCase(); }
  function updateCardHiddenState(card){
    var hidden = isHidden(card.dataset.projectId || '');
    card.dataset.hidden = hidden ? 'true' : 'false';
    card.classList.toggle('project-hidden', hidden);
    var btn = card.querySelector('[data-hide-project]');
    if (btn) {
      btn.innerHTML = hidden ? '<span class="lc-btn-icon" aria-hidden="true">👁️</span> Unhide' : '<span class="lc-btn-icon" aria-hidden="true">🙈</span> Hide';
      btn.setAttribute('aria-label', (hidden ? 'Unhide ' : 'Hide ') + (card.dataset.projectId || 'project'));
    }
  }
  function compareCards(a, b){
    var s = val(sort, 'name');
    if (s === 'stars') return (parseInt(b.dataset.stars || '0', 10) - parseInt(a.dataset.stars || '0', 10));
    if (s === 'updated') return (b.dataset.updated || '').localeCompare(a.dataset.updated || '');
    if (s === 'language') return (a.dataset.lang || '').localeCompare(b.dataset.lang || '') || (a.dataset.title || '').localeCompare(b.dataset.title || '');
    return (a.dataset.title || '').localeCompare(b.dataset.title || '');
  }
  function applyProjectFilters(){
    var q = (search && search.value || '').toLowerCase().trim();
    var selectedLang = val(lang, 'all');
    var selectedVisibility = val(visibility, 'all');
    var selectedKind = val(kind, 'all');
    var selectedHidden = val(hiddenMode, 'visible');
    var cards = Array.prototype.slice.call(document.querySelectorAll('.launcher-card'));
    cards.forEach(updateCardHiddenState);
    cards.sort(compareCards).forEach(function(card){ if(grid) grid.appendChild(card); });
    var visible = 0;
    var hiddenTotal = 0;
    cards.forEach(function(card){
      var hidden = card.dataset.hidden === 'true';
      if (hidden) hiddenTotal++;
      var matchesText = !q || (card.getAttribute('data-name') || '').indexOf(q) !== -1;
      var matchesLang = selectedLang === 'all' || (card.dataset.lang || '') === selectedLang;
      var matchesVisibility = selectedVisibility === 'all' || (selectedVisibility === 'private' && card.dataset.private === 'true') || (selectedVisibility === 'public' && card.dataset.private !== 'true');
      var matchesKind = selectedKind === 'all' || (selectedKind === 'service' && card.dataset.service === 'true') || (selectedKind === 'docs' && card.dataset.docs === 'true');
      var matchesHidden = (selectedHidden === 'show') || (selectedHidden === 'hidden' && hidden) || (selectedHidden === 'visible' && !hidden);
      var show = matchesText && matchesLang && matchesVisibility && matchesKind && matchesHidden;
      card.style.display = show ? '' : 'none';
      if(show) visible++;
    });
    if(count) count.textContent = visible + ' shown' + (hiddenTotal ? ' · ' + hiddenTotal + ' hidden' : '');
  }
  document.querySelectorAll('[data-hide-project]').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.preventDefault();
      e.stopPropagation();
      var id = btn.getAttribute('data-hide-project');
      var currentlyHidden = isHidden(id);
      setHidden(id, !currentlyHidden);
      applyProjectFilters();
    });
  });
  [search, lang, visibility, kind, sort, hiddenMode].forEach(function(el){ if(el) el.addEventListener(el === search ? 'input' : 'change', applyProjectFilters); });
  applyProjectFilters();
})();
</script>"""

    # Toast container
    body += '<div id="launcher-toast" class="launcher-toast"></div>'

    # JS for restart + toast
    body += """<script>
function showToast(msg, type) {
    var t = document.getElementById("launcher-toast");
    if (!t) { t = document.createElement("div"); t.id = "launcher-toast"; t.className = "launcher-toast"; document.body.appendChild(t); }
    t.textContent = msg;
    t.className = "launcher-toast " + (type || "") + " show";
    clearTimeout(t._to);
    t._to = setTimeout(function() { t.className = "launcher-toast"; }, 4000);
}
function confirmRestart(name, svc) {
    if (name === "devmclovin-landing") {
        showToast("Cannot self-restart the landing page server.", "error");
        return;
    }
    if (!svc) {
        showToast("No service_name configured for this project.", "error");
        return;
    }
    var o = document.createElement("div");
    o.className = "restart-overlay";
    o.innerHTML =
        '<div class="restart-dialog">' +
        '<h3 class="restart-title"></h3>' +
        '<p>This will restart the systemd service. The service may be briefly unavailable.</p>' +
        '<button class="dialog-btn-primary" type="button" data-action="restart">Restart</button>' +
        '<button class="dialog-btn-secondary" type="button" data-action="cancel">Cancel</button>' +
        '</div>';
    document.body.appendChild(o);
    o.querySelector('.restart-title').textContent = 'Restart "' + svc + '"?';
    o.querySelector('[data-action="restart"]').onclick = function() {
        o.remove();
        doRestart(name);
    };
    o.querySelector('[data-action="cancel"]').onclick = function() { o.remove(); };
    o.addEventListener("click", function(e) { if (e.target === o) o.remove(); });
}
function doRestart(name) {
    showToast("Restarting...", "");
    fetch("/api/projects/" + encodeURIComponent(name) + "/restart", { method: "POST" })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(r) {
            if (r.ok) {
                showToast("Restarted " + r.data.service + " successfully.", "ok");
            } else {
                showToast("Restart failed: " + (r.data.error || "unknown"), "error");
            }
        })
        .catch(function(e) { showToast("Restart request failed: " + e, "error"); });
}
</script>"""

    body += '</div>'
    return html_page("Projects", body, active_nav="projects",
                      extra_head='<link rel="prefetch" href="/api/projects/launcher">')


def project_logs_page(name: str) -> str:
    """Render a log viewer page for a project's systemd service using journalctl."""
    configs = load_project_configs()
    cfg = configs.get(name, {})
    service_name = cfg.get("service_name", "")
    if not service_name:
        body = ('<div style="padding:2rem 0 1rem">'
                '<h1>&#128196; Project Logs: ' + name + '</h1>'
                '<div class="empty-state"><p>&#9888;&#65039; No service_name configured for this project. '
                'Add "service_name" to ~/.devmclovin/project-launcher.json to enable log viewing.</p></div>'
                '<p><a href="/projects" style="color:var(--accent)">&larr; Back to Projects</a></p></div>')
        return html_page("Logs — " + name, body, active_nav="projects")

    import subprocess
    log_output = ""
    log_error = ""
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", service_name, "-n", "200", "--no-pager", "--no-hostname"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            log_output = r.stdout
        else:
            log_error = r.stderr or "journalctl exited with code " + str(r.returncode)
    except FileNotFoundError:
        log_error = "journalctl not found on this system"
    except subprocess.TimeoutExpired:
        log_error = "journalctl timed out after 5 seconds"
    except Exception as e:
        log_error = str(e)

    if log_error and not log_output:
        try:
            r = subprocess.run(
                ["journalctl", "-u", service_name, "-n", "200", "--no-pager", "--no-hostname"],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                log_output = r.stdout
                log_error = ""
        except Exception:
            pass

    esc = log_output.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body = ('<div style="padding:1rem 0">'
            '<h1>&#128196; Project Logs: ' + name + '</h1>'
            '<p style="color:var(--text-muted);font-size:0.85rem;margin-bottom:1rem">'
            'Service: <code>' + service_name + '</code> &middot; '
            'Last 200 lines &middot; '
            '<a href="/projects" style="color:var(--accent)">&larr; Back to Projects</a></p>')
    if log_error and not log_output:
        body += '<div class="empty-state"><p>&#9888;&#65039; ' + log_error + '</p></div>'
    elif log_output:
        body += ('<pre style="background:var(--bg-card);border:1px solid var(--border);'
                 'border-radius:8px;padding:1rem;font-size:0.78rem;line-height:1.5;'
                 'overflow-x:auto;max-height:70vh;overflow-y:auto;'
                 'white-space:pre-wrap;word-break:break-all">' + esc + '</pre>')
    else:
        body += '<div class="empty-state"><p>No log output for ' + service_name + '</p></div>'
    body += '</div>'
    return html_page("Logs — " + name, body, active_nav="projects")


def project_config_page(name: str, saved: bool = False, error: str = "") -> str:
    """Render a config view/edit page for a project."""
    configs = load_project_configs()
    cfg = configs.get(name, {})

    current_json = json.dumps(cfg, indent=2)

    body = ('<div style="padding:1rem 0">'
            '<h1>&#9881;&#65039; Project Config: ' + name + '</h1>'
            '<p style="color:var(--text-muted);font-size:0.85rem;margin-bottom:1rem">'
            '<a href="/projects" style="color:var(--accent)">&larr; Back to Projects</a></p>')

    if saved:
        body += ('<div style="background:var(--green);color:#000;padding:0.5rem 1rem;'
                 'border-radius:6px;margin-bottom:1rem;font-weight:500">'
                 '&#10003; Config saved. <a href="/projects" style="color:#000;text-decoration:underline">Back to Projects</a></div>')
    if error:
        body += ('<div style="background:var(--red);color:#fff;padding:0.5rem 1rem;'
                 'border-radius:6px;margin-bottom:1rem;font-weight:500">'
                 '&#9888; ' + error + '</div>')

    body += ('<form method="POST" action="/projects/' + name + '/config" style="max-width:700px">'
             '<label style="display:block;color:var(--text-muted);font-size:0.8rem;margin-bottom:0.5rem">'
             'Edit the JSON config for this project. Available fields: local_path, description, service_name, local_url, docs_url, docs_label.</label>'
             '<textarea name="config_json" rows="16" style="width:100%;background:var(--bg-card);'
             'color:var(--text);border:1px solid var(--border);border-radius:8px;padding:0.75rem;'
             'font-family:monospace;font-size:0.82rem;resize:vertical">'
             + current_json.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") +
             '</textarea>'
             '<div style="margin-top:1rem;display:flex;gap:0.75rem">'
             '<button type="submit" style="background:var(--accent);color:#fff;border:none;'
             'padding:0.6rem 1.5rem;border-radius:8px;font-size:0.9rem;cursor:pointer;font-weight:500">'
             'Save Changes</button>'
             '<a href="/projects" style="display:inline-block;padding:0.6rem 1.5rem;'
             'border:1px solid var(--border);border-radius:8px;color:var(--text-muted);'
             'text-decoration:none;font-size:0.9rem">Cancel</a>'
             '</div>'
             '</form>')
    body += '</div>'
    return html_page("Config — " + name, body, active_nav="projects")


def project_config_save(name: str, config_json: str) -> tuple:
    """Save updated project config. Returns (success: bool, error: str)."""
    try:
        new_cfg = json.loads(config_json)
        if not isinstance(new_cfg, dict):
            return False, "Config must be a JSON object (dictionary)."
        configs = load_project_configs()
        configs[name] = new_cfg
        save_project_configs(configs)
        return True, ""
    except json.JSONDecodeError as e:
        return False, "Invalid JSON: " + str(e)
    except Exception as e:
        return False, str(e)


def project_restart(name: str) -> dict:
    """Restart a project's systemd service. Returns result dict."""
    if name == "devmclovin-landing":
        return {"ok": False, "error": "Cannot self-restart the landing page server — it would kill the response mid-flight."}

    configs = load_project_configs()
    cfg = configs.get(name, {})
    service_name = cfg.get("service_name", "")
    if not service_name:
        return {"ok": False, "error": "No service_name configured for this project."}

    import subprocess
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", service_name],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            return {"ok": True, "service": service_name}
        else:
            err = r.stderr.strip() or r.stdout.strip() or "systemctl exited with code " + str(r.returncode)
            return {"ok": False, "error": err}
    except FileNotFoundError:
        return {"ok": False, "error": "systemctl not available"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "systemctl restart timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
#  HTTP Handler
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# Model tuning helpers
# ═══════════════════════════════════════════════════════════════

def _tuning_slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip()).strip("-._")
    return slug[:96] or "model"


def _tuning_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _tuning_ensure_dirs() -> None:
    TUNING_DIR.mkdir(parents=True, exist_ok=True)
    TUNING_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TUNING_DATASET_DIR.mkdir(parents=True, exist_ok=True)
    HF_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _load_tuning_examples() -> list[dict]:
    _tuning_ensure_dirs()
    if not TUNING_EXAMPLES_FILE.exists():
        return []
    try:
        data = json.loads(TUNING_EXAMPLES_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_tuning_examples(examples: list[dict]) -> None:
    _tuning_ensure_dirs()
    tmp = TUNING_EXAMPLES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(examples, indent=2, ensure_ascii=False))
    tmp.rename(TUNING_EXAMPLES_FILE)


def _normalise_tuning_example(item: dict) -> dict:
    import hashlib
    messages = item.get("messages") or []
    if not messages and (item.get("prompt") or item.get("completion")):
        messages = []
        if item.get("system"):
            messages.append({"role": "system", "content": str(item.get("system") or "")})
        messages.append({"role": "user", "content": str(item.get("prompt") or "")})
        messages.append({"role": "assistant", "content": str(item.get("completion") or "")})
    clean = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        content = str(m.get("content") or "").strip()
        if role in {"system", "user", "assistant", "tool"} and content:
            clean.append({"role": role, "content": content})
    if not any(m["role"] == "user" for m in clean) or not any(m["role"] == "assistant" for m in clean):
        raise ValueError("Each tuning example needs at least one user and one assistant message")
    raw = json.dumps(clean, sort_keys=True, ensure_ascii=False) + str(item.get("split") or "train")
    ex_id = item.get("id") or hashlib.sha1(raw.encode()).hexdigest()[:12]
    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [x.strip() for x in tags.split(",") if x.strip()]
    return {"id": str(ex_id), "created_at": item.get("created_at") or _tuning_now(), "split": item.get("split") or "train", "tags": tags, "messages": clean}


def _tuning_add_examples(payload: dict) -> dict:
    examples = _load_tuning_examples()
    incoming = payload.get("examples")
    if incoming is None:
        incoming = [payload]
    added = []
    seen = {ex.get("id") for ex in examples}
    for item in incoming:
        ex = _normalise_tuning_example(item if isinstance(item, dict) else {})
        if ex["id"] in seen:
            ex["id"] = ex["id"] + "-" + str(int(time.time()))
        seen.add(ex["id"])
        examples.append(ex)
        added.append(ex)
    _save_tuning_examples(examples)
    return {"ok": True, "added": added, "count": len(examples)}


def _tuning_import_text(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "No import text provided"}
    items = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list): items = parsed
        elif isinstance(parsed, dict) and isinstance(parsed.get("examples"), list): items = parsed.get("examples")
        elif isinstance(parsed, dict): items = [parsed]
    except Exception:
        for line in text.splitlines():
            if line.strip(): items.append(json.loads(line))
    return _tuning_add_examples({"examples": items})


def _tuning_delete_example(ex_id: str) -> dict:
    examples = _load_tuning_examples()
    kept = [ex for ex in examples if ex.get("id") != ex_id]
    _save_tuning_examples(kept)
    return {"ok": True, "deleted": len(examples) - len(kept), "count": len(kept)}


def _tuning_dataset_jsonl(examples: list[dict] | None = None, split: str | None = None) -> str:
    rows = []
    for ex in examples if examples is not None else _load_tuning_examples():
        if split and ex.get("split", "train") != split:
            continue
        rows.append(json.dumps({"messages": ex.get("messages", [])}, ensure_ascii=False))
    return "\n".join(rows) + ("\n" if rows else "")


def _tuning_python_status() -> dict:
    import importlib.util, shutil
    return {"trl": bool(importlib.util.find_spec("trl")), "transformers": bool(importlib.util.find_spec("transformers")), "datasets": bool(importlib.util.find_spec("datasets")), "peft": bool(importlib.util.find_spec("peft")), "torch": bool(importlib.util.find_spec("torch")), "accelerate_cmd": shutil.which("accelerate") or ""}


def _tuning_state() -> dict:
    import shutil
    _tuning_ensure_dirs()
    configs = []
    for f in sorted(TUNING_CONFIG_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        configs.append({"name": f.name, "path": str(f), "modified_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat()})
    # Include one level of run directories too.
    for f in sorted(TUNING_CONFIG_DIR.glob("*/config.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        configs.append({"name": f.parent.name, "path": str(f), "modified_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat()})
    downloads = []
    if HF_DOWNLOAD_DIR.exists():
        for child in sorted(HF_DOWNLOAD_DIR.glob("*"))[:100]:
            downloads.append({"name": child.name, "path": str(child), "is_dir": child.is_dir()})
    py = _tuning_python_status()
    return {"ok": True, "workspace": str(TUNING_DIR), "examples_file": str(TUNING_EXAMPLES_FILE), "examples": _load_tuning_examples(), "configs": configs, "downloads": downloads, "hf_download_dir": str(HF_DOWNLOAD_DIR), "hf_token": bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")), "commands": {"hf": shutil.which("hf") or "", "git": shutil.which("git") or ""}, "python": py, "runtime": {"ready": True, "note": "Training files can be generated even if TRL dependencies are installed later."}}


def _hf_api_request(url: str):
    headers = {"User-Agent": "devmclovin-model-tuning"}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _tuning_hf_files(payload: dict) -> dict:
    import urllib.parse
    repo_id = (payload.get("repo_id") or "").strip()
    revision = (payload.get("revision") or "main").strip()
    repo_type = (payload.get("repo_type") or "model").strip()
    if not repo_id:
        return {"ok": False, "error": "Enter a Hugging Face repo id"}
    # HF API expects repo ids as path segments (owner/name), not %2F-encoded.
    repo_q = urllib.parse.quote(repo_id, safe="/")
    rev_q = urllib.parse.quote(revision, safe="")
    prefix = "datasets/" if repo_type == "dataset" else "models/"
    url = f"https://huggingface.co/api/{prefix}{repo_q}/tree/{rev_q}?recursive=true"
    data = _hf_api_request(url)
    files = []
    for item in data if isinstance(data, list) else []:
        if item.get("type") != "file":
            continue
        path = item.get("path") or ""
        lower = path.lower()
        interesting = lower.endswith((".gguf", ".safetensors", ".bin", ".json", ".jsonl", ".txt", ".model", ".py", ".md")) or "tokenizer" in lower or "config" in lower
        files.append({"path": path, "size": item.get("size") or 0, "oid": item.get("oid", ""), "lfs": item.get("lfs") or {}, "interesting": interesting})
    files.sort(key=lambda x: (not x["interesting"], x["path"].lower()))
    return {"ok": True, "repo_id": repo_id, "revision": revision, "repo_type": repo_type, "files": files}


def _hf_download_url(repo_id: str, revision: str, filename: str, repo_type: str = "model") -> str:
    import urllib.parse
    path = "/datasets/" + repo_id if repo_type == "dataset" else "/" + repo_id
    return "https://huggingface.co" + path + "/resolve/" + urllib.parse.quote(revision, safe="") + "/" + urllib.parse.quote(filename, safe="/")


def _tuning_hf_pull_stream(handler, payload: dict) -> None:
    repo_id = (payload.get("repo_id") or "").strip()
    revision = (payload.get("revision") or "main").strip()
    repo_type = (payload.get("repo_type") or "model").strip()
    files = payload.get("files") or []
    dest_root = Path(os.path.expanduser(payload.get("dest") or str(HF_DOWNLOAD_DIR))).resolve()
    if not repo_id or not files:
        handler.wfile.write(b'data: {"ok":false,"error":"repo_id and selected files are required"}\n\n'); handler.wfile.flush(); return
    if len(files) > 40:
        handler.wfile.write(b'data: {"ok":false,"error":"Refusing to pull more than 40 files in one request"}\n\n'); handler.wfile.flush(); return
    safe_repo_dir = dest_root / _tuning_slug(repo_id.replace("/", "--"))
    safe_repo_dir.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    headers = {"User-Agent": "devmclovin-model-tuning"}
    if token: headers["Authorization"] = "Bearer " + token
    total = len(files)
    for idx, filename in enumerate(files, start=1):
        rel = Path(filename)
        if rel.is_absolute() or ".." in rel.parts:
            handler.wfile.write(("data: " + json.dumps({"status":"skipped","file":filename,"error":"unsafe path"}) + "\n\n").encode()); handler.wfile.flush(); continue
        out = safe_repo_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        url = _hf_download_url(repo_id, revision, filename, repo_type)
        try:
            handler.wfile.write(("data: " + json.dumps({"status":"downloading","file":filename,"index":idx,"total":total,"percent":round((idx-1)/total*100,1)}) + "\n\n").encode()); handler.wfile.flush()
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp, out.open("wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk: break
                    f.write(chunk)
            handler.wfile.write(("data: " + json.dumps({"status":"saved","file":filename,"path":str(out),"bytes":out.stat().st_size,"percent":round(idx/total*100,1)}) + "\n\n").encode()); handler.wfile.flush()
        except Exception as e:
            handler.wfile.write(("data: " + json.dumps({"status":"error","file":filename,"error":str(e),"percent":round(idx/total*100,1)}) + "\n\n").encode()); handler.wfile.flush()
    handler.wfile.write(("data: " + json.dumps({"status":"done","path":str(safe_repo_dir),"percent":100}) + "\n\n").encode()); handler.wfile.flush()


def _tuning_training_script(config: dict, dataset_path: str) -> str:
    target_modules = config.get("lora", {}).get("target_modules") or ["q_proj", "k_proj", "v_proj", "o_proj"]
    return f'''#!/usr/bin/env python3
\"\"\"Generated by devmclovin Model Tuning page. Review before long training runs.\"\"\"
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import LoraConfig
from trl import SFTConfig, SFTTrainer

MODEL = {json.dumps(config.get("base_model") or "")}
DATASET = {json.dumps(dataset_path)}
OUTPUT_DIR = {json.dumps(config.get("output_dir") or str(TUNING_DIR / "outputs"))}

if not MODEL:
    raise SystemExit("Set base_model in the generated config or UI before training")

tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

quantization_config = None
if {bool(config.get("qlora", True))!r}:
    quantization_config = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype="bfloat16", bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True)

model = AutoModelForCausalLM.from_pretrained(MODEL, trust_remote_code=True, device_map="auto", quantization_config=quantization_config)

peft_config = LoraConfig(r={int(config.get("lora", {}).get("r", 16))}, lora_alpha={int(config.get("lora", {}).get("alpha", 32))}, lora_dropout={float(config.get("lora", {}).get("dropout", 0.05))}, bias="none", task_type="CAUSAL_LM", target_modules={json.dumps(target_modules)})

dataset = load_dataset("json", data_files=DATASET, split="train")

args = SFTConfig(output_dir=OUTPUT_DIR, run_name={json.dumps(config.get("run_name") or "devmclovin-sft")}, num_train_epochs={float(config.get("epochs", 1))}, learning_rate={config.get("learning_rate") or "2e-4"}, per_device_train_batch_size={int(config.get("batch_size", 1))}, gradient_accumulation_steps={int(config.get("gradient_accumulation_steps", 8))}, max_seq_length={int(config.get("max_seq_length", 2048))}, packing={bool(config.get("packing", False))!r}, bf16={bool(config.get("bf16", True))!r}, gradient_checkpointing={bool(config.get("gradient_checkpointing", True))!r}, optim={json.dumps(config.get("optimizer") or "paged_adamw_8bit")}, lr_scheduler_type={json.dumps(config.get("scheduler") or "cosine")}, warmup_ratio={float(config.get("warmup_ratio", 0.03))}, logging_steps={int(config.get("logging_steps", 5))}, save_steps={int(config.get("save_steps", 100))}, seed={int(config.get("seed", 42))}, dataset_kwargs={{"add_special_tokens": False, "append_concat_token": False}})

trainer = SFTTrainer(model=model, args=args, train_dataset=dataset, processing_class=tokenizer, peft_config=peft_config)
trainer.train()
trainer.save_model(OUTPUT_DIR)
tokenizer.save_pretrained(OUTPUT_DIR)
print("Saved tuned adapter/model to", OUTPUT_DIR)
'''


def _tuning_write_config(payload: dict) -> dict:
    _tuning_ensure_dirs()
    config = dict(payload)
    run_name = _tuning_slug(config.get("run_name") or "devmclovin-sft")
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    run_dir = TUNING_CONFIG_DIR / f"{stamp}-{run_name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = config.get("dataset_path") or str(run_dir / "train.jsonl")
    if not config.get("dataset_path"):
        Path(dataset_path).write_text(_tuning_dataset_jsonl())
    config["dataset_path"] = dataset_path
    config["generated_at"] = _tuning_now()
    config_path = run_dir / "config.json"
    script_path = run_dir / "train_sft_lora.py"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    script_path.write_text(_tuning_training_script(config, dataset_path))
    command = f"cd {run_dir} && python3 -m venv .venv && . .venv/bin/activate && pip install -U trl transformers datasets peft accelerate bitsandbytes torch && python train_sft_lora.py"
    (run_dir / "RUN_COMMAND.txt").write_text(command + "\n")
    return {"ok": True, "run_dir": str(run_dir), "config_path": str(config_path), "dataset_path": dataset_path, "script_path": str(script_path), "command": command}



# ── LLM Lab: evals, traces, arena, routing, HF GGUF discovery ──

def _json_load_file(path: Path, default):
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data
    except Exception:
        pass
    return default


def _json_save_file(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    tmp.replace(path)


def _llm_lab_ensure() -> None:
    LLM_LAB_DIR.mkdir(parents=True, exist_ok=True)
    for path, default in [
        (LLM_LAB_EVALS_FILE, []),
        (LLM_LAB_TRACES_FILE, []),
        (LLM_LAB_ARENA_FILE, {"matches": [], "scores": {}}),
        (LLM_LAB_ROUTER_FILE, {"rules": [], "fallbacks": []}),
        (LLM_LAB_PROMPTS_FILE, []),
        (LLM_LAB_PRESETS_FILE, []),
        (LLM_LAB_RUNS_FILE, []),
    ]:
        if not path.exists():
            _json_save_file(path, default)


def _lab_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _lab_id(prefix: str) -> str:
    import secrets
    return prefix + "-" + datetime.utcnow().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def _lab_score_response(response: str, criteria: dict | None = None) -> dict:
    criteria = criteria or {}
    text = response or ""
    score = 0
    checks = []
    contains = criteria.get("contains") or []
    if isinstance(contains, str):
        contains = [contains]
    for item in contains:
        ok = str(item).lower() in text.lower()
        score += 25 if ok else 0
        checks.append({"type": "contains", "value": item, "ok": ok})
    not_contains = criteria.get("not_contains") or []
    if isinstance(not_contains, str):
        not_contains = [not_contains]
    for item in not_contains:
        ok = str(item).lower() not in text.lower()
        score += 15 if ok else 0
        checks.append({"type": "not_contains", "value": item, "ok": ok})
    regexes = criteria.get("regex") or criteria.get("matches") or []
    if isinstance(regexes, str):
        regexes = [regexes]
    for pattern in regexes:
        try:
            ok = bool(re.search(str(pattern), text, re.IGNORECASE | re.MULTILINE))
        except re.error as e:
            ok = False
            checks.append({"type": "regex", "value": pattern, "ok": False, "error": str(e)})
            continue
        score += 20 if ok else 0
        checks.append({"type": "regex", "value": pattern, "ok": ok})
    min_words = int(criteria.get("min_words") or 0)
    if min_words:
        words = len(re.findall(r"\S+", text))
        ok = words >= min_words
        score += 20 if ok else 0
        checks.append({"type": "min_words", "value": min_words, "actual": words, "ok": ok})
    max_words = int(criteria.get("max_words") or 0)
    if max_words:
        words = len(re.findall(r"\S+", text))
        ok = words <= max_words
        score += 20 if ok else 0
        checks.append({"type": "max_words", "value": max_words, "actual": words, "ok": ok})
    json_required = bool(criteria.get("json"))
    parsed_json = None
    if json_required:
        try:
            parsed_json = json.loads(text)
            ok = True
        except Exception:
            ok = False
        score += 25 if ok else 0
        checks.append({"type": "json", "ok": ok})
    json_fields = criteria.get("json_fields") or []
    if isinstance(json_fields, str):
        json_fields = [json_fields]
    if json_fields:
        if parsed_json is None:
            try:
                parsed_json = json.loads(text)
            except Exception:
                parsed_json = None
        for field in json_fields:
            ok = isinstance(parsed_json, dict) and str(field) in parsed_json
            score += 10 if ok else 0
            checks.append({"type": "json_field", "value": field, "ok": ok})
    if not checks:
        # Heuristic fallback: successful, non-trivial answers get a useful baseline score.
        words = len(re.findall(r"\S+", text))
        score = 50 + min(40, words // 6)
        checks.append({"type": "heuristic", "words": words, "ok": words >= 20})
    return {"score": min(100, score), "checks": checks}


def _lab_record_trace(trace: dict) -> dict:
    _llm_lab_ensure()
    traces = _json_load_file(LLM_LAB_TRACES_FILE, [])
    if not isinstance(traces, list):
        traces = []
    trace.setdefault("id", _lab_id("trace"))
    trace.setdefault("created_at", _lab_now())
    traces.insert(0, trace)
    traces = traces[:500]
    _json_save_file(LLM_LAB_TRACES_FILE, traces)
    return trace




def _llm_lab_append_run(run: dict) -> dict:
    _llm_lab_ensure()
    runs = _json_load_file(LLM_LAB_RUNS_FILE, [])
    if not isinstance(runs, list):
        runs = []
    run.setdefault("id", _lab_id("run"))
    run.setdefault("created_at", _lab_now())
    runs.insert(0, run)
    runs = runs[:200]
    _json_save_file(LLM_LAB_RUNS_FILE, runs)
    return run


def _llm_lab_leaderboard(traces: list | None = None, arena: dict | None = None) -> list[dict]:
    traces = traces if isinstance(traces, list) else _json_load_file(LLM_LAB_TRACES_FILE, [])
    arena = arena if isinstance(arena, dict) else _json_load_file(LLM_LAB_ARENA_FILE, {"matches": [], "scores": {}})
    stats: dict[str, dict] = {}
    def rec(model: str) -> dict:
        model = model or "unknown"
        return stats.setdefault(model, {"model": model, "runs": 0, "ok": 0, "errors": 0, "scores": [], "latencies": [], "tok_s": [], "wins": 0, "losses": 0, "ties": 0})
    for trace in traces if isinstance(traces, list) else []:
        result = trace.get("result") if isinstance(trace, dict) else {}
        request = trace.get("request") if isinstance(trace, dict) else {}
        model = trace.get("model") or (request or {}).get("model") or (result or {}).get("model")
        if not model:
            continue
        r = rec(str(model))
        r["runs"] += 1
        if (result or {}).get("ok", True) is False:
            r["errors"] += 1
        else:
            r["ok"] += 1
        if trace.get("score") is not None:
            try: r["scores"].append(float(trace.get("score")))
            except Exception: pass
        if isinstance(result, dict):
            if result.get("total_ms") is not None:
                try: r["latencies"].append(float(result.get("total_ms")))
                except Exception: pass
            if result.get("tokens_per_sec") is not None:
                try: r["tok_s"].append(float(result.get("tokens_per_sec")))
                except Exception: pass
    scores = (arena or {}).get("scores", {}) if isinstance(arena, dict) else {}
    for model, score in scores.items():
        r = rec(str(model))
        r["wins"] += int(score.get("wins") or 0)
        r["losses"] += int(score.get("losses") or 0)
        r["ties"] += int(score.get("ties") or 0)
    rows = []
    for model, r in stats.items():
        avg_score = round(sum(r["scores"]) / len(r["scores"]), 1) if r["scores"] else None
        avg_latency = round(sum(r["latencies"]) / len(r["latencies"]), 1) if r["latencies"] else None
        avg_tok_s = round(sum(r["tok_s"]) / len(r["tok_s"]), 2) if r["tok_s"] else None
        win_rate = round(r["wins"] / max(1, r["wins"] + r["losses"]) * 100, 1) if (r["wins"] or r["losses"]) else None
        quality = avg_score if avg_score is not None else 0
        speed = min(25, (avg_tok_s or 0) / 2)
        arena_bonus = (win_rate or 0) / 4
        overall = round(quality + speed + arena_bonus - (r["errors"] * 5), 1)
        rows.append({"model": model, "runs": r["runs"], "ok": r["ok"], "errors": r["errors"], "avg_score": avg_score, "avg_latency_ms": avg_latency, "avg_tokens_per_sec": avg_tok_s, "wins": r["wins"], "losses": r["losses"], "ties": r["ties"], "win_rate": win_rate, "overall": overall})
    rows.sort(key=lambda x: (x.get("overall") or 0, x.get("runs") or 0), reverse=True)
    return rows


def _llm_lab_upsert_list_item(path: Path, payload: dict, key: str, default_name: str) -> dict:
    _llm_lab_ensure()
    items = _json_load_file(path, [])
    if not isinstance(items, list):
        items = []
    item = payload.get(key) if isinstance(payload.get(key), dict) else dict(payload)
    item.setdefault("id", _lab_id(key))
    item.setdefault("name", default_name)
    item.setdefault("created_at", _lab_now())
    item["updated_at"] = _lab_now()
    items = [x for x in items if x.get("id") != item.get("id")]
    items.insert(0, item)
    _json_save_file(path, items[:200])
    return {"ok": True, key: item, "count": len(items)}


def _llm_lab_delete_list_item(path: Path, payload: dict, key: str) -> dict:
    _llm_lab_ensure()
    item_id = str(payload.get("id") or "")
    items = _json_load_file(path, [])
    if not isinstance(items, list):
        items = []
    before = len(items)
    items = [x for x in items if x.get("id") != item_id]
    _json_save_file(path, items)
    return {"ok": True, "deleted": before - len(items), "count": len(items), key + "s": items}


def _llm_lab_save_prompt(payload: dict) -> dict:
    return _llm_lab_upsert_list_item(LLM_LAB_PROMPTS_FILE, payload, "prompt", "Untitled prompt")


def _llm_lab_delete_prompt(payload: dict) -> dict:
    return _llm_lab_delete_list_item(LLM_LAB_PROMPTS_FILE, payload, "prompt")


def _llm_lab_save_preset(payload: dict) -> dict:
    return _llm_lab_upsert_list_item(LLM_LAB_PRESETS_FILE, payload, "preset", "Untitled preset")


def _llm_lab_delete_preset(payload: dict) -> dict:
    return _llm_lab_delete_list_item(LLM_LAB_PRESETS_FILE, payload, "preset")


def _llm_lab_compare_run(payload: dict) -> dict:
    result = _ollama_compare(payload)
    if result.get("ok"):
        run = _llm_lab_append_run({"type": "compare", "request": payload, "result_count": len(result.get("results", [])), "run_id": result.get("run_id")})
        for row in result.get("results", []):
            _lab_record_trace({"type": "generation", "run_id": result.get("run_id"), "request": {"model": row.get("model"), "system": payload.get("system", ""), "prompt": payload.get("prompt", ""), "options": payload.get("options") or {}, "format": payload.get("format", "")}, "result": row, "score": None, "tags": ["compare"]})
        result["lab_run_id"] = run.get("id")
    return result


def _llm_lab_matrix_run(payload: dict) -> dict:
    models = payload.get("models") or []
    cases = payload.get("cases") or []
    presets = payload.get("presets") or [{"name": "default", "options": payload.get("options") or {}}]
    if not models or not isinstance(models, list):
        return {"ok": False, "error": "Select at least one model"}
    if not cases or not isinstance(cases, list):
        return {"ok": False, "error": "Add at least one prompt/case"}
    if not isinstance(presets, list) or not presets:
        presets = [{"name": "default", "options": payload.get("options") or {}}]
    run_id = _lab_id("matrix")
    rows = []
    for preset in presets[:4]:
        if not isinstance(preset, dict):
            continue
        preset_name = preset.get("name") or preset.get("id") or "preset"
        options = dict(payload.get("options") or {})
        options.update(preset.get("options") if isinstance(preset.get("options"), dict) else {})
        for case in cases[:40]:
            if not isinstance(case, dict):
                case = {"prompt": str(case)}
            prompt = str(case.get("prompt") or "").strip()
            if not prompt:
                continue
            for model in models[:6]:
                req = {"model": str(model), "system": case.get("system") or payload.get("system") or "", "prompt": prompt, "options": options, "timeout": payload.get("timeout") or 180, "format": case.get("format") or payload.get("format") or ""}
                result = _generate_any(req)
                scoring = _lab_score_response(result.get("response", "") if result.get("ok") else "", case.get("criteria") or {})
                row = {"run_id": run_id, "preset": preset_name, "case_name": case.get("name") or prompt[:60], "model": str(model), "ok": bool(result.get("ok")), "score": scoring["score"] if result.get("ok") else 0, "checks": scoring["checks"], "result": result}
                rows.append(row)
                _lab_record_trace({"type": "matrix-cell", "run_id": run_id, "preset": preset_name, "case": case, "request": req, "result": result, "score": row["score"], "checks": row["checks"], "tags": ["matrix"] + (case.get("tags") or [])})
    summary: dict[str, dict] = {}
    for row in rows:
        key = row["model"] + " · " + row["preset"]
        rec = summary.setdefault(key, {"model": row["model"], "preset": row["preset"], "scores": [], "ok": 0, "errors": 0})
        rec["scores"].append(row["score"])
        if row["ok"]: rec["ok"] += 1
        else: rec["errors"] += 1
    summary_rows = []
    for rec in summary.values():
        scores = rec.pop("scores")
        rec["avg_score"] = round(sum(scores) / len(scores), 1) if scores else 0
        rec["cells"] = len(scores)
        summary_rows.append(rec)
    summary_rows.sort(key=lambda x: x.get("avg_score", 0), reverse=True)
    run = _llm_lab_append_run({"type": "matrix", "run_id": run_id, "models": models[:6], "case_count": len(cases[:40]), "preset_count": len(presets[:4]), "summary": summary_rows})
    return {"ok": True, "run_id": run_id, "lab_run_id": run.get("id"), "summary": summary_rows, "rows": rows}


def _llm_lab_trace_action(payload: dict) -> dict:
    _llm_lab_ensure()
    action = payload.get("action") or ""
    traces = _json_load_file(LLM_LAB_TRACES_FILE, [])
    if not isinstance(traces, list):
        traces = []
    if action == "delete":
        trace_id = str(payload.get("id") or "")
        before = len(traces)
        traces = [t for t in traces if t.get("id") != trace_id]
        _json_save_file(LLM_LAB_TRACES_FILE, traces)
        return {"ok": True, "deleted": before - len(traces), "count": len(traces)}
    if action == "clear":
        if payload.get("confirm") != "CLEAR":
            return {"ok": False, "error": "Send confirm=CLEAR to clear traces"}
        _json_save_file(LLM_LAB_TRACES_FILE, [])
        return {"ok": True, "deleted": len(traces), "count": 0}
    if action == "tag":
        trace_id = str(payload.get("id") or "")
        tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
        for trace in traces:
            if trace.get("id") == trace_id:
                trace["tags"] = sorted(set((trace.get("tags") or []) + tags))
                trace["updated_at"] = _lab_now()
        _json_save_file(LLM_LAB_TRACES_FILE, traces)
        return {"ok": True, "count": len(traces)}
    return {"ok": False, "error": "Unknown trace action"}


def _llm_lab_export_state() -> dict:
    _llm_lab_ensure()
    evals = _json_load_file(LLM_LAB_EVALS_FILE, [])
    traces = _json_load_file(LLM_LAB_TRACES_FILE, [])
    arena = _json_load_file(LLM_LAB_ARENA_FILE, {"matches": [], "scores": {}})
    router = _json_load_file(LLM_LAB_ROUTER_FILE, {"rules": [], "fallbacks": []})
    prompts = _json_load_file(LLM_LAB_PROMPTS_FILE, [])
    presets = _json_load_file(LLM_LAB_PRESETS_FILE, [])
    runs = _json_load_file(LLM_LAB_RUNS_FILE, [])
    return {"ok": True, "generated_at": _lab_now(), "workspace": str(LLM_LAB_DIR), "eval_suites": evals, "traces": traces, "arena": arena, "router": router, "prompt_library": prompts, "settings_presets": presets, "runs": runs}

def _llm_lab_state() -> dict:
    _llm_lab_ensure()
    evals = _json_load_file(LLM_LAB_EVALS_FILE, [])
    traces = _json_load_file(LLM_LAB_TRACES_FILE, [])
    arena = _json_load_file(LLM_LAB_ARENA_FILE, {"matches": [], "scores": {}})
    router = _json_load_file(LLM_LAB_ROUTER_FILE, {"rules": [], "fallbacks": []})
    prompts = _json_load_file(LLM_LAB_PROMPTS_FILE, [])
    presets = _json_load_file(LLM_LAB_PRESETS_FILE, [])
    runs = _json_load_file(LLM_LAB_RUNS_FILE, [])
    ollama = _get_ollama_models()
    ggufs = _scan_gguf_models()
    if not isinstance(prompts, list):
        prompts = []
    if not isinstance(presets, list):
        presets = []
    if not isinstance(runs, list):
        runs = []
    return {
        "ok": True,
        "workspace": str(LLM_LAB_DIR),
        "eval_suites": evals if isinstance(evals, list) else [],
        "recent_traces": traces[:100] if isinstance(traces, list) else [],
        "recent_runs": runs[:50],
        "arena": arena if isinstance(arena, dict) else {"matches": [], "scores": {}},
        "router": router if isinstance(router, dict) else {"rules": [], "fallbacks": []},
        "prompt_library": prompts,
        "settings_presets": presets,
        "leaderboard": _llm_lab_leaderboard(traces if isinstance(traces, list) else [], arena if isinstance(arena, dict) else {}),
        "features": ["single-model generation", "side-by-side compare", "prompt library", "settings presets", "eval suites", "prompt/settings matrix", "trace browser", "dataset promotion", "blind arena", "HF GGUF exact-file pull", "router lab", "leaderboard", "JSON/regex criteria"],
        "models": [{"id": "ollama:" + (m.get("name") or m.get("model") or ""), "backend": "ollama", "name": m.get("name") or m.get("model"), "size_gb": m.get("size_gb"), "quantization_level": m.get("quantization_level", ""), "family": m.get("family", "")} for m in ollama] + ggufs,
        "gguf_folders": _gguf_load_dirs(),
        "hf_download_dir": str(HF_DOWNLOAD_DIR),
        "counts": {"eval_suites": len(evals) if isinstance(evals, list) else 0, "traces": len(traces) if isinstance(traces, list) else 0, "arena_matches": len((arena or {}).get("matches", [])) if isinstance(arena, dict) else 0, "prompts": len(prompts), "presets": len(presets), "runs": len(runs)},
    }


def _llm_lab_save_eval(payload: dict) -> dict:
    _llm_lab_ensure()
    suites = _json_load_file(LLM_LAB_EVALS_FILE, [])
    if not isinstance(suites, list):
        suites = []
    suite = payload.get("suite") if isinstance(payload.get("suite"), dict) else dict(payload)
    suite.setdefault("id", _lab_id("suite"))
    suite.setdefault("name", "Untitled eval suite")
    suite.setdefault("created_at", _lab_now())
    suite["updated_at"] = _lab_now()
    cases = suite.get("cases") or []
    if not isinstance(cases, list):
        cases = []
    cleaned = []
    for c in cases:
        if not isinstance(c, dict):
            continue
        prompt = str(c.get("prompt") or "").strip()
        if not prompt:
            continue
        cleaned.append({
            "id": c.get("id") or _lab_id("case"),
            "name": c.get("name") or prompt[:60],
            "system": c.get("system") or "",
            "prompt": prompt,
            "criteria": c.get("criteria") if isinstance(c.get("criteria"), dict) else {},
            "tags": c.get("tags") if isinstance(c.get("tags"), list) else [],
        })
    suite["cases"] = cleaned
    suites = [x for x in suites if x.get("id") != suite.get("id")]
    suites.insert(0, suite)
    _json_save_file(LLM_LAB_EVALS_FILE, suites[:100])
    return {"ok": True, "suite": suite, "count": len(suites)}


def _llm_lab_run_eval(payload: dict) -> dict:
    suite = payload.get("suite") if isinstance(payload.get("suite"), dict) else {}
    models = payload.get("models") or []
    if not models or not isinstance(models, list):
        return {"ok": False, "error": "Select at least one model"}
    cases = suite.get("cases") or payload.get("cases") or []
    if not cases:
        return {"ok": False, "error": "Add at least one eval case"}
    options = payload.get("options") or {}
    timeout = payload.get("timeout") or 180
    run_id = _lab_id("eval")
    rows = []
    for case in cases[:50]:
        if not isinstance(case, dict) or not str(case.get("prompt") or "").strip():
            continue
        for model in models[:6]:
            req = {"model": str(model), "system": case.get("system") or suite.get("system") or "", "prompt": case.get("prompt") or "", "options": options, "timeout": timeout}
            result = _generate_any(req)
            scoring = _lab_score_response(result.get("response", "") if result.get("ok") else "", case.get("criteria") or {})
            row = {"case_id": case.get("id"), "case_name": case.get("name") or "Case", "model": str(model), "ok": bool(result.get("ok")), "score": scoring["score"] if result.get("ok") else 0, "checks": scoring["checks"], "result": result}
            rows.append(row)
            _lab_record_trace({"type": "eval-case", "run_id": run_id, "suite_name": suite.get("name", "Ad hoc eval"), "case": case, "request": req, "result": result, "score": row["score"], "checks": row["checks"], "tags": ["eval"] + (case.get("tags") or [])})
    by_model = {}
    for row in rows:
        by_model.setdefault(row["model"], []).append(row["score"])
    summary = {m: round(sum(scores) / len(scores), 1) for m, scores in by_model.items() if scores}
    trace = _lab_record_trace({"type": "eval-run", "run_id": run_id, "suite": suite, "models": models[:6], "summary": summary, "row_count": len(rows), "tags": ["eval", "batch"]})
    return {"ok": True, "run_id": run_id, "created_at": _lab_now(), "summary": summary, "rows": rows, "trace_id": trace["id"]}


def _llm_lab_promote_dataset(payload: dict) -> dict:
    kind = payload.get("kind") or "sft"
    model = payload.get("model") or ""
    prompt = payload.get("prompt") or payload.get("user") or ""
    system = payload.get("system") or ""
    assistant = payload.get("assistant") or payload.get("response") or ""
    rejected = payload.get("rejected") or ""
    if not prompt or not assistant:
        return {"ok": False, "error": "Prompt and assistant/response are required"}
    if kind == "dpo" and rejected:
        item = {"prompt": prompt, "chosen": assistant, "rejected": rejected, "model": model, "created_at": _lab_now(), "tags": payload.get("tags") or ["promoted", "dpo"]}
        dpo_path = TUNING_DATASET_DIR / "dpo_pairs.jsonl"
        dpo_path.parent.mkdir(parents=True, exist_ok=True)
        with dpo_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": assistant}])
    result = _tuning_add_examples({"examples": [{"messages": messages, "split": payload.get("split") or "train", "tags": payload.get("tags") or ["promoted", kind]}]})
    return {"ok": True, "tuning": result}


def _llm_lab_arena_start(payload: dict) -> dict:
    models = payload.get("models") or []
    prompt = (payload.get("prompt") or "").strip()
    system = payload.get("system") or ""
    if len(models) < 2:
        return {"ok": False, "error": "Select at least two models for arena mode"}
    if not prompt:
        return {"ok": False, "error": "Prompt is required"}
    import random
    match_id = _lab_id("arena")
    answers = []
    for model in models[:4]:
        req = {"model": str(model), "system": system, "prompt": prompt, "options": payload.get("options") or {}, "timeout": payload.get("timeout") or 180}
        result = _generate_any(req)
        answers.append({"answer_id": _lab_id("ans"), "model": str(model), "result": result})
    blind = [{"answer_id": a["answer_id"], "label": chr(65+i), "response": a["result"].get("response", ""), "ok": a["result"].get("ok"), "error": a["result"].get("error", "")} for i, a in enumerate(random.sample(answers, len(answers)))]
    arena = _json_load_file(LLM_LAB_ARENA_FILE, {"matches": [], "scores": {}})
    if not isinstance(arena, dict):
        arena = {"matches": [], "scores": {}}
    match = {"id": match_id, "created_at": _lab_now(), "prompt": prompt, "system": system, "answers": answers, "blind": blind, "votes": []}
    arena.setdefault("matches", []).insert(0, match)
    arena["matches"] = arena["matches"][:200]
    arena.setdefault("scores", {})
    _json_save_file(LLM_LAB_ARENA_FILE, arena)
    _lab_record_trace({"type": "arena-match", "match_id": match_id, "prompt": prompt, "models": models[:4], "tags": ["arena"]})
    return {"ok": True, "match_id": match_id, "blind": blind}


def _llm_lab_arena_vote(payload: dict) -> dict:
    match_id = payload.get("match_id") or ""
    winner = payload.get("winner") or "tie"
    arena = _json_load_file(LLM_LAB_ARENA_FILE, {"matches": [], "scores": {}})
    if not isinstance(arena, dict):
        arena = {"matches": [], "scores": {}}
    scores = arena.setdefault("scores", {})
    found = None
    for m in arena.get("matches", []):
        if m.get("id") == match_id:
            found = m
            break
    if not found:
        return {"ok": False, "error": "Arena match not found"}
    answer_map = {a.get("answer_id"): a for a in found.get("answers", [])}
    found.setdefault("votes", []).append({"winner": winner, "created_at": _lab_now(), "note": payload.get("note", "")})
    if winner in answer_map:
        win_model = answer_map[winner].get("model", "")
        for aid, answer in answer_map.items():
            model = answer.get("model", "")
            rec = scores.setdefault(model, {"wins": 0, "losses": 0, "ties": 0})
            if aid == winner:
                rec["wins"] += 1
            else:
                rec["losses"] += 1
    else:
        for answer in answer_map.values():
            rec = scores.setdefault(answer.get("model", ""), {"wins": 0, "losses": 0, "ties": 0})
            rec["ties"] += 1
    _json_save_file(LLM_LAB_ARENA_FILE, arena)
    return {"ok": True, "arena": arena}


def _llm_lab_router_save(payload: dict) -> dict:
    router = {"rules": payload.get("rules") if isinstance(payload.get("rules"), list) else [], "fallbacks": payload.get("fallbacks") if isinstance(payload.get("fallbacks"), list) else [], "updated_at": _lab_now()}
    _json_save_file(LLM_LAB_ROUTER_FILE, router)
    return {"ok": True, "router": router}


def _llm_lab_router_test(payload: dict) -> dict:
    prompt = (payload.get("prompt") or "").lower()
    router = payload.get("router") if isinstance(payload.get("router"), dict) else _json_load_file(LLM_LAB_ROUTER_FILE, {"rules": [], "fallbacks": []})
    chosen = None
    reason = "fallback"
    for rule in router.get("rules", []):
        if not isinstance(rule, dict):
            continue
        terms = [t.strip().lower() for t in str(rule.get("match") or "").split(",") if t.strip()]
        if terms and any(t in prompt for t in terms):
            chosen = rule.get("model")
            reason = "matched: " + ", ".join([t for t in terms if t in prompt][:3])
            break
    if not chosen:
        fallbacks = router.get("fallbacks") or []
        chosen = fallbacks[0] if fallbacks else ""
    return {"ok": True, "model": chosen, "reason": reason, "router": router}


def _hf_model_search(payload: dict) -> dict:
    import urllib.parse
    query = (payload.get("query") or "GGUF").strip() or "GGUF"
    limit = int(payload.get("limit") or 25)
    limit = max(1, min(limit, 50))
    url = "https://huggingface.co/api/models?search=" + urllib.parse.quote(query) + "&sort=downloads&direction=-1&limit=" + str(limit)
    data = _hf_api_request(url)
    models = []
    for item in data if isinstance(data, list) else []:
        models.append({"id": item.get("modelId"), "downloads": item.get("downloads"), "likes": item.get("likes"), "tags": item.get("tags", [])[:12], "pipeline_tag": item.get("pipeline_tag", "")})
    return {"ok": True, "query": query, "models": models}


def _hf_gguf_options(payload: dict) -> dict:
    repo_id = (payload.get("repo_id") or "").strip()
    revision = payload.get("revision") or "main"
    if not repo_id or "/" not in repo_id:
        return {"ok": False, "error": "Enter a repo id like owner/name"}
    files = _tuning_hf_files({"repo_id": repo_id, "revision": revision, "repo_type": "model"})
    if not files.get("ok"):
        return files
    options = []
    for f in files.get("files", []):
        path = f.get("path", "")
        if not path.lower().endswith(".gguf"):
            continue
        name = Path(path).name
        lower = name.lower()
        options.append({
            "path": path,
            "name": name,
            "size": f.get("size") or 0,
            "size_gb": round((f.get("size") or 0) / (1024 ** 3), 2) if f.get("size") else 0,
            "quantization_level": _gguf_quant_from_name(name),
            "is_projector": lower.startswith("mmproj") or "mmproj" in lower,
            "download_url": _hf_download_url(repo_id, revision, path, "model"),
        })
    options.sort(key=lambda x: (x.get("is_projector", False), x.get("quantization_level") or "", x.get("name", "")))
    recommended = ""
    for preferred in ("Q4_K_M", "Q5_K_M", "Q6_K", "Q8_0"):
        hit = next((o for o in options if o.get("quantization_level") == preferred and not o.get("is_projector")), None)
        if hit:
            recommended = hit.get("path", "")
            break
    return {"ok": True, "repo_id": repo_id, "revision": revision, "options": options, "recommended": recommended, "count": len(options)}


def _llm_lab_hf_pull_stream(handler, payload: dict) -> None:
    repo_id = (payload.get("repo_id") or "").strip()
    revision = payload.get("revision") or "main"
    files = payload.get("files") or []
    dest = payload.get("dest") or str(HF_DOWNLOAD_DIR)
    if not repo_id or not files:
        handler.wfile.write(b'data: {"ok":false,"error":"repo_id and files are required"}\n\n'); handler.wfile.flush(); return
    if len(files) > 20:
        handler.wfile.write(b'data: {"ok":false,"error":"Select 20 files or fewer per pull"}\n\n'); handler.wfile.flush(); return
    dest_root = Path(os.path.expanduser(dest)) / _tuning_slug(repo_id.replace("/", "--"))
    dest_root.mkdir(parents=True, exist_ok=True)
    downloaded = []
    import urllib.request as _ur
    for i, filename in enumerate(files, 1):
        filename = str(filename).lstrip("/")
        out = dest_root / filename
        if not str(out.resolve()).startswith(str(dest_root.resolve())):
            handler.wfile.write(b'data: {"error":"invalid filename"}\n\n'); handler.wfile.flush(); return
        out.parent.mkdir(parents=True, exist_ok=True)
        event = {"status": "downloading", "file": filename, "index": i, "total": len(files), "percent": int((i - 1) / max(1, len(files)) * 100)}
        handler.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); handler.wfile.flush()
        url = _hf_download_url(repo_id, revision, filename, "model")
        try:
            req = _ur.Request(url, headers={"User-Agent": "devmclovin-llm-lab"})
            token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with _ur.urlopen(req, timeout=60) as resp, out.open("wb") as fh:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            downloaded.append(str(out))
            handler.wfile.write(("data: " + json.dumps({"status": "downloaded", "file": filename, "path": str(out), "percent": int(i / max(1, len(files)) * 100)}) + "\n\n").encode()); handler.wfile.flush()
        except Exception as e:
            handler.wfile.write(("data: " + json.dumps({"error": str(e), "file": filename}) + "\n\n").encode()); handler.wfile.flush()
    # Automatically add the repo root to GGUF scan folders when GGUFs were downloaded.
    if any(x.lower().endswith(".gguf") for x in downloaded):
        _gguf_save_dir(str(dest_root))
    handler.wfile.write(("data: " + json.dumps({"status": "done", "ok": True, "downloaded": downloaded, "folder_added": str(dest_root), "percent": 100}) + "\n\n").encode()); handler.wfile.flush()


def llm_lab_page() -> str:
    """Placeholder for the LLM Lab. The real Lab is moving to its own Docker
    container on this host, fronted by Caddy (Caddy will route /llm-lab straight
    to the container). Until that container is live this renders a 'coming soon'
    notice in the shared shell. The old llm_lab.html and /api/llm-lab/* handlers
    stay in the repo unused — swapping the Lab back in is a one-function change."""
    body = (
        '<div class="hero" style="padding:2rem 0 1rem"><h1>🧪 LLM Lab</h1></div>'
        '<div class="empty-state" style="margin-top:1rem">'
        '<p style="font-size:1.05rem;color:var(--text)">🚧 The LLM Lab is moving to its own service.</p>'
        '<p style="margin-top:0.6rem">It will run in a separate Docker container on this machine, '
        'routed here by Caddy. Check back soon.</p>'
        '</div>'
    )
    return html_page("LLM Lab", body, active_nav="llm-lab")

def model_tuning_page() -> str:
    tuning_html = SITE_DIR / "model_tuning.html"
    if tuning_html.exists():
        return inject_nav(tuning_html.read_text(), "model-tuning")
    return "<html><body><h1>Model tuning page not found</h1></body></html>"

def notes_page() -> str:
    """Serve the standalone notes.html page (with shared nav injected)."""
    notes_html = SITE_DIR / "notes.html"
    if notes_html.exists():
        with open(notes_html, "r") as f:
            return inject_nav(f.read(), "notes")
    return "<html><body><h1>Notes page not found</h1></body></html>"


def inbox_page() -> str:
    """Serve the standalone inbox.html page (with shared nav injected)."""
    inbox_html = SITE_DIR / "inbox.html"
    if inbox_html.exists():
        with open(inbox_html, "r") as f:
            return inject_nav(f.read(), "inbox")
    return "<html><body><h1>Inbox page not found</h1></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    def _get_query_param(self, key: str):
        path = self.path
        if "?" not in path:
            return None
        qs = path.split("?", 1)[1]
        for pair in qs.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k == key:
                    from urllib.parse import unquote
                    return unquote(v)
        return None

    def _send_redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        import urllib.parse
        # Parse query string
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)
        category = qs.get("category", [""])[0]

        if path == "/":
            content = home_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/briefings":
            content = briefings_page(category=category or None).encode()
            self._respond(200, "text/html", content)
        elif path.startswith("/briefing/"):
            date = path.split("/briefing/")[1]
            content = briefing_detail_page(date, category=category).encode()
            self._respond(200, "text/html", content)
        elif path == "/hermes":
            content = hermes_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/runbooks":
            content = runbooks_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/api/models":
            _data, _err = fetch_models()
            if _err:
                self._respond(503, "application/json", json.dumps({"error": _err, "models": []}).encode())
            else:
                self._respond(200, "application/json", json.dumps(_data).encode())
        elif path == "/models":
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            models_data, err = fetch_models()
            if err:
                body = ('<div class="hero" style="padding:2rem 0 1rem"><h1>Models</h1></div>'
                        '<div class="empty-state" style="margin-top:2rem"><p>📦 Model comparison is '
                        'temporarily unavailable.<br><small>' + html.escape(err) + '</small></p></div>')
                self._respond(200, "text/html", html_page("Models", body, active_nav="models").encode())
            else:
                models_json = json.dumps(models_data)
                html_template = (SITE_DIR / "model_comparison.html").read_text()
                html_template = inject_nav(html_template, "models")
                content = html_template.replace("__MODELS_JSON__", models_json).encode()
                self._respond(200, "text/html", content)
        elif path == "/model-tuning":
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            content = model_tuning_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/llm-lab":
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            content = llm_lab_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/api/llm-lab/state":
            self._respond(200, "application/json", json.dumps(_llm_lab_state()).encode())
        elif path == "/api/llm-lab/export":
            self._respond(200, "application/json", json.dumps(_llm_lab_export_state()).encode())
        elif path == "/api/tuning/state":
            self._respond(200, "application/json", json.dumps(_tuning_state()).encode())
        elif path == "/api/tuning/messages/export":
            body = _tuning_dataset_jsonl().encode()
            self._respond(200, "application/x-ndjson", body)
        elif path == "/cron":
            content = cron_page().encode()
            self._respond(200, "text/html", content)
        elif path.startswith("/cron/"):
            # /cron/<job_id> or /cron/<job_id>/<filename>
            parts = path.split("/")
            # parts: ["", "cron", "job_id"] or ["", "cron", "job_id", "filename"]
            if len(parts) == 3:
                job_id = parts[2]
                status_code, page = cron_job_detail_page(job_id)
                self._respond(status_code, "text/html", page.encode())
            elif len(parts) >= 4:
                job_id = parts[2]
                filename = "/".join(parts[3:])
                content = cron_output_preview_page(job_id, filename).encode()
                self._respond(200, "text/html", content)
            else:
                self._respond(404, "text/plain", b"Not Found")
        elif path == "/api/ollama/models":
            models = _get_ollama_models()
            self._respond(200, "application/json", json.dumps({"models": models}).encode())
        elif path == "/api/ollama/health" or path == "/api/ollama/status":
            self._respond(200, "application/json", json.dumps(_ollama_health()).encode())
        elif path == "/api/gguf/models":
            self._respond(200, "application/json", json.dumps({"models": _scan_gguf_models(), "folders": _gguf_load_dirs(), "health": _gguf_health()}).encode())
        elif path == "/api/gguf/health":
            self._respond(200, "application/json", json.dumps(_gguf_health()).encode())
        elif path == "/api/notes" or path.startswith("/api/notes/"):
            self._proxy_notes(self.command)
            return
        elif path == "/api/inbox" or path.startswith("/api/inbox/"):
            self._proxy_inbox()
            return
        elif path == "/notes":
            content = notes_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/inbox":
            content = inbox_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/tunnel":
            content = cloudflare_tunnel_page().encode()
            self._respond(200, "text/html", content)
        elif path.startswith("/api/briefings/search"):
            q = self._get_query_param("q")
            if not q:
                self._respond(200, "application/json", b"[]")
                return
            archive = BriefingArchive(str(BRIEFING_DB))
            results = archive.search_articles(q, limit=50)
            out = []
            for r in results:
                out.append({
                    "id": r["id"],
                    "title": r["title"],
                    "source_name": r["source_name"],
                    "source_url": r.get("source_url", ""),
                    "summary": r.get("summary", ""),
                    "snippet": r.get("snippet", ""),
                    "categories": r.get("categories", ""),
                    "briefing_date": r["briefing_date"],
                    "full_date": r.get("full_date", ""),
                })
            self._respond(200, "application/json", json.dumps(out).encode())
        elif path == "/logs":
            content = logs_page(qs.get("tab", [""])[0]).encode()
            self._respond(200, "text/html", content)
        elif path == "/logs/router":
            self.send_response(301)
            self.send_header("Location", "/logs?tab=router")
            self.end_headers()
        elif path == "/status":
            content = status_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/portfolio":
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            content = portfolio_page().encode()
            self._respond(200, "text/html", content)
        elif path.startswith("/api/service/"):
            self._proxy_api()
        elif path == "/api/status":
            self._proxy_api()
        elif path == "/projects":
            content = projects_page().encode()
            self._respond(200, "text/html", content)
        elif path.startswith("/projects/") and path.endswith("/logs"):
            name = path.split("/projects/")[1].split("/logs")[0]
            content = project_logs_page(name).encode()
            self._respond(200, "text/html", content)
        elif path.startswith("/projects/") and path.endswith("/config"):
            name = path.split("/projects/")[1].split("/config")[0]
            content = project_config_page(name).encode()
            self._respond(200, "text/html", content)
        elif path == "/api/projects/launcher":
            data = get_project_launcher_data()
            self._respond(200, "application/json", json.dumps(data).encode())
        elif path == "/health":
            self._respond(200, "text/plain", b"ok")
        elif path == "/bookmarks":
            content = bookmarks_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/disk-cleanup":
            content = disk_cleanup_page().encode()
            self._respond(200, "text/html", content)
        else:
            self._respond(404, "text/plain", b"Not Found")

    def do_POST(self):
        import urllib.parse
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        params = urllib.parse.parse_qs(raw)
        get = lambda k: params.get(k, [""])[0]
        path = self.path.rstrip("/") or "/"

        # Check if client wants JSON response (new kanban page) or redirect (old-style form)
        want_json = "application/json" in self.headers.get("Accept", "")

        if path == "/kanban/create":
            title = get("title").strip()
            assignee = get("assignee").strip()
            status = get("status").strip() or "running"
            if title:
                tid = kanban_create(title, assignee=assignee)
                if tid and status != "running":
                    kanban_move(tid, status)
            if want_json:
                tasks_json = json.dumps(load_kanban_tasks_full()).encode()
                self._respond(200, "application/json", tasks_json)
            else:
                self._send_redirect("/kanban")
        elif path == "/kanban/move":
            tid = get("task_id"); st = get("status")
            if tid and st: kanban_move(tid, st)
            if want_json:
                tasks_json = json.dumps(load_kanban_tasks_full()).encode()
                self._respond(200, "application/json", tasks_json)
            else:
                self._send_redirect("/kanban")
        elif path == "/kanban/comment":
            tid = get("task_id"); author = get("author").strip() or "anon"; body_text = get("body").strip()
            if tid and body_text: kanban_comment(tid, author, body_text)
            if want_json:
                tasks_json = json.dumps(load_kanban_tasks_full()).encode()
                self._respond(200, "application/json", tasks_json)
            else:
                self._send_redirect("/kanban")
        elif path == "/api/llm-lab/run/compare":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_compare_run(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/matrix/run":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_matrix_run(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/prompts/save":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_save_prompt(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/prompts/delete":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_delete_prompt(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/presets/save":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_save_preset(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/presets/delete":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_delete_preset(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/traces/action":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_trace_action(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/eval/save":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_save_eval(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/eval/run":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_run_eval(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/dataset/promote":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_promote_dataset(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/arena/start":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_arena_start(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/arena/vote":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_arena_vote(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/router/save":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_router_save(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/router/test":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _llm_lab_router_test(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/hf/search":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _hf_model_search(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(502, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/hf/ggufs":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _hf_gguf_options(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(502, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/llm-lab/hf/pull":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            _llm_lab_hf_pull_stream(self, body_data)
            return
        elif path == "/api/tuning/messages/add":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _tuning_add_examples(body_data)
                self._respond(200, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/tuning/messages/import":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _tuning_import_text(body_data.get("text", ""))
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/tuning/messages/delete":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _tuning_delete_example(str(body_data.get("id", "")))
                self._respond(200, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/tuning/hf/files":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _tuning_hf_files(body_data)
                self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(502, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/tuning/hf/pull":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            _tuning_hf_pull_stream(self, body_data)
            return
        elif path == "/api/tuning/config/write":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
                result = _tuning_write_config(body_data)
                self._respond(200, "application/json", json.dumps(result).encode())
            except Exception as e:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/ollama/pull":
            import subprocess as _sp
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            model_name = data.get("name", "").strip()
            if not model_name:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"Model name required"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                proc = _sp.Popen(
                    ["ollama", "pull", model_name],
                    stdout=_sp.PIPE, stderr=_sp.STDOUT,
                    text=True, bufsize=1
                )
                import re as _re
                _ansi_re = _re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\[[0-9;?]*[KhlGJ]')
                _err_re = _re.compile(r'(?:^error|error[: ])', _re.IGNORECASE)
                success = False
                for line_p in proc.stdout:
                    line_p = _ansi_re.sub('', line_p).strip()
                    if not line_p:
                        continue
                    event = {"status": line_p}
                    # Check for error before regex matching
                    if _err_re.search(line_p):
                        event["error"] = line_p
                        payload = json.dumps(event)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                        continue
                    m = _re.search(r'(\d+)%', line_p)
                    if m:
                        event["percent"] = int(m.group(1))
                    m2 = _re.search(r'([\d.]+)\s*(GB|MB|KB|B)\s*/\s*([\d.]+)', line_p)
                    if m2:
                        event["downloading"] = True
                    if "success" in line_p.lower():
                        event["status"] = "success"
                        success = True
                    payload = json.dumps(event)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                proc.wait()
                if proc.returncode != 0:
                    self.wfile.write(f'data: {{"error":"ollama pull exited with code {proc.returncode}"}}\n\n'.encode())
                elif not success:
                    self.wfile.write(b'data: {"status":"success"}\n\n')
                self.wfile.flush()
            except Exception as e:
                self.wfile.write(f'data: {{"error":"{str(e)}"}}\n\n'.encode())
                self.wfile.flush()
            return
        elif path == "/api/ollama/models":
            models = _get_ollama_models()
            self._respond(200, "application/json", json.dumps({"models": models}).encode())
            return
        elif path == "/api/ollama/models/delete":
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            model_name = data.get("name", "").strip()
            if not model_name:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": "Missing model name"}).encode())
                return
            try:
                import subprocess as _sp2
                result = _sp2.run(["ollama", "rm", model_name], capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    self._respond(200, "application/json", json.dumps({"ok": True}).encode())
                else:
                    err = result.stderr.strip() or result.stdout.strip()
                    err = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', err).strip()
                    err = re.sub(r'\x1b\[[0-9]*[GK]', '', err).strip()
                    self._respond(200, "application/json", json.dumps({"ok": False, "error": err or f"exit code {result.returncode}"}).encode())
            except FileNotFoundError:
                self._respond(500, "application/json", json.dumps({"ok": False, "error": "ollama CLI not found"}).encode())
            except _sp2.TimeoutExpired:
                self._respond(500, "application/json", json.dumps({"ok": False, "error": "ollama rm timed out after 30s"}).encode())
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return
        elif path == "/api/gguf/folders":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            result = _gguf_save_dir(body_data.get("path", ""))
            self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            return
        elif path == "/api/gguf/folders/remove":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            result = _gguf_remove_dir(body_data.get("path", ""))
            self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            return
        elif path == "/api/gguf/generate":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            result = _gguf_generate(body_data)
            self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            return
        elif path == "/api/gguf/compare":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            result = _ollama_compare(body_data)
            self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            return
        elif path == "/api/ollama/generate":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            result = _generate_any(body_data)
            self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            return
        elif path == "/api/ollama/compare":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            result = _ollama_compare(body_data)
            self._respond(200 if result.get("ok") else 400, "application/json", json.dumps(result).encode())
            return
        elif path == "/api/ollama/benchmark":
            try:
                body_data = json.loads(raw) if raw.strip() else {}
            except Exception:
                body_data = {}
            model = body_data.get("model", "").strip()
            prompt = body_data.get("prompt", "Hello").strip()
            if not model:
                self._respond(400, "application/json", json.dumps({"error": "Missing model name"}).encode())
            else:
                result = _ollama_benchmark(model, prompt)
                self._respond(200, "application/json", json.dumps(result).encode())
            return
        elif path == "/api/notes" or path.startswith("/api/notes/"):
            self._proxy_notes(self.command, body=raw.encode() if raw else None)
            return
        elif path == "/api/inbox" or path.startswith("/api/inbox/"):
            self._proxy_inbox(body=raw.encode() if raw else None)
            return
        elif path.startswith("/api/proxy/commands/"):
            # Proxy to commands API on port 8092 with API key auth
            action = path[len("/api/proxy/commands/"):]
            valid_actions = ("restart-dashboard", "restart-router", "pull-github", "run-backup")
            if action not in valid_actions:
                self._respond(400, "application/json", json.dumps({"error": "invalid action", "valid": list(valid_actions)}).encode())
                return
            import urllib.request as _ur
            try:
                commands_url = f"http://localhost:8092/api/commands/{action}"
                req_data = raw.encode() if raw else b"{}"
                req = _ur.Request(commands_url, data=req_data, method="POST")
                req.add_header("Content-Type", "application/json")
                _commands_key = _load_env_var("COMMANDS_API_KEY") or "hermes-commands-api-key-change-me"
                req.add_header("X-API-Key", _commands_key)
                resp = _ur.urlopen(req, timeout=65)
                proxied_body = resp.read()
                self._respond(resp.status, "application/json", proxied_body)
            except Exception as e:
                self._respond(502, "application/json", json.dumps({"error": f"Commands API unreachable: {str(e)}"}).encode())
            return
        elif path == "/bookmarks/toggle":
            sid = get("id")
            btype = get("type") or "saved"
            story = {
                "title": get("title"),
                "source_name": get("source_name"),
                "source_url": get("source_url"),
                "body": get("body"),
                "date": get("date"),
            }
            _toggle_bookmark(sid, story, btype)
            active = _is_bookmarked(sid, btype)
            self._respond(200, "application/json", json.dumps({"ok": True, "active": active}).encode())
        elif path == "/bookmarks/remove":
            sid = get("id")
            btype = get("type") or "saved"
            bookmarks = _load_bookmarks()
            if sid:
                lst = bookmarks.get(btype, [])
                bookmarks[btype] = [bm for bm in lst if bm.get("id") != sid]
                _save_bookmarks(bookmarks)
            self._send_redirect("/bookmarks")
        elif path.startswith("/api/service/") and path.endswith("/restart"):
            self._proxy_api()
        elif path.startswith("/projects/") and path.endswith("/config"):
            name = path.split("/projects/")[1].split("/config")[0]
            config_json = get("config_json")
            ok, err = project_config_save(name, config_json)
            if ok:
                content = project_config_page(name, saved=True).encode()
                self._respond(200, "text/html", content)
            else:
                content = project_config_page(name, error=err).encode()
                self._respond(200, "text/html", content)
        elif path.startswith("/api/projects/") and path.endswith("/restart"):
            name = path.split("/api/projects/")[1].split("/restart")[0]
            result = project_restart(name)
            self._respond(200 if result["ok"] else 500, "application/json", json.dumps(result).encode())
        else:
            self.send_response(404); self.end_headers()

    def do_PUT(self):
        """Handle PUT requests — proxy config edits to API server."""
        path = self.path.rstrip("/") or "/"
        if path.startswith("/api/service/") and path.endswith("/config"):
            self._proxy_api()
        elif path.startswith("/api/status"):
            self._proxy_api()
        elif path.startswith("/api/inbox"):
            self._proxy_inbox()
        else:
            self.send_response(405); self.end_headers()

    def do_OPTIONS(self):
        """Handle CORS preflight for /api/notes/* and /api/service/*"""
        if self.path.startswith("/api/notes"):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()
        elif self.path.startswith("/api/ollama") or self.path.startswith("/api/gguf") or self.path.startswith("/api/llm-lab") or self.path.startswith("/api/tuning"):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()
        elif self.path.startswith("/api/service/") or self.path.startswith("/api/status"):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()
        elif self.path.startswith("/api/inbox"):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()
        else:
            self.send_response(405); self.end_headers()

    def do_PATCH(self):
        path = self.path.rstrip("/") or "/"
        if path.startswith("/api/notes/"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else None
            self._proxy_notes("PATCH", body=body)
        else:
            self.send_response(405); self.end_headers()

    def do_DELETE(self):
        path = self.path.rstrip("/") or "/"
        if path.startswith("/api/notes/"):
            self._proxy_notes("DELETE")
        elif path.startswith("/api/inbox"):
            self._proxy_inbox()
        else:
            self.send_response(405); self.end_headers()

    def _proxy_inbox(self, body: bytes = None):
        """Proxy /api/inbox/* to the Agent Inbox API on 127.0.0.1:8000.

        Strips ONLY the /api/inbox prefix and forwards the remainder (path + query),
        method, body and status verbatim. Adds no auth header — the inbox guide
        specifies none — but relays the client's Content-Type / X-API-Key if present.
        `body` is passed in by do_POST (which already consumed the request body);
        for other methods it is read here.
        """
        import urllib.request as _ur
        from urllib.error import HTTPError
        # /api/inbox            -> /            on the API
        # /api/inbox/api/v1/... -> /api/v1/...  on the API (query preserved)
        rest = self.path[len("/api/inbox"):] or "/"
        url = "http://127.0.0.1:8000" + rest

        if body is None:
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)

        req = _ur.Request(url, data=body, method=self.command)
        for h in ("Content-Type", "X-API-Key"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)

        try:
            with _ur.urlopen(req, timeout=15) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self._respond(502, "application/json", json.dumps({"error": f"Inbox API unreachable: {e}"}).encode())

    def _proxy_notes(self, method: str, body: bytes = None):
        """Proxy /api/notes/* requests to the Notes API on port 8081 with auth."""
        import urllib.request as _ur
        from urllib.error import HTTPError
        # /api/notes      -> /notes on the API
        # /api/notes/xxx  -> /notes/xxx on the API
        api_path = "/notes" + self.path[len("/api/notes"):]  # strip prefix, keep rest + id
        url = f"http://127.0.0.1:8081{api_path}"
        req = _ur.Request(url, method=method)
        _notes_token = _load_env_var("NOTES_API_TOKEN") or "notes-secret-token"
        req.add_header("Authorization", "Bearer " + _notes_token)

        # Forward body for POST/PATCH
        if method in ("POST", "PATCH") and body:
            req.add_header("Content-Type", "application/json")
            req.data = body

        try:
            with _ur.urlopen(req, timeout=10) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except HTTPError as e:
            # Forward the upstream error response body and status
            err_body = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(err_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self._respond(502, "application/json", json.dumps({"error": f"Notes API unreachable: {e}"}).encode())

    def _proxy_api(self):
        """Proxy /api/status and /api/service/* requests to the backend API server on port 9091."""
        import urllib.request as _ur
        from urllib.error import HTTPError
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        upstream_path = parsed.path
        if parsed.query:
            upstream_path += "?" + parsed.query
        url = f"http://127.0.0.1:9091{upstream_path}"

        body = None
        if self.command in ("POST", "PUT", "PATCH"):
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                body = self.rfile.read(length)

        req = _ur.Request(url, data=body, method=self.command)
        if body:
            req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))

        try:
            with _ur.urlopen(req, timeout=15) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self._respond(502, "application/json", json.dumps({"error": f"API server unreachable: {e}"}).encode())

    def _get_query_param(self, key: str) -> str:
        """Parse query string and return a single param value."""
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        return qs.get(key, [""])[0]

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence logs


if __name__ == "__main__":
    import sys
    import socket
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT

    class ReuseHTTPServer(http.server.HTTPServer):
        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            http.server.HTTPServer.server_bind(self)

    server = ReuseHTTPServer((os.environ.get("BIND_HOST", "127.0.0.1"), port), Handler)
    print(f"devmclovin landing page → http://127.0.0.1:{port}")
    server.serve_forever()
