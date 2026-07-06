# devmclovin.com — Landing Page Redesign Plan

**Repo:** `VerduzcoTristan/landingpage` (deployed as `devmclovin-landing`, served on port 3002 behind a Cloudflare Tunnel)
**Audience for this document:** a coding agent implementing the redesign. Follow it exactly. Do not invent design. Anything marked `VERIFY` must be checked against the live server/repo before acting on it; if a `VERIFY` assumption is wrong, stop and report instead of guessing.

**Stack (unchanged by this plan):** single-file Python 3 stdlib HTTP server (`server.py`, 8,850 lines), no framework, no build step, systemd unit, Cloudflare Tunnel + Cloudflare Access in front. This plan deliberately does NOT change the stack — it deletes dead weight, fixes broken wiring, and unifies the UI shell.

---

# 1. Current Site Diagnosis

Blunt summary: the site is one good idea (a personal command center) buried under three generations of half-finished rewrites. Roughly **40% of the repo is dead code**, the homepage leads with a marketing tagline nobody needs, five pages carry a stale copy-pasted nav that no longer matches the real nav, and at least two things visibly lie to you (fake backup status, an inbox that silently can't load off-box).

## Clutter
- `server.py` is 8,850 lines; **lines 636–3151 are a single 2,515-line CSS string**. Every page's HTML is built by string concatenation in the same file. Unmaintainable, but functional — the fix is deletion and consolidation, not a rewrite.
- **Two abandoned rewrites live in the repo as full files**: `server_health.py` (2,841 lines — a stale copy of `server.py` with the identical docstring) and `launcher_card.py` (547 lines of a "Project Launcher Card" component with `MOCK_PROJECTS` fake data, imported by nothing).
- **Five overlapping standalone servers** duplicate features `server.py` already serves: `inbox_server.py` (inbox on 8001), `runbook_server.py` (runbooks on 3009), `ollama_api.py` (port 3097) *and* `ollama_api_server.py` (port 3004) (two separate Ollama APIs; `server.py` has a third built-in implementation), `notes_proxy.py` (port 3005 → API on 8123, while `server.py` proxies notes itself → API on 8081).
- One-off scripts committed at repo root: `apply_runbook_integration.py`, `upload_qwen_gguf_to_github.py`, plus process docs `UX_DECLUTTER_NOTES.md`, `UX_IMPROVEMENTS.md`.

## Broken / lying features
- **Backups summary on the homepage is fake.** `backup_panel.py` fetches `http://localhost:8091/api/backups/status`, which is `backups_api.py` — a FastAPI app whose own comment says `Hardcoded sample data. Replace this block with real backup service integration later.` The homepage confidently shows "Hermes: ok, GitHub: ok" from sample data.
- **The Inbox page cannot work off the server.** `inbox.html` line ~240 hardcodes `const API = 'http://127.0.0.1:8000'` — every remote browser tries to call the *visitor's own machine*. The page renders and then silently fails to load items.
- **The documented inbox API doesn't exist.** `docs/agent-inbox-guide.md` tells agents to POST to `https://devmclovin.com/api/inbox` — `server.py` has no `/api/inbox` route at all.
- **`/kanban` is an orphaned page.** 1,624-line, 97KB `kanban_template.html` with a nav containing only `href="/"`. No nav entry, no hub link, no inbound link anywhere on the site.
- **Nav drift, again.** `server.py` renders the current nav (Home/Briefings/Projects/Hermes + Tools▾ + System▾ + SSH), but `status-board.html`, `notes.html`, `inbox.html`, `model_comparison.html`, `model_tuning.html`, and `llm_lab.html` each carry their own hardcoded, older, flat 13-link nav. Users see a different nav depending on the page. This exact problem was "fixed" once before (per `UX_IMPROVEMENTS.md` §9) and regressed because the shell isn't shared.
- **Hermes page links to `http://localhost:11434`** ("Ollama Models" shortcut) — dead for any remote browser.
- **`router-dashboard.html` (served standalone on port 3003) has nav links to `/briefings`, `/projects`, etc.** — those routes don't exist on the 3003 server, so its nav 404s.
- **Dead route + dead file:** `server.py` serves `/models.js`, and `models.js` exists, but no HTML references it.
- **Deploy config contradiction:** `devmclovin-landing.service` runs `User=ubuntu`, `/home/ubuntu/devmclovin-landing/server.py`, but `runbook_server.py` and the UX notes hardcode `/home/hermes/devmclovin-landing` and say the server runs as user `hermes`. One of these is stale. `VERIFY` which is deployed before touching.
- **Boot fragility:** `server.py` lines 20–27 `exec()` `/home/hermes/projects/model-price-comparison/init_db.py` at import time. If that repo moves, the entire landing page crashes on start. Similarly line 67 imports `briefing_archive` from `~/.hermes/tools` (acceptable — briefings are core — but the models import must become lazy/guarded).
- `/cron/<nonexistent-id>` returns 200 with an empty page instead of 404 (already noted in `UX_IMPROVEMENTS.md`, never fixed).

## Duplicated UI / logic
- `/models` route and `/api/models` route contain **two copy-pasted SQL blocks**, and `_get_models_data()` is a third, unused way to read the same DB.
- `backup_panels_row` is imported in `server.py` line 38 and never called; `BACKUP_CSS` is injected into every single page for a panel that no longer renders.
- The 403 page (`_UNAUTH_PAGE`) duplicates the theme CSS inline.
- Every standalone HTML file duplicates the full dark-theme CSS (~45–97KB pages).

## Information hierarchy
- Homepage order is Hero tagline → hub links → status → briefing → collapsed overview. For a personal command center, the tagline ("Personal projects, daily briefings, and AI-powered tools — organized by what you want to do") is self-marketing to an audience of one; status and today's briefing are what you actually came for.
- Briefing cards still use the horizontal-scroll + arrow pattern (`.briefing-scroll`, `.scroll-arrow` with `left:-42px` off-container positioning) — awkward on desktop, worse on mobile.
- `/bookmarks` and `/kanban` exist but aren't in the nav; `/status` is in a dropdown while being one of the most-checked pages.

## Security / privacy
- **Hardcoded credentials in source:** `Bearer notes-secret-token` (server.py `_proxy_notes`, also default in `notes_proxy.py` and `notes_api_server.py`) and `X-API-Key: hermes-commands-api-key-change-me` (server.py `/api/proxy/commands/`). The repo is private and the services are localhost-only, so exposure is low — but the "change-me" key was literally never changed.
- Auth (`is_authenticated`) protects only 3 pages (`/models`, `/model-tuning`, `/llm-lab`); every mutating API (`/api/ollama/pull`, model delete, tuning writes, service restarts) is unauthenticated at the app layer. Acceptable **only if** Cloudflare Access covers the entire hostname. `VERIFY` that CF Access policy applies to `devmclovin.com/*`, not just specific paths. If it does, app-layer auth is redundant everywhere and stays as-is; if not, stop and report.
- Multiple endpoints set `Access-Control-Allow-Origin: *` on APIs that can delete models and restart services. Behind CF Access this is mitigated but sloppy; not a blocker.

## Accessibility / responsive
- Prior pass added skip-link, `:focus-visible`, tablet breakpoint — good, **but only in `BASE_CSS`**. The five standalone HTML pages have partial or no copies of those fixes.
- Muted text `#8b949e` on `#0d1117` is ~4.6:1 — passes AA for normal text; the dimmer `.nav-item-hint` styles `VERIFY` against 4.5:1.
- Nav on mobile is horizontal-scroll; with the dropdown groups this is now acceptable — keep, don't build a hamburger.

## Performance
- Server-side status checks (`get_system_status`, link health) run shell commands/HTTP checks with 5–10 min caches — fine.
- Every page ships the full ~80KB inline CSS+HTML. For a single user on a LAN/tunnel this is a non-issue. No JS framework, no build — this is a *feature*; keep it.

---

# 2. Keep / Remove / Replace / Add

Priorities: **P0** = do first (junk removal, broken things), **P1** = the redesign itself, **P2** = polish.

| Item | Current purpose | Verdict | Reason | Exact change | Priority |
|---|---|---|---|---|---|
| `/` homepage | Entry point: hero, hub, status, briefing, overview | REPLACE | Wrong order, marketing hero, fake backup card | Rebuild per §8 wireframe | P1 |
| Hero tagline block | Self-description | REMOVE | Audience of one; wastes above-fold space | Replace with one-line header row (title + date), per §8 | P1 |
| Hub cards (Read/Build/Monitor/Maintain) | Grouped links | KEEP (restyle) | Good IA, currently oversized | Compact to chip rows per §8 | P1 |
| Landing status strip | Collapsed service status | KEEP (fix) | Right idea, buried mid-page | Move to top; auto-open when any service unhealthy | P1 |
| Today's Briefing section | Latest briefing stories | KEEP (restyle) | Core daily value | Vertical list of 5 headlines, no horizontal scroll | P1 |
| System Overview `<details>` | Backups/OpenRouter/GitHub/Tunnel summaries + services + quick links | KEEP (trim) | Useful secondary info | Remove Backups card (fake data); keep OpenRouter, GitHub, Tunnel summaries + services + quick links | P0 (backup card), P1 (rest) |
| Backups summary card + `backup_panel.py` + `backups_api.py` + `BACKUP_CSS` | Show backup status | REMOVE | Data is hardcoded sample data — it lies | Delete both files, the import at server.py:38, `BACKUP_CSS` from `html_page`, and the backups block in `system_summary_row()` | P0 |
| `/briefings` + `/briefing/<date>` | Briefing archive + detail | KEEP | Core feature, works | Only shell/nav restyle; kill horizontal scroll on cards | P1 |
| `/bookmarks` | Saved/read-later stories | KEEP | Works, reachable via Briefings subnav | Keep out of main nav (subnav only), restyle with shared shell | P2 |
| `/hermes` | Hermes admin actions, cron summary | KEEP (fix) | Used; has dead localhost link | Remove `http://localhost:11434` shortcut card; keep the rest | P0 (link), P2 (styling) |
| `/kanban` + `kanban_template.html` + kanban POST routes | Agent kanban board | REMOVE | Orphaned (zero inbound links), 97KB template, duplicates Hermes tooling | Locked default (see §7 M1): delete template + GET route + `kanban_page()`; keep POST routes + DB helpers unless M0 proves them unused | P0 |
| `/cron`, `/cron/<id>`, `/cron/<id>/<file>` | Cron job dashboard | KEEP (fix) | Used, works | 404 on unknown job id | P0 (404 fix) |
| `/notes` + `notes.html` | Personal notes app | KEEP (fix) | Works via server proxy | Replace hardcoded nav with injected shared nav (§5 NavInjection) | P1 |
| `/inbox` + `inbox.html` | Agent inbox UI | FIX | Hardcoded `127.0.0.1:8000` API base — broken remotely; documented `/api/inbox` proxy missing | Add `/api/inbox/*` proxy in server.py → `http://127.0.0.1:8000/api/v1/*`; change `inbox.html` to `const API = ''` + same-origin paths; inject shared nav | P0 |
| `/runbooks` | Copy-paste server fixes | KEEP | Used, self-contained (`runbook_data.py`) | Shell restyle only | P2 |
| `/models` | Model price comparison (auth) | KEEP (fix) | Used | Deduplicate the 3 SQL paths into one helper; make the external-repo import lazy so server boots without it | P0 (import), P2 (dedupe) |
| `/model-tuning`, `/llm-lab` | Tuning datasets / eval lab (auth) | KEEP | Real, recent, functional tools | No feature work; inject shared nav only | P1 (nav) |
| `/projects`, `/projects/<n>/logs`, `/projects/<n>/config` | Project launcher + GitHub repos | KEEP | Used | Shell restyle only | P2 |
| `/status` + `status-board.html` | Service status board | FIX | Standalone file with stale nav; full feature set NOT inventoried — a rebuild risks silent feature loss | Keep the file; apply NavInjection (§5) like the other templates; delete its stale local nav CSS. No rebuild | P1 |
| `/tunnel` | Cloudflare tunnel monitor | KEEP | Works, secondary | Nothing beyond shell consistency | P2 |
| `/disk-cleanup` | Disk usage/cleanup | KEEP | Useful ops page | Nothing beyond shell consistency | P2 |
| `/logs` and `/logs/router` | Journal viewers | REPLACE (merge) | Two near-identical pages | One `/logs` page with two tabs (Server / Router); `/logs/router` becomes 301 → `/logs?tab=router` | P1 |
| `/health` | Uptime probe | KEEP | Used by monitoring | None | — |
| `/models.js` route + `models.js` file | Legacy script | REMOVE | Referenced by nothing | Delete file and route | P0 |
| Main nav (4 links + Tools▾ + System▾ + SSH) | Site navigation | KEEP (adjust) | Current grouping is good | Move Status out of Tools▾ to top level (it's a command center); final nav per §3 | P1 |
| Footer nav | Secondary links | KEEP | Fine | Sync with final nav | P2 |
| `server_health.py` | Stale 2,841-line copy of server.py | REMOVE | Dead | Delete file | P0 |
| `launcher_card.py` | Mock launcher component | REMOVE | Dead, fake data | Delete file | P0 |
| `ollama_api.py`, `ollama_api_server.py` | Standalone Ollama APIs (3097, 3004) | REMOVE | server.py serves all `/api/ollama/*` itself; nothing in repo references these | Delete both. `VERIFY`: `systemctl list-units --all | grep -i ollama` shows no unit running them | P0 |
| `inbox_server.py` | Standalone inbox frontend (8001) | REMOVE | `/inbox` served by server.py | Delete. `VERIFY` no systemd unit | P0 |
| `runbook_server.py` | Standalone runbooks (3009) | REMOVE | `/runbooks` served by server.py | Delete. `VERIFY` no systemd unit | P0 |
| `notes_proxy.py` | Notes proxy (3005→8123) | REMOVE | server.py proxies notes (→8081); this one targets a different port and is legacy | Delete. `VERIFY` no systemd unit and notes work via `/api/notes` | P0 |
| `apply_runbook_integration.py`, `upload_qwen_gguf_to_github.py` | One-off scripts | REMOVE | Already applied / one-shot | Delete | P0 |
| `UX_DECLUTTER_NOTES.md`, `UX_IMPROVEMENTS.md` | Past process notes | REMOVE (archive) | Historical noise at root | Move to `docs/archive/` | P0 |
| `router_server.py`, `router_metrics.py`, `router-dashboard.html`, `router-dashboard.service`, `e2e_integration_test.py` | Standalone LLM router dashboard (3003) + its tests | KEEP (fix) | Separate deployed service; out of redesign scope | Only fix: replace its fake main-site nav with a single `← devmclovin.com` home link | P2 |
| `test_runbooks.py` | Runbook tests | KEEP | Real tests | Run in verification | — |
| `setup.sh`, `devmclovin-landing.service` | Deploy | KEEP (fix) | Paths contradict reality (`ubuntu` vs `hermes`) | `VERIFY` deployed user/path on the box (`systemctl cat devmclovin-landing`); update the files in-repo to match reality; do NOT touch the live systemd unit | P0 |
| Hardcoded tokens (`notes-secret-token`, `hermes-commands-api-key-change-me`) | Auth to local services | FIX | Secrets in source | Read from `.env` via existing `_load_env_var()` (`NOTES_API_TOKEN`, `COMMANDS_API_KEY`), fall back to current values so nothing breaks; document rotation in README | P1 |
| `docs/agent-inbox-guide.md` | Agent API docs | FIX | Documents a nonexistent endpoint | After `/api/inbox` proxy exists, verify examples match implementation | P0 |
| README | — | ADD | Repo has no README; agents/you re-derive architecture every time | Add README.md: what runs where (ports table), how to deploy, how to restart | P1 |

---

# 3. New Site Concept

**Primary purpose:** a single-glance answer to "is my stack healthy, what happened today (briefing), and jump me to any tool in one click." A command center, not a portfolio.

**Target user:** Tristan, alone, authenticated via Cloudflare Access, on desktop most of the time and phone sometimes.

**Top-level layout:** one shared shell (`html_page()`) used by *every* page — including the five template-file pages — with: sticky top nav, `<main>` container (max-width 1100px), minimal footer.

**Navigation model (final, locked):**

```
Home | Briefings | Projects | Status | Hermes | Tools ▾ | SSH
                                        Tools ▾ = Notes, Inbox, Runbooks,
                                                  Cron, Models, Tuning, LLM Lab,
                                                  Disk, Tunnel, Logs
```

- Status is promoted to top level (dashboard site → health is primary).
- Tools▾ and System▾ merge into one dropdown; two dropdowns for 11 items was over-engineering. Group labels inside the dropdown: "Daily" (Notes, Inbox, Runbooks) and "Ops" (Cron, Models, Tuning, LLM Lab, Disk, Tunnel, Logs).
- Bookmarks stays reachable only via the Briefings subnav. Kanban is gone.

**Visual hierarchy (homepage, top→bottom):** 1) health strip, 2) today's briefing, 3) go-anywhere hub chips, 4) collapsed system overview. 

