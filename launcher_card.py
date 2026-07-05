#!/usr/bin/env python3
"""
Project Launcher Card — component for devmclovin.com landing page.

Renders responsive project launcher cards with status, metadata, and
action buttons. Designed to match the existing dark-theme design system
(base tokens: --bg, --bg-card, --border, --accent, etc.).

Usage:
    from launcher_card import render_launcher_cards, LAUNCHER_CSS, MOCK_PROJECTS
    body += render_launcher_cards(MOCK_PROJECTS)

    # In server.py, append LAUNCHER_CSS to the existing BASE_CSS.
"""

from typing import Optional

# ═══════════════════════════════════════════════════════════════
#  CSS
# ═══════════════════════════════════════════════════════════════

LAUNCHER_CSS = """
/* ── Launcher Grid ── */
.launcher-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 1.25rem;
    margin-bottom: 2rem;
    padding: 0.5rem 0 1rem;
}

@media (max-width: 640px) {
    .launcher-grid {
        grid-template-columns: 1fr;
    }
}

/* ── Launcher Card ── */
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

/* ── Card header row: name + status badge ── */
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

/* ── Status badge ── */
.lc-status-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.68rem;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    white-space: nowrap;
    flex-shrink: 0;
}

.lc-status-badge .lc-status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    display: inline-block;
}

.lc-status-badge.online {
    background: rgba(63, 185, 80, 0.12);
    color: var(--green);
}
.lc-status-badge.online .lc-status-dot { background: var(--green); }

.lc-status-badge.offline {
    background: rgba(248, 81, 73, 0.12);
    color: var(--red);
}
.lc-status-badge.offline .lc-status-dot { background: var(--red); }

.lc-status-badge.maintenance {
    background: rgba(210, 153, 29, 0.12);
    color: var(--orange);
}
.lc-status-badge.maintenance .lc-status-dot { background: var(--orange); }

.lc-status-badge.building {
    background: rgba(88, 166, 255, 0.12);
    color: var(--blue);
}
.lc-status-badge.building .lc-status-dot { background: var(--blue); }

/* ── Meta tags row ── */
.lc-meta-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
    font-size: 0.72rem;
    color: var(--text-muted);
}

.lc-lang {
    display: flex;
    align-items: center;
    gap: 0.35rem;
}

.lc-lang-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
}

.lc-deploy-status {
    font-size: 0.68rem;
    padding: 0.1rem 0.4rem;
    border-radius: 4px;
    font-weight: 600;
    letter-spacing: 0.03em;
}

.lc-deploy-status.deployed {
    background: rgba(63, 185, 80, 0.12);
    color: var(--green);
}
.lc-deploy-status.staged {
    background: rgba(210, 153, 29, 0.12);
    color: var(--orange);
}
.lc-deploy-status.none {
    background: rgba(139, 148, 158, 0.12);
    color: var(--text-muted);
}

/* ── Info grid (URL, docs, commit, issues) ── */
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

.lc-info-value.issues {
    color: var(--text-muted);
}
.lc-info-value.issues.has-open {
    color: var(--text);
}

/* ── Dev server command ── */
.lc-dev-cmd {
    font-family: 'SF Mono', 'Fira Code', 'Fira Mono', Menlo, Consolas, monospace;
    font-size: 0.72rem;
    color: var(--text-muted);
    background: var(--bg);
    padding: 0.4rem 0.6rem;
    border-radius: 6px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    border: 1px solid var(--border);
}

/* ── Action buttons ── */
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

.lc-btn .lc-btn-icon {
    font-size: 0.85rem;
}
"""


# ═══════════════════════════════════════════════════════════════
#  LANGUAGE → COLOR MAP
# ═══════════════════════════════════════════════════════════════

_LANG_COLORS = {
    "Python": "#3572A5",
    "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a",
    "Rust": "#dea584",
    "Go": "#00ADD8",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "Shell": "#89e051",
    "Java": "#b07219",
    "Ruby": "#701516",
    "C": "#555555",
    "C++": "#f34b7d",
    "C#": "#178600",
    "Swift": "#F05138",
    "Kotlin": "#A97BFF",
    "Dart": "#00B4AB",
    "Vue": "#41b883",
    "Svelte": "#ff3e00",
}


def _lang_color(lang: str) -> str:
    """Return a hex color for a given programming language."""
    return _LANG_COLORS.get(lang, "#8b949e")


# ═══════════════════════════════════════════════════════════════
#  CARD RENDERER
# ═══════════════════════════════════════════════════════════════


