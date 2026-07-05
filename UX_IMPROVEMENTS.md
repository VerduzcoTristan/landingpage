# UX Improvements — devmclovin.com Landing Page

**Date:** 2026-07-02  
**Scope:** Navigation, dashboard, accessibility, responsive design, visual polish  
**Constraint:** No changes to secrets, auth, Cloudflare tunnel, DNS, systemd services, or production infrastructure

---

## Summary of Changes

### 1. Keyboard Accessibility — `:focus-visible` Styles
- **What:** Added visible purple focus outlines (`outline: 2px solid var(--accent)`) to all interactive elements: links, buttons, inputs, selects, textareas, category pills/tabs, kanban cards, and link cards.
- **Why:** Keyboard users (and anyone who tabs through the site) now see a clear visual indicator of which element has focus. Previously, focus was invisible — creating a complete accessibility gap.
- **Files:** `server.py` (BASE_CSS), `status-board.html`

### 2. Skip-to-Content Link
- **What:** Added a hidden "Skip to main content" link that appears on Tab for keyboard users, jumping directly to `<main id="main-content">`.
- **Why:** Keyboard users navigating the 12+ nav links no longer have to tab through the entire nav on every page load.
- **Files:** `server.py` (html_page template + CSS)

### 3. Tablet Breakpoint (`@media 481px–900px`)
- **What:** Added a new responsive breakpoint covering tablet-width devices that was previously unhandled. Adjusts: container padding, hero sizing, nav spacing/link size, card widths, quick-links grid, command center, and scroll arrows.
- **Why:** The old CSS only had `@media (max-width: 480px)` for phones and `@media (max-width: 640px)` for a partial range. iPads and small laptops in the 481–900px range had no responsive treatment — cards overflowed, nav cramped, and spacing was inconsistent.
- **Files:** `server.py` (BASE_CSS)

### 4. Hero Quick-Actions Row
- **What:** Added a 4-card grid under the homepage hero with direct links to Briefings, Projects, Hermes, and Status — each with an emoji icon, label, and short description.
- **Why:** The homepage now immediately answers "What can I do here?" instead of requiring users to scan the nav or scroll. Each card is a prominent, tappable entry point to the most-used sections.
- **Files:** `server.py` (home_page + CSS)

### 5. Navigation Visual Grouping
- **What:** Added subtle 1px vertical separators in the nav bar to visually group related links: (Home/Briefings/Bookmarks/Notes) | (Disk/Models/Inbox/Hermes/Runbooks/Projects/Status/Logs) | (SSH). Also added the `/logs` link for discoverability. The SSH terminal button remains a distinct purple accent.
- **Why:** 13 nav links without grouping created visual scanning fatigue. The separators create three logical clusters: core content, system tools, and external access.
- **Files:** `server.py` (nav_links + CSS)

### 6. Semantic HTML Landmarks
- **What:** Wrapped page content in `<main id="main-content">` (was bare `<div class="container">`). Added `aria-label` to the logo link. All pages already had `<nav>` and `<footer>`.
- **Why:** Screen readers and accessibility tools now understand the page structure. The skip-link targets the main landmark.
- **Files:** `server.py` (html_page template)

### 7. Footer Improvement
- **What:** Replaced "devmclovin.com — more coming soon" with a styled footer containing the site name plus a horizontal nav row linking to Home, Hermes, Status, Projects, and Briefings.
- **Why:** The old footer was a dead-end placeholder. The new footer provides useful secondary navigation and looks professional.
- **Files:** `server.py` (html_page template + CSS)

### 8. Page Heading Consistency
- **What:** Changed `/logs` and `/logs/router` pages to use proper `<h1>` elements (were using `<div class="section-title">` as primary heading). Added descriptive subtitles via `<p class="section-timestamp">`.
- **Why:** Every page should have exactly one `<h1>` for accessibility and SEO. The logs pages were the only offenders.
- **Files:** `server.py` (logs_page, router_logs_page)