**Above the fold (desktop 1280×800):** nav, status strip, briefing header + first 2–3 headlines.
**Hidden deeper:** service cards, quick links, OpenRouter/GitHub/Tunnel summaries (inside the collapsible), all ops tools (dropdown).
**Removed entirely:** hero tagline, backups card, kanban, dead servers/files per §2.

---

# 4. Proposed New Page Structure

| # | Page | Route | Purpose |
|---|---|---|---|
| 1 | Home | `/` | Command center: health, today's briefing, jump-off |
| 2 | Briefings | `/briefings`, `/briefing/<date>`, `/bookmarks` | Read/search/save daily briefings |
| 3 | Projects | `/projects` (+ `/logs`, `/config` children) | Launch/restart/inspect projects + GitHub repos |
| 4 | Status | `/status` | Full service health board |
| 5 | Hermes | `/hermes` | Agent admin actions + cron summary |
| 6 | Notes | `/notes` | Personal notes CRUD |
| 7 | Inbox | `/inbox` | Agent update queue |
| 8 | Runbooks | `/runbooks` | Copy-paste fixes |
| 9 | Cron | `/cron`, `/cron/<id>`, `/cron/<id>/<file>` | Job schedules + outputs |
| 10 | Models | `/models` | Price comparison + local models |
| 11 | Tuning | `/model-tuning` | Fine-tune datasets/HF pulls |
| 12 | LLM Lab | `/llm-lab` | Evals, traces, arena |
| 13 | Disk | `/disk-cleanup` | Storage usage |
| 14 | Tunnel | `/tunnel` | Cloudflare tunnel monitor |
| 15 | Logs | `/logs` (tabs: server, router; `/logs/router` → 301) | Journals |

