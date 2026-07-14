# PLAN.md — Control Center overhaul

Plan pass output per AGENTS.md. Single source of truth for the build pass.
Build on branch `overhaul/control-center`; merge to `main` and deploy at step 19.
One step = one commit (`step N: <description>`); verify, tick, commit, continue.

## Context

The site (single-file stdlib `server.py`, Docker container behind Caddy + Cloudflare
Tunnel) is being converted from the "devmclovin landing page" into **Control Center**:
briefings first, monitoring fixed, projects/portfolio manageable, everything else
deleted, zero "devmclovin" strings in the repo.

Diagnosis that drives this plan:

- **Monitoring root cause:** `/status` (status-board.html) and the homepage strip call
  `/api/status` → `_proxy_api()` → `http://127.0.0.1:9091` = `api_server.py`, which is
  **not running in the container** (Dockerfile CMD starts only server.py) and cannot
  work there (it shells out to host `systemctl`/`journalctl` and imports a
  health_check module from an unmounted `~/.hermes/kanban/...` path). Every monitoring
  call returns 502. This is a deployment-architecture mismatch, not a small config
  fix — the replacement is decision D2.
- **Projects root cause:** list = GitHub API (token in unmounted `~/.hermes/.env`) +
  overlay JSON in unmounted `~/.devmclovin/`; actions run `systemctl --user`. All
  three are impossible in the container. Rebuild per D3.
- **Briefings work today** via two read-only mounts (briefings.db + cron output .md
  dir) and must not regress (D6).
- **Portfolio works** (static generated file, auth-gated) and just shipped (D4).

## Decisions (ratified when you leave them in this file)

- **D1 — Hostname:** the public domain stays whatever DNS/tunnel serve today
  (devmclovin.com); the repo stops knowing it. `ALLOWED_HOSTS` / any absolute-URL
  need becomes env vars set in the server's gitignored `.env`
  (default `localhost,127.0.0.1`). Domain rename/migration is explicitly out of scope.
  ⚑ flagged per AGENTS.md hostname rule.
- **D2 — Monitoring architecture:** delete `api_server.py` + `status-board.html`.
  Replace with an in-process checker in server.py: reads `DATA_DIR/monitors.json`
  (list of `{name, url, timeout}` HTTP targets reachable from the container — self,
  `http://caddy:80`, other app containers on proxy_net, or public URLs), cached ~30s,
  served as `/api/status` JSON; `/status` becomes a simple server-rendered page from
  the same data plus a links section (uptime-kuma, dozzle at /srv/infra/monitoring —
  URLs come from monitors.json `link` entries, not hardcoded). Justification: the old
  backend can never run in this container; existing infra monitoring is linked, not
  duplicated; stdlib-only, no new service. Lost with status-board.html: service
  restart/log/config UI — it never worked from the container (all 502s today).
- **D3 — Projects storage/UI:** flat file `DATA_DIR/projects.json` (fields: name,
  description, url, repo_url, status, order, hidden). `/projects` renders visible
  entries by order; `/projects/admin` (auth-gated via existing `is_authenticated()`)
  is a server-rendered form UI: add / edit / delete / hide / move up/down, POSTing to
  auth-gated endpoints. No GitHub API, no systemctl, no JS framework. Flat file
  chosen over SQLite: single-writer, tiny, diffable, trivially backed up.
- **D4 — Portfolio:** keep the shipped pipeline unchanged (generated
  `portfolio.html` from STATE.md files via `Skills/dashboard.py`, published by
  `publish-dashboard.bat`, served auth-gated at `/portfolio`). Management flow =
  edit STATE.md + run the bat — no code edits, satisfies the directive. Not merged
  into the projects UI. One-line external touch: fix the generated `<title>` to
  `Portfolio — Control Center` in `Skills/dashboard.py` (outside this repo).
- **D5 — Bookmarks fold into /briefings (no separate page):** delete the
  `/bookmarks` page. Each article on `/briefings` / `/briefing/<date>` keeps a star
  toggle; a **Saved** chip joins the existing category filter row, and `/briefings`
  gains a sort control (newest / oldest / saved first). Storage:
  `DATA_DIR/bookmarks.json` (new rw `data/` mount). No migration needed — the
  container never had the old file, so live bookmarks are empty today.
- **D6 — Briefings data flow is untouchable:** homepage keeps its current md-file +
  DB fallback chain; archive/detail/search keep reading the DB; both ro mounts stay
  in compose. Restyle only.
