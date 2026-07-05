"""
Personal Notes REST API

Storage: SQLite (notes.db)
Auth:    Bearer token (API_KEY env var, default "notes-secret-token")
Endpoints:
  GET    /notes        — list all notes (newest first)
  POST   /notes        — create a note {title, content}
  PATCH  /notes/{id}   — update a note {title?, content?}
  DELETE /notes/{id}   — delete a note

Run: python server.py
Default port: 8080 (set NOTES_PORT to override)
"""

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("API_KEY", "notes-secret-token")
PORT = int(os.environ.get("NOTES_PORT", "8080"))
DB_PATH = Path(__file__).resolve().parent / "notes.db"

app = FastAPI(title="Personal Notes API", version="1.0.0")

# CORS — allow the landing page (port 3002) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3002", "http://127.0.0.1:3002", "https://devmclovin.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                content    TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------
class NoteCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(default="", max_length=50000)


class NoteUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    content: Optional[str] = Field(default=None, max_length=50000)


class NoteOut(BaseModel):
    id: str
    title: str
    content: str
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
def authenticate(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = auth_header[7:]
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API token")


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/notes", response_model=list[NoteOut], dependencies=[Depends(authenticate)])
def list_notes():
    """Return all notes, newest first."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM notes ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.post("/notes", status_code=201, response_model=NoteOut, dependencies=[Depends(authenticate)])
def create_note(body: NoteCreate):
    """Create a new note."""
    note_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO notes (id, title, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (note_id, body.title.strip(), body.content.strip(), now, now),
        )
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


@app.patch("/notes/{note_id}", response_model=NoteOut, dependencies=[Depends(authenticate)])
def update_note(note_id: str, body: NoteUpdate):
    """Partially update a note's title and/or content."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Note not found")

        new_title = body.title.strip() if body.title is not None else row["title"]
        new_content = body.content.strip() if body.content is not None else row["content"]
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "UPDATE notes SET title = ?, content = ?, updated_at = ? WHERE id = ?",
            (new_title, new_content, now, note_id),
        )
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row)