Removed pages: `/kanban` (410 Gone or 404), `/models.js` (404).

Per-page details (only pages that change materially; others get shell restyle only):

### 1. Home `/`
- **Sections:** StatusStrip, TodayBriefing, HubChips, SystemOverview (collapsed), footer.
- **Data:** `/api/status` (client fetch), briefing DB (server-side), quick links JSON, OpenRouter/GitHub/Tunnel cached fetchers (server-side).
- **Empty/loading/error:** StatusStrip shows "Checking services…" then either "All N services online" or "K need attention" (auto-open). Briefing: existing 3-level fallback chain (today DB → today file → latest DB → "☕ No briefings yet — the morning briefing runs at 7am UTC."). SystemOverview cards each render "Not configured" when their token/API is absent (already implemented — keep).
- **Mobile:** single column; StatusStrip pills wrap; hub chips wrap 2-per-row; briefing list unchanged.

### 4. Status `/status`
- **Unchanged in features.** Stays served from `status-board.html`; the only change is NavInjection (§5) and removal of its stale local nav CSS. Do not rebuild, restructure, or restyle its body.

### 7. Inbox `/inbox`
- **Sections:** unchanged UI (filter pills, item cards, detail modal) — only the API base and nav change.
- **Data:** `/api/inbox/*` (new same-origin proxy → `127.0.0.1:8000/api/v1/*`). `VERIFY` the upstream inbox API is actually on port 8000 and path prefix `/api/v1` (check `docs/agent-inbox-guide.md` examples + `curl 127.0.0.1:8000/api/v1/items` on the box).
- **States:** loading text; error banner "Inbox API unreachable" (currently fails silently — must be added); empty: "No inbox items."
- **Mobile:** existing responsive behavior.

