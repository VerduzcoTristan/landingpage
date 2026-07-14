#!/usr/bin/env python3
"""Landing page server for devmclovin.com — dark mode, morning briefings, Hermes link."""

import http.server
import html
import json
import os
import re
import glob
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

PORT = 3002
BRIEFING_DIR = Path(os.path.expanduser("~/.hermes/cron/output/7dc1d641173d"))
SITE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", SITE_DIR / "data"))

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
BRIEFING_DB = Path(os.path.expanduser("~/.hermes/data/briefings.db"))
IMPACT_CACHE_DIR = Path(os.path.expanduser("~/.devmclovin/impacts"))
BOOKMARKS_FILE = DATA_DIR / "bookmarks.json"

# ── GitHub projects cache ──
# ── System status cache ──
# ── Link Health Check ──
# ── Cloudflare tunnel cache ──


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
    tmp.replace(BOOKMARKS_FILE)

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













# ── Cloudflare Tunnel API ──







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




# ── Link Health Check helpers ──









# ═══════════════════════════════════════════════════════════════
#  Cron Job Status Viewer
# ═══════════════════════════════════════════════════════════════







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








# ═══════════════════════════════════════════════════════════════
#  Kanban Board Dashboard
# ═══════════════════════════════════════════════════════════════






















































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
    key = os.environ.get("OPENROUTER_API_KEY") or _load_env_var("OPENROUTER_API_KEY")
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


def _category_filter_html(
    active_category: str = "All",
    counts: dict | None = None,
    saved_only: bool = False,
    saved_count: int = 0,
    sort: str = "newest",
) -> str:
    """Render horizontal category filter tabs. 'active_category' of 'All' means no filter."""
    def filter_url(category: str | None = None, saved: bool = False) -> str:
        params = {}
        if category:
            params["category"] = category
        if saved:
            params["saved"] = "1"
        if sort != "newest":
            params["sort"] = sort
        return "/briefings" + (("?" + urllib.parse.urlencode(params)) if params else "")

    html = '<div class="category-tabs">'
    all_cls = 'active' if active_category == "All" and not saved_only else ''
    all_count = sum(counts.values()) if counts else ""
    html += f'<a href="{filter_url()}" class="category-tab {all_cls}">All'
    if all_count:
        html += f'<span class="tab-count">{all_count}</span>'
    html += '</a>'
    for cat in CATEGORY_ORDER:
        cls = 'active' if active_category == cat and not saved_only else ''
        cnt = counts.get(cat, 0) if counts else ""
        html += f'<a href="{filter_url(category=cat)}" class="category-tab {cls}">{cat}'
        if cnt:
            html += f'<span class="tab-count">{cnt}</span>'
        html += '</a>'
    saved_cls = 'active' if saved_only else ''
    html += f'<a href="{filter_url(saved=True)}" class="category-tab {saved_cls}">★ Saved'
    if saved_count:
        html += f'<span class="tab-count">{saved_count}</span>'
    html += '</a></div>'
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
        saved_label = '⭐ Saved' if _is_bookmarked(sid, 'saved') else '⭐ Save'
        import html as _html
        args_saved = ','.join(["'"+_html.escape(str(x), quote=False)+"'" for x in [sid, date_str, title, url, source, (summary or '')[:500], 'saved']])
        html += f'<div class="bm-btn-row"><button class="bm-btn saved-btn{saved_active}" onclick="toggleBookmark(this,{args_saved})">{saved_label}</button></div>'
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
        saved_label = '⭐ Saved' if _is_bookmarked(sid, 'saved') else '⭐ Save'
        import html as _html
        args_saved = ','.join(["'"+_html.escape(str(x), quote=False)+"'" for x in [sid, date_str, s["title"], s.get("source_url", ""), s.get("source_name", ""), s.get("body", "")[:500], 'saved']])
        html += f'<div class="bm-btn-row"><button class="bm-btn saved-btn{saved_active}" onclick="toggleBookmark(this,{args_saved})">{saved_label}</button></div>'
        html += '</div>'
    html += '</div>'
    return html

