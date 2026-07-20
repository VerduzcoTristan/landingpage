#!/usr/bin/env python3
"""Control Center server for briefings, monitoring, and living projects."""

import http.server
import html
import json
import hmac
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from github_client import GitHubClient
from hub_store import HubStore, InsightStore, normalise_hub
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
CSRF_TOKEN = secrets.token_urlsafe(32)
_BOOKMARK_LOCK = threading.RLock()
_ACCESS_JWKS_LOCK = threading.RLock()
_ACCESS_JWKS_CACHE: dict = {"keys": {}, "expires_at": 0.0}
_ACCESS_JWKS_TTL = 3600

def _csrf_field() -> str:
    return f'<input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN, quote=True)}">'

def _valid_csrf(value: str) -> bool:
    return bool(value) and hmac.compare_digest(str(value), CSRF_TOKEN)

# ── Auth helpers (Cloudflare Access) ──
def _access_team_domain() -> str:
    value = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")
    if not value:
        return ""
    if not value.startswith(("https://", "http://")):
        value = "https://" + value
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _access_jwks() -> dict:
    """Load and cache the configured Cloudflare Access JSON web keys."""
    team_domain = _access_team_domain()
    if not team_domain:
        return {}
    now = time.time()
    with _ACCESS_JWKS_LOCK:
        if _ACCESS_JWKS_CACHE["keys"] and _ACCESS_JWKS_CACHE["expires_at"] > now:
            return dict(_ACCESS_JWKS_CACHE["keys"])
        url = os.environ.get(
            "CF_ACCESS_JWKS_URL",
            f"{team_domain}/cdn-cgi/access/certs",
        ).strip()
        if not url.startswith("https://"):
            return {}
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read(512 * 1024).decode("utf-8"))
            keys = {
                item.get("kid"): item
                for item in payload.get("keys", [])
                if isinstance(item, dict) and item.get("kid") and item.get("kty") == "RSA"
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        if not keys:
            return {}
        _ACCESS_JWKS_CACHE.update({"keys": keys, "expires_at": now + _ACCESS_JWKS_TTL})
        return dict(keys)


def _verify_access_jwt(token: str) -> bool:
    """Verify an RS256 Cloudflare Access JWT and its issuer/audience/time claims."""
    audience = os.environ.get("CF_ACCESS_AUDIENCE", "").strip()
    team_domain = _access_team_domain()
    if not token or not audience or not team_domain:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        signature = _b64url_decode(parts[2])
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if header.get("alg") != "RS256" or not header.get("kid"):
        return False
    if payload.get("iss") != team_domain:
        return False
    token_audience = payload.get("aud")
    if isinstance(token_audience, str):
        token_audience = [token_audience]
    if not isinstance(token_audience, list) or audience not in token_audience:
        return False
    now = time.time()
    try:
        if float(payload["exp"]) <= now:
            return False
        if "nbf" in payload and float(payload["nbf"]) > now + 30:
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    key = _access_jwks().get(header["kid"], {})
    try:
        modulus = int.from_bytes(_b64url_decode(key["n"]), "big")
        exponent = int.from_bytes(_b64url_decode(key["e"]), "big")
        if modulus <= 0 or exponent <= 1:
            return False
        key_size = (modulus.bit_length() + 7) // 8
        if len(signature) != key_size:
            return False
        encoded = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(key_size, "big")
        digest_info = bytes.fromhex(
            "3031300d060960864801650304020105000420"
        ) + hashlib.sha256(f"{parts[0]}.{parts[1]}".encode("ascii")).digest()
        expected = b"\x00\x01" + b"\xff" * (key_size - len(digest_info) - 3) + b"\x00" + digest_info
        return hmac.compare_digest(encoded, expected)
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def is_authenticated(handler) -> bool:
    """Check a signed Cloudflare Access JWT or the explicit localhost bypass."""
    client_ip = handler.client_address[0] if hasattr(handler, "client_address") else ""
    if client_ip in ("127.0.0.1", "::1"):
        return True
    return _verify_access_jwt(handler.headers.get("Cf-Access-Jwt-Assertion", ""))

_UNAUTH_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Access required — Control Center</title><style>
:root{color-scheme:dark;--page:#080b12;--surface:#111725;--border:#263148;--text:#edf2ff;--muted:#98a5bd;--accent:#8b7cff}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:1.5rem;background:radial-gradient(circle at 50% 0,#18213a 0,transparent 42%),var(--page);color:var(--text);font:16px/1.6 Inter,ui-sans-serif,system-ui,sans-serif}
.access-card{width:min(100%,28rem);padding:2.5rem;border:1px solid var(--border);border-radius:1.25rem;background:linear-gradient(145deg,rgba(255,255,255,.04),transparent),var(--surface);box-shadow:0 24px 70px rgba(0,0,0,.45);text-align:center}.eyebrow{color:var(--accent);font-size:.75rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase}h1{margin:.5rem 0;font-size:2rem}p{margin:0;color:var(--muted)}
</style></head><body><main class="access-card"><div class="eyebrow">Control Center</div><h1>Access required</h1><p>Authenticate through Cloudflare Access to continue.</p></main></body></html>"""

# ── Briefing Archive (DB-backed) ──
# Import the checkout-local module. The script directory is already on
# sys.path; prepending a user tools directory would let a stale copy shadow it.
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
    with _BOOKMARK_LOCK:
        if BOOKMARKS_FILE.exists():
            try:
                data = json.loads(BOOKMARKS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        "saved": data.get("saved") if isinstance(data.get("saved"), list) else [],
                        "read_later": data.get("read_later") if isinstance(data.get("read_later"), list) else [],
                    }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        return {"saved": [], "read_later": []}

def _save_bookmarks(data: dict):
    """Write bookmarks to disk atomically."""
    with _BOOKMARK_LOCK:
        BOOKMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=BOOKMARKS_FILE.parent,
            prefix=f".{BOOKMARKS_FILE.name}.", suffix=".tmp", delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(data, handle, indent=2, default=str)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, BOOKMARKS_FILE)
        finally:
            temporary.unlink(missing_ok=True)

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
    with _BOOKMARK_LOCK:
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
                "saved_at": datetime.now(timezone.utc).isoformat(),
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
# Server-rendered pages share these navigation rules and markup.
NAV_CSS = """
:root{--page:#080b12;--surface:#101624;--surface-raised:#151d2e;--overlay:#1b2538;--border:#28344b;--border-strong:#3b4964;--text:#eef3ff;--muted:#9ba9c1;--subtle:#6f7d95;--accent:#8b7cff;--accent-strong:#a99eff;--accent-soft:rgba(139,124,255,.13);--success:#45d69a;--warning:#f3b95f;--danger:#ff6b7a;--shadow-1:0 10px 30px rgba(0,0,0,.22);--shadow-2:0 24px 70px rgba(0,0,0,.38);--radius-sm:.55rem;--radius-md:.85rem;--radius-lg:1.2rem;--shell:72rem;--ease:180ms ease}
.skip-link{position:fixed;left:1rem;top:-5rem;z-index:500;padding:.65rem 1rem;border-radius:0 0 var(--radius-sm) var(--radius-sm);background:var(--accent);color:#fff;font-weight:800;text-decoration:none;transition:top var(--ease)}.skip-link:focus{top:0}
.site-nav{position:sticky;top:0;z-index:100;border-bottom:1px solid rgba(59,73,100,.65);background:rgba(8,11,18,.78);backdrop-filter:blur(18px) saturate(140%)}.nav-shell{width:min(100% - 2rem,var(--shell));min-height:4rem;margin:auto;display:flex;align-items:center;justify-content:space-between;gap:1.5rem}.brand{display:inline-flex;align-items:center;gap:.65rem;color:var(--text);font-weight:850;letter-spacing:-.025em;text-decoration:none;white-space:nowrap}.brand-mark{display:grid;place-items:center;width:1.9rem;height:1.9rem;border:1px solid rgba(169,158,255,.45);border-radius:.6rem;background:linear-gradient(145deg,rgba(139,124,255,.28),rgba(69,214,154,.08));color:var(--accent-strong);box-shadow:inset 0 1px rgba(255,255,255,.12)}.nav-links{display:flex;align-items:center;gap:1.25rem}.nav-links a{position:relative;padding:1.35rem 0 1.2rem;color:var(--muted);font-size:.88rem;font-weight:700;text-decoration:none;transition:color var(--ease)}.nav-links a::after{content:"";position:absolute;left:0;right:0;bottom:.75rem;height:2px;border-radius:2px;background:var(--accent);transform:scaleX(0);transition:transform var(--ease)}.nav-links a:hover,.nav-links a.active{color:var(--text)}.nav-links a.active::after{transform:scaleX(1)}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid rgba(139,124,255,.58);outline-offset:3px}
.site-footer{width:min(100% - 2rem,var(--shell));margin:4rem auto 0;padding:1.5rem 0 2.5rem;border-top:1px solid var(--border);display:flex;justify-content:space-between;gap:1rem;color:var(--subtle);font-size:.8rem}.site-footer nav{display:flex;gap:1rem}.site-footer a{color:var(--muted);text-decoration:none}.site-footer a:hover{color:var(--text)}
@media(max-width:720px){.nav-shell{min-height:3.5rem;gap:.75rem}.brand>span:last-child{display:none}.nav-links{min-width:0;gap:.8rem;overflow-x:auto;scrollbar-width:none}.nav-links::-webkit-scrollbar{display:none}.nav-links a{display:inline-flex;align-items:center;min-height:2.75rem;padding:.55rem 0;font-size:.8rem;white-space:nowrap}.nav-links a::after{bottom:.25rem}.site-footer{align-items:flex-start;flex-direction:column}}
@media(orientation:landscape) and (max-height:600px){.nav-shell{min-height:3rem;gap:1rem}.brand-mark{width:1.7rem;height:1.7rem}.nav-links a{display:inline-flex;align-items:center;min-height:2.75rem;padding:.45rem 0}.nav-links a::after{bottom:.15rem}.site-footer{margin-top:2rem;padding:.8rem 0 1rem}}
"""

def render_nav(active: str = "home") -> str:
    links = []
    for href, label, key in (
        ("/", "Home", "home"),
        ("/briefings", "Briefings", "briefings"),
        ("/hub", "Projects", "hub"),
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
.button,button.button{display:inline-flex;align-items:center;justify-content:center;min-height:2.75rem;padding:.55rem .9rem;border:1px solid var(--border-strong);border-radius:var(--radius-sm);background:var(--surface-raised);color:var(--text);font-weight:750;text-decoration:none;cursor:pointer;box-shadow:0 1px rgba(255,255,255,.04);transition:transform var(--ease),border-color var(--ease),background var(--ease),box-shadow var(--ease)}.button:hover{transform:translateY(-1px);border-color:var(--accent);box-shadow:var(--shadow-1)}.button.primary{border-color:transparent;background:linear-gradient(135deg,var(--accent),#6f80ff);color:white}.button.danger{border-color:rgba(255,107,122,.45);color:#ff9ca6}.button.danger:hover{background:rgba(255,107,122,.1)}
.empty-state{padding:2.5rem;border:1px dashed var(--border-strong);border-radius:var(--radius-lg);background:linear-gradient(145deg,rgba(255,255,255,.025),transparent),var(--surface);text-align:center;color:var(--muted);box-shadow:var(--shadow-1)}.empty-state p{margin:0 0 1rem}.empty-state p:last-child{margin-bottom:0}.notice{margin:0 0 1rem;padding:.8rem 1rem;border:1px solid rgba(69,214,154,.3);border-radius:var(--radius-sm);background:rgba(69,214,154,.08);color:#a9f2d3}
.category-tabs{display:flex;align-items:center;gap:.5rem;overflow-x:auto;padding:.2rem 0 .8rem;scrollbar-width:none}.category-tabs::-webkit-scrollbar{display:none}.category-tab{display:inline-flex;align-items:center;gap:.4rem;flex:0 0 auto;padding:.42rem .75rem;border:1px solid var(--border);border-radius:999px;background:rgba(16,22,36,.72);color:var(--muted);font-size:.78rem;font-weight:750;text-decoration:none;transition:all var(--ease)}.category-tab:hover,.category-tab.active{border-color:rgba(139,124,255,.7);background:var(--accent-soft);color:var(--text)}.tab-count{display:inline-grid;place-items:center;min-width:1.25rem;height:1.25rem;padding:0 .3rem;border-radius:999px;background:rgba(255,255,255,.07);font-size:.68rem}.briefing-sort{display:flex;align-items:center;justify-content:flex-end;gap:.55rem;margin:.2rem 0 1rem;color:var(--muted);font-size:.78rem}.briefing-sort select,input,textarea,select{border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);padding:.65rem .75rem;transition:border-color var(--ease),box-shadow var(--ease)}input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);outline:0}
.briefing-archive-grid,.projects-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,19rem),1fr));gap:1rem}.briefing-archive-card,.project-card,.admin-panel{border:1px solid var(--border);border-radius:var(--radius-lg);background:linear-gradient(150deg,rgba(255,255,255,.04),transparent 38%),var(--surface);box-shadow:var(--shadow-1);transition:transform var(--ease),border-color var(--ease),box-shadow var(--ease)}.briefing-archive-card{display:flex;min-height:12rem;flex-direction:column;gap:.8rem;padding:1.1rem;color:var(--text);text-decoration:none}.briefing-archive-card:hover,.project-card:hover{transform:translateY(-3px);border-color:var(--border-strong);box-shadow:var(--shadow-2)}.briefing-card-topline,.project-card-head{display:flex;align-items:center;justify-content:space-between;gap:.75rem}.briefing-date-chip,.briefing-count-chip,.status-pill,.category-badge{display:inline-flex;align-items:center;width:max-content;border-radius:999px;font-size:.7rem;font-weight:800;letter-spacing:.025em}.briefing-date-chip{color:var(--accent-strong)}.briefing-count-chip,.status-pill{padding:.25rem .55rem;border:1px solid var(--border);background:rgba(255,255,255,.035);color:var(--muted)}.briefing-top-story{font-size:1.08rem;font-weight:800;line-height:1.35}.briefing-preview-list{margin:0;padding-left:1.1rem;color:var(--muted);font-size:.83rem}.briefing-card-footer{margin-top:auto;color:var(--subtle);font-size:.75rem;font-weight:700}
.briefing-header{margin:1.5rem 0 1rem}.briefing-header .date{color:var(--accent-strong);font-size:.76rem;font-weight:800;text-transform:uppercase;letter-spacing:.1em}.briefing-grid,.dashboard-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,20rem),1fr));gap:1rem}.briefing-card{padding:1.2rem;border:1px solid var(--border);border-radius:var(--radius-lg);background:linear-gradient(145deg,rgba(139,124,255,.06),transparent 40%),var(--surface);box-shadow:var(--shadow-1)}.briefing-card h3{margin:.35rem 0 .6rem}.card-num{color:var(--accent-strong);font-size:.72rem;font-weight:850}.card-summary,.card-impact{color:var(--muted);font-size:.92rem;line-height:1.6}.card-impact{margin-bottom:.5rem;color:#cad4e8}.card-source{margin-top:.8rem;color:var(--subtle);font-size:.78rem}.card-source a{text-decoration:none}.card-categories{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.7rem}.category-badge{padding:.2rem .5rem}.bm-btn-row{margin-top:.85rem}.bm-btn{padding:.4rem .7rem;border:1px solid var(--border);border-radius:999px;background:transparent;color:var(--muted);font-size:.74rem;font-weight:750;cursor:pointer;transition:all var(--ease)}.bm-btn:hover,.bm-btn.active{border-color:var(--accent);background:var(--accent-soft);color:var(--text)}
.home-eyebrow{display:block;margin-bottom:.35rem;color:var(--accent-strong);font-size:.7rem;font-weight:850;letter-spacing:.14em;text-transform:uppercase}.home-head h1{margin:0;font-size:clamp(2.2rem,5vw,3.6rem)}.briefing-home{overflow:hidden;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface);box-shadow:var(--shadow-1)}.briefing-home-row{display:flex;align-items:flex-start;gap:.75rem;padding:1.1rem 1.2rem;border-top:1px solid var(--border)}.briefing-home-row:first-child{border-top:0}.bh-main{min-width:0;width:100%}.bh-title-line{display:flex;align-items:baseline;flex-wrap:wrap;gap:.55rem}.bh-title{color:var(--text);font-size:1rem;font-weight:800;text-decoration:none}.bh-impact{margin-top:.4rem;color:var(--muted);font-size:.96rem;line-height:1.68;white-space:pre-line;overflow:visible}
.home-dashboard{display:grid;grid-template-columns:minmax(0,2fr) minmax(17rem,1fr);gap:1.25rem;align-items:start}.home-primary .section-head,.home-rail .section-head{margin-top:0}.home-rail{display:grid;gap:1rem}.home-rail-block{padding:1rem;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface);box-shadow:var(--shadow-1)}.home-rail-block .landing-status-strip{margin:0;box-shadow:none}.home-focus-list{display:grid;gap:.55rem}.home-focus-item{display:grid;grid-template-columns:1fr auto;gap:.15rem .6rem;padding:.7rem;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--page);color:var(--text);text-decoration:none}.home-focus-item strong{font-size:.86rem}.home-focus-item small{grid-column:1;color:var(--muted);line-height:1.35}.home-focus-item .status-pill{grid-column:2;grid-row:1/3;align-self:center}.home-rail-empty{margin:0;color:var(--muted);font-size:.84rem}
.home-focus-item .home-focus-current,.home-focus-item .home-focus-next{grid-column:1;display:block}.home-focus-item .home-focus-current{color:var(--text)}.home-focus-item .home-focus-next{color:var(--muted)}.home-focus-next b{color:var(--accent-strong);font-size:.68rem;text-transform:uppercase;letter-spacing:.04em}.home-focus-item.is-pinned{border-color:rgba(169,158,255,.4)}
.landing-status-strip{margin:1.4rem 0;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface);box-shadow:var(--shadow-1);overflow:hidden}.landing-status-strip summary{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.85rem 1rem;cursor:pointer;list-style:none}.landing-status-strip summary::-webkit-details-marker{display:none}.status-summary-left,.status-summary-right{display:flex;align-items:center;gap:.55rem}.status-summary-title{font-weight:800}.status-summary-meta{color:var(--muted);font-size:.8rem}.status-mini-pill{padding:.2rem .5rem;border:1px solid var(--border);border-radius:999px;color:var(--muted);font-size:.68rem}.status-mini-pill.ok{color:var(--success)}.status-mini-pill.warn{color:var(--warning)}.status-strip-dot,.status-dot{display:inline-block;width:.65rem;height:.65rem;border-radius:50%;background:var(--subtle);box-shadow:0 0 0 4px rgba(111,125,149,.1)}.green{background:var(--success)!important}.red{background:var(--danger)!important}.amber{background:var(--warning)!important}.landing-status-body{padding:1rem;border-top:1px solid var(--border)}
.status-mini-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:.55rem}.status-mini-service{display:flex;align-items:center;justify-content:space-between;gap:.7rem;padding:.65rem .75rem;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--page);color:var(--text);font-size:.78rem;text-decoration:none}.status-mini-service>span{display:flex;align-items:center;gap:.5rem}.status-mini-service small{color:var(--muted)}.status-board{display:grid;gap:.75rem}.status-check{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1rem 1.1rem;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface);box-shadow:var(--shadow-1)}.status-check>div{display:flex;align-items:center;gap:.7rem}.status-check>span{color:var(--muted);font-size:.8rem}.monitor-links,.project-actions,.admin-actions{display:flex;flex-wrap:wrap;gap:.6rem}.link-card{padding:.7rem .9rem;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);font-weight:750;text-decoration:none;transition:all var(--ease)}.link-card:hover{transform:translateY(-1px);border-color:var(--accent)}
.home-links{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem;margin:1rem 0 0}.home-links a{display:grid;grid-template-columns:1fr auto;gap:.1rem .7rem;padding:1rem 1.1rem;border:1px solid var(--border);border-radius:var(--radius-md);background:linear-gradient(145deg,rgba(255,255,255,.035),transparent),var(--surface);color:var(--text);text-decoration:none;box-shadow:var(--shadow-1);transition:all var(--ease)}.home-links a:hover{transform:translateY(-2px);border-color:var(--accent)}.home-links span{font-weight:850}.home-links small{grid-column:1;color:var(--muted)}.home-links b{grid-column:2;grid-row:1/3;align-self:center;color:var(--accent-strong)}.project-card{padding:1.2rem}.project-card h3{margin:0;font-size:1.15rem}.project-card p{margin:.65rem 0;color:var(--muted)}.admin-link{margin:1.3rem 0;text-align:right}.admin-panel{margin-bottom:1rem;padding:1.2rem}.admin-panel h2{font-size:1.05rem}.project-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.85rem}.project-form label{display:grid;gap:.35rem;color:var(--muted);font-size:.75rem;font-weight:750}.project-form .wide{grid-column:1/-1}.project-form .check{display:flex;align-items:center;gap:.5rem}.project-form .check input{width:auto}.project-form textarea{resize:vertical}.admin-actions{margin-top:.9rem;padding-top:.9rem;border-top:1px solid var(--border)}.admin-actions form{margin:0}
.admin-toolbar{display:grid;grid-template-columns:minmax(14rem,1fr) auto;gap:.75rem;align-items:end;margin:0 0 1rem}.admin-search{display:grid;gap:.35rem;color:var(--muted);font-size:.75rem;font-weight:750}.admin-filters{display:flex;flex-wrap:wrap;gap:.4rem}.admin-filters button{min-height:2.75rem;padding:.5rem .7rem;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--muted);cursor:pointer}.admin-filters button[aria-pressed="true"]{border-color:var(--accent);background:var(--accent-soft);color:var(--text)}.admin-result-count{grid-column:1/-1;color:var(--subtle);font-size:.76rem}.admin-repo-list{display:grid;gap:.55rem}.admin-repo{border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface);box-shadow:var(--shadow-1)}.admin-repo>summary{display:flex;align-items:center;justify-content:space-between;gap:1rem;min-height:3.5rem;padding:.75rem .9rem;cursor:pointer}.admin-repo>summary>span:first-child{display:grid}.admin-repo>summary small{color:var(--subtle);font-size:.72rem}.admin-repo-badges{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:.35rem}.admin-repo-body{padding:1rem;border-top:1px solid var(--border)}.admin-tech-links{display:flex;flex-wrap:wrap;gap:.8rem;margin:0 0 1rem;color:var(--subtle);font-size:.78rem}.admin-tech-links a{text-decoration:none}
.hub-overview{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:.55rem;margin:0 0 1.5rem}.hub-filter{display:flex;align-items:center;justify-content:space-between;gap:.5rem;min-height:2.75rem;padding:.55rem .7rem;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--muted);cursor:pointer}.hub-filter strong{color:var(--text)}.hub-filter[aria-pressed="true"]{border-color:var(--accent);background:var(--accent-soft);color:var(--text)}.hub-group{margin:1.5rem 0}.hub-low-priority{border-top:1px solid var(--border)}.hub-low-priority>summary{display:flex;align-items:center;justify-content:space-between;min-height:3.25rem;padding:.65rem 0;cursor:pointer}.hub-group-title{font-size:1.3rem;font-weight:800}.hub-low-priority>.projects-grid{padding-top:.65rem}.status-active{border-color:rgba(69,214,154,.35);color:var(--success)}.status-maintain{border-color:rgba(169,158,255,.35);color:var(--accent-strong)}.status-stalled{border-color:rgba(243,185,95,.35);color:var(--warning)}.status-done{color:var(--subtle)}.hub-description{font-size:.88rem}.hub-decision{margin:.7rem 0;padding:.65rem .75rem;border-left:2px solid var(--border-strong);background:rgba(255,255,255,.02)}.hub-decision span,.hub-attention>span{color:var(--subtle);font-size:.68rem;font-weight:850;letter-spacing:.08em;text-transform:uppercase}.hub-decision p{margin:.2rem 0 0;color:var(--text)}.hub-updated{font-size:.76rem}.hub-commits{margin:.6rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:.82rem}.hub-commits li{margin:.15rem 0}.hub-lang{color:var(--subtle);font-size:.78rem;margin:.4rem 0 0}.hub-attention{margin:.75rem 0;padding:.6rem .7rem;border:1px solid rgba(243,185,95,.22);border-radius:var(--radius-sm);background:rgba(243,185,95,.05);color:var(--warning);font-size:.78rem}.hub-attention ul{margin:.3rem 0 0;padding-left:1rem}.hub-references{display:flex;flex-wrap:wrap;gap:.65rem;margin:.75rem 0;color:var(--subtle);font-size:.76rem}.hub-references a{text-decoration:none}.hub-edit{color:var(--subtle);font-size:.76rem;text-decoration:none}.hub-edit:hover{color:var(--text)}.summarizing{color:var(--subtle);font-style:italic}
.project-card{position:relative;overflow:hidden}.project-card.is-pinned{border-color:rgba(169,158,255,.48)}.project-card.is-new::before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--accent)}.project-card-head{align-items:flex-start}.hub-card-badges{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:.35rem}.hub-card-badges .status-pill{white-space:nowrap}.hub-pin{color:var(--accent-strong)}.hub-new-badge{display:none;color:var(--success)}.project-card.is-new .hub-new-badge{display:inline-flex}.hub-description{margin-bottom:.45rem!important}.hub-decision.current{border-left-color:var(--accent);background:var(--accent-soft)}.hub-decision.next{border-left-color:var(--success)}.hub-decision .source{float:right;color:var(--subtle);font-size:.62rem;letter-spacing:.03em;text-transform:none}.hub-decision .placeholder{color:var(--subtle);font-style:italic}.hub-goal{margin:.55rem 0;color:var(--muted);font-size:.8rem}.hub-goal strong{color:var(--text)}.hub-insight-meta{display:flex;flex-wrap:wrap;gap:.4rem .75rem;margin:.65rem 0;color:var(--subtle);font-size:.72rem}.hub-insight-meta a{color:var(--muted);text-decoration:none}.hub-evidence,.hub-history{margin:.65rem 0;border-top:1px solid var(--border)}.hub-evidence>summary,.hub-history>summary{min-height:2.75rem;padding:.65rem 0;color:var(--muted);font-size:.76rem;font-weight:750;cursor:pointer}.hub-file-list,.hub-history-list{display:grid;gap:.4rem;margin:0 0 .65rem;padding:0;list-style:none}.hub-file-list li{display:flex;justify-content:space-between;gap:.6rem;padding:.4rem .5rem;border-radius:var(--radius-sm);background:var(--page);color:var(--muted);font:500 .7rem/1.4 ui-monospace,SFMono-Regular,Consolas,monospace}.hub-file-list li span:first-child{min-width:0;overflow-wrap:anywhere}.hub-file-list small{color:var(--subtle);white-space:nowrap}.hub-history-list li{padding:.55rem .65rem;border-left:2px solid var(--border);background:rgba(255,255,255,.02);color:var(--muted);font-size:.76rem}.hub-history-list strong{display:block;margin-bottom:.15rem;color:var(--text)}.hub-history-list time{display:block;margin-top:.25rem;color:var(--subtle);font-size:.68rem}.hub-filter[data-hub-filter="new"] strong{color:var(--success)}
.admin-suggestion{grid-column:1/-1;padding:.7rem .8rem;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--page)}.admin-suggestion span{color:var(--subtle);font-size:.68rem;font-weight:850;letter-spacing:.08em;text-transform:uppercase}.admin-suggestion p{margin:.25rem 0 0;color:var(--muted);font-size:.82rem}.field-label-row{display:flex;align-items:center;justify-content:space-between;gap:.6rem}.field-label-row button{padding:0;border:0;background:none;color:var(--accent-strong);font-size:.7rem;font-weight:750;cursor:pointer}.project-form .admin-help{color:var(--subtle);font-size:.68rem;font-weight:500}.admin-repo-badges .hub-pin{color:var(--accent-strong)}
code{padding:.15rem .35rem;border:1px solid var(--border);border-radius:.35rem;background:var(--page);font:500 .86em ui-monospace,SFMono-Regular,Consolas,monospace}
@media(max-width:820px){.home-dashboard{grid-template-columns:1fr}.home-rail{grid-template-columns:1fr 1fr}.hub-overview{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:640px){.container{width:min(100% - 1.25rem,var(--shell));padding-top:1.5rem}.page-head,.section-head{align-items:flex-start;flex-direction:column}.project-form,.admin-toolbar{grid-template-columns:1fr}.project-form .wide{grid-column:auto}.landing-status-strip summary{align-items:flex-start;flex-direction:column;min-height:2.75rem}.status-summary-right{flex-wrap:wrap}.status-summary-meta{white-space:normal}.home-links,.home-rail{grid-template-columns:1fr}.hub-overview{grid-template-columns:repeat(2,minmax(0,1fr))}.site-footer{width:min(100% - 1.25rem,var(--shell))}}
@media(orientation:landscape) and (max-height:600px){body{font-size:15px;line-height:1.5}.container{padding-top:1rem}h1{font-size:clamp(1.75rem,3vw,2.35rem)}h2{font-size:clamp(1.08rem,2vw,1.35rem)}.home-head h1{font-size:clamp(1.85rem,3vw,2.4rem)}.page-head{gap:1rem;margin:.35rem 0 1rem}.page-head h1{margin-bottom:.2rem}.hero{padding:.8rem 0 .65rem!important}.hero h1{margin-bottom:.3rem}.hero p,.page-head p{font-size:.92rem;line-height:1.45}.section-head{margin:1rem 0 .5rem}.empty-state{padding:1.25rem}.briefing-header{margin:.75rem 0 .55rem}.briefing-home-row{gap:.55rem;padding:.65rem .8rem}.bh-title-line{gap:.35rem}.bh-title{font-size:.92rem}.bh-impact{margin-top:.2rem;font-size:.88rem;line-height:1.45}.briefing-archive-grid,.briefing-grid,.dashboard-grid,.projects-grid{gap:.7rem}.briefing-archive-card{min-height:0;gap:.5rem;padding:.8rem}.briefing-top-story{font-size:1rem}.briefing-card{padding:.8rem}.briefing-card h3{margin:.2rem 0 .35rem}.card-summary,.card-impact{font-size:.88rem;line-height:1.45}.card-source,.card-categories,.bm-btn-row{margin-top:.5rem}.home-dashboard{gap:.8rem}.home-rail{gap:.65rem}.home-rail-block{padding:.7rem}.home-focus-list{gap:.35rem}.home-focus-item{padding:.5rem .6rem}.landing-status-strip summary{padding:.6rem .7rem}.landing-status-body{padding:.7rem}.status-board{gap:.5rem}.status-check{padding:.7rem .8rem}.project-card{padding:.8rem}.project-card p{margin:.4rem 0}.hub-overview{gap:.4rem;margin-bottom:.8rem}.hub-group{margin:.8rem 0}.hub-low-priority>summary{min-height:2.75rem;padding:.4rem 0}.hub-group-title{font-size:1.15rem}.hub-decision{margin:.45rem 0;padding:.45rem .6rem}.hub-commits{margin-top:.4rem}.hub-attention{margin:.5rem 0;padding:.45rem .6rem}.hub-references{margin:.5rem 0}.admin-link{margin:.65rem 0}.admin-toolbar{margin-bottom:.65rem}.admin-repo-list{gap:.4rem}.admin-repo>summary{min-height:2.75rem;padding:.5rem .7rem}.admin-repo-body{padding:.7rem}.admin-tech-links{margin-bottom:.65rem}.admin-panel{margin-bottom:.65rem;padding:.8rem}}
@media(orientation:landscape) and (max-height:600px) and (min-width:700px){.home-dashboard{grid-template-columns:minmax(0,2fr) minmax(15rem,.95fr)}.home-rail,.home-rail-block{min-width:0}.home-rail{grid-template-columns:1fr}.home-rail .section-head{flex-wrap:wrap}}
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
    var csrfToken = {json.dumps(CSRF_TOKEN)};
    function toggleBookmark(btn, sid, date, title, url, srcName, body, btype) {{
        var formData = new URLSearchParams({{csrf_token:csrfToken,id:sid,type:btype,title:title,source_name:srcName,source_url:url,body:body,date:date}});
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
    <footer class="site-footer"><span>Control Center</span><nav aria-label="Footer"><a href="/briefings">Briefings</a><a href="/status">Status</a><a href="/hub">Projects</a></nav></footer>
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
        html += f'<span class="category-badge" style="background:{bg};color:{fg}">{html_escape(c)}</span>'
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

def html_escape(value: object, quote: bool = False) -> str:
    """Escape arbitrary stored/external text before inserting it into HTML."""
    return html.escape(str(value or ""), quote=quote)


def briefing_card_from_db(
    articles: list[dict], date_str: str, show_date: bool = True, story_date: str | None = None
) -> str:
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
        title = str(a.get("title") or "Untitled")
        summary = str(a.get("summary") or "")
        source = str(a.get("source_name") or "")
        url = str(a.get("source_url") or "")
        categories = a.get("categories", "")
        position = a.get("position", 0)
        safe_url = _safe_http_url(url)
        safe_summary = html_escape(
            re.sub(r"<br\s*/?>", "\n", summary, flags=re.IGNORECASE)
        ).replace("\n", "<br>")

        html += '<div class="briefing-card">'
        html += f'<span class="card-num">{html_escape(position)}</span>'
        html += f'<h3>{html_escape(title)}</h3>'
        if summary:
            html += f'<div class="card-summary">{safe_summary}</div>'
        html += category_badge_html(categories)
        html += '<div class="card-source">'
        if safe_url:
            html += f'<a href="{html_escape(safe_url, quote=True)}" target="_blank" rel="noopener">{html_escape(source)}</a>'
        else:
            html += html_escape(source)
        html += '</div>'
        # Bookmark buttons
        bookmark_date = story_date or date_str
        sid = _story_id(bookmark_date, title, url)
        saved_active = ' active' if _is_bookmarked(sid, 'saved') else ''
        saved_label = '⭐ Saved' if _is_bookmarked(sid, 'saved') else '⭐ Save'
        args_saved = ",".join(
            json.dumps(str(x), ensure_ascii=False)
            for x in [sid, bookmark_date, title, url, source, (summary or '')[:500], "saved"]
        )
        args_saved = html_escape(args_saved, quote=True)
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
            if(data.status==='checking' && !checks.length){
                dot.classList.add('amber');meta.textContent='Checking monitors…';
                body.innerHTML='<span class="status-summary-meta">Live checks are running in the background…</span>';return;
            }
            if(!checks.length){dot.classList.add('amber');meta.textContent='No monitors configured';body.innerHTML='<span class="status-summary-meta">Add checks in monitors.json.</span> <a href="/status">Open status →</a>';return;}
            dot.classList.add(down?'red':'green');
            meta.textContent=down?(down+' monitor'+(down===1?'':'s')+' need attention'):'All monitors healthy';
            body.innerHTML='<div class="status-mini-grid">'+checks.map(function(check){return '<a class="status-mini-service" href="/status"><span><span class="status-dot '+(check.healthy?'green':'red')+'"></span>'+esc(check.name)+'</span><small>'+esc(check.healthy?(check.latency_ms+' ms'):(check.error||'Unavailable'))+'</small></a>';}).join('')+'</div>';
            if(down){panel.open=true;}
        }).catch(function(){dot.classList.add('amber');meta.textContent='Status unavailable';body.innerHTML='<span class="status-summary-meta">Live monitoring could not be loaded.</span> <a href="/status">Open status →</a>';});
    })();
    </script>"""

def home_focus_projects(limit: int = 4) -> str:
    """Render pinned and active living-project decisions for the homepage rail."""
    merged = _merge_hub_entries()
    focus = list(merged["groups"]["active"]) + list(merged["groups"]["maintain"])
    focus.extend(
        entry for entry in merged["groups"]["stalled"]
        if entry.get("pinned") or entry.get("attention_reasons")
    )
    focus.sort(key=lambda entry: not bool(entry.get("pinned")))
    focus = focus[:limit]
    if not focus:
        return '<p class="home-rail-empty">No focus projects yet. Manage priorities in Projects.</p>'
    output = '<div class="home-focus-list">'
    for entry in focus:
        full_name = entry.get("full_name", "")
        current = (entry.get("current_state") or entry.get("goal")
                   or entry.get("description") or "Automatic insight not available yet")
        next_step = entry.get("whats_next") or "No next action yet"
        status = "done" if entry.get("status_override") == "done" else entry.get("recency", "stalled")
        pinned = bool(entry.get("pinned"))
        output += (
            f'<a class="home-focus-item{" is-pinned" if pinned else ""}" '
            f'href="/hub#{urllib.parse.quote(full_name)}">'
            f'<strong>{html.escape(entry.get("name") or full_name)}</strong>'
            f'<small class="home-focus-current">{html.escape(current)}</small>'
            f'<small class="home-focus-next"><b>Next</b> {html.escape(next_step)}</small>'
            f'<span class="status-pill">{html.escape(status)}</span></a>'
        )
    return output + '</div>'

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

    body += '<div class="home-dashboard"><section class="home-primary">'
    body += ('<div class="section-head"><h2>Today\'s Briefing — '
             + html.escape(section_date) + '</h2>'
             '<a href="/briefings">All briefings →</a></div>')
    if stories:
        body += briefing_list_home(stories[:5], iso_for_links)
    else:
        body += '<div class="empty-state"><p>☕ No briefings found. The morning briefing runs at 7am UTC.</p></div>'
    body += '</section><aside class="home-rail" aria-label="Daily status">'
    body += ('<section class="home-rail-block"><div class="section-head"><h2>Monitoring</h2>'
             '<a href="/status">Full status →</a></div>' + status_strip() + '</section>')
    body += ('<section class="home-rail-block"><div class="section-head"><h2>Focus projects</h2>'
             '<a href="/hub">Open projects →</a></div>' + home_focus_projects() + '</section>')
    body += '</aside></div>'

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
    body += '" style="color:var(--muted);text-decoration:none;font-size:0.9rem">← Back to all briefings</a></div>'

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

    body += briefing_card_from_db(articles, date_str, show_date=True, story_date=date)
    return html_page(f"Briefing — {date}", body, active_nav="briefings")

#  Status Board Page

_MONITOR_CACHE: dict = {"data": None, "ts": 0.0, "refreshing": False}
_MONITOR_CACHE_LOCK = threading.RLock()
_MONITOR_CACHE_TTL = 30
_MONITOR_MAX_WORKERS = 4

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

def _monitor_refresh() -> None:
    """Refresh monitor probes off-request and publish one complete snapshot."""
    checks, links, config_error = _load_monitor_config()
    try:
        with ThreadPoolExecutor(
            max_workers=min(_MONITOR_MAX_WORKERS, max(1, len(checks))),
            thread_name_prefix="monitor",
        ) as executor:
            results = list(executor.map(_check_monitor, checks))
    except Exception:
        # A malformed probe must never strand the refresh flag or the request
        # path. Keep the public error deliberately generic.
        results = []
        config_error = config_error or "Monitor refresh unavailable."
    if config_error or not results:
        status = "unconfigured"
    elif all(item["healthy"] for item in results):
        status = "ok"
    else:
        status = "issues"
    data = {"status": status, "checks": results, "links": links, "error": config_error}
    with _MONITOR_CACHE_LOCK:
        _MONITOR_CACHE.update({"data": data, "ts": time.time(), "refreshing": False})


def _queue_monitor_refresh() -> None:
    with _MONITOR_CACHE_LOCK:
        if _MONITOR_CACHE.get("refreshing"):
            return
        _MONITOR_CACHE["refreshing"] = True
        if _MONITOR_CACHE.get("data") is None:
            _, links, config_error = _load_monitor_config()
            _MONITOR_CACHE["data"] = {
                "status": "checking", "checks": [], "links": links, "error": config_error
            }
    thread = threading.Thread(target=_monitor_refresh, name="monitor-refresh", daemon=True)
    thread.start()


def get_monitor_status(force: bool = False) -> dict:
    """Return a snapshot immediately and refresh it stale-while-revalidate."""
    now = time.time()
    with _MONITOR_CACHE_LOCK:
        cached = _MONITOR_CACHE.get("data")
        fresh = cached is not None and now - _MONITOR_CACHE.get("ts", 0.0) < _MONITOR_CACHE_TTL
    if force or not fresh:
        _queue_monitor_refresh()
    with _MONITOR_CACHE_LOCK:
        return dict(_MONITOR_CACHE.get("data") or {
            "status": "checking", "checks": [], "links": [], "error": None
        })

def status_page() -> str:
    data = get_monitor_status()
    body = '<div class="hero" style="padding:2rem 0 1rem"><h1>Server Status</h1><p>Live checks from inside the app container.</p></div>'
    if data.get("error"):
        body += '<div class="empty-state"><p>' + html.escape(data["error"]) + '</p></div>'
    elif data.get("status") == "checking":
        body += '<div class="empty-state"><p>Checking monitors…</p></div>'
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
INSIGHTS_FILE = DATA_DIR / "project-insights.json"
_INSIGHT_STORE = InsightStore(INSIGHTS_FILE)
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
# Reads the user's owned repos + bounded code changes via the REST API.
# Token is read from GITHUB_TOKEN (local development) or GITHUB_TOKEN_FILE
# (production secret mount). It is never logged or echoed.

_GITHUB_CLIENT = GitHubClient(insight_loader=_INSIGHT_STORE.load)
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

def classify_recency(pushed_at: str) -> str:
    """Active (<7d) / Maintain (<30d) / Stalled (>30d)."""
    return _GITHUB_CLIENT.classify_recency(pushed_at)

def get_hub_repos(force: bool = False) -> dict:
    """Return merged Hub data: {repos: [...], status: "ok"|"token_missing"|"error", banner: str|None, ts: float}.
    Cached 10 min; serves stale on failure. Each repo entry carries:
    full_name, repository facts, recency, head SHA, and bounded change context."""
    return _GITHUB_CLIENT.get_repos(force=force)

# ── Ollama client (living project insights) ──
# Structured current/next insights are persisted without raw patch content.

_OLLAMA_CLIENT = OllamaClient(_INSIGHT_STORE)

def _hub_runtime_state() -> dict:
    github = _GITHUB_CLIENT.state()
    generation = _OLLAMA_CLIENT.state()
    if github.get("state") == "refreshing" or generation.get("pending", 0):
        state = "refreshing"
    elif github.get("state") == "error" and not github.get("has_data"):
        state = "error"
    elif github.get("state") == "ready":
        state = "ready"
    else:
        state = "idle"
    return {
        "state": state,
        "version": github.get("version", 0),
        "updated_at": github.get("updated_at"),
        "has_data": github.get("has_data", False),
        "github_state": github.get("state", "idle"),
        "insight_state": generation.get("state", "idle"),
        "pending": generation.get("pending", 0),
    }

def _merge_hub_entries(include_hidden: bool = False) -> dict:
    """Merge GitHub repos with curated overrides keyed by full_name.

    Returns {"groups": {group: [entry,...]}, "status": str, "banner": str|None}.
    Group order is Active, Maintain, Stalled, Done. Done is forced by a
    curation status_override of "done"; otherwise grouping follows recency.
    """
    data = get_hub_repos(force=False)
    curated = load_hub()
    insights = _INSIGHT_STORE.load()
    groups = {"active": [], "maintain": [], "stalled": [], "done": []}
    github_repos = {}
    for repo in data.get("repos", []) or []:
        if not isinstance(repo, dict):
            continue
        full_name = repo.get("full_name")
        full_name = full_name.strip() if isinstance(full_name, str) else ""
        if full_name:
            github_repos[full_name] = repo

    for full_name in dict.fromkeys([*github_repos, *curated, *insights]):
        repo = github_repos.get(full_name, {})
        cur = curated.get(full_name, {}) if isinstance(curated.get(full_name, {}), dict) else {}
        insight = insights.get(full_name, {}) if isinstance(insights.get(full_name), dict) else {}
        text = lambda value: value.strip() if isinstance(value, str) else ""
        hidden = bool(cur.get("hidden", False))
        if hidden and not include_hidden:
            continue
        override = "done" if cur.get("status_override") == "done" else ""
        pushed_at = text(repo.get("pushed_at")) or text(insight.get("source_pushed_at"))
        recency = repo.get("recency") if isinstance(repo.get("recency"), str) else classify_recency(pushed_at)
        if recency not in {"active", "maintain", "stalled"}:
            recency = "stalled"
        group = "done" if override == "done" else recency
        try:
            order = int(cur.get("order", 999))
        except (TypeError, ValueError, OverflowError):
            order = 999
        goal = text(cur.get("goal"))
        current_override = text(cur.get("current_override"))
        next_override = text(cur.get("whats_next"))
        generated_current = text(insight.get("current_state"))
        generated_next = text(insight.get("next_step"))
        current_state = current_override or generated_current
        whats_next = next_override or generated_next
        attention_reasons = []
        if recency == "stalled" and override != "done":
            attention_reasons.append("No activity in 30+ days")
        if not goal:
            attention_reasons.append("Goal missing")
        if not whats_next and override != "done":
            attention_reasons.append("Next action missing")
        if insight.get("confidence") == "low" and not current_override:
            attention_reasons.append("Automatic insight needs review")
        if insight.get("state") in {"stale", "unavailable"} and generated_current:
            attention_reasons.append("Automatic insight is stale")
        if override == "done" and recency == "active":
            attention_reasons.append("Marked done but updated recently")
        entry = {
            "full_name": full_name,
            "name": text(repo.get("name")) or full_name.split("/")[-1],
            "html_url": text(repo.get("html_url")),
            "description": text(repo.get("description")),
            "language": text(repo.get("language")) or None,
            "pushed_at": pushed_at,
            "recency": recency,
            "head_sha": text(repo.get("head_sha")) or text(insight.get("head_sha")),
            "change_status": text(repo.get("change_status")),
            "order": order,
            "goal": goal,
            "current_override": current_override,
            "generated_current": generated_current,
            "current_state": current_state,
            "current_source": "manual" if current_override else "automatic",
            "whats_next": whats_next,
            "next_override": next_override,
            "generated_next": generated_next,
            "next_source": "manual" if next_override else "automatic",
            "live_url": text(cur.get("live_url")),
            "local_path": text(cur.get("local_path")),
            "pinned": bool(cur.get("pinned", False)),
            "hidden": hidden,
            "curated": full_name in curated,
            "curated_only": full_name not in github_repos,
            "has_note": bool(goal),
            "status_override": override,
            "attention_reasons": attention_reasons,
            "group": group,
            "insight": insight,
        }
        groups[group].append(entry)
    for entries in groups.values():
        entries.sort(key=lambda entry: (
            not entry["pinned"], entry["order"], entry["full_name"].lower()
        ))
    return {"groups": groups, "status": data.get("status", "ok"),
            "state": data.get("state", "ready"),
            "version": data.get("version", 0), "banner": data.get("banner")}

def _safe_http_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except (TypeError, ValueError):
        return ""
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else ""

def _relative_time(pushed_at: str, now: datetime | None = None) -> str:
    if not pushed_at:
        return "Activity date unavailable"
    try:
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        if pushed.tzinfo is None:
            pushed = pushed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "Activity date unavailable"
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - pushed).total_seconds()))
    if seconds < 3600:
        minutes = max(1, seconds // 60)
        return f"Updated {minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"Updated {hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86400
    return f"Updated {days} day{'s' if days != 1 else ''} ago"

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
    body = '<div class="page-head"><div><h1>Projects</h1>'
    body += '<p>Living project state from real code changes, with your decisions layered on top.</p></div>'
    body += '<div class="admin-link"><a class="button" href="/hub/admin">Manage projects</a></div></div>'
    if merged.get("banner"):
        body += '<div class="notice">' + html.escape(merged["banner"]) + '</div>'
    if total == 0:
        if merged.get("state") == "refreshing":
            body += '<div class="empty-state"><p>Loading GitHub activity…</p>'
            body += '<p>Your curated projects remain available while the first snapshot loads.</p></div>'
        elif merged.get("status") == "error":
            body += '<div class="empty-state"><p>Project data is temporarily unavailable.</p>'
            body += '<p>Refresh later or check the GitHub integration.</p></div>'
        else:
            body += '<div class="empty-state"><p>No projects yet.</p>'
            body += '<p>Set <code>GITHUB_TOKEN</code> to populate projects from your repositories.</p></div>'
        body += state_script
        return html_page("Projects", body, active_nav="hub")
    group_labels = {"active": "Active", "maintain": "Maintaining",
                    "stalled": "Stalled", "done": "Done"}
    all_entries = [entry for key in ("active", "maintain", "stalled", "done")
                   for entry in groups.get(key, [])]
    pinned_names = {entry["full_name"] for entry in all_entries if entry.get("pinned")}
    stalled_focus = {entry["full_name"] for entry in groups.get("stalled", [])[:4]}
    focus_names = pinned_names | {
        entry["full_name"] for entry in groups.get("active", []) + groups.get("maintain", [])
    } | stalled_focus
    review_count = sum(bool(entry.get("attention_reasons")) for entry in all_entries)
    body += '<div class="hub-overview" aria-label="Project status overview">'
    for key, label in (("focus", "Focus"), ("pinned", "Pinned"), ("new", "New"),
                       ("active", "Active"), ("maintain", "Maintaining"),
                       ("stalled", "Stalled"), ("review", "Review"),
                       ("done", "Done"), ("all", "All")):
        if key == "focus":
            count = len(focus_names)
        elif key == "pinned":
            count = len(pinned_names)
        elif key == "review":
            count = review_count
        elif key == "new":
            count = 0
        elif key == "all":
            count = total
        else:
            count = len(groups.get(key, []))
        pressed = 'true' if key == "focus" else 'false'
        body += (f'<button type="button" class="hub-filter" data-hub-filter="{key}" '
                 f'aria-pressed="{pressed}"><span>{label}</span><strong'
                 f'{" id=hub-new-count" if key == "new" else ""}>{count}</strong></button>')
    body += '</div>'
    for key in ("active", "maintain", "stalled", "done"):
        entries = groups.get(key, [])
        if not entries:
            continue
        if key in {"stalled", "done"}:
            body += (f'<details class="hub-group hub-low-priority" data-hub-section data-group="{key}">'
                     f'<summary><span class="hub-group-title" role="heading" aria-level="2">{group_labels[key]}</span>'
                     f'<span class="status-pill">{len(entries)}</span></summary>')
        else:
            body += (f'<section class="hub-group" data-hub-section data-group="{key}">'
                     f'<div class="section-head"><h2>{group_labels[key]}</h2>'
                     f'<span class="status-pill">{len(entries)}</span></div>')
        body += '<div class="projects-grid">'
        for e in entries:
            e["focus_visible"] = e["full_name"] in focus_names
            body += _hub_card_html(e)
        body += '</div>' + ('</details>' if key in {"stalled", "done"} else '</section>')
    # JS poll for structured insights (non-blocking fill from persistent state)
    body += state_script
    body += ('<script>'
             'document.addEventListener("DOMContentLoaded",function(){'
             'var buttons=[].slice.call(document.querySelectorAll("[data-hub-filter]"));'
             'var sections=[].slice.call(document.querySelectorAll("[data-hub-section]"));'
             'var cards=[].slice.call(document.querySelectorAll("[data-hub-card]"));'
             'var lastVisit=0;try{lastVisit=Number(localStorage.getItem("control-center-projects-last-visit")||0);'
             'var visit=Date.now();if(lastVisit){cards.forEach(function(card){var generated=Date.parse(card.dataset.generatedAt||"");'
             'if(generated>lastVisit){card.classList.add("is-new");card.dataset.new="true";}});}'
             'localStorage.setItem("control-center-projects-last-visit",String(visit));}catch(error){}'
             'var newCount=cards.filter(function(card){return card.dataset.new==="true";}).length;'
             'var newCounter=document.getElementById("hub-new-count");if(newCounter){newCounter.textContent=String(newCount);}'
             'function applyHubFilter(filter){buttons.forEach(function(button){'
             'button.setAttribute("aria-pressed",button.dataset.hubFilter===filter?"true":"false");});'
             'sections.forEach(function(section){var visible=0;'
             '[].slice.call(section.querySelectorAll("[data-hub-card]")).forEach(function(card){'
             'var show=filter==="all"||card.dataset.group===filter||'
             '(filter==="focus"&&card.dataset.focus==="true")||'
             '(filter==="pinned"&&card.dataset.pinned==="true")||'
             '(filter==="review"&&card.dataset.review==="true")||'
             '(filter==="new"&&card.dataset.new==="true");'
             'card.hidden=!show;if(show){visible++;}});section.hidden=visible===0;'
             'if(visible&&section.tagName==="DETAILS"){section.open=true;}});}'
             'buttons.forEach(function(button){button.addEventListener("click",function(){applyHubFilter(button.dataset.hubFilter);});});'
             'applyHubFilter("focus");});</script>')
    body += ('<script>'
             'function refreshInsights(){'
             'fetch("/api/hub/insights").then(function(r){return r.json();}).then(function(d){'
             'var reload=false,insights=d.insights||{};Object.keys(insights).forEach(function(fn){'
             'var card=[].slice.call(document.querySelectorAll("[data-project-name]")).find(function(item){'
             'return item.dataset.projectName===fn;});if(!card){return;}var insight=insights[fn];'
             'var current=card.querySelector("[data-insight-current]");var next=card.querySelector("[data-insight-next]");'
             'if(current&&current.dataset.source!=="manual"&&insight.current_state){current.textContent=insight.current_state;current.classList.remove("placeholder");}'
             'if(next&&next.dataset.source!=="manual"&&insight.next_step){next.textContent=insight.next_step;next.classList.remove("placeholder");}'
             'if(insight.head_sha&&card.dataset.insightHead!==insight.head_sha){reload=true;}});'
             'if(d.pending&&d.pending.length){setTimeout(refreshInsights,2500);}else if(reload){window.location.reload();}'
             '}).catch(function(){});}'
             'document.addEventListener("DOMContentLoaded",function(){setTimeout(refreshInsights,800);});'
             '</script>')
    return html_page("Projects", body, active_nav="hub")

def _hub_card_html(e: dict) -> str:
    text = lambda value: value.strip() if isinstance(value, str) else ""
    fn = text(e.get("full_name"))
    name = html.escape(text(e.get("name")) or fn)
    repo_url = _safe_http_url(text(e.get("html_url")))
    live_url = _safe_http_url(text(e.get("live_url")))
    desc = html.escape(text(e.get("description")))
    lang = html.escape(text(e.get("language")))
    insight = e.get("insight") if isinstance(e.get("insight"), dict) else {}
    current_state = text(e.get("current_state"))
    whats_next = text(e.get("whats_next"))
    current_source = text(e.get("current_source")) or "automatic"
    next_source = text(e.get("next_source")) or "automatic"
    head_sha = text(e.get("head_sha"))
    insight_head = text(insight.get("head_sha"))
    generated_at = text(insight.get("generated_at"))
    updating = text(e.get("change_status")) == "ready" and head_sha != insight_head
    if e.get("status_override") == "done":
        status = "done"
    else:
        status = text(e.get("recency")) or "stalled"
    group = text(e.get("group")) or status
    focus = "true" if e.get("focus_visible") else "false"
    pinned = bool(e.get("pinned"))
    reasons = e.get("attention_reasons") if isinstance(e.get("attention_reasons"), list) else []
    review = bool(reasons)
    classes = "project-card" + (" is-pinned" if pinned else "")
    card = (f'<article class="{classes}" id="{html.escape(fn, quote=True)}" data-hub-card '
            f'data-project-name="{html.escape(fn, quote=True)}" '
            f'data-insight-head="{html.escape(insight_head, quote=True)}" '
            f'data-generated-at="{html.escape(generated_at, quote=True)}" data-new="false" '
            f'data-group="{html.escape(group, quote=True)}" data-focus="{focus}" '
            f'data-pinned="{str(pinned).lower()}" data-review="{str(review).lower()}">'
            f'<div class="project-card-head"><h3>')
    if repo_url:
        card += f'<a href="{html.escape(repo_url, quote=True)}" target="_blank" rel="noopener">{name}</a>'
    else:
        card += name
    card += '</h3><span class="hub-card-badges">'
    if pinned:
        card += '<span class="status-pill hub-pin">Pinned</span>'
    card += '<span class="status-pill hub-new-badge" data-new-badge>New</span>'
    card += f'<span class="status-pill status-{html.escape(status, quote=True)}">{html.escape(status)}</span>'
    if updating:
        card += '<span class="status-pill">Updating</span>'
    elif insight.get("state") == "stale":
        card += '<span class="status-pill">Stale insight</span>'
    elif insight.get("confidence"):
        card += f'<span class="status-pill">AI {html.escape(text(insight.get("confidence")))}</span>'
    card += '</span></div>'
    if desc:
        card += f'<p class="hub-description">{desc}</p>'
    goal = text(e.get("goal"))
    if goal:
        card += f'<p class="hub-goal"><strong>Goal:</strong> {html.escape(goal)}</p>'
    current_placeholder = "Analyzing the latest code changes…" if updating else "No current-state insight yet."
    next_placeholder = "Waiting for the current-state analysis…" if updating else "No automatic next step yet."
    card += '<div class="hub-decision current"><span>Current'
    card += f'<small class="source">{html.escape(current_source)}</small></span>'
    card += (f'<p data-insight-current data-source="{html.escape(current_source, quote=True)}"'
             f' class="{"" if current_state else "placeholder"}">'
             f'{html.escape(current_state or current_placeholder)}</p></div>')
    card += '<div class="hub-decision next"><span>Next'
    card += f'<small class="source">{html.escape(next_source)}</small></span>'
    card += (f'<p data-insight-next data-source="{html.escape(next_source, quote=True)}"'
             f' class="{"" if whats_next else "placeholder"}">'
             f'{html.escape(whats_next or next_placeholder)}</p></div>')
    card += '<div class="hub-insight-meta">'
    card += f'<span>{html.escape(_relative_time(text(e.get("pushed_at"))))}</span>'
    if head_sha:
        revision = html.escape(head_sha[:8])
        if repo_url and re.fullmatch(r"[0-9a-fA-F]{7,64}", head_sha):
            commit_url = repo_url.rstrip("/") + "/commit/" + urllib.parse.quote(head_sha, safe="")
            card += f'<a href="{html.escape(commit_url, quote=True)}" target="_blank" rel="noopener">Revision {revision} ↗</a>'
        else:
            card += f'<span>Revision {revision}</span>'
    if generated_at:
        card += f'<span>{html.escape(_relative_time(generated_at).replace("Updated", "Generated", 1))}</span>'
    card += '</div>'
    if lang:
        card += f'<p class="hub-lang">{lang}</p>'
    changed_files = insight.get("changed_files") if isinstance(insight.get("changed_files"), list) else []
    if changed_files:
        additions = max(0, insight.get("additions", 0)) if isinstance(insight.get("additions"), int) else 0
        deletions = max(0, insight.get("deletions", 0)) if isinstance(insight.get("deletions"), int) else 0
        card += (f'<details class="hub-evidence"><summary>{len(changed_files)} changed file'
                 f'{"s" if len(changed_files) != 1 else ""} · +{additions}/−{deletions}</summary>'
                 '<ul class="hub-file-list">')
        for changed in changed_files:
            if not isinstance(changed, dict) or not text(changed.get("path")):
                continue
            path = html.escape(text(changed.get("path")))
            file_status = html.escape(text(changed.get("status")) or "modified")
            add = changed.get("additions", 0) if isinstance(changed.get("additions"), int) else 0
            delete = changed.get("deletions", 0) if isinstance(changed.get("deletions"), int) else 0
            card += f'<li><span>{path}</span><small>{file_status} +{max(0, add)}/−{max(0, delete)}</small></li>'
        card += '</ul></details>'
    history = insight.get("history") if isinstance(insight.get("history"), list) else []
    if history:
        card += f'<details class="hub-history"><summary>Previous insights · {len(history)}</summary><ol class="hub-history-list">'
        for previous in history[:5]:
            if not isinstance(previous, dict):
                continue
            previous_current = text(previous.get("current_state"))
            previous_next = text(previous.get("next_step"))
            card += '<li>'
            if previous_current:
                card += f'<strong>{html.escape(previous_current)}</strong>'
            if previous_next:
                card += f'<span>Next: {html.escape(previous_next)}</span>'
            if text(previous.get("generated_at")):
                card += f'<time>{html.escape(_relative_time(text(previous.get("generated_at"))).replace("Updated", "Generated", 1))}</time>'
            card += '</li>'
        card += '</ol></details>'
    if reasons:
        card += '<div class="hub-attention"><span>Needs attention</span><ul>'
        card += "".join(f'<li>{html.escape(text(reason))}</li>' for reason in reasons if text(reason))
        card += '</ul></div>'
    references = []
    if repo_url:
        references.append(f'<a href="{html.escape(repo_url, quote=True)}" target="_blank" rel="noopener">Repository ↗</a>')
    if live_url:
        references.append(f'<a href="{html.escape(live_url, quote=True)}" target="_blank" rel="noopener">Live site ↗</a>')
    local_path = text(e.get("local_path"))
    if local_path:
        references.append(f'<span class="hub-local">Local: {html.escape(local_path)}</span>')
    if references:
        card += '<div class="hub-references">' + "".join(references) + '</div>'
    card += f'<div class="project-actions"><a class="hub-edit" href="/hub/admin#{urllib.parse.quote(fn)}">Manage project</a></div>'
    card += '</article>'
    return card

def hub_admin_page(message: str = "", repo_context: str = "") -> str:
    """Auth-gated project management with explicit automatic/manual precedence."""
    merged = _merge_hub_entries(include_hidden=True)
    repos = [entry for group in ("active", "maintain", "stalled", "done")
             for entry in merged["groups"][group]]
    body = '<div class="page-head"><div><h1>Manage Projects</h1>'
    body += '<p>Guide automatic insights, prioritize work, and override only what needs human judgment.</p></div></div>'
    known_names = {entry.get("full_name") for entry in repos}
    if message and repo_context not in known_names:
        body += '<div class="notice">' + html.escape(message) + '</div>'
    # Action buttons (refresh + backup)
    body += '<div class="admin-actions" style="margin-bottom:1.5rem">'
    body += '<form method="post" action="/hub/admin/refresh" style="display:inline">'
    body += _csrf_field()
    body += '<button class="button" type="submit">Refresh projects now</button></form>'
    body += '<form method="post" action="/hub/admin/backup" style="display:inline;margin-left:.5rem">'
    body += _csrf_field()
    body += '<button class="button" type="submit">Download backup</button></form>'
    body += '</div>'
    if not repos:
        body += '<div class="empty-state"><p>No projects to curate yet.</p>'
        body += '<p>Set <code>GITHUB_TOKEN</code> to populate Projects.</p></div>'
        return html_page("Manage Projects", body, active_nav="hub")
    counts = {
        "all": len(repos),
        "uncurated": sum(not entry.get("curated") for entry in repos),
        "hidden": sum(bool(entry.get("hidden")) for entry in repos),
        "done": sum(entry.get("status_override") == "done" for entry in repos),
    }
    body += ('<div class="admin-toolbar"><label class="admin-search">Search repositories'
             '<input type="search" id="admin-repo-search" placeholder="Name, goal, or next action" '
             'autocomplete="off"></label><div class="admin-filters" aria-label="Repository filters">')
    for key, label in (("all", "All"), ("uncurated", "Uncurated"),
                       ("hidden", "Hidden"), ("done", "Done")):
        body += (f'<button type="button" data-admin-filter="{key}" '
                 f'aria-pressed="{"true" if key == "all" else "false"}">'
                 f'{label} <strong>{counts[key]}</strong></button>')
    body += '</div><span class="admin-result-count" id="admin-result-count" aria-live="polite"></span></div>'
    body += '<div class="admin-repo-list">'
    for repo_index, repo in enumerate(repos):
        fn = repo.get("full_name") if isinstance(repo.get("full_name"), str) else ""
        fn = fn.strip()
        if not fn:
            continue
        text = lambda value: value.strip() if isinstance(value, str) else ""
        goal = text(repo.get("goal"))
        current_override = text(repo.get("current_override"))
        whats_next = text(repo.get("next_override"))
        generated_current = text(repo.get("generated_current"))
        generated_next = text(repo.get("generated_next"))
        live = text(repo.get("live_url"))
        local = text(repo.get("local_path"))
        override = "done" if repo.get("status_override") == "done" else ""
        order = repo.get("order", 999) if isinstance(repo.get("order"), int) else 999
        hidden = bool(repo.get("hidden"))
        pinned = bool(repo.get("pinned"))
        curated = bool(repo.get("curated"))
        search_value = " ".join((fn, text(repo.get("name")), goal, current_override,
                                 whats_next, generated_current, generated_next)).lower()
        opened = ' open' if repo_context == fn else ''
        body += (f'<details class="admin-repo" id="{html.escape(fn, quote=True)}" data-admin-entry '
                 f'data-search="{html.escape(search_value, quote=True)}" '
                 f'data-uncurated="{str(not curated).lower()}" data-hidden="{str(hidden).lower()}" '
                 f'data-done="{str(override == "done").lower()}"{opened}><summary>'
                 f'<span><strong>{html.escape(text(repo.get("name")) or fn)}</strong>'
                 f'<small>{html.escape(fn)}</small></span><span class="admin-repo-badges">')
        if not curated:
            body += '<span class="status-pill">uncurated</span>'
        if repo.get("curated_only"):
            body += '<span class="status-pill">curated only</span>'
        if hidden:
            body += '<span class="status-pill">hidden</span>'
        if pinned:
            body += '<span class="status-pill hub-pin">pinned</span>'
        if override == "done":
            body += '<span class="status-pill status-done">done</span>'
        body += '</span></summary><div class="admin-repo-body">'
        if message and repo_context == fn:
            body += '<div class="notice" role="status">' + html.escape(message) + '</div>'
        references = []
        repo_url = _safe_http_url(text(repo.get("html_url")))
        live_url = _safe_http_url(live)
        if repo_url:
            references.append(f'<a href="{html.escape(repo_url, quote=True)}" target="_blank" rel="noopener">Repository ↗</a>')
        if live_url:
            references.append(f'<a href="{html.escape(live_url, quote=True)}" target="_blank" rel="noopener">Live site ↗</a>')
        if local:
            references.append(f'<span>Local: {html.escape(local)}</span>')
        if references:
            body += '<div class="admin-tech-links">' + "".join(references) + '</div>'
        body += '<form class="project-form" method="post" action="/hub/admin/update">'
        body += _csrf_field()
        body += f'<input type="hidden" name="full_name" value="{html.escape(fn, quote=True)}">'
        body += f'<label class="wide">Goal / note<textarea name="goal" rows="2" placeholder="What is this project for?">{html.escape(goal)}</textarea></label>'
        body += '<div class="admin-suggestion"><span>Automatic current</span><p>'
        body += html.escape(generated_current or "No automatic current-state insight yet.") + '</p></div>'
        current_id = f'current-override-{repo_index}'
        body += (f'<label class="wide"><span class="field-label-row">Current override (optional)'
                 f'<button type="button" data-clear-field="{current_id}">Use automatic</button></span>'
                 f'<textarea id="{current_id}" name="current_override" rows="2" '
                 f'placeholder="Leave blank to use the automatic current state">{html.escape(current_override)}</textarea>'
                 '<small class="admin-help">Only fill this when the generated state needs correction.</small></label>')
        body += '<div class="admin-suggestion"><span>Automatic next</span><p>'
        body += html.escape(generated_next or "No automatic next-step suggestion yet.") + '</p></div>'
        next_id = f'next-override-{repo_index}'
        body += (f'<label class="wide"><span class="field-label-row">Next override (optional)'
                 f'<button type="button" data-clear-field="{next_id}">Use automatic</button></span>'
                 f'<textarea id="{next_id}" name="whats_next" rows="2" '
                 f'placeholder="Leave blank to use the automatic next step">{html.escape(whats_next)}</textarea>'
                 '<small class="admin-help">A manual next action stays in control until you clear it.</small></label>')
        body += f'<label>Live URL<input name="live_url" value="{html.escape(live, quote=True)}" placeholder="https://…"></label>'
        body += f'<label>Local path<input name="local_path" value="{html.escape(local, quote=True)}" placeholder="/srv/…"></label>'
        body += ('<label>Status override<select name="status_override">'
                 f'<option value=""{" selected" if override=="" else ""}>Auto (by recency)</option>'
                 f'<option value="done"{" selected" if override == "done" else ""}>Done</option>'
                 '</select></label>')
        body += f'<label>Order<input type="number" name="order" value="{order}" min="0"></label>'
        body += f'<label class="check"><input type="checkbox" name="pinned" value="1"{" checked" if pinned else ""}> Pinned to focus</label>'
        body += f'<label class="check"><input type="checkbox" name="hidden" value="1"{" checked" if hidden else ""}> Hidden</label>'
        body += '<div class="admin-actions">'
        body += '<button class="button primary" type="submit">Save</button>'
        body += ('<button class="button" type="submit" formmethod="post" '
                 'formaction="/hub/admin/regenerate">Regenerate from code changes</button>')
        if curated:
            body += ('<button class="button danger" type="submit" formmethod="post" '
                     'formaction="/hub/admin/delete" '
                     'onclick="return confirm(\'Delete this curation entry?\')">Delete curation</button>')
        body += f'<a class="button" href="/hub#{urllib.parse.quote(fn)}">View project</a>'
        body += '</div></form></div></details>'
    body += '</div>'
    body += ('<script>document.addEventListener("DOMContentLoaded",function(){'
             'var input=document.getElementById("admin-repo-search");'
             'var entries=[].slice.call(document.querySelectorAll("[data-admin-entry]"));'
             'var buttons=[].slice.call(document.querySelectorAll("[data-admin-filter]"));'
             'var count=document.getElementById("admin-result-count");var filter="all";'
             '[].slice.call(document.querySelectorAll("[data-clear-field]")).forEach(function(button){'
             'button.addEventListener("click",function(){var field=document.getElementById(button.dataset.clearField);'
             'if(field){field.value="";field.focus();}});});'
             'function applyAdminFilters(){var query=input.value.trim().toLowerCase();var visible=0;'
             'entries.forEach(function(entry){var matchesText=!query||entry.dataset.search.indexOf(query)!==-1;'
             'var matchesFilter=filter==="all"||entry.dataset[filter]==="true";'
             'entry.hidden=!(matchesText&&matchesFilter);if(!entry.hidden){visible++;}});'
             'count.textContent=visible+" repositories";}'
             'input.addEventListener("input",applyAdminFilters);buttons.forEach(function(button){'
             'button.addEventListener("click",function(){filter=button.dataset.adminFilter;'
             'buttons.forEach(function(item){item.setAttribute("aria-pressed",item===button?"true":"false");});'
             'applyAdminFilters();});});applyAdminFilters();});</script>')
    return html_page("Manage Projects", body, active_nav="hub")

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

    def _send_redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def hub_insights_api(self):
        """Return display-safe insights and enqueue new heads without blocking."""
        data = get_hub_repos(force=False)
        repos = data.get("repos", []) or []
        curated = load_hub()
        goals = {
            full_name: entry.get("goal", "")
            for full_name, entry in curated.items() if isinstance(entry, dict)
        }
        result = _OLLAMA_CLIENT.request_insights(repos, goals)
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
            self._respond(200, "application/json", json.dumps(_hub_runtime_state()).encode())
        elif path == "/api/hub/insights":
            self.hub_insights_api()
        elif path == "/hub":
            content = hub_page().encode()
            self._respond(200, "text/html", content)
        elif path == "/hub/admin":
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            content = hub_admin_page(qs.get("message", [""])[0], qs.get("repo", [""])[0]).encode()
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
        get = lambda k, default="": params.get(k, [default])[0]
        path = self.path.rstrip("/") or "/"

        if path == "/bookmarks/toggle":
            if not is_authenticated(self):
                self._respond(403, "text/html", _UNAUTH_PAGE.encode())
                return
            if not _valid_csrf(get("csrf_token")):
                self._respond(403, "text/plain", b"Invalid form token.")
                return
            sid = get("id")
            btype = get("type") or "saved"
            if not sid or btype not in {"saved", "read_later"}:
                self._respond(400, "text/plain", b"Invalid bookmark request.")
                return
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
            if not _valid_csrf(get("csrf_token")):
                self._respond(403, "text/plain", b"Invalid form token.")
                return
            action = path.removeprefix("/hub/admin/")
            if action == "update":
                message = update_hub("update", get)
                anchor = urllib.parse.quote(get("full_name"), safe="")
                self._send_redirect("/hub/admin?" + urllib.parse.urlencode({
                    "message": message, "repo": get("full_name")
                }) + f"#{anchor}")
            elif action == "delete":
                fn = get("full_name")
                message = update_hub("delete", lambda k, default="": fn if k == "full_name" else get(k, default))
                self._send_redirect("/hub/admin?" + urllib.parse.urlencode({"message": message}))
            elif action == "toggle-hide":
                fn = get("full_name")
                message = update_hub("toggle-hide", lambda k, default="": fn if k == "full_name" else get(k, default))
                anchor = urllib.parse.quote(fn, safe="")
                self._send_redirect("/hub/admin?" + urllib.parse.urlencode({
                    "message": message, "repo": fn
                }) + f"#{anchor}")
            elif action == "refresh":
                _OLLAMA_CLIENT.invalidate()
                _GITHUB_CLIENT.invalidate()
                get_hub_repos(force=True)
                self._send_redirect("/hub/admin?" + urllib.parse.urlencode({"message": "Projects refreshed."}))
            elif action == "regenerate":
                fn = get("full_name").strip()
                if not fn:
                    self._respond(400, "text/plain", b"Repository identifier missing.")
                    return
                _OLLAMA_CLIENT.invalidate(fn)
                _GITHUB_CLIENT.invalidate_repo(fn)
                get_hub_repos(force=True)
                anchor = urllib.parse.quote(fn, safe="")
                self._send_redirect("/hub/admin?" + urllib.parse.urlencode({
                    "message": "Project regeneration queued.", "repo": fn
                }) + f"#{anchor}")
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
    print(f"Control Center: http://127.0.0.1:{port}")
    server.serve_forever()
