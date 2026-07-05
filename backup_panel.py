"""Backup panel module for devmclovin.com — imported by server.py."""

import json
import time
import urllib.request

_BACKUP_CACHE: dict = {"data": None, "ts": 0}
_CACHE_TTL = 300


def get_backup_status():
    """Fetch backup status from local API with 5-min caching."""
    global _BACKUP_CACHE
    now = time.time()
    if _BACKUP_CACHE["data"] is not None and (now - _BACKUP_CACHE["ts"]) < _CACHE_TTL:
        return _BACKUP_CACHE["data"]
    try:
        req = urllib.request.Request("http://localhost:8091/api/backups/status")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        _BACKUP_CACHE = {"data": data, "ts": now}
        return data
    except Exception:
        _BACKUP_CACHE = {"data": None, "ts": now}
        return None


def backup_panels_row():
    """Render backup status cards for Hermes and GitHub backups."""
    data = get_backup_status()
    html = '<div class="section-title">\U0001f6e1\ufe0f Backups</div>'
    if data is None:
        html += ('<div class="backup-grid"><div class="backup-card error">'
                 '<div class="backup-card-header">'
                 '<span class="backup-status-dot error"></span>'
                 '<h3>Backup Status</h3></div>'
                 '<p style="color:var(--text-muted);margin:0.5rem 0">Backup service is currently unavailable.</p>'
                 '<button class="backup-retry-btn" onclick="location.reload()">Retry</button>'
                 '</div></div>')
        return html
    backups = data.get("backups", [])
    if not backups:
        html += '<div class="empty-state"><p>No backup data available.</p></div>'
        return html
    html += '<div class="backup-grid">'
    for b in backups:
        bt = b.get("type", "unknown")
        title = {"hermes": "Hermes Backup", "github": "GitHub Backup"}.get(bt, bt + " Backup")
        emoji = {"hermes": "\u2699\ufe0f", "github": "\U0001f4bb"}.get(bt, "\U0001f4e6")
        ls = b.get("lastSuccess")
        dot = "success" if ls is True else ("failed" if ls is False else "unknown")
        st = {"success": "Last backup succeeded", "failed": "Last backup failed", "unknown": "Backup status unknown"}[dot]
        lt = b.get("lastBackupTime", "")
        if lt:
            try:
                import datetime as _d
                diff = time.time() - _d.datetime.fromisoformat(lt.replace("Z", "+00:00")).timestamp()
                rel = "just now" if diff < 60 else (str(int(diff//60)) + "m ago" if diff < 3600 else (str(int(diff//3600)) + "h ago" if diff < 86400 else str(int(diff//86400)) + "d ago"))
            except Exception:
                rel = lt[:10]
        else:
            rel = "\u2014"
        ch = b.get("changedFilesCount")
        cs = str(ch) if ch is not None else "\u2014"
        sz = b.get("backupSize") or "\u2014"
        rp = b.get("repoLink") or "#"
        ri = b.get("restoreInstructionsLink") or "#"
        html += ('<div class="backup-card"><div class="backup-card-header">'
                 '<span class="backup-status-dot ' + dot + '"></span>'
                 '<h3>' + emoji + ' ' + title + '</h3></div>'
                 '<div class="backup-meta">'
                 '<div class="backup-field"><span class="backup-label">Last success</span><span class="backup-value">' + rel + '</span></div>'
                 '<div class="backup-field"><span class="backup-label">Files changed</span><span class="backup-value">' + cs + '</span></div>'
                 '<div class="backup-field"><span class="backup-label">Size</span><span class="backup-value">' + sz + '</span></div>'
                 '</div>'
                 '<div class="backup-status-line">' + st + '</div>'
                 '<div class="backup-actions">'
                 '<a href="' + rp + '" target="_blank" rel="noopener" class="card-action">View repo \u2192</a>'
                 '<a href="' + ri + '" target="_blank" rel="noopener" class="card-action">Restore instructions \u2192</a>'
                 '</div></div>')
    html += '</div>'
    return html


# ── Inline CSS (injected into home page head) ──

BACKUP_CSS = """
.backup-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin-bottom:1.5rem}
.backup-card{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:1.25rem;transition:border-color .2s,background .2s}
.backup-card:hover{border-color:var(--accent);background:#1c2333}
.backup-card.error{border-color:var(--red);background:rgba(248,81,73,.08);text-align:center;grid-column:1/-1}
.backup-card-header{display:flex;align-items:center;gap:.5rem;margin-bottom:.75rem}
.backup-card-header h3{margin:0;font-size:1rem;font-weight:600;color:var(--text)}
.backup-status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.backup-status-dot.success{background:var(--green)}
.backup-status-dot.failed{background:var(--red)}
.backup-status-dot.unknown{background:var(--text-muted)}
.backup-status-dot.error{background:var(--orange)}
.backup-meta{display:grid;grid-template-columns:1fr 1fr;gap:.5rem;margin-bottom:.75rem}
.backup-field{display:flex;flex-direction:column;gap:.15rem}
.backup-label{font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em}
.backup-value{font-size:.9rem;color:var(--text);font-variant-numeric:tabular-nums}
.backup-status-line{font-size:.82rem;color:var(--text-muted);margin-bottom:.75rem;padding:.4rem .5rem;background:rgba(59,130,246,.08);border-radius:4px}
.backup-actions{display:flex;gap:.5rem;flex-wrap:wrap}
.backup-retry-btn{margin-top:.75rem;padding:.5rem 1.25rem;background:var(--accent);color:#fff;border:none;border-radius:6px;font-size:.9rem;cursor:pointer;transition:background .2s}
.backup-retry-btn:hover{background:var(--accent-hover)}
"""