# ── GitHub language colours ──
# ── Cloudflare Tunnel Monitor UI ──









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

    return html_page("devmclovin", body, active_nav="home")

def briefings_page(category=None, saved_only: bool = False, sort: str = "newest") -> str:
    sort = sort if sort in {"newest", "oldest", "saved"} else "newest"
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>Past Briefings</h1><p>Filter and scan the briefing archive.</p></div>'
    archive = _get_archive()
    bookmarks = _load_bookmarks()
    saved_ids = {item.get("id") for item in bookmarks.get("saved", []) if item.get("id")}
    cat_counts_raw = archive.get_category_counts()
    cat_counts = {item["category"]: item["count"] for item in cat_counts_raw}
    body += _category_filter_html(
        active_category=category or "All",
        counts=cat_counts,
        saved_only=saved_only,
        saved_count=len(saved_ids),
        sort=sort,
    )
    body += '<form class="briefing-sort" method="GET" action="/briefings"><label for="briefing-sort">Sort</label>'
    if category:
        body += '<input type="hidden" name="category" value="' + html.escape(category, quote=True) + '">'
    if saved_only:
        body += '<input type="hidden" name="saved" value="1">'
    body += '<select id="briefing-sort" name="sort" onchange="this.form.submit()">'
    for value, label in (("newest", "Newest"), ("oldest", "Oldest"), ("saved", "Saved first")):
        selected = ' selected' if sort == value else ''
        body += f'<option value="{value}"{selected}>{label}</option>'
    body += '</select></form>'

    entries = []
    for briefing in archive.get_briefings(limit=30):
        date_part = briefing["date"]
        if category:
            articles = archive.get_articles_by_category(category, start_date=date_part, end_date=date_part, limit=100)
        else:
            articles = archive.get_articles(date_str=date_part)
        for article in articles:
            article["_saved"] = _story_id(
                date_part,
                article.get("title", "Untitled"),
                article.get("source_url", ""),
            ) in saved_ids
        if saved_only:
            articles = [article for article in articles if article["_saved"]]
        if articles:
            entries.append((briefing, articles))

    if sort == "oldest":
        entries.sort(key=lambda item: item[0]["date"])
    elif sort == "saved":
        entries.sort(
            key=lambda item: (sum(1 for article in item[1] if article["_saved"]), item[0]["date"]),
            reverse=True,
        )
    else:
        entries.sort(key=lambda item: item[0]["date"], reverse=True)

    if not entries:
        message = "No saved stories yet." if saved_only else "No briefings found."
        body += f'<div class="empty-state"><p>{message}</p></div>'
        return html_page("Briefings", body, active_nav="briefings")

    body += '<div class="briefing-archive-grid">'
    for briefing, articles in entries:
        date_part = briefing["date"]
        try:
            dt = datetime.strptime(date_part, "%Y-%m-%d")
            display_date = dt.strftime("%b %d, %Y")
            weekday = dt.strftime("%A")
        except ValueError:
            display_date = date_part
            weekday = ""
        titles = [article.get("title", "Untitled") for article in articles]
        params = {}
        if category:
            params["category"] = category
        if saved_only:
            params["saved"] = "1"
        if sort != "newest":
            params["sort"] = sort
        href = "/briefing/" + date_part
        if params:
            href += "?" + urllib.parse.urlencode(params)
        saved_count = sum(1 for article in articles if article["_saved"])
        body += '<a class="briefing-archive-card" href="' + html.escape(href, quote=True) + '">'
        body += '<div class="briefing-card-topline"><span class="briefing-date-chip">' + html.escape(display_date) + '</span>'
        body += '<span class="briefing-count-chip">' + str(len(articles)) + ' stories</span></div>'
        body += '<div class="briefing-top-story">' + html.escape(titles[0]) + '</div>'
        body += '<ul class="briefing-preview-list">'
        for title in titles[1:4]:
            body += '<li>' + html.escape(title) + '</li>'
        if len(titles) > 4:
            body += '<li>+' + str(len(titles) - 4) + ' more stories</li>'
        body += '</ul>'
        footer = html.escape(weekday) + ' · Read briefing →'
        if saved_count:
            footer = '★ ' + str(saved_count) + ' saved · ' + footer
        body += '<div class="briefing-card-footer">' + footer + '</div></a>'
    body += '</div>'
    return html_page("Briefings", body, active_nav="briefings")



