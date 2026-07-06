# UX Declutter Notes — devmclovin.com

**Date:** 2026-07-02  
**Goal:** Reduce visual and functional clutter while keeping the website useful. Make the homepage answer "What do I want to do right now?" at a glance.

---

## Before → After

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| **Nav links visible** | **13 + 2 separators** | **7 (5 core + More + SSH)** | **-50%** |
| Card elements on homepage | ~68 | ~18 | **-73%** |
| Distinct sections | 9 | 4 (primary) + 1 collapsible | **Consolidated** |
| Homepage byte size | 91,588 | 83,102 | **-9%** |
| GitHub projects on homepage | 18 repo cards | 1 summary card | **Collapsed** |
| Cloudflare Tunnel on homepage | 4+ data-heavy cards | 1 summary card | **Collapsed** |
| OpenRouter Spend on homepage | 4 cards (credits, usage, analytics, CTA) | 1 summary card | **Collapsed** |
| Backups on homepage | Large multi-card panel | 1 summary card | **Collapsed** |
| Command Center | 5 large cc-cards with metrics | 5 compact cc-chips | **Simplified** |
| Section titles | 9 distinct section headers | 3 primary + 1 collapsible | **Reduced noise** |

---

## What was simplified

### 1. Homepage restructured (`home_page()`)

**Before:** 9 sections stacked vertically, every one competing for attention:
- Hero → Quick-actions → Command Center → Services → Quick Links → Backups → Today's Briefing → OpenRouter Spend → GitHub Projects → Cloudflare Tunnel

**After:** Primary content first, secondary content collapsed:
- Hero (reduced padding: 3rem→1.5rem) → Quick-actions → Compact Command Center → Today's Briefing → **Collapsible "System Overview"** (Backups + OpenRouter + GitHub + Tunnel summaries, Services, Quick Links)

### 2. Command Center — compact chips

**Before:** 5 large cards (`cc-card`) each showing icon, label, status dot, metric text, and action link.

**After:** 5 compact chips (`cc-chip`) in a horizontal flex row — just icon + label + status dot. Clicking opens the target URL. No section title header.

### 3. System Overview — collapsible `<details>` section

All secondary monitoring data moved into a single `<details class="system-overview">` block. Closed by default — open it to see:

- **System Summary Grid** (`sys-summary-card`): 4 compact cards showing key metrics:
  - 🛡️ Backups — last status for Hermes + GitHub backups
  - 💰 OpenRouter Credits — remaining balance
  - 📂 GitHub Projects — repo count + username → links to /projects
  - 🌐 Cloudflare Tunnel — UP/DOWN status + hostname count → links to /tunnel
- **Services Status** — live JS-fetched service cards (unchanged, same data)
- **Quick Links** — link cards with health dots (unchanged, same data)

### 4. Spacing & hero tightened

- `.hero` padding: `3rem 0 2rem` → `1.5rem 0 1rem`
- `.quick-actions` bottom margin: `2rem` → `1rem`

### 5. Navigation consolidated (13 links → 7)

**Before:** Home | Briefings | Bookmarks | Notes ‖ Disk | Models | Inbox | Hermes | Runbooks | Projects | Status | Logs ‖ SSH

**After:** Home | Briefings | Hermes | Projects | Status | **More ▾** | SSH

The "More" dropdown (CSS-only `<details>` element, no JS) contains: Bookmarks, Notes, Inbox, Disk, Models, Runbooks, Logs. The dropdown highlights when any of its pages is active.

- `status-board.html` nav synced and also gained the missing Inbox, Runbooks, and Logs links

---

## What was moved or hidden