### 15. Logs `/logs`
- **Sections:** h1, tab row (Server | Router), `<pre>` log block per tab.
- **Data:** existing `logs_page()` / `router_logs_page()` journal reads, merged into one handler with `?tab=` param.
- **States:** empty journal → "No recent entries."; command failure → show stderr in a muted block.
- **Mobile:** `<pre>` gets `overflow-x:auto`.

---

# 5. Component-Level Redesign

All components are Python functions returning HTML strings inside `server.py` (keep that pattern), except NavInjection which touches templates.

### CREATE — `render_nav(active: str) -> str`
- **Purpose:** single source of truth for the nav (currently inlined in `html_page()`); reused by template injection.
- **Props:** `active` key.
- **Behavior:** exactly the nav in §3. Dropdown = CSS `<details>` (keep existing close-on-outside-click JS). Active link gets `.active`. SSH stays a purple button.
- **Used by:** `html_page()`, and injected into `notes.html`, `inbox.html`, `model_comparison.html`, `model_tuning.html`, `llm_lab.html`.

### CREATE — NavInjection (template mechanism) — applies to all 6 templates: `notes.html`, `inbox.html`, `status-board.html`, `model_comparison.html`, `model_tuning.html`, `llm_lab.html`
- **Purpose:** kill nav drift permanently.
- **Step 1 — extract nav assets in `server.py`:** move the nav-related CSS out of `BASE_CSS` into a new module constant `NAV_CSS`, and move the dropdown-close JS (the `details.nav-dropdown` toggle/outside-click/Escape handlers currently inline in `html_page()`) into a constant `NAV_JS`. `html_page()` then emits `{NAV_CSS}{BASE_CSS}` and embeds `NAV_JS` where the JS was — zero rendered-output change on server-rendered pages.
  - Selectors to move into `NAV_CSS` (move each whole rule block; if a selector is shared with non-nav layout, copy it instead of untangling): top-bar `nav` rules, `nav a`, `.nav-dropdown`, `.nav-more-summary`, `.nav-dropdown-menu`, `.nav-menu-label`, `.nav-item-main`, `.nav-item-hint`, `.hermes-btn`, `.skip-link`, the global `:focus-visible` rules, plus copies of every `@media` block that targets these selectors.
- **Step 2 — templates:** in each template, delete the entire hardcoded `<nav>…</nav>` block AND that template's own nav CSS rules (same selector list), then place three placeholders: `__SITE_NAV__` where the nav was, `__SITE_NAV_CSS__` at the top of the `<style>` tag, `__SITE_NAV_JS__` (inside a `<script>` tag) just before `</body>`.
- **Step 3 — serving:** every template-serving function runs `.replace("__SITE_NAV__", render_nav("<active-key>")).replace("__SITE_NAV_CSS__", NAV_CSS).replace("__SITE_NAV_JS__", NAV_JS)`.
- **Hard rule for the agent:** change nothing else inside the templates. If a template's nav block cannot be cleanly isolated, stop on that file and report; do the others.

### CREATE — `status_strip() -> str` (rework of `home_status_strip`)
- **Purpose:** topmost homepage element; instant health read.
- **Data:** client-side fetch of `/api/status` (existing).
- **Visual:** full-width `<details>` bar, 44px collapsed height: left = "● All systems normal" (green dot) or "● 2 services need attention" (red dot, bold); right = `N OK / K issues` pills + expand hint. Expanded = existing mini service grid + "Open full status board →".
- **Behavior:** if `issues > 0`, JS sets `details.open = true` automatically. On fetch error: amber dot, "Status API unavailable".
- **Used by:** homepage only.

### REWRITE — `home_page()`
- Order: header row → `status_strip()` → TodayBriefing → `home_hub_html()` (compact) → SystemOverview.
- Header row replaces `.hero`: `<div class="page-head"><h1>devmclovin</h1><span class="page-date">Saturday, Jul 5</span></div>` — one line, ~1rem bottom margin, no tagline paragraph.

### CREATE — `briefing_list_home(articles: list[dict], date_str: str) -> str`
- **Purpose:** today's stories on the homepage without horizontal scrolling.
- **Hard rule:** this is a NEW function used ONLY by `home_page()`. Do NOT modify `briefing_card_from_db` or `briefing_card` — the `/briefings` archive page uses them and must keep working unchanged.
- **Visual:** section title row ("Today's Briefing — {date}" left, "All briefings →" link right, flex space-between); then a plain vertical list (max 5 rows): category badge, story title linking to `source_url` (fallback `/briefing/<date>`), one-line impact/summary in muted text. Row padding 0.6rem, 1px `--border` dividers, no per-story cards. **No bookmark toggle on the homepage** — bookmarking lives on `/briefings` only.
- **States:** `home_page()` keeps its existing 3-level fallback chain and ☕ empty state; only the final render call switches to this function.

### REWRITE — `home_hub_html()`
- Keep 4 groups (Read/Build/Monitor/Maintain) but compact: each group = one row: icon + group label (600 weight, 0.85rem) followed by inline link chips (pill buttons, 0.8rem, `--bg-card` bg, border, 0.35rem×0.7rem padding). Remove per-group description sentences. Total component height ≤ 180px desktop. Remove `/kanban`-adjacent links if any remain; "Saved" chip stays → `/bookmarks`.

### REWRITE — `system_summary_row()`
- Delete the Backups card block entirely (with `backup_panel` import). 3 remaining cards: OpenRouter Credits, GitHub Projects, Cloudflare Tunnel. Grid `repeat(auto-fit, minmax(220px,1fr))`.

### FIX — `/status` (`status-board.html`) — corrected from an earlier REPLACE verdict
- Do NOT rebuild this page. Its 1,170 lines were not fully inventoried; a rebuild risks silently dropping features. It stays a template file.
- Only changes: NavInjection (above); delete its now-dead local nav CSS; remove the orphan `--green-dim/--orange-dim/--red-dim` vars only if grep within the file shows zero uses.