def briefing_detail_page(date: str, category: str = "", saved_only: bool = False, sort: str = "newest") -> str:
    query = {}
    if category:
        query["category"] = category
    if saved_only:
        query["saved"] = "1"
    if sort != "newest":
        query["sort"] = sort
    body = '<div style="padding-top:1rem"><a href="/briefings'
    if query:
        body += '?' + urllib.parse.urlencode(query)
    body += '" style="color:var(--text-muted);text-decoration:none;font-size:0.9rem">← Back to all briefings</a></div>'

    archive = _get_archive()
    briefing = archive.get_briefing(date)

    if not briefing or not briefing.get("articles"):
        body += f'<div class="empty-state" style="margin-top:2rem"><p>No briefing found for {date}.</p></div>'
        return html_page(f"Briefing — {date}", body, active_nav="briefings")

    articles = briefing["articles"]
    if category:
        articles = [a for a in articles if category in (a.get("categories") or "").split(",")]
    if saved_only:
        saved_ids = {item.get("id") for item in _load_bookmarks().get("saved", [])}
        articles = [
            article for article in articles
            if _story_id(date, article.get("title", "Untitled"), article.get("source_url", "")) in saved_ids
        ]

    date_str = _render_briefing_date(briefing.get("full_date"), date)

    # Show category tabs on detail page too (for quick switching)
    cat_counts_raw = archive.get_category_counts()
    cat_counts = {c["category"]: c["count"] for c in cat_counts_raw}
    body += _category_filter_html(
        active_category=category or "All",
        counts=cat_counts,
        saved_only=saved_only,
        saved_count=len(_load_bookmarks().get("saved", [])),
        sort=sort,
    )

    body += briefing_card_from_db(articles, date_str, show_date=True)
    return html_page(f"Briefing — {date}", body, active_nav="briefings")


# ═══════════════════════════════════════════════════════════════
#  Logs Pages
# ═══════════════════════════════════════════════════════════════







# ═══════════════════════════════════════════════════════════════
#  Status Board Page
# ═══════════════════════════════════════════════════════════════

_MONITOR_CACHE: dict = {"data": None, "ts": 0.0}
_MONITOR_CACHE_TTL = 30


def _load_monitor_config() -> tuple[list[dict], list[dict], str | None]:
    path = DATA_DIR / "monitors.json"
    if not path.exists():
        return [], [], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("root must be an object")
        checks = payload.get("checks", [])
        links = payload.get("links", [])
        if not isinstance(checks, list) or not isinstance(links, list):
            raise ValueError("checks and links must be arrays")
        clean_checks = []
        for item in checks:
            if not isinstance(item, dict) or not item.get("name") or not item.get("url"):
                raise ValueError("each check requires name and url")
            url = str(item["url"])
            if not url.startswith(("http://", "https://")):
                raise ValueError("check URLs must use http or https")
            clean_checks.append({
                "name": str(item["name"]),
                "url": url,
                "timeout": max(0.2, min(float(item.get("timeout", 3)), 30.0)),
            })
        clean_links = []
        for item in links:
            if isinstance(item, dict) and item.get("name") and item.get("url"):
                clean_links.append({"name": str(item["name"]), "url": str(item["url"])})
        return clean_checks, clean_links, None
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return [], [], f"Invalid monitors.json: {error}"