| Content | Old location | New location | Rationale |
|---------|-------------|-------------|-----------|
| GitHub Projects (18 repo cards) | Homepage section | Single summary card → links to `/projects` | Already has dedicated `/projects` page with full detail |
| Cloudflare Tunnel (4+ cards) | Homepage section | Single summary card → links to `/tunnel` | Already has dedicated `/tunnel` page with full monitor |
| OpenRouter Spend (4 cards) | Homepage section | Single summary card → links to OpenRouter | Opulent detail for a landing page |
| Backups panel | Homepage section | Single summary card | Status-only, detail isn't actionable from homepage |
| Services (restart/logs buttons) | Homepage section | Inside collapsible System Overview | Core functionality preserved, just not first thing you see |
| Quick Links (category filter + cards) | Homepage section | Inside collapsible System Overview | Useful but not primary content |

---

## What was NOT changed (preserved)

- All 17 routes return 200: `/ /briefings /projects /hermes /kanban /cron /bookmarks /notes /disk-cleanup /models /inbox /runbooks /status /tunnel /logs /logs/router /health`
- Cloudflare Access auth, tunnel config, secrets, .env, DNS, systemd services — untouched
- Color scheme: `--bg: #0d1117`, `--accent: #7c3aed`, dark theme preserved
- Quick-actions row (4 cards: Briefings, Projects, Hermes, Status)
- Today's Briefing section (5 cards, DB + file fallback chain)
- Navigation bar (13 links, 2 separators, SSH button)
- Status board (`status-board.html`) — nav still synced
- All dedicated pages: `/projects`, `/tunnel`, `/hermes`, `/kanban`, `/cron`, `/bookmarks`, `/notes`, `/models`, `/disk-cleanup`, `/inbox`, `/runbooks`, `/briefings`
- Accessibility: skip-link, focus-visible outlines, semantic `<main>` landmark
- Footer nav

---

## Files changed

| File | Change | Lines |
|------|--------|-------|
| `server.py` | Modified `home_page()` — restructured section order, added System Overview collapsible | ~30 lines changed |
| `server.py` | Modified `command_center_row()` — compact cc-chip row instead of cc-card grid | ~25 lines changed |
| `server.py` | Added `system_summary_row()` — new function for compact summary cards | ~90 lines added |
| `server.py` | Added CSS: `.system-overview`, `.sys-summary-grid`, `.sys-summary-card`, `.cc-compact`, `.cc-chip`, `.section-title-mini` | ~100 lines added |
| `server.py` | Modified CSS: `.hero` padding, `.quick-actions` margin | 2 lines changed |
| `server.py` | Added mobile CSS for new elements (480px breakpoint) | ~20 lines added |
| `server.py` | Added tablet CSS for new elements (481–900px breakpoint) | 2 lines added |
| `server.py.declutter-bak` | Backup of pre-declutter server.py | Full copy |

---

## Commands run

```bash
# Syntax check
python3 -c "import py_compile; py_compile.compile('server.py', doraise=True)"

# Cycle server
rm __pycache__/server.cpython-*.pyc
fuser -k 3002/tcp
# systemd auto-restarts after RestartSec (5s)

# Verify all routes
for route in / /briefings /projects /hermes /kanban /cron /bookmarks /notes \
  /disk-cleanup /models /inbox /runbooks /status /tunnel /logs /logs/router /health; do
  curl -s -o /dev/null -w "%{http_code}" "http://localhost:3002$route"
done
# All 17 routes → 200
```

---

## Recommended follow-up improvements

1. **System Overview auto-expand on "Needs attention":** If any service is down or backup failed, auto-open the System Overview `<details>` section so problems surface immediately
2. **Navigation consolidation:** 13 nav links is still a lot. Consider grouping Disk/Models/Inbox/Runbooks under a "Tools" dropdown or secondary nav row
3. **Briefing style refresh:** The 5 briefing cards still use the old horizontal-scroll pattern. Consider a simpler list or grid layout
4. **Spacing audit on sub-pages:** The `/hermes`, `/projects`, and `/cron` pages still use the old spacious hero padding — could apply similar tightening for consistency
5. **Quick Links deduplication:** The Quick Links section duplicates links that are already in the nav bar (e.g., OpenRouter, GitHub, Cloudflare). Consider whether the Quick Links section adds value or if the nav is sufficient