- **D7 — Runbooks: REMOVE** (`runbook_data.py`, `test_runbooks.py`, `/runbooks`).
  Content is stale bare-metal fixes full of devmclovin paths. Delete this line and
  flip the audit row to KEEP if you disagree.
- **D8 — SSH button: REMOVE** entirely (nav button, `ssh.` hostname references, no
  env fallback).
- **D9 — Networks:** stay on `proxy_net` only. No sidecars/DB exist, so the
  template's `app_net` adds nothing; add it if a sidecar ever appears.
  ⚑ deviation from the two-network template, flagged.
- **D10 — Compose location:** `compose.yml` and `.env` stay in
  `/srv/apps/landing-page/repo/` (current working setup; the deploy command and
  `publish-dashboard.bat` hardcode it). `data/` is created at
  `/srv/apps/landing-page/data/` per the template and bind-mounted into the
  container. ⚑ deviation from template (compose.yaml/.env at app root), flagged.
- **D11 — Docs/process files count for the grep:** AGENTS.md's own "formerly
  devmclovin" mentions and STATE.md's goal line get reworded so
  `git grep -ri devmclovin` over tracked files returns nothing. Gitignored local
  files (docs/deploy-facts.md etc.) are exempt and untouched.
- **D12 — Single-file stdlib pattern stays.** No framework, no build step, no new
  dependency anywhere in this plan.
- **D13 — Branch strategy:** all steps on `overhaul/control-center`; merge to main +
  single deploy at step 19 so the live site never runs a half-overhauled build.
- **D14 — Briefings search: REMOVE** (`/api/briefings/search` + the search box on
  `/briefings`). Daily reading doesn't need full-text search over the archive; the
  date list + category/Saved filters cover navigation. Flip this row in the audit if
  you actually use search.
- **D15 — Aggressive-cut baseline:** the finished site is exactly six surfaces —
  `/` (briefing + status + two links), `/briefings` (+ `/briefing/<date>`),
  `/status`, `/projects` (+ admin), `/portfolio`, `/health` — and nothing else. Any
  feature not on this list that surfaces during the build gets deleted, not ported.