def _check_monitor(item: dict) -> dict:
    started = time.perf_counter()
    error_text = None
    healthy = False
    status_code = None
    try:
        request = urllib.request.Request(item["url"], method="HEAD", headers={"User-Agent": "control-center-monitor/1"})
        try:
            response = urllib.request.urlopen(request, timeout=item["timeout"])
        except urllib.error.HTTPError as error:
            if error.code not in (405, 501):
                raise
            request = urllib.request.Request(item["url"], method="GET", headers={"User-Agent": "control-center-monitor/1"})
            response = urllib.request.urlopen(request, timeout=item["timeout"])
        with response:
            status_code = response.status
            healthy = 200 <= status_code < 400
    except urllib.error.HTTPError as error:
        status_code = error.code
        error_text = f"HTTP {error.code}"
    except Exception as error:
        error_text = str(error)
    return {
        "name": item["name"],
        "healthy": healthy,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "error": error_text,
        "status_code": status_code,
    }


def get_monitor_status(force: bool = False) -> dict:
    now = time.time()
    cached = _MONITOR_CACHE.get("data")
    if not force and cached is not None and now - _MONITOR_CACHE["ts"] < _MONITOR_CACHE_TTL:
        return cached
    checks, links, config_error = _load_monitor_config()
    results = [_check_monitor(item) for item in checks]
    if config_error or not results:
        status = "unconfigured"
    elif all(item["healthy"] for item in results):
        status = "ok"
    else:
        status = "issues"
    data = {"status": status, "checks": results, "links": links, "error": config_error}
    _MONITOR_CACHE.update({"data": data, "ts": now})
    return data


def status_page() -> str:
    data = get_monitor_status()
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>Server Status</h1><p>Live checks from inside the app container.</p></div>'
    if data.get("error"):
        body += '<div class="empty-state"><p>' + html.escape(data["error"]) + '</p></div>'
    elif not data["checks"]:
        body += '<div class="empty-state"><p>No monitors configured.</p></div>'
    else:
        body += '<div class="status-board">'
        for check in data["checks"]:
            state = "up" if check["healthy"] else "down"
            detail = f'{check["latency_ms"]} ms' if check["healthy"] else (check["error"] or "Unavailable")
            body += '<article class="status-check ' + state + '"><div><span class="status-dot ' + ("green" if check["healthy"] else "red") + '"></span>'
            body += '<strong>' + html.escape(check["name"]) + '</strong></div><span>' + html.escape(detail) + '</span></article>'
        body += '</div>'
    if data["links"]:
        body += '<div class="section-head"><h2>Monitoring tools</h2></div><div class="monitor-links">'
        for link in data["links"]:
            body += '<a class="link-card" href="' + html.escape(link["url"], quote=True) + '" target="_blank" rel="noopener">' + html.escape(link["name"]) + ' ↗</a>'
        body += '</div>'
    return html_page("Status", body, active_nav="status")


def portfolio_page() -> str:
    """Serve the standalone portfolio.html page (with shared nav injected)."""
    portfolio_html = SITE_DIR / "portfolio.html"
    if portfolio_html.exists():
        return inject_nav(portfolio_html.read_text(), "portfolio")
    return "<html><body><h1>Portfolio Not Found</h1></body></html>"






# ── Project Launcher Config ──

PROJECTS_FILE = DATA_DIR / "projects.json"


