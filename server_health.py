#!/usr/bin/env python3
"""Landing page server for devmclovin.com — dark mode, morning briefings, Hermes link."""

import http.server
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

PORT = 3002
BRIEFING_DIR = Path(os.path.expanduser("~/.hermes/cron/output/7dc1d641173d"))
SITE_DIR = Path(__file__).parent

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

# ── Link Health Check ──
_INTERNAL_DOMAINS = os.environ.get(
    "INTERNAL_DOMAINS", "devmclovin.com,localhost,127.0.0.1,puzzlelabs.app,ssh.devmclovin.com"
).split(",")
_INTERNAL_DOMAINS = [d.strip() for d in _INTERNAL_DOMAINS if d.strip()]
_LINK_HEALTH_CACHE: dict = {}
_LINK_HEALTH_TTL = 600  # 10 minutes

# ── GitHub projects cache ──
_GITHUB_CACHE: dict = {"data": None, "ts": 0, "username": None}
_OPENROUTER_CACHE: dict = {"data": None, "ts": 0}
_CACHE_TTL = 300  # 5 minutes


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


# ── Cloudflare Tunnel API ──────────────────────────────────────────

def _load_cf_credentials() -> tuple:
    """Load CF_API_TOKEN, CF_ACCOUNT_ID, CF_TUNNEL_ID from .env or os.environ."""
    api_token = os.environ.get("CF_API_TOKEN") or _load_env_var("CF_API_TOKEN")
    account_id = os.environ.get("CF_ACCOUNT_ID") or _load_env_var("CF_ACCOUNT_ID")
    tunnel_id = os.environ.get("CF_TUNNEL_ID") or _load_env_var("CF_TUNNEL_ID")
    return api_token, account_id, tunnel_id


def get_cloudflare_tunnel_data() -> dict:
    """Fetch Cloudflare tunnel report with 5-min caching.
    Returns dict with keys: ok, data, error, checked_at, account_id, tunnel_id.
    """
    global _CF_TUNNEL_CACHE
    now = time.time()
    if _CF_TUNNEL_CACHE["data"] is not None and (now - _CF_TUNNEL_CACHE["ts"]) < _CACHE_TTL:
        return _CF_TUNNEL_CACHE["data"]

    api_token, account_id, tunnel_id = _load_cf_credentials()
    if not api_token or not account_id:
        result = {
            "ok": False, "data": None,
            "error": "CF_API_TOKEN and CF_ACCOUNT_ID must be set in ~/.hermes/.env or environment.",
            "checked_at": now, "account_id": account_id, "tunnel_id": tunnel_id,
        }
        _CF_TUNNEL_CACHE = {"data": result, "ts": now}
        return result

    try:
        import asyncio
        _dir = os.path.dirname(os.path.abspath(__file__))
        if _dir not in sys.path:
            sys.path.insert(0, _dir)
        from cloudflare_api import get_full_report

        report = asyncio.run(get_full_report(
            api_token=api_token, account_id=account_id,
            tunnel_id=tunnel_id or None,
        ))

        data = {
            "ok": True,
            "data": {
                "tunnel_id": report.tunnel_id,
                "tunnel_name": report.status.tunnel_name,
                "is_up": report.status.is_up,
                "connections": [
                    {"connection_id": c.connection_id, "client_id": c.client_id,
                     "arch": c.arch, "version": c.version,
                     "origin_ip": c.origin_ip, "opened_at": c.opened_at}
                    for c in report.status.connections
                ],
                "hostnames": [
                    {"hostname": h.hostname, "service": h.service}
                    for h in report.config.hostnames
                ],
                "port_mappings": [
                    {"protocol": pm.protocol, "host": pm.host, "port": pm.port}
                    for pm in report.port_mappings
                ],
                "access_policies": {
                    "total_policies": report.access_policies.total_policies,
                    "policies": [
                        {"policy_id": p.policy_id, "name": p.name,
                         "decision": p.decision, "include_count": p.include_count,
                         "exclude_count": p.exclude_count, "require_count": p.require_count}
                        for p in report.access_policies.policies
                    ],
                    "types_breakdown": report.access_policies.types_breakdown,
                },
                "last_reconnect_at": report.reconnect.last_reconnect_at,
                "connection_count": report.reconnect.connection_count,
            },
            "error": None, "checked_at": now,
            "account_id": account_id, "tunnel_id": report.tunnel_id,
        }
    except Exception as e:
        data = {
            "ok": False, "data": None, "error": str(e),
            "checked_at": now, "account_id": account_id, "tunnel_id": tunnel_id,
        }

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

.container {
    max-width: 960px;
    margin: 0 auto;
    padding: 2rem;
}

/* ── Hero ── */
.hero {
    text-align: center;
    padding: 3rem 0 2rem;
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
    display: flex;
    gap: 1rem;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    -webkit-overflow-scrolling: touch;
    padding: 0.5rem 0.25rem 1rem;
    margin-bottom: 2rem;
    scrollbar-width: none;
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
}

.scroll-arrow {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    z-index: 10;
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: var(--bg-card);
    border: 2px solid var(--border);
    color: var(--text);
    font-size: 1.1rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: border-color 0.2s, background 0.2s;
}

.scroll-arrow:hover {
    border-color: var(--accent);
    background: #1c2333;
}

.scroll-arrow.left  { left: -42px; }
.scroll-arrow.right { right: -42px; }

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

/* ── Kanban Board ── */
.kanban-board {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1rem;
    margin: 1.5rem 0;
    align-items: start;
}

@media (max-width: 900px) {
    .kanban-board {
        grid-template-columns: repeat(2, 1fr);
    }
}