- **D16 — Full visual redesign (production feel, not flat):** BASE_CSS is replaced
  wholesale with a small token-based design system, still inline CSS in server.py —
  no build step, no framework, no CDN/external fonts (self-contained pages).
  Concretely: layered dark surface palette (page / raised card / overlay elevations
  instead of one flat background), accent + semantic status colors as CSS variables;
  a real type scale (display/heading/body/caption) on a tuned system-font stack;
  spacing/radius/shadow tokens; sticky translucent nav with backdrop-blur and
  active-link underline; cards with subtle borders, elevation shadows, and gentle
  hover lift; pill badges, proper button states, visible focus rings; 150–200ms
  micro-transitions; consistent max-width shell; designed (not bare-text) empty
  states. Same system applied to every server-rendered page. `portfolio.html` gets
  a matching restyle via the CSS in `Skills/dashboard.py` (external touch, flagged —
  skip if you'd rather leave the generator alone and accept a style mismatch).

## Keep / Remove audit

Verdicts: KEEP (unchanged/restyle), FIX (keep, repair), REBUILD, REMOVE (delete code).

| Route / feature / file | What it is | Verdict |
|---|---|---|
| `/` homepage | entry point | REBUILD — briefings-first layout (step 13) |
| `/briefings`, `/briefing/<date>` | briefing archive (DB) | KEEP — protect; add Saved filter + sort (D5); restyle |
| `/api/briefings/search` + search box | full-text archive search | REMOVE (D14) |
| `/bookmarks` page | separate saved-stories page | REMOVE — folded into `/briefings` as Saved filter (D5); star-toggle POST kept, storage → `data/` |
| homepage briefing block | newest cron .md files | KEEP logic, restyle (D6) |
| `briefing_archive.py` (repo root) | DB reader | KEEP |
| `/status` + `status-board.html` + `api_server.py` | service health board | REBUILD per D2 (html + api_server.py deleted) |
| `/api/status`, `/api/service/*` proxies (`_proxy_api`) | proxy → dead :9091 | REMOVE — replaced by in-process `/api/status` |
| `/projects`, `/projects/<n>/logs`, `/projects/<n>/config`, `/api/projects/*` | GitHub+systemctl launcher | REBUILD per D3 (old code deleted) |
| `/portfolio` + `portfolio.html` | generated dashboard | KEEP (D4) |
| `/health` | uptime probe | KEEP |
| nav / `html_page` / `inject_nav` / `BASE_CSS` / `is_authenticated` | shared shell + auth | KEEP — trim to surviving pages |
| `Dockerfile`, `compose.yml` | container build/run | KEEP — update (data/ mount, env) |
| `/hermes` + `/api/proxy/commands/*` | Hermes admin + commands proxy | REMOVE (Hermes has its own UI) |
| `/cron`, `/cron/<id>`, `/cron/<id>/<file>` | cron dashboard (unmounted jobs.json) | REMOVE |
| `/notes`, `/api/notes/*`, `notes.html`, `notes_api_server.py` | notes app + proxy + backend | REMOVE |
| `/inbox`, `/api/inbox/*`, `inbox.html`, `docs/agent-inbox-guide.md` | agent inbox | REMOVE |
| `/models`, `/api/models`, `model_comparison.html` | LLM playground (auth) | REMOVE |
| `/model-tuning`, `/api/tuning/*`, `model_tuning.html` | SFT tuning UI | REMOVE |
| `/llm-lab` placeholder, `/api/llm-lab/*`, `llm_lab.html` | LLM lab (orphaned template) | REMOVE |
| `/api/ollama/*`, `/api/gguf/*` | local-model APIs | REMOVE |
| kanban POST routes + helpers + KANBAN_DB | headless kanban API (no GET page) | REMOVE |
| `/tunnel` + `cloudflare_api.py` | CF tunnel monitor (module not even imported) | REMOVE |
| `/logs`, `/logs/router` | journalctl viewers (impossible in container) | REMOVE |
| `/disk-cleanup` | host disk ops page | REMOVE |
| `/runbooks`, `runbook_data.py`, `test_runbooks.py` | copy-paste fixes | REMOVE (D7) |
| homepage hub chips, System Overview (OpenRouter/GitHub/tunnel cards), quicklinks | secondary homepage blocks (mostly dead data) | REMOVE |
| `get_system_status()` + GitHub/OpenRouter/link-health caches | dead/orphaned helpers | REMOVE |
| `router_server.py`, `router_metrics.py`, `router-dashboard.html`, `router-dashboard.service`, `e2e_integration_test.py` | standalone router dashboard (LLM-Router project's concern) | REMOVE |
| `attic/` (6 parked servers + README) | superseded standalone servers; old systemd unit confirmed gone 2026-07-11 | REMOVE |
| `devmclovin-landing.service`, `setup.sh` | deprecated bare-metal deploy | REMOVE |
| `docs/landing-page-redesign-plan.md`, `docs/archive/*`, `docs/portfolio-deploy-prompts.md` | superseded plans/notes (devmclovin-heavy) | REMOVE |
| `docs/deploy-facts.md`, `docs/server-remediation-*.md` | gitignored local memory | KEEP (untracked, untouched) |
| `README.md` | stale (systemd-era) | REBUILD — short intro pointing at OPERATIONS.md |
| `AGENTS.md`, `PROMPTS.md`, `STATE.md` | process files | KEEP — reword devmclovin mentions (D11) |
| SSH nav button | jump to web SSH | REMOVE (D8) |

## Build steps

- [x] **Step 1 — Branch + smoke harness.** Create `overhaul/control-center`. Add
  `scripts/smoke.py`: stdlib script that hits a route matrix on a given port,
  asserts expected status codes, and fails if any 200 body contains `devmclovin`
  (case-insensitive). Baseline it against current routes (expected values updated
  per step as routes are removed). Local runs always `PYTHONUTF8=1 python server.py 3102`.
- [x] **Step 2 — Delete deploy relics + attic.** `git rm` `attic/` (all),
  `devmclovin-landing.service`, `setup.sh`, `router-dashboard.service`.
- [x] **Step 3 — Delete router dashboard.** `router_server.py`, `router_metrics.py`,
  `router-dashboard.html`, `e2e_integration_test.py`.
- [x] **Step 4 — Delete notes.** `notes.html`, `notes_api_server.py`; `/notes` route,
  `notes_page()`, `_proxy_notes` + all `/api/notes*` dispatch (GET/POST/PATCH/DELETE/
  OPTIONS) and `NOTES_API_TOKEN` handling. Verify: boot + smoke; `/notes` → 404.
- [x] **Step 5 — Delete inbox.** `inbox.html`, `docs/agent-inbox-guide.md`; `/inbox`,
  `inbox_page()`, `_proxy_inbox` + `/api/inbox*` dispatch.
- [x] **Step 6 — Delete LLM tooling.** `llm_lab.html`, `model_comparison.html`,
  `model_tuning.html`; routes `/models`, `/api/models`, `/model-tuning`, `/llm-lab`;
  all `/api/llm-lab/*`, `/api/tuning/*`, `/api/ollama/*`, `/api/gguf/*` handlers and
  their `_llm_lab_*`, `_tuning_*`, `_ollama_*`, `_gguf_*`, `_hf_*`, `fetch_models`
  helpers, constants (MODELS_DB, TUNING_DIR, LLM_LAB_DIR, HF_DOWNLOAD_DIR) and
  OPTIONS/CORS entries. Biggest single shrink of server.py.
- [x] **Step 7 — Delete kanban, hermes, cron.** Kanban POST routes + `kanban_*`
  helpers + KANBAN_DB; `/hermes` + `hermes_page()` + `/api/proxy/commands/*` proxy +
  `COMMANDS_API_KEY`; `/cron*` routes + pages + CRON_* constants.
- [x] **Step 8 — Delete tunnel, logs, disk-cleanup, runbooks, dead helpers.**
  `/tunnel` + `cloudflare_tunnel_page` + `cloudflare_api.py`; `/logs` + `/logs/router`;
  `/disk-cleanup`; `/runbooks` + `runbook_data.py` + `test_runbooks.py`;
  quicklinks/System-Overview helpers (`system_summary_row`, OpenRouter/GitHub caches,
  `_load_openrouter_key`, link-health, `get_system_status`). Verify smoke: removed
  routes 404; `/`, `/briefings`, `/status`, `/projects`, `/portfolio`, `/health` still 200.
- [x] **Step 9 — DATA_DIR + bookmarks-in-briefings (D5, D14).** New env `DATA_DIR`
  (default `<repo>/data` locally, `/app/data` in container); `data/` gitignored.
  Bookmarks storage → `DATA_DIR/bookmarks.json`. Delete the `/bookmarks` page and
  `/api/briefings/search` + the search box; add to `/briefings`: a **Saved** chip in
  the filter row, star toggle per article (existing POST endpoint, repointed), and a
  sort control (newest / oldest / saved first). Verify: toggle round-trip locally,
  Saved filter shows only starred articles, `/bookmarks` → 404.
- [x] **Step 10 — Monitoring rebuild (D2).** In server.py: loader for
  `DATA_DIR/monitors.json` (`checks: [{name,url,timeout}]`, `links: [{name,url}]`),
  concurrent-ish sequential HTTP HEAD/GET checks with ~30s cache; `/api/status`
  returns `{status, checks:[{name, healthy, latency_ms, error}]}`; `/status` =
  server-rendered board from the same data + links section; delete
  `status-board.html`, `api_server.py`, `_proxy_api`, `/api/service/*`. Error states:
  missing/invalid monitors.json → page says "No monitors configured" (not a crash).
  Verify: local run with a test monitors.json (one good URL, one bad) shows
  correct up/down; `/api/status` JSON matches.
- [ ] **Step 11 — Projects rebuild (D3).** `DATA_DIR/projects.json` CRUD helpers with
  atomic write (temp+rename); `/projects` public list (order-sorted, hidden filtered,
  empty state "No projects yet — add one in admin"); `/projects/admin` +
  POST `/projects/admin/{add,update,delete,move,toggle-hide}` all gated by
  `is_authenticated()`. Delete old projects/launcher/GitHub/systemctl code and
  `/projects/<n>/logs|config` routes. Verify: full CRUD + reorder + hide via curl
  locally; unauthenticated non-localhost POST → 403 (simulate by binding non-loopback).
- [ ] **Step 12 — Design system + nav/shell (D16).** Replace BASE_CSS/NAV_CSS with
  the token-based design system (palette, type scale, spacing/radius/shadow tokens,
  card/badge/button/form components, transitions, focus states). `render_nav()` →
  Home · Briefings · Projects · Portfolio · Status as a sticky translucent bar with
  active underline; no SSH button (D8), no Tools dropdown. `html_page()` shell:
  max-width container, title suffix `— Control Center`; `_UNAUTH_PAGE` rebranded
  and restyled. `inject_nav` now serves only `portfolio.html`. Verify: nav identical
  on all pages; pages read as layered/elevated, not flat.
- [ ] **Step 13 — Homepage redesign (readability first).** Order: header row
  (`Control Center` + date) → today's briefing → status strip (new `/api/status`,
  auto-expand on issues, amber "unavailable" state) → one compact link row
  (Projects · Portfolio). Briefing block requirements (fixes "hard to read /
  summary cut off"): existing data logic per D6; vertical list, one story per row;
  **full summary text always visible — no truncation, no ellipsis, no fixed-height
  clipping, no horizontal scroll**; body ≥0.95rem with relaxed line-height; clear
  title/summary contrast; category badge inline with title. Delete
  hub/System-Overview markup. No filler, no placeholders.
  Then an **interior-pages polish pass** with the same system: `/briefings` +
  `/briefing/<date>` (filter/sort chips, story cards), `/status` (status board,
  health pills), `/projects` + admin (cards, forms, buttons), restyled
  `portfolio.html` via `Skills/dashboard.py` (D16 external touch) + regenerate.
  Verify visually at 1280 / 768 / 375 px: no horizontal scroll, consistent look on
  every page, feels production-grade rather than flat.
- [ ] **Step 14 — Rebrand sweep in code.** Remaining titles/h1/footers/UA strings/
  startup banner/comments → Control Center; `ALLOWED_HOSTS` → env with
  `localhost,127.0.0.1` default (D1). `git grep -i devmclovin -- '*.py' '*.html'`
  → 0.
- [ ] **Step 15 — Docs + generated portfolio.** Reword AGENTS.md/STATE.md mentions
  (D11); delete superseded docs (audit table); fix `Portfolio — Control Center`
  title in `Skills/dashboard.py` (external, one line) and regenerate
  `portfolio.html` (`python ..\Skills\dashboard.py --site`); if another project's
  STATE.md leaks "devmclovin" into a card, reword that goal line too and regenerate.
  Verify: `git grep -ri devmclovin` → 0 tracked hits.
- [ ] **Step 16 — CSS purge.** Delete a CSS block from BASE_CSS/NAV_CSS only if its
  class greps to zero across `server.py` + `portfolio.html`. Target: kanban, hub,
  overview, scroll-arrow, tool-page leftovers.
- [ ] **Step 17 — compose + OPERATIONS.md + backup.** compose.yml: add
  `../data:/app/data` rw bind + `DATA_DIR=/app/data`, pass `ALLOWED_HOSTS`
  from `.env`, keep both briefing ro mounts, hardening, healthcheck. Add
  `scripts/export-data.sh` (tar `data/` → `/srv/backups/exports/landing-page/`).
  Write `OPERATIONS.md` (build/run/deploy/logs/data/secrets/smoke); rewrite
  `README.md` as a short intro pointing to it.
- [ ] **Step 18 — Full local verification.** `py_compile` all remaining .py;
  `PYTHONUTF8=1 python server.py 3102`; `python scripts/smoke.py 3102` green
  (matrix: `/ /briefings /briefing/<latest|any> /status /projects
  /projects/admin /portfolio /health /api/status` → 200;
  `/notes /inbox /models /llm-lab /hermes /cron /tunnel /logs /disk-cleanup
  /runbooks /kanban /bookmarks /api/briefings/search /models.js` → 404). Desktop has no briefing data → all pages
  must render graceful empty states, not tracebacks.
- [ ] **Step 19 — Merge + deploy + server prep.** Merge to main, push. Over
  `ssh server`: `mkdir -p /srv/apps/landing-page/data` chowned to uid 10001; seed
  `data/monitors.json` (self, `http://caddy:80`, uptime-kuma/dozzle links) and empty
  `data/projects.json`; write `repo/.env` (`ALLOWED_HOSTS=<current domain>,localhost,127.0.0.1`);
  then `cd /srv/apps/landing-page/repo && git pull
  --ff-only && docker compose up -d --build landing-page`. Verify: container Up
  (healthy); in-container 200s on the matrix; external `https://<domain>/` → 302 to
  Access (no leak); monitors show live correct data.
- [ ] **Step 20 — Definition-of-done audit.** Walk AGENTS.md's checklist: grep zero;
  compose-up reachable via Caddy; briefings primary; monitoring live+correct;
  projects/portfolio manageable without code edits; removed code actually gone;
  OPERATIONS.md matches reality. Record results here; update STATE.md log; ask
  Tristan for the authenticated phone/desktop visual check (only human-owned item).

## Verification summary

- Per-step: boot on 3102 (`PYTHONUTF8=1`) + `scripts/smoke.py` + `py_compile`.
- End-to-end: step 18 matrix locally, step 19 in-container + external Access check.
- Regression tripwire for briefings: `/briefings` and `/briefing/<date>` byte-diff
  of article content before/after (only shell/nav/CSS may change).

## Decision log

(build pass appends one line per mid-run decision)
- Step 10: switched the stdlib listener to `ThreadingHTTPServer` so configured self-checks can call `/health` without deadlocking the request handling them.