@app.delete("/notes/{note_id}", status_code=204, dependencies=[Depends(authenticate)])
def delete_note(note_id: str):
    """Delete a note. Returns 204 No Content on success."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Note not found")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))


# ---------------------------------------------------------------------------
# Frontend — self-contained SPA served at /
# ---------------------------------------------------------------------------

NOTES_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Notes — devmclovin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;450;500;600;700&display=swap" rel="stylesheet">
<style>
/* ── Tokens (matching devmclovin.com dark theme) ── */
:root {
    --bg: #0d1117;
    --bg-card: #161b22;
    --bg-nav: #161b22;
    --border: #30363d;
    --border-2: #3c465a;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --accent: #7c3aed;
    --accent-hover: #8b5cf6;
    --accent-glow: rgba(124, 58, 237, 0.3);
    --green: #3fb950;
    --orange: #d2991d;
    --red: #f85149;
    --blue: #58a6ff;
    --danger-soft: rgba(248, 81, 73, 0.13);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    min-height: 100vh;
}
/* ── Nav ── */
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
    font-size: 1.25rem; font-weight: 700; color: var(--text);
    text-decoration: none; display: flex; align-items: center; gap: 0.5rem;
}
nav .logo span { color: var(--accent); }
nav .links { display: flex; gap: 1.5rem; align-items: center; flex-wrap: wrap; }
nav .links a {
    color: var(--text-muted); text-decoration: none; font-size: 0.9rem;
    transition: color 0.2s; padding: 0.5rem 0; white-space: nowrap;
}
nav .links a:hover, nav .links a.active { color: var(--text); }
nav .links a.hermes-btn {
    background: var(--accent); color: #fff; padding: 0.4rem 1rem;
    border-radius: 6px; font-weight: 600; transition: background 0.2s, box-shadow 0.2s;
}
nav .links a.hermes-btn:hover { background: var(--accent-hover); box-shadow: 0 0 20px var(--accent-glow); }
.container { max-width: 960px; margin: 0 auto; padding: 2rem; }

/* ── Notes specific ── */
.notes-header {
    display: flex; justify-content: space-between; align-items: center;
    flex-wrap: wrap; gap: 1rem; margin-bottom: 2rem;
}
.notes-header h1 { font-size: 1.5rem; color: var(--text); }
.btn-add {
    background: var(--accent); color: #fff; border: none;
    padding: 0.6rem 1.4rem; border-radius: 8px; font-size: 0.9rem;
    font-weight: 600; cursor: pointer; transition: background 0.2s, box-shadow 0.2s;
}
.btn-add:hover { background: var(--accent-hover); box-shadow: 0 0 20px var(--accent-glow); }
.notes-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1rem;
}
.note-card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px;
    padding: 1.25rem; cursor: pointer; transition: border-color 0.2s, box-shadow 0.2s;
    position: relative; display: flex; flex-direction: column;
}
.note-card:hover { border-color: var(--accent); box-shadow: 0 0 16px var(--accent-glow); }
.note-card h3 {
    font-size: 1.05rem; margin-bottom: 0.5rem; color: var(--text); word-break: break-word;
    padding-right: 2rem;
}
.note-card .note-body {
    font-size: 0.85rem; color: var(--text-muted); flex: 1; overflow: hidden;
    display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; word-break: break-word;
}
.note-card .note-meta {
    font-size: 0.72rem; color: var(--text-muted); margin-top: 0.75rem;
    display: flex; justify-content: space-between; align-items: center; opacity: 0.7;
}
.note-card .btn-delete {
    position: absolute; top: 0.75rem; right: 0.75rem;
    background: none; border: none; color: var(--red); cursor: pointer;
    font-size: 0.9rem; padding: 0.2rem 0.5rem; border-radius: 4px;
    opacity: 0; transition: opacity 0.2s, background 0.2s; z-index: 2;
}
.note-card:hover .btn-delete { opacity: 1; }
.note-card .btn-delete:hover { background: var(--danger-soft); }

/* ── Modal ── */
.modal-overlay {
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.65); z-index: 1000;
    display: flex; align-items: center; justify-content: center; padding: 1rem;
}
.modal-overlay[hidden] { display: none; }
.modal-box {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px;
    padding: 2rem; max-width: 560px; width: 100%;
    box-shadow: 0 24px 60px rgba(0,0,0,0.7);
}
.modal-box h2 { margin-bottom: 1.25rem; font-size: 1.2rem; color: var(--text); }
.modal-box label {
    display: block; font-size: 0.8rem; color: var(--text-muted);
    margin-bottom: 0.3rem; text-transform: uppercase; letter-spacing: 0.05em;
}
.modal-box input, .modal-box textarea {
    width: 100%; padding: 0.6rem 0.8rem; background: var(--bg);
    border: 1px solid var(--border); border-radius: 8px; color: var(--text);
    font-size: 0.9rem; margin-bottom: 1rem; resize: vertical; font-family: inherit;
}
.modal-box input:focus, .modal-box textarea:focus {
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow);
}
.modal-box textarea { min-height: 140px; }
.modal-actions { display: flex; gap: 0.6rem; justify-content: flex-end; }
.btn {
    padding: 0.5rem 1.2rem; border-radius: 8px; font-size: 0.85rem;
    font-weight: 600; cursor: pointer; border: none; transition: all 0.2s;
}
.btn-cancel { background: var(--border); color: var(--text); }
.btn-cancel:hover { background: var(--border-2); }
.btn-save { background: var(--accent); color: #fff; }
.btn-save:hover { background: var(--accent-hover); box-shadow: 0 0 16px var(--accent-glow); }
.btn-save:disabled { opacity: 0.5; cursor: not-allowed; box-shadow: none; }
.btn-danger { background: var(--red); color: #fff; }
.btn-danger:hover { background: #e04040; }

/* ── Confirm dialog ── */
.confirm-overlay {
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.65); z-index: 2000;
    display: flex; align-items: center; justify-content: center; padding: 1rem;
}
.confirm-modal {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px;
    padding: 2rem; max-width: 420px; width: 100%; text-align: center;
    box-shadow: 0 24px 60px rgba(0,0,0,0.7);
}
.confirm-icon { font-size: 2.5rem; margin-bottom: 0.75rem; }
.confirm-title { font-size: 1.1rem; font-weight: 600; margin-bottom: 0.5rem; color: var(--text); }
.confirm-desc { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 1.25rem; }
.confirm-buttons { display: flex; gap: 0.6rem; justify-content: center; }

/* ── Error toast ── */
.error-toast {
    position: fixed; bottom: 2rem; right: 2rem;
    background: var(--red); color: #fff; padding: 0.8rem 1.5rem;
    border-radius: 10px; font-size: 0.85rem; z-index: 3000;
    box-shadow: 0 8px 24px rgba(248,81,73,0.3); display: none;
}
.empty-state { text-align: center; padding: 4rem 2rem; color: var(--text-muted); }
.empty-state .empty-icon { font-size: 3rem; margin-bottom: 1rem; }
.empty-state p { font-size: 1rem; margin-bottom: 1.5rem; }
.loading { text-align: center; padding: 3rem; color: var(--text-muted); }
.spinner {
    display: inline-block; width: 24px; height: 24px; border: 3px solid var(--border);
    border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 600px) {
    .notes-header { flex-direction: column; align-items: flex-start; }
    .notes-grid { grid-template-columns: 1fr; }
    .modal-box { padding: 1.25rem; margin: 0.5rem; }
    nav { padding: 0 1rem; }
    nav .links { gap: 0.75rem; }
}
</style>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📝</text></svg>">
</head>
<body>
<nav>
    <a href="https://devmclovin.com" class="logo">devmclovin<span>.com</span></a>
    <div class="links">
        <a href="https://devmclovin.com">Home</a>
        <a href="https://devmclovin.com/briefings">Briefings</a>
        <a href="https://devmclovin.com/projects">Projects</a>
        <a href="https://devmclovin.com/hermes">Hermes</a>
        <a href="/" class="active">Notes</a>
    </div>
</nav>

<div class="container">
    <div class="notes-header">
        <h1>📝 Personal Notes</h1>
        <button class="btn-add" onclick="openEditor()">+ New Note</button>
    </div>

    <div id="notes-container"><div class="loading"><span class="spinner"></span> Loading notes…</div></div>
</div>

<!-- Editor Modal -->
<div id="editor-overlay" class="modal-overlay" hidden>
    <div class="modal-box">
        <h2 id="editor-title">New Note</h2>
        <label for="note-title-input">Title</label>
        <input type="text" id="note-title-input" placeholder="Note title" maxlength="500" />
        <label for="note-content-input">Content</label>
        <textarea id="note-content-input" placeholder="Write something..."></textarea>
        <div class="modal-actions">
            <button class="btn btn-cancel" onclick="closeEditor()">Cancel</button>
            <button class="btn btn-save" id="btn-save" onclick="saveNote()">Save</button>
        </div>
    </div>
</div>

<!-- Error Toast -->
<div id="error-toast" class="error-toast"></div>

<script>
const TOKEN = "%s";
let editingId = null;

function showError(msg) {
    const el = document.getElementById('error-toast');
    el.textContent = msg; el.style.display = 'block';
    setTimeout(function() { el.style.display = 'none'; }, 4000);
}

function formatDate(iso) {
    try { return new Date(iso).toLocaleDateString('en-US', { month:'short', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit' }); }
    catch(e) { return iso.slice(0,10); }
}

async function api(method, path, body) {
    var opts = { method: method, headers: { 'Authorization': 'Bearer ' + TOKEN } };
    if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    var resp = await fetch(path, opts);
    if (!resp.ok) {
        var err = await resp.json().catch(function() { return { error: resp.statusText }; });
        throw new Error(err.error || err.detail || 'Request failed');
    }
    if (resp.status === 204) return null;
    return resp.json();
}

function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function loadNotes() {
    var container = document.getElementById('notes-container');
    try {
        var notes = await api('GET', '/notes');
        if (!notes || notes.length === 0) {
            container.innerHTML =
                '<div class="empty-state">' +
                '<div class="empty-icon">📭</div>' +
                '<p>No notes yet. Create your first one!</p>' +
                '<button class="btn-add" onclick="openEditor()">+ New Note</button>' +
                '</div>';
            return;
        }
        var html = '<div class="notes-grid">';
        for (var i = 0; i < notes.length; i++) {
            var n = notes[i];
            var bodySnippet = escapeHtml(n.content || '');
            html +=
                '<div class="note-card" onclick="openEditor(\'' + n.id + '\')" title="Click to edit">' +
                '<button class="btn-delete" onclick="event.stopPropagation();confirmDelete(\'' + n.id + '\',\'' + escapeHtml(n.title).replace(/'/g, "\\'") + '\')" title="Delete">🗑</button>' +
                '<h3>' + escapeHtml(n.title) + '</h3>' +
                '<div class="note-body">' + (bodySnippet || '<em>No content</em>') + '</div>' +
                '<div class="note-meta">' +
                '<span>' + formatDate(n.updated_at) + '</span>' +
                '<span style="font-size:0.7rem;opacity:0.5">' + n.id.slice(0,8) + '</span>' +
                '</div></div>';
        }
        html += '</div>';
        container.innerHTML = html;
    } catch(e) {
        container.innerHTML = '<div class="empty-state"><p>⚠️ Failed to load notes.</p><p style="font-size:0.8rem">' + escapeHtml(e.message) + '</p></div>';
        showError('Load failed: ' + e.message);
    }
}

function openEditor(id) {
    editingId = id || null;
    document.getElementById('editor-title').textContent = id ? 'Edit Note' : 'New Note';
    document.getElementById('note-title-input').value = '';
    document.getElementById('note-content-input').value = '';
    document.getElementById('btn-save').disabled = false;

    if (id) {
        api('GET', '/notes/' + id).then(function(note) {
            document.getElementById('note-title-input').value = note.title;
            document.getElementById('note-content-input').value = note.content;
        }).catch(function(e) { showError('Failed to load note: ' + e.message); });
    }

    document.getElementById('editor-overlay').hidden = false;
    document.getElementById('note-title-input').focus();
}

function closeEditor() {
    document.getElementById('editor-overlay').hidden = true;
    editingId = null;
}

async function saveNote() {
    var title = document.getElementById('note-title-input').value.trim();
    var content = document.getElementById('note-content-input').value.trim();
    if (!title) { showError('Title is required.'); return; }

    var btn = document.getElementById('btn-save');
    btn.disabled = true; btn.textContent = 'Saving...';

    try {
        if (editingId) {
            await api('PATCH', '/notes/' + editingId, { title: title, content: content });
        } else {
            await api('POST', '/notes', { title: title, content: content });
        }
        closeEditor();
        await loadNotes();
    } catch(e) {
        showError('Save failed: ' + e.message);
    } finally {
        btn.disabled = false; btn.textContent = 'Save';
    }
}

function confirmDelete(id, title) {
    // Remove existing dialog if any
    var existing = document.getElementById('__confirm_dlg');
    if (existing) existing.remove();

    var overlay = document.createElement('div');
    overlay.className = 'confirm-overlay';
    overlay.id = '__confirm_dlg';
    overlay.innerHTML =
        '<div class="confirm-modal">' +
        '<div class="confirm-icon">🗑️</div>' +
        '<div class="confirm-title">Delete Note</div>' +
        '<div class="confirm-desc">Permanently delete "' + title.replace(/"/g, '&quot;') + '"? This cannot be undone.</div>' +
        '<div class="confirm-buttons">' +
        '<button class="btn btn-cancel" id="__confirm_cancel">Cancel</button>' +
        '<button class="btn btn-danger" id="__confirm_ok">Delete</button>' +
        '</div></div>';
    document.body.appendChild(overlay);

    document.getElementById('__confirm_cancel').onclick = function() { overlay.remove(); };
    document.getElementById('__confirm_ok').onclick = async function() {
        overlay.remove();
        try {
            await api('DELETE', '/notes/' + id);
            await loadNotes();
        } catch(e) { showError('Delete failed: ' + e.message); }
    };
}

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var overlay = document.getElementById('editor-overlay');
        if (overlay && !overlay.hidden) closeEditor();
    }
    if (e.ctrlKey && e.key === 'n' && document.activeElement === document.body) {
        e.preventDefault(); openEditor();
    }
});

// Click outside modal to close
document.getElementById('editor-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeEditor();
});

// Initial load
loadNotes();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    """Serve the self-contained notes frontend SPA."""
    return HTMLResponse(content=NOTES_PAGE_HTML.replace("%s", API_KEY))


# ---------------------------------------------------------------------------
# Health check (no auth)
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    init_db()
    print(f"Starting Personal Notes API on http://0.0.0.0:{PORT}")
    print(f"Auth token: {API_KEY}")
    print(f"DB path:    {DB_PATH}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