def render_launcher_card(project: dict) -> str:
    """Render a single launcher card from a project data dict.

    Required keys:
        name          — repo / project name (str)
        github_url    — GitHub repo URL (str or None)
        status        — "online" | "offline" | "maintenance" | "building"
        language      — programming language (str or None)
        last_commit   — relative time string e.g. "6d ago" (str or None)
        local_url     — local / deployment URL (str or None)
        docs_url      — documentation URL (str or None)
        docs_label    — display label for docs link (str or None)
        dev_command   — dev server start command (str or None)
        open_issues   — number of open issues (int or None)
        deploy_status — "deployed" | "staged" | None for "not deployed"

    Optional keys:
        id_slug       — safe identifier slug for ARIA label IDs (auto-generated from name if omitted)
    """
    name = project.get("name", "Untitled")
    id_slug = project.get("id_slug") or name.lower().replace(" ", "-").replace(".", "-")
    github_url = project.get("github_url") or "#"
    status = project.get("status", "offline")
    language = project.get("language")
    last_commit = project.get("last_commit")
    local_url = project.get("local_url")
    docs_url = project.get("docs_url")
    docs_label = project.get("docs_label") or "Docs"
    dev_command = project.get("dev_command")
    open_issues = project.get("open_issues")
    deploy_status = project.get("deploy_status")

    # --- Status badge ---
    status_labels = {
        "online": "Online",
        "offline": "Offline",
        "maintenance": "Maintenance",
        "building": "Building",
    }
    status_label = status_labels.get(status, status.title())

    def esc(s: str) -> str:
        """Minimal HTML escape for content inside tags."""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    html = '<article class="launcher-card" role="article" aria-label="Project launcher card for ' + esc(name) + '">\n'

    # ── Header: repo name + status badge ──
    html += '  <div class="lc-header">\n'
    html += f'    <span class="lc-repo-name"><a href="{esc(github_url)}" target="_blank" rel="noopener" aria-label="{esc(name)} on GitHub">{esc(name)}</a></span>\n'
    html += f'    <span class="lc-status-badge {status}" role="status" aria-label="Status: {status_label}">\n'
    html += f'      <span class="lc-status-dot" aria-hidden="true"></span>{status_label}\n'
    html += '    </span>\n'
    html += '  </div>\n'

    # ── Meta row: language, last commit, deploy status ──
    meta_parts = []
    if language:
        color = _lang_color(language)
        meta_parts.append(f'<span class="lc-lang"><span class="lc-lang-dot" style="background:{color}" aria-hidden="true"></span>{esc(language)}</span>')
    if last_commit:
        meta_parts.append(f'<span>Last commit <time datetime="">{esc(last_commit)}</time></span>')
    if deploy_status:
        deploy_classes = {"deployed": "deployed", "staged": "staged"}
        dc = deploy_classes.get(deploy_status, "none")
        dl = deploy_status.capitalize() if deploy_status != "none" else "Not deployed"
        meta_parts.append(f'<span class="lc-deploy-status {dc}">{dl}</span>')

    if meta_parts:
        html += '  <div class="lc-meta-row">\n'
        html += '    ' + ' <span aria-hidden="true">\u00b7</span> '.join(meta_parts) + '\n'
        html += '  </div>\n'

    # ── Info grid ──
    html += '  <div class="lc-info-grid">\n'

    # Local URL
    html += '    <div class="lc-info-item">\n'
    html += f'      <span class="lc-info-label" id="{id_slug}-local-label">Local URL</span>\n'
    if local_url:
        html += f'      <span class="lc-info-value" aria-labelledby="{id_slug}-local-label"><a href="{esc(local_url)}" target="_blank" rel="noopener">{esc(local_url)}</a></span>\n'
    else:
        html += f'      <span class="lc-info-value" aria-labelledby="{id_slug}-local-label" style="color:var(--text-muted)">—</span>\n'
    html += '    </div>\n'

    # Docs
    html += '    <div class="lc-info-item">\n'
    html += f'      <span class="lc-info-label" id="{id_slug}-docs-label">Docs</span>\n'
    if docs_url:
        html += f'      <span class="lc-info-value" aria-labelledby="{id_slug}-docs-label"><a href="{esc(docs_url)}" target="_blank" rel="noopener">{esc(docs_label)}</a></span>\n'
    else:
        html += f'      <span class="lc-info-value" aria-labelledby="{id_slug}-docs-label" style="color:var(--text-muted)">—</span>\n'
    html += '    </div>\n'

    # Dev command
    html += '    <div class="lc-info-item">\n'
    html += f'      <span class="lc-info-label" id="{id_slug}-cmd-label">Dev Server</span>\n'
    if dev_command:
        html += f'      <span class="lc-info-value" aria-labelledby="{id_slug}-cmd-label"><span class="lc-dev-cmd">{esc(dev_command)}</span></span>\n'
    else:
        html += f'      <span class="lc-info-value" aria-labelledby="{id_slug}-cmd-label" style="color:var(--text-muted)">—</span>\n'
    html += '    </div>\n'

    # Open issues
    has_open = open_issues is not None and open_issues > 0
    issue_class = "issues has-open" if has_open else "issues"
    issue_text = f"{open_issues} open" if open_issues is not None else "—"
    html += '    <div class="lc-info-item">\n'
    html += f'      <span class="lc-info-label" id="{id_slug}-issues-label">Open Issues</span>\n'
    html += f'      <span class="lc-info-value {issue_class}" aria-labelledby="{id_slug}-issues-label">{issue_text}</span>\n'
    html += '    </div>\n'

    html += '  </div>\n'

    # ── Action buttons ──
    html += f'  <div class="lc-actions" role="group" aria-label="Project actions for {esc(name)}">\n'
    html += f'    <button class="lc-btn" type="button" aria-label="View logs for {esc(name)}" disabled>\n'
    html += f'      <span class="lc-btn-icon" aria-hidden="true">\U0001f4cb</span> Logs\n'
    html += '    </button>\n'
    html += f'    <button class="lc-btn" type="button" aria-label="Restart {esc(name)}" disabled>\n'
    html += f'      <span class="lc-btn-icon" aria-hidden="true">\U0001f504</span> Restart\n'
    html += '    </button>\n'
    html += f'    <a class="lc-btn" href="{esc(github_url)}" target="_blank" rel="noopener" aria-label="{esc(name)} on GitHub">\n'
    html += f'      <span class="lc-btn-icon" aria-hidden="true">\U0001f419</span> GitHub\n'
    html += '    </a>\n'
    html += f'    <button class="lc-btn" type="button" aria-label="Edit configuration for {esc(name)}" disabled>\n'
    html += f'      <span class="lc-btn-icon" aria-hidden="true">\u2699</span> Config\n'
    html += '    </button>\n'
    html += '  </div>\n'

    html += '</article>\n'
    return html