@media (max-width: 500px) {
    .kanban-board {
        grid-template-columns: 1fr;
    }
}

.kanban-column {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1rem;
    min-height: 200px;
}

.kanban-column h3 {
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
}

.kanban-column h3 .count {
    display: inline-block;
    background: rgba(139, 148, 158, 0.15);
    color: var(--text-muted);
    border-radius: 10px;
    padding: 0.1rem 0.5rem;
    font-size: 0.7rem;
    font-weight: 600;
}

.kanban-card {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.75rem;
    margin-bottom: 0.75rem;
    cursor: pointer;
    transition: border-color 0.2s;
}

.kanban-card:hover {
    border-color: var(--accent);
}

.kanban-card .kc-title {
    font-size: 0.85rem;
    font-weight: 600;
    margin-bottom: 0.35rem;
    line-height: 1.3;
}

.kanban-card .kc-meta {
    font-size: 0.7rem;
    color: var(--text-muted);
    display: flex;
    gap: 0.75rem;
    align-items: center;
    flex-wrap: wrap;
}

.kanban-card .kc-meta .prio {
    font-weight: 600;
}

.kanban-card .kc-body {
    display: none;
    margin-top: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border);
    font-size: 0.8rem;
    color: var(--text-muted);
    line-height: 1.5;
    white-space: pre-wrap;
    max-height: 300px;
    overflow-y: auto;
}

.kanban-card.expanded .kc-body {
    display: block;
}

.kanban-card .kc-comment {
    margin-top: 0.5rem;
    padding: 0.5rem;
    background: var(--bg-card);
    border-radius: 6px;
    font-size: 0.75rem;
}

.kanban-card .kc-comment .comment-author {
    color: var(--accent-hover);
    font-weight: 600;
    margin-bottom: 0.2rem;
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

@media (max-width: 640px) {
    nav { padding: 0 1rem; }
    .container { padding: 1rem; }
    .hero h1 { font-size: 1.8rem; }
    .quicklinks-grid { grid-template-columns: 1fr; }
}

/* ── Link Health Dots ── */
.link-health-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-left: 0.5rem;
    flex-shrink: 0;
    position: relative;
    cursor: help;
    align-self: center;
}
.link-health-dot.ok { background: var(--green); }
.link-health-dot.error {
    background: var(--red);
    animation: health-pulse 2s infinite;
}
.link-health-dot.unknown {
    background: var(--text-muted);
    animation: health-pulse 2s infinite;
}
@keyframes health-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
.link-health-dot[data-tooltip]:hover::after {
    content: attr(data-tooltip);
    position: absolute;
    bottom: 140%;
    left: 50%;
    transform: translateX(-50%);
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 0.3rem 0.6rem;
    border-radius: 6px;
    font-size: 0.7rem;
    white-space: nowrap;
    z-index: 100;
    pointer-events: none;
}
.link-card.link-health-error { border-left: 3px solid var(--red); }

"""

# ═══════════════════════════════════════════════════════════════
#  Quick Links
# ═══════════════════════════════════════════════════════════════

def load_quick_links() -> list[dict]:
    """Load quick links from config file, returning sensible defaults if missing."""
    defaults = [
        {"label": "OpenRouter", "url": "https://openrouter.ai/activity", "emoji": "🤖",
         "description": "AI model usage and spending dashboard"},
        {"label": "GitHub", "url": "https://github.com/VerduzcoTristan", "emoji": "💻",
         "description": "All projects and repositories"},
        {"label": "Cloudflare", "url": "https://dash.cloudflare.com", "emoji": "☁️",
         "description": "DNS, tunnels, and domain management"},
        {"label": "Linear", "url": "https://linear.app", "emoji": "📋",
         "description": "Project and task management"},
        {"label": "Hermes Docs", "url": "https://hermes-agent.nousresearch.com/docs", "emoji": "📘",
         "description": "Hermes Agent configuration and reference"},
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


def quick_links_row() -> str:
    """Render a grid of quick-link cards on the home page."""
    links = load_quick_links()
    if not links:
        return ""

    html = '<div class="section-title">🔗 Quick Links</div>'
    html += '<div class="quicklinks-grid">'
    for link in links:
        health = get_link_health(link["url"])
        card_classes = "link-card"
        dot_html = ""
        if health["status"] != "external":
            if health["status"] == "ok":
                dot_class = "link-health-dot ok"
                tooltip = "Reachable"
            elif health["status"] == "error":
                dot_class = "link-health-dot error"
                card_classes += " link-health-error"
                tooltip = f"Unreachable: {health['error']}"
            else:
                dot_class = "link-health-dot unknown"
                tooltip = "Checking..."
            dot_html = f'<span class="{dot_class}" data-tooltip="{tooltip}" title="{tooltip}"></span>'
        html += f'<a href="{link["url"]}" target="_blank" rel="noopener" class="{card_classes}">'
        html += f'<span class="link-emoji">{link.get("emoji", "🔗")}</span>'
        html += '<span class="link-info">'
        html += f'<div class="link-label">{link["label"]}</div>'
        html += f'<div class="link-desc">{link.get("description", "")}</div>'
        html += '</span>'
        html += dot_html
        html += '</a>'
    html += '</div>'
    return html


# ── Link Health Check helpers ──

def _is_internal_link(url: str) -> bool:
    """Check if a URL's hostname matches any internal domain (exact or subdomain)."""
    from urllib.parse import urlparse
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        hostname_lower = hostname.lower()
        for domain in _INTERNAL_DOMAINS:
            domain_lower = domain.lower()
            if hostname_lower == domain_lower or hostname_lower.endswith("." + domain_lower):
                return True
        return False
    except Exception:
        return False