### 9. Status Board Nav Sync
- **What:** Updated `status-board.html` to match the main nav: added Bookmarks, Disk, Models, Logs, Projects links; added nav separators; styled SSH as a purple button; added `:focus-visible` outlines.
- **Why:** The status page had a stale 6-link nav while the main site had 13. Users would see different navigation depending on which page they landed on — disorienting and inconsistent.
- **Files:** `status-board.html`

### 10. Mobile & Tablet Responsive Tweaks
- **What:** Quick-actions grid collapses to 2-column on phones; cards get smaller padding/icon sizes. Footer nav gap tightens. Tablet breakpoint adjusts hero, nav, and card sizing.
- **Why:** The new quick-actions row needed mobile treatment, and the footer nav needed responsive behavior.
- **Files:** `server.py` (CSS @media sections)

---

## Verification Commands Run

```bash
# Python syntax check
python3 -c "import py_compile; py_compile.compile('server.py', doraise=True)"

# All 17 routes return HTTP 200
for route in / /briefings /projects /hermes /kanban /cron /bookmarks /notes \
  /disk-cleanup /models /inbox /runbooks /status /tunnel /logs /logs/router /health; do
  curl -s -o /dev/null -w "%{http_code}" "http://localhost:3002$route"
done

# Features present in live HTML
curl -s http://localhost:3002/ | grep -c 'skip-link'      # = 3
curl -s http://localhost:3002/ | grep -c 'footer-nav'      # = 5
curl -s http://localhost:3002/ | grep -c 'focus-visible'   # = 10
curl -s http://localhost:3002/ | grep -c 'quick-actions'   # = 3
curl -s http://localhost:3002/ | grep -c 'nav-sep'         # = 3

# Status page nav synced (11 links + SSH button)
curl -s http://localhost:3002/status | grep -c 'href="/bookmarks"'    # = 1

# Logs pages have <h1>
curl -s http://localhost:3002/logs | grep -c '<h1 class="section-title"'  # = 1
```

---

## Files Modified

| File | Lines Changed | Description |
|------|--------------|-------------|
| `server.py` | ~150 lines added | CSS additions (focus-visible, tablet bp, quick-actions, nav-sep, footer-nav, skip-link), hero quick-actions row, semantic HTML, nav grouping, footer, logs page headings |
| `status-board.html` | ~50 lines added | Nav sync (5 new links + separators), SSH button styling, nav-sep CSS, focus-visible CSS |

---

## Known Issues Not Fixed

1. **Status board has its own CSS vars** — The standalone `status-board.html` includes `--green-dim`, `--orange-dim`, `--red-dim` that don't exist in the main `BASE_CSS`. Low priority; they don't break anything.
2. **`/cron/<fake-id>` returns 200** — Returns an empty state message but should ideally 404 for non-existent job IDs. Minor.
3. **No pagination on briefings archive** — `/briefings` loads all 30+ entries at once. Acceptable for now given the volume.
4. **Server runs as `hermes` user; restarts require killing PID + waiting for systemd** — The `kill + sleep + systemd restart` cycle is a known workflow quirk documented in the devmclovin-landing skill.

---

## Recommended Next Improvements

1. **Add a dark/light mode toggle** — Site is hardcoded to dark. A simple JS toggle + CSS custom properties switch would be low-effort.
2. **Add toast notifications for actions** — Model deletions, service restarts, and bookmark toggles currently use `alert()` which is jarring. A small toast component would improve feedback.
3. **Add breadcrumbs on detail pages** — Pages like `/cron/<id>`, `/briefing/<date>`, `/projects/<name>/logs` would benefit from breadcrumb navigation.
4. **Add search to the main nav or homepage** — The only search is on `/briefings`. A site-wide search bar would help.
5. **Audit color contrast ratios** — The dark theme uses `#8b949e` muted text on `#0d1117` background. While visually comfortable, contrast might not meet WCAG AA in all cases. Run an automated audit.
6. **Add loading skeleton states** — The services panel has a text-based loading state; skeleton cards would look more polished.
7. **Consider collapsing nav into a hamburger on mobile** — The current horizontal-scroll nav works but a hamburger menu would be more conventional.