def _normalise_projects(items) -> list[dict]:
    if not isinstance(items, list):
        return []
    projects = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            continue
        projects.append({
            "name": str(item.get("name", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "url": str(item.get("url", "")).strip(),
            "repo_url": str(item.get("repo_url", "")).strip(),
            "status": str(item.get("status", "active")).strip() or "active",
            "order": int(item.get("order", index)),
            "hidden": bool(item.get("hidden", False)),
        })
    projects.sort(key=lambda project: (project["order"], project["name"].lower()))
    for order, project in enumerate(projects):
        project["order"] = order
    return projects


def load_projects() -> list[dict]:
    if not PROJECTS_FILE.exists():
        return []
    try:
        return _normalise_projects(json.loads(PROJECTS_FILE.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def save_projects(projects: list[dict]) -> None:
    projects = _normalise_projects(projects)
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PROJECTS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(projects, indent=2) + "\n", encoding="utf-8")
    temporary.replace(PROJECTS_FILE)


def projects_page() -> str:
    projects = [project for project in load_projects() if not project["hidden"]]
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>Projects</h1><p>Active work and useful services.</p></div>'
    if not projects:
        body += '<div class="empty-state"><p>No projects yet — add one in admin.</p><a class="button" href="/projects/admin">Open project admin</a></div>'
        return html_page("Projects", body, active_nav="projects")
    body += '<div class="projects-grid">'
    for project in projects:
        body += '<article class="project-card"><div class="project-card-head"><h2>' + html.escape(project["name"]) + '</h2>'
        body += '<span class="status-pill">' + html.escape(project["status"]) + '</span></div>'
        if project["description"]:
            body += '<p>' + html.escape(project["description"]) + '</p>'
        body += '<div class="project-actions">'
        if project["url"]:
            body += '<a class="button primary" href="' + html.escape(project["url"], quote=True) + '" target="_blank" rel="noopener">Open ↗</a>'
        if project["repo_url"]:
            body += '<a class="button" href="' + html.escape(project["repo_url"], quote=True) + '" target="_blank" rel="noopener">Repository ↗</a>'
        body += '</div></article>'
    body += '</div><p class="admin-link"><a href="/projects/admin">Manage projects</a></p>'
    return html_page("Projects", body, active_nav="projects")


def _project_fields(get) -> dict:
    return {
        "name": get("name").strip(),
        "description": get("description").strip(),
        "url": get("url").strip(),
        "repo_url": get("repo_url").strip(),
        "status": get("status").strip() or "active",
        "hidden": get("hidden") in {"1", "true", "on", "yes"},
    }


def update_projects(action: str, get) -> str:
    projects = load_projects()
    if action == "add":
        project = _project_fields(get)
        if not project["name"]:
            return "Name is required."
        project["order"] = len(projects)
        projects.append(project)
        save_projects(projects)
        return "Project added."
    try:
        index = int(get("index"))
        if index < 0:
            raise IndexError
        project = projects[index]
    except (ValueError, IndexError):
        return "Project not found."
    if action == "update":
        fields = _project_fields(get)
        if not fields["name"]:
            return "Name is required."
        project.update(fields)
    elif action == "delete":
        projects.pop(index)
    elif action == "toggle-hide":
        project["hidden"] = not project["hidden"]
    elif action == "move":
        target = index - 1 if get("direction") == "up" else index + 1
        if 0 <= target < len(projects):
            projects[index], projects[target] = projects[target], projects[index]
            for order, item in enumerate(projects):
                item["order"] = order
    else:
        return "Unknown action."
    save_projects(projects)
    return "Projects updated."


def project_admin_page(message: str = "") -> str:
    projects = load_projects()
    body = '<div class="page-head"><div><h1>Project admin</h1><p>Add, edit, reorder, or hide entries.</p></div><a class="button" href="/projects">View projects</a></div>'
    if message:
        body += '<div class="notice">' + html.escape(message) + '</div>'
    body += '''<section class="admin-panel"><h2>Add project</h2><form method="POST" action="/projects/admin/add" class="project-form">
    <label>Name<input name="name" required></label><label>Status<input name="status" value="active"></label>
    <label class="wide">Description<textarea name="description" rows="2"></textarea></label>
    <label>URL<input name="url" type="url"></label><label>Repository URL<input name="repo_url" type="url"></label>
    <label class="check"><input name="hidden" value="1" type="checkbox"> Hidden</label>
    <button class="button primary" type="submit">Add project</button></form></section>'''
    if not projects:
        body += '<div class="empty-state"><p>No projects yet.</p></div>'
    for index, project in enumerate(projects):
        body += '<section class="admin-panel"><form method="POST" action="/projects/admin/update" class="project-form">'
        body += '<input type="hidden" name="index" value="' + str(index) + '">'
        body += '<label>Name<input name="name" required value="' + html.escape(project["name"], quote=True) + '"></label>'
        body += '<label>Status<input name="status" value="' + html.escape(project["status"], quote=True) + '"></label>'
        body += '<label class="wide">Description<textarea name="description" rows="2">' + html.escape(project["description"]) + '</textarea></label>'
        body += '<label>URL<input name="url" type="url" value="' + html.escape(project["url"], quote=True) + '"></label>'
        body += '<label>Repository URL<input name="repo_url" type="url" value="' + html.escape(project["repo_url"], quote=True) + '"></label>'
        checked = ' checked' if project["hidden"] else ''
        body += '<label class="check"><input name="hidden" value="1" type="checkbox"' + checked + '> Hidden</label>'
        body += '<button class="button primary" type="submit">Save</button></form><div class="admin-actions">'
        for direction, label in (("up", "Move up"), ("down", "Move down")):
            body += '<form method="POST" action="/projects/admin/move"><input type="hidden" name="index" value="' + str(index) + '"><input type="hidden" name="direction" value="' + direction + '"><button class="button" type="submit">' + label + '</button></form>'
        body += '<form method="POST" action="/projects/admin/toggle-hide"><input type="hidden" name="index" value="' + str(index) + '"><button class="button" type="submit">' + ("Show" if project["hidden"] else "Hide") + '</button></form>'
        body += '<form method="POST" action="/projects/admin/delete" onsubmit="return confirm(\'Delete this project?\')"><input type="hidden" name="index" value="' + str(index) + '"><button class="button danger" type="submit">Delete</button></form>'
        body += '</div></section>'
    return html_page("Project admin", body, active_nav="projects")




# ═══════════════════════════════════════════════════════════════
#  HTTP Handler
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# Model tuning helpers
# ═══════════════════════════════════════════════════════════════






































# ── LLM Lab: evals, traces, arena, routing, HF GGUF discovery ──
































































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
        sort = qs.get("sort", ["newest"])[0]
        saved_only = qs.get("saved", [""])[0] == "1"

        if path == "/":
            content = home_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/briefings":
            content = briefings_page(category=category or None, saved_only=saved_only, sort=sort).encode()
            self._respond(200, "text/html", content)
        elif path.startswith("/briefing/"):
            date = path.split("/briefing/")[1]
            content = briefing_detail_page(date, category=category, saved_only=saved_only, sort=sort).encode()
            self._respond(200, "text/html", content)
        elif path == "/status":
            content = status_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/portfolio":
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            content = portfolio_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/api/status":
            self._respond(200, "application/json", json.dumps(get_monitor_status()).encode())
        elif path == "/projects":
            content = projects_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/projects/admin":
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            content = project_admin_page(qs.get("message", [""])[0]).encode()
            self._respond(200, "text/html", content)
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

        if path == "/bookmarks/toggle":
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
        elif path.startswith("/projects/admin/"):
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            action = path.removeprefix("/projects/admin/")
            if action not in {"add", "update", "delete", "move", "toggle-hide"}:
                self._respond(404, "text/plain", b"Not Found")
                return
            message = update_projects(action, get)
            self._send_redirect("/projects/admin?" + urllib.parse.urlencode({"message": message}))
        else:
            self.send_response(404); self.end_headers()



    def do_PATCH(self):
        self.send_response(405); self.end_headers()

    def do_DELETE(self):
        self.send_response(405); self.end_headers()


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

    class ReuseHTTPServer(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            http.server.ThreadingHTTPServer.server_bind(self)

    server = ReuseHTTPServer((os.environ.get("BIND_HOST", "127.0.0.1"), port), Handler)
    print(f"devmclovin landing page → http://127.0.0.1:{port}")
    server.serve_forever()