def render_launcher_cards(projects: list[dict], title: str = "Projects") -> str:
    """Render a full launcher card grid section with a title.

    Args:
        projects: List of project dicts (see render_launcher_card for schema).
        title: Section title text.

    Returns:
        HTML string: section title + responsive grid of launcher cards.
    """
    if not projects:
        return (
            f'<div class="section-title">{title}</div>'
            '<div class="empty-state"><p>No projects found.</p></div>'
        )

    html = f'<div class="section-title">{title}</div>'
    html += '<div class="section-timestamp">Last updated: (static mock data)</div>'
    html += '<div class="launcher-grid" role="list" aria-label="Project launcher cards">\n'

    for p in projects:
        html += render_launcher_card(p)

    html += '</div>\n'
    return html


# ═══════════════════════════════════════════════════════════════
#  MOCK DATA — for development / testing
# ═══════════════════════════════════════════════════════════════

MOCK_PROJECTS: list[dict] = [
    {
        "name": "LLM-Router",
        "github_url": "https://github.com/VerduzcoTristan/LLM-Router",
        "status": "online",
        "language": "Python",
        "last_commit": "6d ago",
        "local_url": "http://router.local",
        "docs_url": "https://github.com/VerduzcoTristan/LLM-Router#readme",
        "docs_label": "README.md",
        "dev_command": "uvicorn main:app --reload",
        "open_issues": 3,
        "deploy_status": "deployed",
        "id_slug": "llm-router",
    },
    {
        "name": "Hermes Agent",
        "github_url": "https://github.com/VerduzcoTristan/Hermes-backup",
        "status": "online",
        "language": "Python",
        "last_commit": "2h ago",
        "local_url": "https://hermes.devmclovin.com",
        "docs_url": "https://hermes-agent.nousresearch.com/docs",
        "docs_label": "hermes-agent docs",
        "dev_command": "hermes tui",
        "open_issues": 0,
        "deploy_status": "staged",
        "id_slug": "hermes-agent",
    },
    {
        "name": "Puzzle Labs",
        "github_url": "https://github.com/VerduzcoTristan/puzzlelabs",
        "status": "offline",
        "language": "TypeScript",
        "last_commit": "13d ago",
        "local_url": "https://puzzlelabs.app",
        "docs_url": "https://github.com/VerduzcoTristan/puzzlelabs#readme",
        "docs_label": "README.md",
        "dev_command": "npm run dev",
        "open_issues": 12,
        "deploy_status": None,
        "id_slug": "puzzle-labs",
    },
    {
        "name": "devmclovin Landing",
        "github_url": "https://github.com/VerduzcoTristan/devmclovin-landing",
        "status": "building",
        "language": "Python",
        "last_commit": "5m ago",
        "local_url": "https://devmclovin.com",
        "docs_url": "https://github.com/VerduzcoTristan/devmclovin-landing#readme",
        "docs_label": "README.md",
        "dev_command": "python3 server.py 3002",
        "open_issues": 7,
        "deploy_status": "deployed",
        "id_slug": "devmclovin-landing",
    },
]


# ═══════════════════════════════════════════════════════════════
#  STANDALONE TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Print rendered HTML for preview
    print(render_launcher_cards(MOCK_PROJECTS))