### CREATE — `logs_page(tab)` merged viewer
- Tabs as links (`/logs`, `/logs?tab=router`), active tab underlined with `--accent`. Body = existing journal `<pre>` output per tab. Delete `router_logs_page()` after merge; keep 301 from `/logs/router`.

### CREATE — `_proxy_inbox` handler
- Mirror the `_proxy_notes` pattern exactly: any method (GET/POST/PUT/DELETE/OPTIONS) on `/api/inbox/<rest>` forwards to `http://127.0.0.1:8000/<rest>` — strip ONLY the `/api/inbox` prefix, forward the remainder verbatim, forward body and status, no auth header (guide says none).
- Client change is exactly ONE line in `inbox.html`: `const API = 'http://127.0.0.1:8000'` → `const API = '/api/inbox'`. The existing `api()` helper prepends `API` to paths like `/api/v1/items`, so requests become `/api/inbox/api/v1/items` → upstream `/api/v1/items`. Touch no other JS in the file except adding the error banner (§6 #1).
- `VERIFY` first on the box: `curl -s http://127.0.0.1:8000/api/v1/items` returns JSON. If it doesn't, stop and report the real port/path — do not guess.

### DELETE — components
- `command_center_row()` if no longer referenced after home rewrite (`VERIFY` with grep).
- `backup_panels_row`, `BACKUP_CSS`, `get_backup_status` (file deletion).
- All `kanban_*` render functions + template (subject to the §2 kanban `VERIFY`).
- `spending_card_row()`, `github_projects_row()`, `cloudflare_tunnel_row()` — `VERIFY`: these are the old full-width homepage rows; if now referenced only by their dedicated pages (`/tunnel` uses `cloudflare_tunnel_page`), delete the unused ones.
- `_get_models_data()` (dead third path to models DB) — fold into one `fetch_models()` helper used by both `/models` and `/api/models`.

---

# 6. Broken Things to Fix

| # | File / path | Problem | User-visible impact | Exact fix | Verify |
|---|---|---|---|---|---|
| 1 | `inbox.html` ~line 240 | `const API = 'http://127.0.0.1:8000'` | Inbox loads no items from any remote browser; silent failure | ONE line: `const API = '/api/inbox'` (the `api()` helper prepends it; touch no other calls). Plus add an error banner div shown when `api()` rejects | From a non-server machine: `/inbox` lists items; DevTools shows no requests to 127.0.0.1 |
| 2 | `server.py` (routes) | Documented `/api/inbox` endpoint missing | Agents following `docs/agent-inbox-guide.md` get 404s | Add `_proxy_inbox` per §5: strip `/api/inbox` prefix, forward rest verbatim to `http://127.0.0.1:8000` (M0 `VERIFY`) | `curl https://devmclovin.com/api/inbox/api/v1/items` returns the same JSON as `curl 127.0.0.1:8000/api/v1/items` on the box; update the guide's Base URL/examples to match reality |
| 3 | `backup_panel.py` + `backups_api.py` + `system_summary_row()` | Homepage backup status is hardcoded sample data | You believe backups are "ok" based on nothing | Delete files, import, CSS, and card (§2) | Homepage renders; grep for `backup` in server.py returns nothing load-bearing |
| 4 | `server.py:21-27` | Module-level `exec()` of external repo file | Server won't boot if `~/projects/model-price-comparison` moves | Wrap in try/except; on failure set flag; `/models` + `/api/models` return a friendly "Models DB unavailable" page/JSON instead of crashing the whole site | Temporarily rename the external dir on a dev run; server boots; `/models` shows error state; rename back |
| 5 | `notes.html`, `inbox.html`, `status-board.html`, `model_comparison.html`, `model_tuning.html`, `llm_lab.html` | Hardcoded stale flat navs | Different nav on 6 pages; missing dropdowns | NavInjection (§5) | Every page's nav HTML is byte-identical (curl + diff the `<nav>` block across all routes) |
| 6 | `server.py` `hermes_page()` | Shortcut links `http://localhost:11434` | Dead link off-box | Delete that shortcut card | `/hermes` has no localhost hrefs |
| 7 | `kanban_template.html` + `/kanban` routes | Orphaned page, no inbound links | Invisible 97KB feature rotting | Delete per §2 (after kanban `VERIFY`) | `/kanban` → 404; `pnpm`-style grep: no references remain |
| 8 | `server.py` `/models.js` + `models.js` | Dead route+file | None (that's the point) | Delete both | `/models.js` → 404 |
| 9 | `server.py` cron routes | `/cron/<unknown>` returns 200 | Fake pages for typo'd URLs | In `cron_job_detail_page`, return `(404, page)` when job id not in `load_cron_jobs()`; handler sends 404 status | `curl -o /dev/null -w "%{http_code}" /cron/nonexistent` → 404 |
| 10 | `devmclovin-landing.service`, `setup.sh` | `User=ubuntu` + `/home/ubuntu/...` vs everything else assuming `hermes` | Re-running setup.sh would install a broken unit | `VERIFY` live: `systemctl cat devmclovin-landing`. Update repo files to match the real user/path | Repo unit file matches `systemctl cat` output |
| 11 | `server.py` `_proxy_notes`, `/api/proxy/commands/` | Hardcoded `notes-secret-token`, `hermes-commands-api-key-change-me` | Secrets in git | Read via `_load_env_var("NOTES_API_TOKEN")` / `_load_env_var("COMMANDS_API_KEY")` with current values as fallback; add both to `.env` on the box (manual step — list it in the final report) | Notes CRUD works; Hermes admin actions work |
| 12 | `router-dashboard.html` | Nav links to main-site routes that 404 on port 3003 | Broken nav on router dashboard | Replace its nav with single `<a href="https://devmclovin.com">← devmclovin.com</a>` | Click-through works; no 404 links |
| 13 | `server.py:38` | `backup_panels_row` imported, never called; `BACKUP_CSS` in every page | Dead CSS on all pages | Removed with #3 | Pages render without `BACKUP_CSS` |
| 14 | `server.py` `/models` + `/api/models` | Copy-pasted SQL blocks ×2 + unused `_get_models_data` | Drift risk when schema changes | Single `fetch_models()` helper | Both endpoints return identical data before/after |
| 15 | Repo root | `server_health.py`, `launcher_card.py`, one-off scripts, 2 UX notes | Cognitive clutter; agents keep reading 3,400 dead lines | Delete / move to `docs/archive/` per §2 | Root listing matches §7 M1 acceptance |

---

# 7. Exact Implementation Plan

Global rules for every milestone:
- Work on a branch `redesign/landing-2026-07`.
- After every server.py change: `python3 -c "import py_compile; py_compile.compile('server.py', doraise=True)"`.
- Local test run: `python3 server.py 3102` (do not fight the live 3002 service during dev) and curl the route matrix (see M6).
- Rollback note applies to all milestones: every step is a plain git commit; `git revert` the offending commit. Never edit the live systemd unit or Cloudflare config.

### Milestone 0 — Environment preflight (mandatory, before any code change)
- **This code only runs on the production box.** `server.py` imports `briefing_archive` from `~/.hermes/tools` and exec's `~/projects/model-price-comparison/init_db.py` at module load, and every page depends on `~/.hermes` / `~/.devmclovin` data. Implementation and testing MUST happen on that box (or an environment with those home directories cloned). If you are not on it, STOP and report — do not stub or mock these dependencies to make the server boot elsewhere.
- Record and paste into the final report (these outputs resolve every live-box `VERIFY` in this spec):
  ```bash
  systemctl cat devmclovin-landing
  ls /etc/systemd/system/ | grep -iE 'devmclovin|router|inbox|runbook|notes|ollama|backup'
  grep -rl 'inbox_server|runbook_server|notes_proxy|ollama_api|backups_api|server_health' /etc/systemd/system/ 2>/dev/null
  curl -s http://127.0.0.1:8000/api/v1/items | head -c 200      # inbox upstream
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9091/api/status   # api_server
  grep -rn '/kanban/' ~/.hermes --include='*.json' --include='*.py' -l 2>/dev/null | head  # kanban writers
  ```
- Locate the file `_load_env_var()` reads (read its source in server.py) and confirm that file exists on the box. If notes CRUD currently works in production, do NOT change any notes port wiring anywhere in this project.
- **Stop conditions:** not on the box; `systemctl cat` fails; inbox upstream not answering on 8000.

### Milestone 1 — Cleanup and remove junk
- **Deletion rule (two tiers):**
  - *Provably dead from the repo alone* — delete outright: `server_health.py`, `launcher_card.py`, `models.js` (+ its route), `apply_runbook_integration.py`, `upload_qwen_gguf_to_github.py`.
  - *Runnable servers* — delete only if the M0 systemd grep showed zero references: `ollama_api.py`, `ollama_api_server.py`, `inbox_server.py`, `runbook_server.py`, `notes_proxy.py`, `backups_api.py`. If a unit references one, or M0 could not be completed, `git mv` it to `attic/` instead and report. Never leave a running systemd unit pointing at a deleted file.
- **Also:** delete `backup_panel.py` + its import at server.py:38 + `BACKUP_CSS` usage + the Backups card in `system_summary_row()`; move `UX_DECLUTTER_NOTES.md`, `UX_IMPROVEMENTS.md` → `docs/archive/`.
- **Kanban (default locked):** delete `kanban_template.html`, the `/kanban` GET route, and `kanban_page()`. KEEP the three `/kanban/*` POST routes and the `kanban_*`/`load_kanban_*` DB helpers unless the M0 kanban grep proved nothing writes to them (in which case delete those too). Report which branch you took.
- **Stop conditions:** M0 not completed → do only the "provably dead" tier.
- **Verification:** py_compile passes; dev server boots on 3102; `/`, `/hermes` render; `/kanban` (GET), `/models.js` → 404.
- **Acceptance:** repo root contains only: `server.py`, `api_server.py`, `notes_api_server.py`, `router_server.py`, `router_metrics.py`, `router-dashboard.html`, `router-dashboard.service`, `runbook_data.py`, `test_runbooks.py`, `e2e_integration_test.py`, the 6 template HTML files (`notes.html`, `inbox.html`, `status-board.html`, `model_comparison.html`, `model_tuning.html`, `llm_lab.html`), `cloudflare_api.py`, `setup.sh`, `devmclovin-landing.service`, `.gitignore`, `docs/`, `specs/`, optionally `attic/`, `README.md` (M4 adds it).

### Milestone 2 — Fix broken routes/features
- **Files:** `server.py`, `inbox.html`, `devmclovin-landing.service`, `setup.sh`, `docs/agent-inbox-guide.md`, `router-dashboard.html`.
- **Tasks:** items #1, #2, #4, #6, #9, #10, #11, #12 from §6, in that order.
- **Stop conditions:** inbox upstream not on `127.0.0.1:8000/api/v1` → stop, report actual port/path. `systemctl cat devmclovin-landing` unavailable/contradictory → leave service files untouched, report.
- **Verification:** curl matrix; inbox proxy round-trip (create + list + delete a test item); boot-without-models-repo test (#4).
- **Acceptance:** all §6 "Verify" columns for the listed items pass.

### Milestone 3 — Redesign landing page layout
- **Files:** `server.py` only (`home_page`, `home_hub_html`, `status_strip`, `system_summary_row`, `BASE_CSS` additions/removals).
- **Tasks:** implement §8 wireframe exactly: page-head row (kill hero), status strip on top with auto-open-on-issue, vertical TodayBriefing list, compact hub chips, trimmed SystemOverview.
- **Stop conditions:** none anticipated.
- **Verification:** visual check at 1280px, 768px, 375px; `curl /` contains `page-head`, no `class="hero"`, no backup card; JS console clean.
- **Acceptance:** above-the-fold at 1280×800 shows nav + status strip + briefing title + ≥2 headlines; zero horizontal scrollbars.

### Milestone 4 — Redesign remaining pages
- **Files:** `server.py` (`render_nav`, `NAV_CSS`, `NAV_JS`, `html_page`, merged `logs_page`), templates ×6 (NavInjection, including `status-board.html`); add `README.md`.
- **Tasks:** NavInjection on all 6 templates; promote Status to top nav; merge logs pages into `/logs?tab=` (+301 from `/logs/router`); README with ports table (3002 landing, 9091 api_server, notes port `VERIFY` from M0, 8000 inbox `VERIFY`, 8092 commands `VERIFY`, 3003 router, 11434 ollama).
- **Stop conditions:** a template's nav block can't be isolated cleanly → stop on that file, report, continue with the others.
- **Verification:** curl each of the 15 content routes, extract the `<nav>…</nav>` block, diff → byte-identical everywhere; `/logs/router` → 301 → renders router tab; `/status` still shows the same services and its restart buttons still work.
- **Acceptance:** nav identical everywhere; README exists; no template retains a hardcoded nav.

### Milestone 5 — Polish, responsive, accessibility
- **Files:** `server.py` (BASE_CSS + pages it renders) only. Do NOT do polish work inside the template apps (`llm_lab.html`, `model_tuning.html`, `model_comparison.html`, `notes.html`, `inbox.html`, `status-board.html`) beyond what NavInjection already did.
- **Tasks:**
  1. Replace `alert()` calls **in server.py-generated HTML only** (grep `alert(` in server.py) with an inline status line next to the triggering control. Leave template-app alerts alone.
  2. `/briefings` archive: CSS-only de-scroll — in BASE_CSS set `.briefing-scroll { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); overflow:visible; }` and `.scroll-arrow { display:none; }`. No HTML/JS changes to that page.
  3. Contrast: bump `.nav-item-hint` and `.section-timestamp` color to `#9aa4b2` if measured below 4.5:1 on `#0d1117`.
  4. `overflow-x:auto` on all `<pre>` blocks in BASE_CSS.
  5. Dead-CSS removal with a hard rule: a CSS block may be deleted ONLY if grep of its class name across `server.py` + all `*.html` returns zero uses. Candidates: `.cc-card`, `.cc-chip`, kanban classes, backup classes, `.quick-actions` (if home rewrite dropped it), `.briefing-scroll` arrows JS hooks.
- **Stop conditions:** none.
- **Verification:** Tab through homepage — every interactive element shows a focus ring; 375px width: no horizontal scroll on `/`, `/briefings`, `/status`, `/notes`, `/inbox`.
- **Acceptance:** `grep -n "alert(" server.py` → 0 matches in UI code paths; every deleted CSS class greps to zero.

### Milestone 6 — Final verification
- **Tasks:** run full route matrix on dev port:
  ```bash
  latest=$(curl -s http://localhost:3102/briefings | grep -o '/briefing/[0-9-]*' | head -1)
  for r in / /briefings "$latest" /bookmarks /hermes /cron /notes /inbox \
    /runbooks /models /model-tuning /llm-lab /projects /status /tunnel /disk-cleanup \
    /logs "/logs?tab=router" /health; do
    echo "$r $(curl -s -o /dev/null -w '%{http_code}' "http://localhost:3102$r")"; done
  # expect 200s (models pages 200 via localhost bypass); then:
  for r in /kanban /models.js /cron/nonexistent; do
    echo "$r $(curl -s -o /dev/null -w '%{http_code}' "http://localhost:3102$r")"; done  # expect 404s
  python3 test_runbooks.py
  ```
- Run the §10 checklist manually. Produce the final report (files changed, checklist results, the two manual server-side steps: add `NOTES_API_TOKEN`/`COMMANDS_API_KEY` to `.env`, restart service).
- **Acceptance:** all checks green; report written.

---

# 8. New Landing Page Wireframe

Desktop (max-width 1100px container, centered):

```
┌────────────────────────────────────────────────────────────────────┐
│ 🚀 devmclovin   Home Briefings Projects Status Hermes Tools▾ [SSH] │  sticky nav, 56px
├────────────────────────────────────────────────────────────────────┤
│ devmclovin                                    Saturday, July 5     │  page-head, h1 1.5rem
│                                                                    │  margin-bottom 1rem
│ ┌────────────────────────────────────────────────────────────────┐ │
│ │ ● All systems normal        [6 OK] [0 issues]        expand ▾  │ │  status strip, 44px
│ └────────────────────────────────────────────────────────────────┘ │  auto-open if issues>0
│                                                                    │  margin-bottom 1.5rem
│ Today's Briefing — Jul 5                          All briefings →  │  section title row
│ ┌────────────────────────────────────────────────────────────────┐ │
│ │ [AI] Story title one……………………………………………………  ☆ │ │  5 rows max,
│ │      one-line impact summary in muted text                    │ │  1px dividers,
│ │ ─────────────────────────────────────────────────────────────  │ │  row padding .6rem
│ │ [security] Story title two…………………………………………  ★ │ │
│ │ … (3 more)                                                     │ │
│ └────────────────────────────────────────────────────────────────┘ │  margin-bottom 2rem
│                                                                    │
│ 📰 Read      (Briefings) (Saved) (Notes)                           │  hub: 4 rows,
│ 🛠️ Build     (Projects) (Hermes) (Inbox)                           │  label + chips,
│ 📡 Monitor   (Status) (Tunnel) (Logs)                              │  row gap .5rem
│ ⚙️ Maintain  (Cron) (Disk) (Models) (Tuning) (LLM Lab) (Runbooks)  │  margin-bottom 2rem
│                                                                    │
│ ▸ 📊 System Overview                                               │  <details>, closed
│   (open: 3 summary cards — OpenRouter / GitHub / Tunnel;           │
│    then Services grid; then Quick Links)                           │
│                                                                    │
│ devmclovin.com · Home · Briefings · Projects · Status              │  footer, muted
└────────────────────────────────────────────────────────────────────┘
```

- **Grouping:** exactly 4 primary blocks (strip, briefing, hub, overview). Nothing else on the page.
- **Spacing:** 1.5rem between strip and briefing; 2rem between remaining blocks; section title rows use flex space-between (title left, action link right).
- **Empty states:** briefing block shows the ☕ empty message in place of the list; status strip never disappears (shows amber "Status API unavailable" on fetch failure); hub is static (no empty state); overview cards show "Not configured" text per card.
- **Mobile (≤480px):** page-head stacks (date under title, 0.8rem muted); status strip keeps one line, pills hidden, only the dot+text; briefing rows unchanged; hub chips wrap, label on its own line; overview cards single column. No horizontal scrolling anywhere.
- **Tablet (481–900px):** container padding 1rem; everything else flows naturally.

---

# 9. Style Direction

Keep the existing GitHub-dark identity. Do not introduce new colors, fonts, or a design system.

- **Palette (existing tokens, keep):** `--bg:#0d1117`, card `#161b22`, `--border:#30363d`, text `#e6edf3`, muted `#8b949e` (bump lowest-contrast usages to `#9aa4b2`), accent `#7c3aed` (purple), green `#3fb950`, red `#f85149`, amber `#d29922`.
- **Typography:** system stack (`-apple-system, 'Segoe UI', sans-serif`, as-is). h1 1.5rem/700 (page-head), section titles 1.05rem/600, body 0.9rem/400, hints & timestamps 0.78rem muted. One h1 per page.
- **Cards:** `#161b22` bg, 1px `--border`, radius 10px, padding 1rem. Hover on *linked* cards only: border-color `--accent`, no transform/shadow theatrics.
- **Buttons:** primary = accent bg, white text, radius 8px, padding .5rem 1rem. Destructive = red border + red text, ghost bg, always behind the existing `showConfirmDialog`. Chips (hub) = `--bg-card` bg, border, radius 999px, 0.8rem.
- **Icons:** emoji only (current convention) — no icon library.
- **Spacing scale:** 0.25 / 0.5 / 0.75 / 1 / 1.5 / 2rem. Nothing off-scale.
- **Hover/focus:** links get accent color on hover; every interactive element keeps `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }` (already in BASE_CSS — extend to the 5 template pages).
- **Mobile rules:** single column under 480px, container padding 0.75rem, tap targets ≥40px, no fixed-position elements except the sticky nav.

---

# 10. Acceptance Checklist

Homepage
1. [ ] `/` contains no `.hero` element and no tagline paragraph.
2. [ ] Status strip is the first element after the page-head.
3. [ ] Status strip auto-expands when ≥1 service is unhealthy (simulate: stop a monitored service or mock `/api/status`).
4. [ ] Status strip shows amber "Status API unavailable" state when api_server (9091) is down — not a blank area.
5. [ ] Today's Briefing renders as a vertical list; no horizontal scrollbar, no `.scroll-arrow` in DOM.
6. [ ] Briefing empty state (☕ message) renders when DB has no entries for any date.
7. [ ] Hub shows exactly 4 group rows; every chip navigates correctly.
8. [ ] System Overview is collapsed by default and contains exactly 3 summary cards (no Backups).
9. [ ] Every visible card/section on `/` has a clear purpose and live (non-fake) data.

Navigation
10. [ ] Nav is byte-identical (same HTML) on all 15 pages, including `/notes`, `/inbox`, `/models`, `/model-tuning`, `/llm-lab`, `/status`.
11. [ ] Nav order: Home, Briefings, Projects, Status, Hermes, Tools▾, SSH.
12. [ ] Tools dropdown contains exactly: Notes, Inbox, Runbooks, Cron, Models, Tuning, LLM Lab, Disk, Tunnel, Logs (with Daily/Ops group labels).
13. [ ] No dead nav links: every href in nav + footer + hub returns 200.
14. [ ] Active page is highlighted in nav on every page, including dropdown pages (summary highlights).
15. [ ] Footer nav matches top nav destinations.

Removals
16. [ ] `/kanban` GET returns 404 and `kanban_template.html` is deleted; the `/kanban/*` POST routes remain unless M0 proved them unused (branch taken is documented in the report).
17. [ ] `/models.js` returns 404; file deleted.
18. [ ] `server_health.py`, `launcher_card.py`, `backups_api.py`, `backup_panel.py`, `ollama_api.py`, `ollama_api_server.py`, `inbox_server.py`, `runbook_server.py`, `notes_proxy.py`, one-off scripts — all absent from repo.
19. [ ] `grep -ri backup server.py` returns no functional code.
20. [ ] `/status` keeps its full existing feature set (`status-board.html` retained) with the shared injected nav.
21. [ ] No unused CSS: `.briefing-scroll`, `.scroll-arrow`, `.cc-card`, kanban/backup CSS blocks absent from BASE_CSS.

Fixes
22. [ ] `/inbox` loads and lists items from a machine that is not the server (no requests to 127.0.0.1 in DevTools).
23. [ ] Inbox shows an explicit error banner when its upstream API is down.
24. [ ] `POST /api/inbox/items` (per docs/agent-inbox-guide.md) creates an item end-to-end.
25. [ ] `/hermes` contains no `localhost` hrefs.
26. [ ] `/cron/<garbage>` returns HTTP 404.
27. [ ] Server boots and serves `/` when `~/projects/model-price-comparison` is missing; `/models` shows a friendly unavailable state.
28. [ ] `/logs?tab=router` shows router journal; `/logs/router` 301-redirects to it.
29. [ ] No hardcoded `notes-secret-token` / `hermes-commands-api-key-change-me` literals except as documented env fallbacks.
30. [ ] Repo `devmclovin-landing.service` matches the actually-deployed unit (user + path).

Quality
31. [ ] All 15 content routes + `/health` return 200 on the dev port (route matrix in §7 M6).
32. [ ] `python3 test_runbooks.py` passes.
33. [ ] `py_compile` passes on every remaining `.py` file.
34. [ ] At 375px width: no horizontal page scroll on `/`, `/briefings`, `/status`, `/notes`, `/inbox`.
35. [ ] Tab key traverses homepage with visible focus rings on every interactive element; skip-link appears on first Tab.
36. [ ] No `alert()` in server.py-generated UI (template apps out of scope).
37. [ ] Primary actions (open briefings, check status, restart a service, open any tool) are reachable within one click of `/` (two for dropdown tools).
38. [ ] README.md exists with a correct ports/services table.

---

# 11. Cheap Coding Agent Prompt

Copy-paste everything below the line to the implementation agent.

---

You are executing a pre-approved redesign of the devmclovin.com dashboard repo. Every design decision is already made in `specs/landing-page-redesign-plan.md`. You execute; you do not design.

HARD RULES
1. Read `specs/landing-page-redesign-plan.md` in full first. When this prompt and the spec conflict, the spec wins.
2. You must be on the production box: run §7 Milestone 0 (environment preflight) before any code change and save its outputs. If any M0 command fails or you are not on the box, STOP and report. Do not stub, mock, or fake dependencies to make `server.py` boot elsewhere.
3. Milestones in order: M0 → M1 → M2 → M3 → M4 → M5 → M6. One or more commits per milestone, message `M<n>: <summary>`, on branch `redesign/landing-2026-07`.
4. Scope is closed. No new colors, fonts, libraries, frameworks, build steps, files, or features beyond the spec. Inside the six template HTML files you may change ONLY what NavInjection and §6 item #1 specify. If something is unspecified, make the smallest change consistent with existing style — or skip it and note it.
5. Deletions follow the two-tier rule in M1 exactly. Never leave a systemd unit pointing at a deleted file; when in doubt, `git mv` to `attic/` and report.
6. Every `VERIFY` in the spec is a precondition: check it, record the evidence, and if it fails, STOP that task (not the whole run), record what you found, and continue with independent tasks.
7. Forbidden: Cloudflare/DNS/tunnel config, live systemd units, `.env` secret values, anything under `~/.hermes` or `~/.devmclovin` data, `runbook_data.py` content, `api_server.py`, `notes_api_server.py`, notes port wiring, and the internals of the router service beyond the single nav-link fix.
8. After every `server.py` edit: `python3 -c "import py_compile; py_compile.compile('server.py', doraise=True)"`. All testing on a dev instance `python3 server.py 3102` — never the live 3002 service. Before finishing: `python3 test_runbooks.py` + the §7 M6 route matrix + the §10 checklist.

FINAL REPORT (required): (a) M0 outputs verbatim; (b) files created/changed/deleted/attic'd; (c) each §10 item pass/fail with the command and output used; (d) every VERIFY outcome and every task you stopped; (e) remaining manual steps for the human (add `NOTES_API_TOKEN` and `COMMANDS_API_KEY` to the env file M0 located; restart `devmclovin-landing.service`).
