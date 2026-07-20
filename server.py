#!/usr/bin/env python3
"""Control Center server for briefings, monitoring, projects, and portfolio."""

import http.server
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from github_client import GitHubClient
from hub_store import HubStore, normalise_hub
from ollama_client import OllamaClient

PORT = 3002
BRIEFING_DIR = Path(os.path.expanduser("~/.hermes/cron/output/7dc1d641173d"))
SITE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", SITE_DIR / "data"))
ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
}

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
<title>Access required — Control Center</title><style>
:root{color-scheme:dark;--page:#080b12;--surface:#111725;--border:#263148;--text:#edf2ff;--muted:#98a5bd;--accent:#8b7cff}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:1.5rem;background:radial-gradient(circle at 50% 0,#18213a 0,transparent 42%),var(--page);color:var(--text);font:16px/1.6 Inter,ui-sans-serif,system-ui,sans-serif}
.access-card{width:min(100%,28rem);padding:2.5rem;border:1px solid var(--border);border-radius:1.25rem;background:linear-gradient(145deg,rgba(255,255,255,.04),transparent),var(--surface);box-shadow:0 24px 70px rgba(0,0,0,.45);text-align:center}.eyebrow{color:var(--accent);font-size:.75rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase}h1{margin:.5rem 0;font-size:2rem}p{margin:0;color:var(--muted)}
</style></head><body><main class="access-card"><div class="eyebrow">Control Center</div><h1>Access required</h1><p>Authenticate through Cloudflare Access to continue.</p></main></body></html>"""

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

# ── Storage paths ──
BOOKMARKS_FILE = DATA_DIR / "bookmarks.json"

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

# ── Shared nav assets (single source of truth for the top nav) ──────────────
# Server-rendered pages use NAV_CSS directly; the generated portfolio receives
# the same rules and markup through its three shell placeholders.
NAV_CSS = """
:root{--page:#080b12;--surface:#101624;--surface-raised:#151d2e;--overlay:#1b2538;--border:#28344b;--border-strong:#3b4964;--text:#eef3ff;--muted:#9ba9c1;--subtle:#6f7d95;--accent:#8b7cff;--accent-strong:#a99eff;--accent-soft:rgba(139,124,255,.13);--success:#45d69a;--warning:#f3b95f;--danger:#ff6b7a;--shadow-1:0 10px 30px rgba(0,0,0,.22);--shadow-2:0 24px 70px rgba(0,0,0,.38);--radius-sm:.55rem;--radius-md:.85rem;--radius-lg:1.2rem;--shell:72rem;--ease:180ms ease}
.skip-link{position:fixed;left:1rem;top:-5rem;z-index:500;padding:.65rem 1rem;border-radius:0 0 var(--radius-sm) var(--radius-sm);background:var(--accent);color:#fff;font-weight:800;text-decoration:none;transition:top var(--ease)}.skip-link:focus{top:0}
.site-nav{position:sticky;top:0;z-index:100;border-bottom:1px solid rgba(59,73,100,.65);background:rgba(8,11,18,.78);backdrop-filter:blur(18px) saturate(140%)}.nav-shell{width:min(100% - 2rem,var(--shell));min-height:4rem;margin:auto;display:flex;align-items:center;justify-content:space-between;gap:1.5rem}.brand{display:inline-flex;align-items:center;gap:.65rem;color:var(--text);font-weight:850;letter-spacing:-.025em;text-decoration:none;white-space:nowrap}.brand-mark{display:grid;place-items:center;width:1.9rem;height:1.9rem;border:1px solid rgba(169,158,255,.45);border-radius:.6rem;background:linear-gradient(145deg,rgba(139,124,255,.28),rgba(69,214,154,.08));color:var(--accent-strong);box-shadow:inset 0 1px rgba(255,255,255,.12)}.nav-links{display:flex;align-items:center;gap:1.25rem}.nav-links a{position:relative;padding:1.35rem 0 1.2rem;color:var(--muted);font-size:.88rem;font-weight:700;text-decoration:none;transition:color var(--ease)}.nav-links a::after{content:"";position:absolute;left:0;right:0;bottom:.75rem;height:2px;border-radius:2px;background:var(--accent);transform:scaleX(0);transition:transform var(--ease)}.nav-links a:hover,.nav-links a.active{color:var(--text)}.nav-links a.active::after{transform:scaleX(1)}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid rgba(139,124,255,.58);outline-offset:3px}
.site-footer{width:min(100% - 2rem,var(--shell));margin:4rem auto 0;padding:1.5rem 0 2.5rem;border-top:1px solid var(--border);display:flex;justify-content:space-between;gap:1rem;color:var(--subtle);font-size:.8rem}.site-footer nav{display:flex;gap:1rem}.site-footer a{color:var(--muted);text-decoration:none}.site-footer a:hover{color:var(--text)}
@media(max-width:720px){.nav-shell{align-items:flex-start;flex-direction:column;gap:.1rem;padding-top:.8rem}.nav-links{width:100%;gap:1rem;overflow-x:auto;scrollbar-width:none}.nav-links::-webkit-scrollbar{display:none}.nav-links a{padding:.7rem 0 1rem}.nav-links a::after{bottom:.5rem}.site-footer{align-items:flex-start;flex-direction:column}}
"""

def render_nav(active: str = "home") -> str:
    links = []
    for href, label, key in (
        ("/", "Home", "home"),
        ("/briefings", "Briefings", "briefings"),
        ("/hub", "Hub", "hub"),
        ("/status", "Status", "status"),
    ):
        current = ' class="active" aria-current="page"' if active == key else ""
        links.append(f'<a href="{href}"{current}>{label}</a>')
    return ('<nav class="site-nav" aria-label="Primary"><div class="nav-shell">'
            '<a href="/" class="brand" aria-label="Control Center home"><span class="brand-mark" aria-hidden="true">◆</span><span>Control Center</span></a>'
            '<div class="nav-links">' + "".join(links) + '</div></div></nav>')

BASE_CSS = """
*{box-sizing:border-box}html{color-scheme:dark;scroll-behavior:smooth}body{margin:0;min-height:100vh;background:radial-gradient(circle at 15% -10%,rgba(139,124,255,.16),transparent 30rem),radial-gradient(circle at 100% 15%,rgba(69,214,154,.07),transparent 26rem),var(--page);color:var(--text);font:400 16px/1.65 Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;text-rendering:optimizeLegibility}button,input,select,textarea{font:inherit}a{color:var(--accent-strong)}img{max-width:100%}.container{width:min(100% - 2rem,var(--shell));margin:auto;padding:2.5rem 0 0}main{min-height:70vh}
h1,h2,h3,h4,p{margin-top:0}h1,h2,h3{line-height:1.15;letter-spacing:-.025em}h1{font-size:clamp(2rem,5vw,3.7rem)}h2{font-size:clamp(1.2rem,2.4vw,1.7rem)}h3{font-size:1.05rem}.hero{padding:2rem 0 1.5rem!important;text-align:left}.hero h1{margin-bottom:.65rem}.hero p,.page-head p{max-width:42rem;color:var(--muted);font-size:1.02rem}.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:1.5rem;margin:1rem 0 2rem}.page-head h1{margin-bottom:.4rem}.page-date{color:var(--muted);font-size:.9rem;white-space:nowrap}.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;margin:2.2rem 0 .85rem}.section-head h2{margin:0}.section-head a,.admin-link a{font-size:.86rem;font-weight:750;text-decoration:none}
.button,button.button{display:inline-flex;align-items:center;justify-content:center;min-height:2.55rem;padding:.55rem .9rem;border:1px solid var(--border-strong);border-radius:var(--radius-sm);background:var(--surface-raised);color:var(--text);font-weight:750;text-decoration:none;cursor:pointer;box-shadow:0 1px rgba(255,255,255,.04);transition:transform var(--ease),border-color var(--ease),background var(--ease),box-shadow var(--ease)}.button:hover{transform:translateY(-1px);border-color:var(--accent);box-shadow:var(--shadow-1)}.button.primary{border-color:transparent;background:linear-gradient(135deg,var(--accent),#6f80ff);color:white}.button.danger{border-color:rgba(255,107,122,.45);color:#ff9ca6}.button.danger:hover{background:rgba(255,107,122,.1)}
.empty-state{padding:2.5rem;border:1px dashed var(--border-strong);border-radius:var(--radius-lg);background:linear-gradient(145deg,rgba(255,255,255,.025),transparent),var(--surface);text-align:center;color:var(--muted);box-shadow:var(--shadow-1)}.empty-state p{margin:0 0 1rem}.empty-state p:last-child{margin-bottom:0}.notice{margin:0 0 1rem;padding:.8rem 1rem;border:1px solid rgba(69,214,154,.3);border-radius:var(--radius-sm);background:rgba(69,214,154,.08);color:#a9f2d3}
.category-tabs{display:flex;align-items:center;gap:.5rem;overflow-x:auto;padding:.2rem 0 .8rem;scrollbar-width:none}.category-tabs::-webkit-scrollbar{display:none}.category-tab{display:inline-flex;align-items:center;gap:.4rem;flex:0 0 auto;padding:.42rem .75rem;border:1px solid var(--border);border-radius:999px;background:rgba(16,22,36,.72);color:var(--muted);font-size:.78rem;font-weight:750;text-decoration:none;transition:all var(--ease)}.category-tab:hover,.category-tab.active{border-color:rgba(139,124,255,.7);background:var(--accent-soft);color:var(--text)}.tab-count{display:inline-grid;place-items:center;min-width:1.25rem;height:1.25rem;padding:0 .3rem;border-radius:999px;background:rgba(255,255,255,.07);font-size:.68rem}.briefing-sort{display:flex;align-items:center;justify-content:flex-end;gap:.55rem;margin:.2rem 0 1rem;color:var(--muted);font-size:.78rem}.briefing-sort select,input,textarea,select{border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);padding:.65rem .75rem;transition:border-color var(--ease),box-shadow var(--ease)}input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);outline:0}
.briefing-archive-grid,.projects-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,19rem),1fr));gap:1rem}.briefing-archive-card,.project-card,.admin-panel{border:1px solid var(--border);border-radius:var(--radius-lg);background:linear-gradient(150deg,rgba(255,255,255,.04),transparent 38%),var(--surface);box-shadow:var(--shadow-1);transition:transform var(--ease),border-color var(--ease),box-shadow var(--ease)}.briefing-archive-card{display:flex;min-height:12rem;flex-direction:column;gap:.8rem;padding:1.1rem;color:var(--text);text-decoration:none}.briefing-archive-card:hover,.project-card:hover{transform:translateY(-3px);border-color:var(--border-strong);box-shadow:var(--shadow-2)}.briefing-card-topline,.project-card-head{display:flex;align-items:center;justify-content:space-between;gap:.75rem}.briefing-date-chip,.briefing-count-chip,.status-pill,.category-badge{display:inline-flex;align-items:center;width:max-content;border-radius:999px;font-size:.7rem;font-weight:800;letter-spacing:.025em}.briefing-date-chip{color:var(--accent-strong)}.briefing-count-chip,.status-pill{padding:.25rem .55rem;border:1px solid var(--border);background:rgba(255,255,255,.035);color:var(--muted)}.briefing-top-story{font-size:1.08rem;font-weight:800;line-height:1.35}.briefing-preview-list{margin:0;padding-left:1.1rem;color:var(--muted);font-size:.83rem}.briefing-card-footer{margin-top:auto;color:var(--subtle);font-size:.75rem;font-weight:700}
.briefing-header{margin:1.5rem 0 1rem}.briefing-header .date{color:var(--accent-strong);font-size:.76rem;font-weight:800;text-transform:uppercase;letter-spacing:.1em}.briefing-grid,.dashboard-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,20rem),1fr));gap:1rem}.briefing-card{padding:1.2rem;border:1px solid var(--border);border-radius:var(--radius-lg);background:linear-gradient(145deg,rgba(139,124,255,.06),transparent 40%),var(--surface);box-shadow:var(--shadow-1)}.briefing-card h3{margin:.35rem 0 .6rem}.card-num{color:var(--accent-strong);font-size:.72rem;font-weight:850}.card-summary,.card-impact{color:var(--muted);font-size:.92rem;line-height:1.6}.card-impact{margin-bottom:.5rem;color:#cad4e8}.card-source{margin-top:.8rem;color:var(--subtle);font-size:.78rem}.card-source a{text-decoration:none}.card-categories{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.7rem}.category-badge{padding:.2rem .5rem}.bm-btn-row{margin-top:.85rem}.bm-btn{padding:.4rem .7rem;border:1px solid var(--border);border-radius:999px;background:transparent;color:var(--muted);font-size:.74rem;font-weight:750;cursor:pointer;transition:all var(--ease)}.bm-btn:hover,.bm-btn.active{border-color:var(--accent);background:var(--accent-soft);color:var(--text)}
.home-eyebrow{display:block;margin-bottom:.35rem;color:var(--accent-strong);font-size:.7rem;font-weight:850;letter-spacing:.14em;text-transform:uppercase}.home-head h1{margin:0;font-size:clamp(2.2rem,5vw,3.6rem)}.briefing-home{overflow:hidden;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface);box-shadow:var(--shadow-1)}.briefing-home-row{display:flex;align-items:flex-start;gap:.75rem;padding:1.1rem 1.2rem;border-top:1px solid var(--border)}.briefing-home-row:first-child{border-top:0}.bh-main{min-width:0;width:100%}.bh-title-line{display:flex;align-items:baseline;flex-wrap:wrap;gap:.55rem}.bh-title{color:var(--text);font-size:1rem;font-weight:800;text-decoration:none}.bh-impact{margin-top:.4rem;color:var(--muted);font-size:.96rem;line-height:1.68;white-space:pre-line;overflow:visible}
.landing-status-strip{margin:1.4rem 0;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface);box-shadow:var(--shadow-1);overflow:hidden}.landing-status-strip summary{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.85rem 1rem;cursor:pointer;list-style:none}.landing-status-strip summary::-webkit-details-marker{display:none}.status-summary-left,.status-summary-right{display:flex;align-items:center;gap:.55rem}.status-summary-title{font-weight:800}.status-summary-meta{color:var(--muted);font-size:.8rem}.status-mini-pill{padding:.2rem .5rem;border:1px solid var(--border);border-radius:999px;color:var(--muted);font-size:.68rem}.status-mini-pill.ok{color:var(--success)}.status-mini-pill.warn{color:var(--warning)}.status-strip-dot,.status-dot{display:inline-block;width:.65rem;height:.65rem;border-radius:50%;background:var(--subtle);box-shadow:0 0 0 4px rgba(111,125,149,.1)}.green{background:var(--success)!important}.red{background:var(--danger)!important}.amber{background:var(--warning)!important}.landing-status-body{padding:1rem;border-top:1px solid var(--border)}
.status-mini-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:.55rem}.status-mini-service{display:flex;align-items:center;justify-content:space-between;gap:.7rem;padding:.65rem .75rem;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--page);color:var(--text);font-size:.78rem;text-decoration:none}.status-mini-service>span{display:flex;align-items:center;gap:.5rem}.status-mini-service small{color:var(--muted)}.status-board{display:grid;gap:.75rem}.status-check{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1rem 1.1rem;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface);box-shadow:var(--shadow-1)}.status-check>div{display:flex;align-items:center;gap:.7rem}.status-check>span{color:var(--muted);font-size:.8rem}.monitor-links,.project-actions,.admin-actions{display:flex;flex-wrap:wrap;gap:.6rem}.link-card{padding:.7rem .9rem;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);font-weight:750;text-decoration:none;transition:all var(--ease)}.link-card:hover{transform:translateY(-1px);border-color:var(--accent)}
.home-links{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem;margin:1rem 0 0}.home-links a{display:grid;grid-template-columns:1fr auto;gap:.1rem .7rem;padding:1rem 1.1rem;border:1px solid var(--border);border-radius:var(--radius-md);background:linear-gradient(145deg,rgba(255,255,255,.035),transparent),var(--surface);color:var(--text);text-decoration:none;box-shadow:var(--shadow-1);transition:all var(--ease)}.home-links a:hover{transform:translateY(-2px);border-color:var(--accent)}.home-links span{font-weight:850}.home-links small{grid-column:1;color:var(--muted)}.home-links b{grid-column:2;grid-row:1/3;align-self:center;color:var(--accent-strong)}.project-card{padding:1.2rem}.project-card h2{margin:0;font-size:1.15rem}.project-card p{min-height:3.2rem;margin:.8rem 0;color:var(--muted)}.admin-link{margin:1.3rem 0;text-align:right}.admin-panel{margin-bottom:1rem;padding:1.2rem}.admin-panel h2{font-size:1.05rem}.project-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.85rem}.project-form label{display:grid;gap:.35rem;color:var(--muted);font-size:.75rem;font-weight:750}.project-form .wide{grid-column:1/-1}.project-form .check{display:flex;align-items:center;gap:.5rem}.project-form .check input{width:auto}.project-form textarea{resize:vertical}.admin-actions{margin-top:.9rem;padding-top:.9rem;border-top:1px solid var(--border)}.admin-actions form{margin:0}
.hub-group{margin:2rem 0}.hub-commits{margin:.6rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:.82rem}.hub-commits li{margin:.15rem 0}.hub-lang{color:var(--subtle);font-size:.78rem;margin:.4rem 0 0}.hub-attention{color:#ffb86b;font-size:.8rem;margin:.5rem 0 0}.summarizing{color:var(--subtle);font-style:italic}
code{padding:.15rem .35rem;border:1px solid var(--border);border-radius:.35rem;background:var(--page);font:500 .86em ui-monospace,SFMono-Regular,Consolas,monospace}
@media(max-width:640px){.container{width:min(100% - 1.25rem,var(--shell));padding-top:1.5rem}.page-head,.section-head{align-items:flex-start;flex-direction:column}.project-form{grid-template-columns:1fr}.project-form .wide{grid-column:auto}.landing-status-strip summary{align-items:flex-start;flex-direction:column}.status-summary-right{flex-wrap:wrap}.status-summary-meta{white-space:normal}.home-links{grid-template-columns:1fr}.site-footer{width:min(100% - 1.25rem,var(--shell))}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
"""

#  HTML helpers

def html_page(title: str, body: str, active_nav: str = "home", extra_head: str = "") -> str:
    page_title = "Control Center" if title == "Control Center" else f"{title} — Control Center"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {extra_head}
    <title>{html.escape(page_title)}</title>
    <style>{NAV_CSS}{BASE_CSS}</style>
    <script>
    function toggleBookmark(btn, sid, date, title, url, srcName, body, btype) {{
        var formData = new URLSearchParams({{id:sid,type:btype,title:title,source_name:srcName,source_url:url,body:body,date:date}});
        fetch('/bookmarks/toggle', {{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:formData.toString()}})
            .then(function(response){{return response.json();}})
            .then(function(data){{if(data.ok){{btn.classList.toggle('active',data.active);btn.textContent=data.active?'⭐ Saved':'⭐ Save';}}}})
            .catch(function(error){{console.error('Bookmark toggle failed:',error);}});
    }}
    </script>
</head>
<body>
    <a href="#main-content" class="skip-link">Skip to main content</a>
    {render_nav(active_nav)}
    <main id="main-content"><div class="container">{body}</div></main>
    <footer class="site-footer"><span>Control Center</span><nav aria-label="Footer"><a href="/briefings">Briefings</a><a href="/status">Status</a><a href="/hub">Hub</a></nav></footer>
</body>
</html>"""

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
    """Render a responsive briefing card grid from DB-format articles."""
    if not articles:
        return '<div class="empty-state"><p>No articles found.</p></div>'

    html = '<div class="briefing-header">'
    if show_date:
        html += f'<div class="date">{date_str}</div>'
    html += '<h2>📰 Morning Briefing</h2>'
    html += '</div>'
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
            html += f'<div class="card-summary">{summary}</div>'
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

def briefing_list_home(articles: list[dict], date_str: str) -> str:
    """Render today's stories as a readable vertical list with full summaries."""
    rows = articles
    h = '<div class="briefing-home">'
    for a in rows:
        title = a.get("title", "Untitled")
        url = a.get("source_url", "")
        summary = a.get("summary") or a.get("impact") or a.get("body") or ""
        summary = re.sub(r"<br\s*/?>", "\n", summary, flags=re.IGNORECASE).strip()
        categories = a.get("categories", "")
        first_cat = ""
        if categories:
            parts = [c.strip() for c in categories.split(",") if c.strip() and c.strip() != "general"]
            first_cat = parts[0] if parts else ""
        h += '<div class="briefing-home-row">'
        h += '<div class="bh-main">'
        href = url or f"/briefing/{date_str}"
        target = ' target="_blank" rel="noopener"' if url else ''
        h += '<div class="bh-title-line">'
        if first_cat:
            bg, fg = CATEGORY_COLORS.get(first_cat, ("#6b7280", "#f3f4f6"))
            h += f'<span class="bh-badge category-badge" style="background:{bg};color:{fg}">{html.escape(first_cat)}</span>'
        h += f'<a class="bh-title" href="{html.escape(href, quote=True)}"{target}>{html.escape(title)}</a></div>'
        if summary:
            h += f'<div class="bh-impact">{html.escape(summary)}</div>'
        h += '</div></div>'
    h += '</div>'
    return h

def status_strip() -> str:
    """Render a compact live monitor summary backed by /api/status."""
    return """<details class="landing-status-strip" id="landing-status-strip">
        <summary>
            <span class="status-summary-left"><span class="status-strip-dot" id="status-strip-dot"></span><span class="status-summary-title">Server status</span><span class="status-summary-meta" id="status-summary-meta">Checking monitors…</span></span>
            <span class="status-summary-right"><span class="status-mini-pill" id="status-ok-pill">— up</span><span class="status-mini-pill" id="status-issue-pill">— down</span></span>
        </summary>
        <div class="landing-status-body" id="landing-status-body"><span class="status-summary-meta">Loading live checks…</span></div>
    </details>
    <script>
    (function(){
        var panel=document.getElementById('landing-status-strip');
        var dot=document.getElementById('status-strip-dot');
        var meta=document.getElementById('status-summary-meta');
        var body=document.getElementById('landing-status-body');
        var okP=document.getElementById('status-ok-pill');
        var badP=document.getElementById('status-issue-pill');
        function esc(value){var el=document.createElement('span');el.textContent=value==null?'':String(value);return el.innerHTML;}
        fetch('/api/status').then(function(response){if(!response.ok)throw new Error(response.status);return response.json();}).then(function(data){
            var checks=Array.isArray(data.checks)?data.checks:[];
            var up=checks.filter(function(check){return !!check.healthy;}).length;
            var down=checks.length-up;
            okP.textContent=up+' up';if(up){okP.classList.add('ok');}
            badP.textContent=down+' down';badP.classList.add(down?'warn':'ok');
            if(!checks.length){dot.classList.add('amber');meta.textContent='No monitors configured';body.innerHTML='<span class="status-summary-meta">Add checks in monitors.json.</span> <a href="/status">Open status →</a>';return;}
            dot.classList.add(down?'red':'green');
            meta.textContent=down?(down+' monitor'+(down===1?'':'s')+' need attention'):'All monitors healthy';
            body.innerHTML='<div class="status-mini-grid">'+checks.map(function(check){return '<a class="status-mini-service" href="/status"><span><span class="status-dot '+(check.healthy?'green':'red')+'"></span>'+esc(check.name)+'</span><small>'+esc(check.healthy?(check.latency_ms+' ms'):(check.error||'Unavailable'))+'</small></a>';}).join('')+'</div>';
            if(down){panel.open=true;}
        }).catch(function(){dot.classList.add('amber');meta.textContent='Status unavailable';body.innerHTML='<span class="status-summary-meta">Live monitoring could not be loaded.</span> <a href="/status">Open status →</a>';});
    })();
    </script>"""

def home_page() -> str:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    page_date = now.strftime("%A, %B ") + str(now.day)  # cross-platform "no leading zero"

    # 1) Header row (replaces the hero tagline)
    body = ('<div class="page-head home-head"><div><span class="home-eyebrow">Tristan</span><h1>Control Center</h1></div>'
            f'<span class="page-date">{page_date}</span></div>')

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

    body += '<div class="section-head"><h2>Monitoring</h2><a href="/status">Full status →</a></div>'
    body += status_strip()
    body += ('<div class="home-links" aria-label="Secondary destinations">'
             '<a href="/hub"><span>Hub</span><small>All projects, GitHub activity, AI summaries</small><b>→</b></a></div>')

    return html_page("Control Center", body, active_nav="home")

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

#  Status Board Page

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

def _migrate_hub_file() -> None:
    """One-time migration: rename legacy projects.json to the clean curation path."""
    _HUB_STORE.migrate_legacy_path()

# ── Hub Curation Config ──

HUB_FILE = DATA_DIR / "curation.json"  # curation layer, keyed by repo full_name
_HUB_STORE = HubStore(HUB_FILE, DATA_DIR / "projects.json")
_migrate_hub_file()

def _normalise_hub(raw) -> dict:
    """Coerce arbitrary JSON into a clean curation dict keyed by full_name."""
    return normalise_hub(raw)

def load_hub() -> dict:
    """Load the curation layer; migrate legacy list format on first read."""
    return _HUB_STORE.load()

def save_hub(hub: dict) -> None:
    """Atomic write of the curation layer."""
    _HUB_STORE.save(hub)

def update_hub(action: str, get) -> str:
    """Mutate a single curation entry by full_name. Returns a message string."""
    return _HUB_STORE.update(action, get)

# ── GitHub client (Hub data source) ──
# Reads the user's owned repos + recent commits via the REST API.
# Token is read from GITHUB_TOKEN (local development) or GITHUB_TOKEN_FILE
# (production secret mount). It is never logged or echoed.

_GITHUB_CLIENT = GitHubClient()
_GH_CACHE_TTL = _GITHUB_CLIENT.cache_ttl
_GH_API = _GITHUB_CLIENT.api_url

def _gh_token() -> str:
    return _GITHUB_CLIENT.token()

def _gh_request(url: str) -> dict | None:
    """GET a GitHub API URL. Returns parsed JSON, or None on any failure.
    Never raises; never logs the token, URL-with-token, or response bodies."""
    return _GITHUB_CLIENT.request(url)

def fetch_all_repos() -> list[dict] | None:
    """Return a list of owned repos (public + private), or None on failure."""
    return _GITHUB_CLIENT.fetch_all_repos()

def fetch_recent_commits(owner: str, repo: str, branch: str) -> list[dict]:
    """Return up to 5 recent commits (subject + body), or [] on failure.
    Degrades silently — a single repo's failure must not break the whole refresh."""
    return _GITHUB_CLIENT.fetch_recent_commits(owner, repo, branch)

def classify_recency(pushed_at: str) -> str:
    """Active (<7d) / Maintain (<30d) / Stalled (>30d)."""
    return _GITHUB_CLIENT.classify_recency(pushed_at)

def get_hub_repos(force: bool = False) -> dict:
    """Return merged Hub data: {repos: [...], status: "ok"|"token_missing"|"error", banner: str|None, ts: float}.
    Cached 10 min; serves stale on failure. Each repo entry carries:
    full_name, name, description, language, html_url, default_branch, pushed_at, recency, commits (list)."""
    return _GITHUB_CLIENT.get_repos(force=force)

# ── Ollama client (Hub summaries) ──
# Local LLM summarization of recent commit activity. Non-blocking: the /hub
# page renders from cache with "Summarizing…" placeholders; this endpoint
# fills them lazily. URL/model/prompt/errors are NEVER logged or returned.

_OLLAMA_CLIENT = OllamaClient()

def _merge_hub_entries() -> dict:
    """Merge GitHub repos with curated overrides keyed by full_name.

    Returns {"groups": {group: [entry,...]}, "status": str, "banner": str|None}.
    Group order is Active, Maintain, Stalled, Done. Done is forced by a
    curation status_override of "done"; otherwise grouping follows recency.
    """
    data = get_hub_repos(force=False)
    curated = load_hub()
    groups = {"active": [], "maintain": [], "stalled": [], "done": []}
    for repo in data.get("repos", []) or []:
        fn = str(repo.get("full_name", "")).strip()
        if not fn:
            continue
        cur = curated.get(fn, {}) or {}
        # status_override "done" forces the Done group
        override = str(cur.get("status_override", "")).strip().lower()
        if override == "done":
            group = "done"
        else:
            group = str(repo.get("recency", "stalled")).strip().lower()
            if group not in groups:
                group = "stalled"
        entry = {
            "full_name": fn,
            "name": str(repo.get("name", fn)).strip(),
            "html_url": str(repo.get("html_url", "")).strip(),
            "description": str(cur.get("goal") or repo.get("description", "")).strip(),
            "language": repo.get("language") or None,
            "recency": str(repo.get("recency", "stalled")),
            "commits": repo.get("commits", []) or [],
            "order": int(cur.get("order", 999) or 999),
            "has_note": bool(str(cur.get("goal", "")).strip()),
            "status_override": override,
        }
        groups[group].append(entry)
    # Sort within group: curated (order < 999) first by order, then by full_name
    for g in groups:
        groups[g].sort(key=lambda e: (e["order"] if e["order"] != 999 else 1000,
                                       e["full_name"]))
    # Fallback: if no GitHub repos were returned (token_missing or error+no cache),
    # surface curated entries as Stalled cards so curated-only data is visible.
    if not any(groups.values()):
        for fn, cur in curated.items():
            fn = str(fn).strip()
            if not fn:
                continue
            if fn in {e["full_name"] for e in (groups["stalled"] + groups["active"]
                                               + groups["maintain"] + groups["done"])}:
                continue
            cur = cur or {}
            override = str(cur.get("status_override", "")).strip().lower()
            goal = str(cur.get("goal", "")).strip()
            entry = {
                "full_name": fn,
                "name": fn,
                "html_url": str(cur.get("live_url", "")).strip(),
                "description": goal,
                "language": None,
                "recency": "stalled",
                "commits": [],
                "order": int(cur.get("order", 999) or 999),
                "has_note": bool(goal),
                "status_override": override,
            }
            bucket = "done" if override == "done" else "stalled"
            groups[bucket].append(entry)
        # Sort again after fallback inserts
        for g in groups:
            groups[g].sort(key=lambda e: (e["order"] if e["order"] != 999 else 1000,
                                           e["full_name"]))
    return {"groups": groups, "status": data.get("status", "ok"),
            "state": data.get("state", "ready"),
            "version": data.get("version", 0), "banner": data.get("banner")}

def hub_page() -> str:
    merged = _merge_hub_entries()
    groups = merged["groups"]
    total = sum(len(v) for v in groups.values())
    state_script = ""
    if merged.get("state") == "refreshing":
        state_script = (
            '<script>(function pollHubState(){fetch("/api/hub/state")'
            '.then(function(r){return r.json();}).then(function(s){'
            'if(s.state==="refreshing"){setTimeout(pollHubState,1000);return;}'
            'window.location.reload();}).catch(function(){setTimeout(pollHubState,2500);});'
            '})();</script>'
        )
    body = '<div class="page-head"><div><h1>Hub</h1>'
    body += '<p>All your projects in one place — GitHub activity, curated notes, and AI summaries.</p></div>'
    body += '<div class="admin-link"><a class="button" href="/hub/admin">Curate Hub</a></div></div>'
    if merged.get("banner"):
        body += '<div class="notice">' + html.escape(merged["banner"]) + '</div>'
    if total == 0:
        if merged.get("state") == "refreshing":
            body += '<div class="empty-state"><p>Loading GitHub activity…</p>'
            body += '<p>Your curated projects remain available while the first snapshot loads.</p></div>'
        elif merged.get("status") == "error":
            body += '<div class="empty-state"><p>Hub data is temporarily unavailable.</p>'
            body += '<p>Refresh later or check the GitHub integration.</p></div>'
        else:
            body += '<div class="empty-state"><p>No projects yet.</p>'
            body += '<p>Set <code>GITHUB_TOKEN</code> to populate the Hub from your repositories.</p></div>'
        body += state_script
        return html_page("Hub", body, active_nav="hub")
    group_labels = {"active": "Active", "maintain": "Maintaining",
                    "stalled": "Stalled", "done": "Done"}
    for key in ("active", "maintain", "stalled", "done"):
        entries = groups.get(key, [])
        if not entries:
            continue
        body += f'<section class="hub-group"><div class="section-head"><h2>{group_labels[key]}</h2>'
        body += f'<span class="status-pill">{len(entries)}</span></div>'
        body += '<div class="projects-grid">'
        for e in entries:
            body += _hub_card_html(e)
        body += '</div></section>'
    # JS poll for summaries (non-blocking fill from cache)
    body += state_script
    body += ('<script>'
             'function refreshSummaries(){'
             'fetch("/api/hub/summaries").then(function(r){return r.json();}).then(function(d){'
             'var s=d.summaries||{};'
             'Object.keys(s).forEach(function(fn){'
             'var el=document.querySelector(\'[data-summary="\'+fn+\'"]\');'
             'if(el&&s[fn]){el.textContent=s[fn];el.classList.remove("summarizing");}});'
             'var states=d.states||{};Object.keys(states).forEach(function(fn){'
             'var el=document.querySelector(\'[data-summary="\'+fn+\'"]\');'
             'if(el&&states[fn]==="fallback"){el.remove();}});'
             'if(d.pending&&d.pending.length){setTimeout(refreshSummaries,2500);}'
             '}).catch(function(){});}'
             'document.addEventListener("DOMContentLoaded",function(){setTimeout(refreshSummaries,800);});'
             '</script>')
    return html_page("Hub", body, active_nav="hub")

def _hub_card_html(e: dict) -> str:
    fn = e["full_name"]
    name = html.escape(e["name"])
    url = html.escape(e["html_url"], quote=True)
    desc = html.escape(e["description"]) if e["description"] else ""
    lang = html.escape(e["language"]) if e["language"] else ""
    # Summary placeholder (filled by JS poll)
    summary_html = (f'<p class="card-summary summarizing" data-summary="{html.escape(fn)}">'
                    f'Summarizing…</p>')
    # Recency / status pill
    if e["status_override"] == "done":
        pill = '<span class="status-pill">done</span>'
    else:
        pill = f'<span class="status-pill">{html.escape(e["recency"])}</span>'
    # Attention flag: stalled + no curation note
    attention = ''
    if e["recency"] == "stalled" and not e["has_note"]:
        attention = '<p class="hub-attention">⚠ Needs attention — add a note or mark done.</p>'
    # Recent commits (last 3 subjects)
    commits_html = ''
    commits = e["commits"][:3]
    if commits:
        commits_html = '<ul class="hub-commits">'
        for c in commits:
            subj = html.escape(str(c.get("subject", "")).strip())
            if subj:
                commits_html += f'<li>{subj}</li>'
        commits_html += '</ul>'
    card = f'<article class="project-card"><div class="project-card-head"><h2>'
    if url:
        card += f'<a href="{url}" target="_blank" rel="noopener">{name}</a>'
    else:
        card += name
    card += f'</h2>{pill}</div>'
    if desc:
        card += f'<p>{desc}</p>'
    card += summary_html
    if lang:
        card += f'<p class="hub-lang">{lang}</p>'
    card += commits_html
    card += attention
    card += f'<div class="project-actions"><a class="button" href="/hub/admin#{html.escape(fn)}">Curate</a></div>'
    card += '</article>'
    return card

def hub_admin_page(message: str = "") -> str:
    """Auth-gated curation page listing every Hub repo with editable fields."""
    data = get_hub_repos(force=False)
    repos = data.get("repos", []) or []
    body = '<div class="page-head"><div><h1>Curate Hub</h1>'
    body += '<p>Add goals, override status, reorder, and hide projects.</p></div></div>'
    if message:
        body += '<div class="notice">' + html.escape(message) + '</div>'
    # Action buttons (refresh + backup)
    body += '<div class="admin-actions" style="margin-bottom:1.5rem">'
    body += '<form method="post" action="/hub/admin/refresh" style="display:inline">'
    body += '<button class="button" type="submit">Refresh hub now</button></form>'
    body += '<form method="post" action="/hub/admin/backup" style="display:inline;margin-left:.5rem">'
    body += '<button class="button" type="submit">Download backup</button></form>'
    body += '</div>'
    if not repos:
        body += '<div class="empty-state"><p>No projects to curate yet.</p>'
        body += '<p>Set <code>GITHUB_TOKEN</code> to populate the Hub.</p></div>'
        return html_page("Curate Hub", body, active_nav="hub")
    for repo in repos:
        fn = str(repo.get("full_name", "")).strip()
        if not fn:
            continue
        cur = load_hub().get(fn, {}) or {}
        goal = html.escape(str(cur.get("goal", "")))
        live = html.escape(str(cur.get("live_url", "")), quote=True)
        local = html.escape(str(cur.get("local_path", "")), quote=True)
        override = str(cur.get("status_override", "")).strip().lower()
        order = int(cur.get("order", 999) or 999)
        hidden = bool(cur.get("hidden"))
        fid = "hub_" + re.sub(r"[^a-zA-Z0-9]", "_", fn)
        body += f'<section class="admin-panel" id="{html.escape(fn)}"><h2>{html.escape(repo.get("name", fn))}</h2>'
        body += f'<form class="project-form" method="post" action="/hub/admin/update">'
        body += f'<input type="hidden" name="full_name" value="{html.escape(fn)}">'
        body += f'<label class="wide">Goal / note<input name="goal" value="{goal}" placeholder="What is this project for?"></label>'
        body += f'<label>Live URL<input name="live_url" value="{live}" placeholder="https://…"></label>'
        body += f'<label>Local path<input name="local_path" value="{local}" placeholder="/srv/…"></label>'
        body += ('<label>Status override<select name="status_override">'
                 f'<option value=""{" selected" if override=="" else ""}>Auto (by recency)</option>'
                 f'<option value="done"{" selected" if override == "done" else ""}>Done</option>'
                 '</select></label>')
        body += f'<label>Order<input type="number" name="order" value="{order}" min="0"></label>'
        body += f'<label class="check"><input type="checkbox" name="hidden" value="1"{" checked" if hidden else ""}> Hidden</label>'
        body += '<div class="admin-actions">'
        body += '<button class="button primary" type="submit">Save</button>'
        body += f'<a class="button danger" href="/hub/admin/delete?full_name={urllib.parse.quote(fn)}">Delete curation</a>'
        body += f'<a class="button" href="/hub#{urllib.parse.quote(fn)}">View on Hub</a>'
        body += '</div></form></section>'
    return html_page("Curate Hub", body, active_nav="hub")

#  HTTP Handler

# Model tuning helpers

# ── LLM Lab: evals, traces, arena, routing, HF GGUF discovery ──

class Handler(http.server.BaseHTTPRequestHandler):
    def _host_is_allowed(self) -> bool:
        """Reject requests for hostnames not explicitly configured."""
        raw_host = self.headers.get("Host", "")
        try:
            hostname = urllib.parse.urlsplit(f"//{raw_host}").hostname or ""
        except ValueError:
            return False
        return hostname.lower() in ALLOWED_HOSTS

    def _reject_unallowed_host(self) -> bool:
        if self._host_is_allowed():
            return False
        self._respond(421, "text/plain", b"Misdirected Request")
        return True

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

    def hub_summaries_api(self):
        """Lazy JSON endpoint: return cached summaries, trigger background fills.
        Never blocks on Ollama; never includes URL/model/prompt/errors."""
        data = get_hub_repos(force=False)
        repos = data.get("repos", []) or []
        result = _OLLAMA_CLIENT.request_summaries(repos)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode("utf-8"))

    def do_GET(self):
        import urllib.parse
        if self._reject_unallowed_host():
            return
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
        elif path == "/api/status":
            self._respond(200, "application/json", json.dumps(get_monitor_status()).encode())
        elif path == "/api/hub/state":
            self._respond(200, "application/json", json.dumps(_GITHUB_CLIENT.state()).encode())
        elif path == "/api/hub/summaries":
            self.hub_summaries_api()
        elif path == "/hub":
            content = hub_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/hub/admin":
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            content = hub_admin_page(qs.get("message", [""])[0]).encode()
            self._respond(200, "text/html", content)
        elif path == "/health":
            self._respond(200, "text/plain", b"ok")
        else:
            self._respond(404, "text/plain", b"Not Found")

    def do_POST(self):
        import urllib.parse
        if self._reject_unallowed_host():
            return
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
        elif path.startswith("/hub/admin/"):
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            action = path.removeprefix("/hub/admin/")
            if action == "update":
                message = update_hub("update", get)
                self._send_redirect("/hub/admin?" + urllib.parse.urlencode({"message": message}))
            elif action == "delete":
                fn = get("full_name")
                message = update_hub("delete", lambda k: fn if k == "full_name" else get(k))
                self._send_redirect("/hub/admin?" + urllib.parse.urlencode({"message": message}))
            elif action == "toggle-hide":
                fn = get("full_name")
                message = update_hub("toggle-hide", lambda k: fn if k == "full_name" else get(k))
                self._send_redirect("/hub/admin?" + urllib.parse.urlencode({"message": message}))
            elif action == "refresh":
                _OLLAMA_CLIENT.invalidate()
                _GITHUB_CLIENT.invalidate()
                get_hub_repos(force=True)
                self._send_redirect("/hub/admin?" + urllib.parse.urlencode({"message": "Hub refreshed."}))
            elif action == "backup":
                import tarfile, tempfile, time
                try:
                    with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b") as spool:
                        with tarfile.open(fileobj=spool, mode="w:gz") as tar:
                            tar.add(DATA_DIR, arcname="hub-backup")
                        size = spool.tell()
                        spool.seek(0)
                        ts = time.strftime("%Y%m%d-%H%M%S")
                        filename = f"hub-backup-{ts}.tar.gz"
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                        self.send_header("Content-Length", str(size))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        while chunk := spool.read(64 * 1024):
                            self.wfile.write(chunk)
                except OSError:
                    self._respond(500, "text/plain", b"Backup failed: data directory unavailable.")
            else:
                self._respond(404, "text/plain", b"Not Found")
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
    print(f"Control Center → http://127.0.0.1:{port}")
    server.serve_forever()