def _check_single_link(url: str, method: str = "HEAD", timeout: int = 5) -> tuple:
    """HTTP HEAD/GET check. Returns (status, error_message). Falls back to GET on 405/501."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, method=method)
        req.add_header("User-Agent", "devmclovin-link-checker/1.0")
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        if resp.status in (405, 501) and method == "HEAD":
            return _check_single_link(url, method="GET", timeout=timeout)
        if method == "GET":
            resp.read(4096)
        return ("ok", None)
    except urllib.error.HTTPError as e:
        if e.code and e.code < 500:
            return ("ok", None)
        return ("error", f"HTTP {e.code}")
    except urllib.error.URLError as e:
        return ("error", str(e.reason))
    except Exception as e:
        return ("error", str(e))


def get_link_health(url: str) -> dict:
    """Cached health check. Returns {status: 'ok'|'error'|'external'|'unknown', error: str|None}."""
    now = time.time()
    cached = _LINK_HEALTH_CACHE.get(url)
    if cached and (now - cached["timestamp"]) < _LINK_HEALTH_TTL:
        return {"status": cached["status"], "error": cached.get("error")}

    if not _is_internal_link(url):
        result = {"status": "external", "error": None}
    else:
        status, error = _check_single_link(url)
        result = {"status": status, "error": error}

    _LINK_HEALTH_CACHE[url] = {
        "status": result["status"], "error": result["error"], "timestamp": now
    }
    return result


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
        return html_page("Cron Jobs", body, active_nav="hermes")

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

    return html_page("Cron Jobs", body, active_nav="hermes")


def cron_job_detail_page(job_id: str) -> str:
    """Render a detail page for a single cron job showing recent outputs."""
    jobs = load_cron_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)

    body = '<div class="page-back"><a href="/cron">← Back to all cron jobs</a></div>'

    if not job:
        body += f'<div class="empty-state" style="margin-top:2rem"><p>Cron job {job_id} not found.</p></div>'
        return html_page(f"Cron — {job_id}", body, active_nav="hermes")

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

    return html_page(f"Cron — {name}", body, active_nav="hermes")


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



def hermes_page() -> str:
    """Consolidated Hermes dashboard."""
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>Hermes</h1>'
    body += '<p>AI agent dashboard — cron, kanban, briefings.</p></div>'
    body += '<div style="text-align:center;margin:0 0 2rem 0">'
    body += '<a href="https://hermes.devmclovin.com" target="_blank" rel="noopener" class="cta-button">🚀 Open Hermes Web UI →</a></div>'

    body += '<a href=/cron style=text-decoration:none;color:inherit><div class="section-title">⏰ Cron Jobs</div></a>'
    jobs = load_cron_jobs()
    if jobs:
        body += '<div class="briefing-scroll"><button class="scroll-arrow left" onclick="this.nextElementSibling.scrollBy({left:-300,behavior:' + "'smooth'" + '})">◂</button>'
        body += '<div class="briefing-grid">'
        for job in jobs:
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
            body += '</a>'
        body += '</div><button class="scroll-arrow right" onclick="this.previousElementSibling.scrollBy({left:300,behavior:' + "'smooth'" + '})">▸</button></div>'
    else:
        body += '<div class="empty-state"><p>No cron jobs configured.</p></div>'

    body += '<a href=/kanban style=text-decoration:none;color:inherit><div class="section-title" style="margin-top:2rem">📋 Kanban Board</div></a>'
    tasks = load_kanban_tasks()
    if tasks and any(len(v) > 0 for v in tasks.values()):
        body += '<div class="briefing-scroll"><button class="scroll-arrow left" onclick="this.nextElementSibling.scrollBy({left:-300,behavior:' + "'smooth'" + '})">◂</button>'
        body += '<div class="briefing-grid">'
        cols = [("ready","Ready","📥"),("running","Running","⚡"),("blocked","Blocked","🚧"),("done","Done","✅")]
        for col_key, col_label, col_emoji in cols:
            col_tasks = tasks.get(col_key, [])
            body += '<div class="briefing-card">'
            body += '<div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.5rem">'
            body += '<span style="font-size:1.5rem">' + col_emoji + '</span>'
            body += '<span class="card-num" style="font-size:1.8rem;font-weight:700">' + str(len(col_tasks)) + '</span>'
            body += '</div>'
            body += '<h3>' + col_label + '</h3>'
            if col_tasks:
                body += '<div class="card-summary" style="font-size:0.8rem">'
                for t in col_tasks[:3]:
                    body += '&bull; ' + t["title"][:50] + '<br>'
                if len(col_tasks) > 3:
                    body += '<span style="color:var(--text-muted);font-size:0.75rem">+' + str(len(col_tasks)-3) + ' more</span>'
                body += '</div>'
            body += '</div>'
        body += '</div><button class="scroll-arrow right" onclick="this.previousElementSibling.scrollBy({left:300,behavior:' + "'smooth'" + '})">▸</button></div>'
    else:
        body += '<div class="empty-state"><p>No kanban tasks yet.</p></div>'

    body += '<a href=/briefings style=text-decoration:none;color:inherit><div class="section-title" style="margin-top:2rem">📰 Recent Briefings</div></a>'
    files = sorted(glob.glob(str(BRIEFING_DIR / "*.md")), reverse=True)[:8]
    if files:
        body += '<div class="briefing-scroll"><button class="scroll-arrow left" onclick="this.nextElementSibling.scrollBy({left:-300,behavior:' + "'smooth'" + '})">◂</button>'
        body += '<div class="briefing-grid">'
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
            body += '</a>'
        body += '</div><button class="scroll-arrow right" onclick="this.previousElementSibling.scrollBy({left:300,behavior:' + "'smooth'" + '})">▸</button></div>'
    else:
        body += '<div class="empty-state"><p>No briefings found.</p></div>'

    return html_page("Hermes", body, active_nav="hermes")

def kanban_page(msg: str = "") -> str:
    """Interactive Kanban board with create/move/comment."""
    tasks = load_kanban_tasks()
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>📋 Kanban Board</h1>'
    body += '<p style="color:var(--text-muted);font-size:0.9rem">Create tasks, move between columns, add comments.</p></div>'

    if msg:
        body += '<div style="text-align:center;margin:1rem 0;padding:0.75rem;background:var(--bg-card);border:1px solid var(--accent);border-radius:8px;color:var(--accent-hover)">' + msg + '</div>'

    # Create form
    body += '<form method=POST action=/kanban/create style="margin-bottom:1.5rem;display:flex;gap:0.5rem;flex-wrap:wrap;align-items:flex-end">'
    body += '<div style="flex:1;min-width:200px"><label style="display:block;font-size:0.8rem;color:var(--text-muted);margin-bottom:0.25rem">Title *</label>'
    body += '<input name=title required placeholder="What needs to be done?" style="width:100%;padding:0.6rem;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:0.9rem"></div>'
    body += '<div style="flex:0 0 130px"><label style="display:block;font-size:0.8rem;color:var(--text-muted);margin-bottom:0.25rem">Assignee</label>'
    body += '<input name=assignee placeholder="@who" style="width:100%;padding:0.6rem;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:0.9rem"></div>'
    body += '<div style="flex:0 0 100px"><label style="display:block;font-size:0.8rem;color:var(--text-muted);margin-bottom:0.25rem">&nbsp;</label>'
    body += '<button type=submit style="width:100%;padding:0.6rem 1rem;background:var(--accent);color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:0.9rem">+ Create</button></div></form>'

    if not tasks or all(len(v) == 0 for v in tasks.values()):
        body += '<div class="empty-state"><p>No tasks yet. Create one above!</p></div>'
        return html_page("Kanban Board", body, active_nav="hermes")

    hints = {
        "ready": "Waiting to be picked up. Web-created tasks auto-start in Running.",
        "running": "Actively being worked on. Complete when done, or Block if stuck.",
        "blocked": "Cannot progress — waiting on something else. Unblock to resume.",
        "done": "Completed tasks. They stay here for reference.",
    }

    cols = [("ready","📥 Ready"),("running","⚡ Running"),("blocked","🚧 Blocked"),("done","✅ Done")]
    body += '<div class="kanban-board">'
    for col_key, col_label in cols:
        col_tasks = tasks.get(col_key, [])
        hint = hints.get(col_key, "")
        body += '<div class="kanban-column">'
        # Header with <details>/<summary> for dropdown hint
        body += '<details class="col-details" style="margin-bottom:0.5rem">'
        body += '<summary style="cursor:pointer;user-select:none;list-style:none;display:flex;align-items:center;gap:0.3rem;padding:0.3rem 0">'
        body += '<span style="font-size:0.65rem;color:var(--text-muted);display:inline-block;width:1em">▶</span>'
        body += '<h3 style="margin:0;font-size:1rem">' + col_label + '<span class="count">' + str(len(col_tasks)) + '</span></h3>'
        body += '</summary>'
        body += '<div style="font-size:0.72rem;color:var(--text-muted);padding:0.3rem 0 0.5rem 1.3rem;line-height:1.5">' + hint + '</div>'
        body += '</details>'

        for t in col_tasks:
            tid = t["id"]; status = t["status"]
            body += '<div class="kanban-card">'
            body += '<div class="kc-title" onclick="this.parentElement.classList.toggle(\'expanded\')" style="cursor:pointer">' + t["title"] + '</div>'
            body += '<div class="kc-meta">'
            if t.get("assignee"): body += '<span>@' + t["assignee"] + '</span>'
            if t.get("priority"): body += '<span class="prio">P' + str(t["priority"]) + '</span>'
            body += '</div>'

            # Preview
            if t.get("body"):
                preview = t["body"][:100].replace("\n", " ").strip()
                body += '<div class="kc-preview" style="font-size:0.78rem;color:var(--text-muted);margin:0.3rem 0;line-height:1.4;overflow:hidden;max-height:2.8em">' + preview + ("..." if len(t.get("body","")) > 100 else "") + '</div>'

            # Stats
            body += '<div style="display:flex;gap:0.8rem;font-size:0.7rem;color:var(--text-muted);margin:0.3rem 0">'
            cc = len(t.get("comments",[]))
            if cc: body += '<span>💬 ' + str(cc) + '</span>'
            age = _kanban_age(t.get("created_at"))
            if age: body += '<span>📅 ' + age + '</span>'
            if t.get("started_at") and t["status"] == "running":
                rf = _kanban_age(t["started_at"])
                if rf: body += '<span>⏱ ' + rf + '</span>'
            body += '</div>'

            # Actions
            body += '<div class="kc-actions" style="margin:0.4rem 0;display:flex;gap:0.3rem;flex-wrap:wrap">'
            if status == "ready":
                body += '<form method=POST action=/kanban/move style="display:inline"><input type=hidden name=task_id value=' + tid + '><input type=hidden name=status value=running><button class=kb-btn style="font-size:0.7rem;padding:0.25rem 0.5rem;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer">▶ Start</button></form>'
            elif status == "running":
                body += '<form method=POST action=/kanban/move style="display:inline"><input type=hidden name=task_id value=' + tid + '><input type=hidden name=status value=done><button class=kb-btn style="font-size:0.7rem;padding:0.25rem 0.5rem;background:#238636;color:#fff;border:none;border-radius:4px;cursor:pointer">✓ Complete</button></form>'
                body += '<form method=POST action=/kanban/move style="display:inline"><input type=hidden name=task_id value=' + tid + '><input type=hidden name=status value=blocked><button class=kb-btn style="font-size:0.7rem;padding:0.25rem 0.5rem;background:#d29922;color:#fff;border:none;border-radius:4px;cursor:pointer">🚧 Block</button></form>'
            elif status == "blocked":
                body += '<form method=POST action=/kanban/move style="display:inline"><input type=hidden name=task_id value=' + tid + '><input type=hidden name=status value=ready><button class=kb-btn style="font-size:0.7rem;padding:0.25rem 0.5rem;background:var(--text-muted);color:#fff;border:none;border-radius:4px;cursor:pointer">↩ Unblock</button></form>'
            body += '</div>'

            # Expandable
            body += '<div class="kc-body">'
            if t.get("body"):
                body += '<div style="white-space:pre-wrap;margin-bottom:0.5rem">' + t["body"][:500] + ("..." if len(t.get("body","")) > 500 else "") + '</div>'
            comments = t.get("comments",[])
            if comments:
                body += '<div style="font-size:0.75rem;color:var(--text-muted);margin-bottom:0.25rem">Comments:</div>'
                for cmt in comments:
                    body += '<div class="kc-comment"><div class="comment-author">' + cmt["author"] + '</div><div>' + cmt["body"][:300] + '</div></div>'
            body += '<form method=POST action=/kanban/comment style="margin-top:0.5rem;display:flex;gap:0.3rem">'
            body += '<input type=hidden name=task_id value=' + tid + '>'
            body += '<input name=author placeholder="Name" style="flex:0 0 70px;padding:0.3rem;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.75rem">'
            body += '<input name=body placeholder="Add comment..." style="flex:1;padding:0.3rem;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.75rem">'
            body += '<button type=submit style="padding:0.3rem 0.5rem;background:var(--bg-card);color:var(--text);border:1px solid var(--border);border-radius:4px;cursor:pointer;font-size:0.75rem">Send</button></form>'
            body += '</div></div>'  # kc-body, kanban-card
        body += '</div>'  # kanban-column
    body += '</div>'  # kanban-board
    return html_page("Kanban Board", body, active_nav="hermes")

# ═══════════════════════════════════════════════════════════════
#  HTML helpers
# ═══════════════════════════════════════════════════════════════


def html_page(title: str, body: str, active_nav: str = "home", extra_head: str = "") -> str:
    nav_links = [
        ("/", "Home", "home"),
        ("/briefings", "Briefings", "briefings"),
        ("/hermes", "Hermes", "hermes"),
        ("https://ssh.devmclovin.com", "SSH", "ssh"),
    ]
    nav_html = ""
    for href, label, key in nav_links:
        cls = 'active' if active_nav == key else ''
        if key in ("ssh",):
            nav_html += f'<a href="{href}" class="hermes-btn {cls}">{label}</a>'
        else:
            nav_html += f'<a href="{href}" class="{cls}">{label}</a>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {extra_head}
    <title>{title} — devmclovin</title>
    <style>{BASE_CSS}</style>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚀</text></svg>">
</head>
<body>
    <nav>
        <a href="/" class="logo">dev<span>mclovin</span></a>
        <div class="links">{nav_html}</div>
    </nav>
    <div class="container">
        {body}
    </div>
    <footer>
        devmclovin.com — more coming soon
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
    full = " ".join(body_lines).strip()
    if not full:
        return ""
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', full)
    last = sentences[-1].strip()
    if not last:
        last = sentences[-2].strip() if len(sentences) > 1 else ""
    return last


def _keyword_impact(story: dict) -> str:
    """Keyword-based fallback impact when body extraction returns nothing."""
    title = story.get("title", "").lower()
    body = story.get("body", "").lower()
    text = title + " " + body

    keywords = [
        (["copyright", "infring", "lawsuit", "sue", "court", "legal"],
         "Legal battles over AI training data could reshape copyright law and content creator rights."),
        (["clone", "distill", "steal", "intellectual property", "ip theft"],
         "AI model cloning and IP theft accusations signal escalating tech competition between nations."),
        (["hack", "jailbreak", "security", "vulnerability", "exploit", "attack"],
         "AI security challenges highlight the ongoing arms race between builders and attackers."),
        (["antibiotic", "superbug", "bacteria", "drug", "treatment", "medicine", "health"],
         "Medical breakthroughs in fighting superbugs could save millions of lives and transform healthcare."),
        (["military", "drone", "weapon", "defense", "army", "warfare", "combat"],
         "Military drone programs signal a fundamental shift in how future conflicts will be fought."),
        (["ai", "artificial intelligence", "model", "openai", "gpt", "llm", "language model", "machine learning"],
         "AI developments continue to redefine what's possible — and who holds the power in technology."),
        (["research", "study", "scientists", "researchers", "discovery", "breakthrough", "found"],
         "New scientific discoveries expand the frontier of human knowledge and practical applications."),
        (["privacy", "surveillance", "data", "tracking", "monitoring"],
         "Privacy and data issues affect how personal information is collected, used, and protected."),
        (["climate", "environment", "energy", "carbon", "emissions", "solar", "renewable"],
         "Environmental and energy developments shape the planet's future and economic competitiveness."),
        (["space", "nasa", "rocket", "mars", "moon", "orbit", "satellite", "launch"],
         "Space exploration advances push the boundaries of human achievement and scientific discovery."),
        (["policy", "regulation", "government", "law", "bill", "congress", "senate"],
         "Policy and regulatory changes affect industries, innovation, and everyday life."),
        (["google", "apple", "microsoft", "meta", "amazon", "tesla", "nvidia"],
         "Big Tech moves ripple across markets, competition, and the products billions use daily."),
        (["chip", "semiconductor", "processor", "hardware", "gpu", "cpu", "compute"],
         "Semiconductor and hardware advances are the physical foundation of the AI revolution."),
        (["startup", "funding", "venture", "ipo", "acquisition", "invest", "valuation"],
         "Startup funding and investment trends reveal where the smart money sees the next big opportunity."),
    ]

    for triggers, impact in keywords:
        for t in triggers:
            if t in text:
                return impact

    return "This development could have significant implications worth watching closely."


# ── Impact generation for briefing stories ──

def _load_env_var(name: str) -> str:
    """Load a single environment variable from ~/.hermes/.env."""
    import os
    env_path = os.path.expanduser("~/.hermes/.env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip('"').strip("'")
    except (FileNotFoundError, OSError):
        pass
    return ""


def _load_openrouter_key() -> str:
    return _load_env_var("OPENROUTER_API_KEY")


def _openrouter_chat(messages: list[dict], model: str = "google/gemini-2.5-flash-lite") -> str | None:
    """Call OpenRouter chat completions API. Returns response text or None."""
    import json as _json
    import urllib.request as _ur
    key = _load_openrouter_key()
    if not key:
        return None
    body = _json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 256,
        "temperature": 0.7,
    }).encode()
    req = _ur.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "devmclovin-landing",
        },
    )
    try:
        with _ur.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _load_impacts_cache(date_key: str) -> dict[str, str]:
    """Load cached impact statements for a date from disk."""
    import os as _os
    import json as _json
    cache_dir = _os.path.expanduser("~/.devmclovin/impacts")
    cache_file = _os.path.join(cache_dir, f"{date_key}.json")
    try:
        with open(cache_file) as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        return {}


def _save_impacts_cache(date_key: str, impacts: dict[str, str]) -> None:
    """Save impact statements cache for a date to disk."""
    import os as _os
    import json as _json
    cache_dir = _os.path.expanduser("~/.devmclovin/impacts")
    _os.makedirs(cache_dir, exist_ok=True)
    cache_file = _os.path.join(cache_dir, f"{date_key}.json")
    with open(cache_file, "w") as f:
        _json.dump(impacts, f, indent=2)


def _generate_impacts_via_llm(stories: list[dict]) -> dict[str, str]:
    """Generate impact statements for stories via OpenRouter LLM."""
    import json as _json
    stories_text = ""
    for i, s in enumerate(stories, 1):
        stories_text += f"{i}. {s['title']}\n"
    prompt = (
        "For each news headline below, write a single sentence explaining "
        "why this development matters. Be specific and insightful. "
        "Return ONLY a JSON object mapping the number to the impact sentence.\n\n"
        f"{stories_text}\n"
        'Example response: {"1": "This matters because...", "2": "This matters because..."}'
    )
    response = _openrouter_chat([
        {"role": "system", "content": "You are a concise news analyst. Return ONLY valid JSON."},
        {"role": "user", "content": prompt},
    ])
    if not response:
        return {}
    response = response.strip()
    if response.startswith("```"):
        response = response.split("\n", 1)[1] if "\n" in response else response[3:]
        if response.endswith("```"):
            response = response[:-3]
        response = response.strip()
    try:
        result = _json.loads(response)
    except _json.JSONDecodeError:
        return {}
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
            for s in missing:
                if s["title"] in cached:
                    s["impact"] = cached[s["title"]]
        except Exception:
            pass

    # Fallback: extract impact from body text for any story still missing one
    updated_cache = False
    for s in stories:
        if not s["impact"] and s.get("body"):
            body_lines = s["body"].split("<br>")
            extracted = _extract_impact(body_lines)
            if extracted:
                s["impact"] = extracted
                cached[s["title"]] = extracted
                updated_cache = True
            else:
                s["impact"] = _keyword_impact(s)
                cached[s["title"]] = s["impact"]
                updated_cache = True
    if updated_cache:
        _save_impacts_cache(date_key, cached)


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


def category_tabs_html(active_category: str = "", counts: dict | None = None) -> str:
    """Render horizontal category filter tabs. 'active_category' of '' means 'All'."""
    html = '<div class="category-tabs">'
    # "All" tab
    all_cls = 'active' if not active_category else ''
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
    briefing_start = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("MORNING BRIEFING"):
            briefing_start = i

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

    return stories, date_str


def briefing_card(stories: list[dict], date_str: str) -> str:
    if not stories:
        return '<div class="empty-state"><p>No briefing available for today yet. Check back after 7am UTC.</p></div>'

    # Populate impact statements (cached, generated via LLM if needed)
    _get_story_impacts(stories, date_str)

    html = '<div class="briefing-header">'
    html += f'<div class="date">{date_str}</div>'
    html += '<h2>📰 Morning Briefing</h2>'
    html += '</div>'
    html += '<div class="briefing-scroll">'
    html += '<button class="scroll-arrow left" onclick="this.nextElementSibling.scrollBy({left:-340,behavior:\'smooth\'})">◂</button>'
    html += '<div class="briefing-grid">'
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
        html += '</div>'
    html += '</div>'
    html += '<button class="scroll-arrow right" onclick="this.previousElementSibling.scrollBy({left:340,behavior:\'smooth\'})">▸</button>'
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


def github_projects_row() -> str:
    """Render a horizontally-scrollable row of GitHub repo cards."""
    repos, username = get_github_repos()

    if not repos:
        return (
            '<div class="section-title">GitHub Projects</div>'
            '<div class="empty-state"><p>🔑 GitHub token not found. '
            'Set GITHUB_READ_TOKEN in ~/.hermes/.env to show your projects.</p></div>'
        )

    html = f'<div class="section-title">GitHub Projects <span style="font-weight:400;color:var(--text-muted);font-size:0.85rem">@{username}</span></div>'
    html += '<div class="briefing-scroll">'
    html += '<button class="scroll-arrow left" onclick="this.nextElementSibling.scrollBy({left:-300,behavior:\'smooth\'})">◂</button>'
    html += '<div class="briefing-grid">'

    for r in repos:
        desc = r["description"]
        if len(desc) > 140:
            desc = desc[:137].rsplit(" ", 1)[0] + "…"

        badges = ""
        if r["private"]:
            badges += '<span class="repo-badge private">private</span> '
        if r["fork"]:
            badges += '<span class="repo-badge fork">fork</span> '

        html += '<div class="repo-card">'
        html += f'<h3>{badges}<a href="{r["html_url"]}" target="_blank" rel="noopener">{r["name"]}</a></h3>'
        if desc:
            html += f'<div class="repo-desc">{desc}</div>'
        else:
            html += '<div class="repo-desc" style="font-style:italic">No description</div>'

        html += '<div class="repo-meta">'
        if r["language"]:
            html += f'<span class="lang"><span class="repo-lang-dot" style="background:{_lang_color(r["language"])}"></span>{r["language"]}</span>'
        if r["stars"]:
            html += f'<span class="stars">⭐ {r["stars"]}</span>'
        if r["updated_at"]:
            html += f'<span>{_relative_time(r["updated_at"])}</span>'
        html += '</div>'

        html += '</div>'

    html += '</div>'
    html += '<button class="scroll-arrow right" onclick="this.previousElementSibling.scrollBy({left:300,behavior:\'smooth\'})">▸</button>'
    html += '</div>'
    return html


def spending_card_row() -> str:
    """Render a horizontally-scrollable row of OpenRouter spending cards for the home page."""
    data = get_openrouter_data()

    if data["error"]:
        return (
            '<div class="section-title">OpenRouter Spend</div>'
            f'<div class="empty-state"><p>🔑 {data["error"]}. '
            'Set OPENROUTER_API_KEY in ~/.hermes/.env.</p></div>'
        )

    balance = data["balance"]
    total_usage = data["total_usage"]
    remaining = (balance - total_usage) if (balance is not None and total_usage is not None) else None

    html = '<div class="section-title">OpenRouter Spend</div>'
    html += '<div class="briefing-scroll">'
    html += '<button class="scroll-arrow left" onclick="this.nextElementSibling.scrollBy({left:-280,behavior:\'smooth\'})">◂</button>'
    html += '<div class="briefing-grid">'

    rem_class = "positive" if (remaining is not None and remaining > 0) else "negative"
    html += '<div class="spending-mini">'
    html += '<div class="spend-mini-label">Credits Remaining</div>'
    html += f'<div class="spend-mini-value {rem_class}">${remaining:.2f}</div>' if remaining is not None else '<div class="spend-mini-value">--</div>'
    html += f'<div class="spend-mini-sub">of ${balance:.2f} purchased</div>' if balance else ''
    html += '</div>'

    html += '<div class="spending-mini">'
    html += '<div class="spend-mini-label">Lifetime Usage</div>'
    html += f'<div class="spend-mini-value">${total_usage:.2f}</div>' if total_usage else '<div class="spend-mini-value">--</div>'
    if balance and total_usage:
        pct = (total_usage / balance * 100) if balance > 0 else 0
        html += f'<div class="spend-mini-sub">{pct:.0f}% of credits</div>'
    html += '</div>'

    html += '<div class="spending-mini">'
    html += '<div class="spend-mini-label">Model Analytics</div>'
    html += '<div class="spend-mini-value" style="font-size:1.1rem;color:var(--text-muted)">Per-model data</div>'
    html += '<div class="spend-mini-sub">available on <a href="https://openrouter.ai/activity" target="_blank" rel="noopener" style="color:var(--accent-hover)">OpenRouter →</a></div>'
    html += '</div>'

    html += '<a href="https://openrouter.ai/activity" target="_blank" rel="noopener" style="text-decoration:none">'
    html += '<div class="spending-mini" style="border-color:var(--accent);justify-content:center;align-items:center">'
    html += '<div style="color:var(--accent-hover);font-size:1rem;font-weight:600">Open in OpenRouter →</div>'
    html += '<div class="spend-mini-sub">Full activity &amp; models</div>'
    html += '</div></a>'

    html += '</div>'
    html += '<button class="scroll-arrow right" onclick="this.previousElementSibling.scrollBy({left:280,behavior:\'smooth\'})">▸</button>'
    html += '</div>'
    return html


def home_page() -> str:
    body = """
    <div class="hero">
        <h1>devmclovin</h1>
        <p>Personal projects, daily briefings, and AI-powered tools.</p>
    </div>
    """

    # Quick Links (Phase 1 — above briefings, start-page utility)
    body += quick_links_row()

    # Today's briefing (from DB)
    body += '<div class="section-title">Today\'s Briefing</div>'
    today = datetime.now().strftime("%Y-%m-%d")
    archive = _get_archive()
    briefing = archive.get_briefing(today)
    if briefing and briefing.get("articles"):
        date_str = _render_briefing_date(briefing.get("full_date"), briefing["date"])
        body += briefing_card_from_db(briefing["articles"], date_str)
    else:
        # Fallback: try the most recent briefing
        recent = archive.get_briefings(limit=1)
        if recent:
            b = archive.get_briefing(recent[0]["date"])
            if b and b.get("articles"):
                date_str = _render_briefing_date(b.get("full_date"), b["date"])
                body += briefing_card_from_db(b["articles"], date_str)
            else:
                body += '<div class="empty-state"><p>☕ No briefings found. The morning briefing runs at 7am UTC.</p></div>'
        else:
            body += '<div class="empty-state"><p>☕ No briefings found. The morning briefing runs at 7am UTC.</p></div>'

    # GitHub projects
    body += spending_card_row()
    body += github_projects_row()

    return html_page("devmclovin", body, active_nav="home")


def briefings_page(category: str = "") -> str:
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>Past Briefings</h1></div>'

    archive = _get_archive()

    # Get category counts for tabs
    cat_counts_raw = archive.get_category_counts()
    cat_counts = {c["category"]: c["count"] for c in cat_counts_raw}

    # Render category filter tabs
    body += category_tabs_html(active_category=category, counts=cat_counts)

    # Get briefings (from DB)
    briefings = archive.get_briefings(limit=30)

    if not briefings:
        body += '<div class="empty-state"><p>No briefings found.</p></div>'
        return html_page("Briefings", body, active_nav="briefings")

    body += '<div class="briefing-list">'
    for b in briefings:
        date_part = b["date"]
        try:
            dt = datetime.strptime(date_part, "%Y-%m-%d")
            display_date = dt.strftime("%A, %B %d, %Y")
        except ValueError:
            display_date = date_part

        # Get articles for this briefing (filtered if category is set)
        if category:
            articles = archive.get_articles_by_category(category, date_str=date_part, limit=100)
        else:
            articles = archive.get_articles(date_str=date_part)

        count = len(articles)
        if count == 0:
            continue  # skip briefings with no matching articles after filtering

        titles = [a.get("title", "Untitled") for a in articles]

        body += f'<a href="/briefing/{date_part}'
        if category:
            body += f'?category={category}'
        body += '">'
        body += f'<span class="brief-date">{display_date}</span>'
        body += f'<span class="brief-meta"> — {count} stories</span>'
        body += '<ul class="brief-titles">'
        for t in titles:
            body += f'<li>{t}</li>'
        body += '</ul>'
        body += '</a>'

    body += '</div>'
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
    body += category_tabs_html(active_category=category, counts=cat_counts)

    body += briefing_card_from_db(articles, date_str, show_date=True)
    return html_page(f"Briefing — {date}", body, active_nav="briefings")


# ═══════════════════════════════════════════════════════════════
#  HTTP Handler
# ═══════════════════════════════════════════════════════════════

class Handler(http.server.BaseHTTPRequestHandler):
    def _send_redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        import urllib.parse
        path = self.path.rstrip("/") or "/"

        # Parse query string
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)
        category = qs.get("category", [""])[0]

        if path == "/":
            content = home_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/briefings":
            content = briefings_page(category=category).encode()
            self._respond(200, "text/html", content)
        elif path.startswith("/briefing/"):
            date = path.split("/briefing/")[1]
            content = briefing_detail_page(date, category=category).encode()
            self._respond(200, "text/html", content)
        elif path == "/hermes":
            content = hermes_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/kanban":
            content = kanban_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/cron":
            content = cron_page().encode()
            self._respond(200, "text/html", content)
        elif path.startswith("/cron/"):
            # /cron/<job_id> or /cron/<job_id>/<filename>
            parts = path.split("/")
            # parts: ["", "cron", "job_id"] or ["", "cron", "job_id", "filename"]
            if len(parts) == 3:
                job_id = parts[2]
                content = cron_job_detail_page(job_id).encode()
                self._respond(200, "text/html", content)
            elif len(parts) >= 4:
                job_id = parts[2]
                filename = "/".join(parts[3:])
                content = cron_output_preview_page(job_id, filename).encode()
                self._respond(200, "text/html", content)
            else:
                self._respond(404, "text/plain", b"Not Found")
        elif path == "/health":
            self._respond(200, "text/plain", b"ok")
        else:
            self._respond(404, "text/plain", b"Not Found")

    def do_POST(self):
        import urllib.parse
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        params = urllib.parse.parse_qs(raw)
        get = lambda k: params.get(k, [""])[0]
        path = self.path.rstrip("/") or "/"

        if path == "/kanban/create":
            title = get("title").strip()
            if title:
                kanban_create(title, assignee=get("assignee").strip())
            self._send_redirect("/kanban")
        elif path == "/kanban/move":
            tid = get("task_id"); st = get("status")
            if tid and st: kanban_move(tid, st)
            self._send_redirect("/kanban")
        elif path == "/kanban/comment":
            tid = get("task_id"); author = get("author").strip() or "anon"; body_text = get("body").strip()
            if tid and body_text: kanban_comment(tid, author, body_text)
            self._send_redirect("/kanban")
        else:
            self.send_response(404); self.end_headers()

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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print(f"devmclovin landing page → http://127.0.0.1:{port}")
    server.serve_forever()
