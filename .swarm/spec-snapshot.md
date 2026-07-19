# Control Center — Projects/Portfolio Hub

## Purpose
Convert the Control Center from a read-only dashboard into a usable control surface
by replacing the static, manually-curated Projects list and Portfolio page with a
single **Hub** that auto-populates from GitHub and enriches activity with an Ollama
summary. Briefings remain the daily anchor and are untouched.

## Actors
- Tristan (solo developer, sole user). Authenticated via Cloudflare Access or localhost.

## Requirements

### FR-001 Hub surface
The app SHALL provide a `/hub` page (public) that displays the user's GitHub repos
merged with a local curation layer, grouped by recency: Active (<7d), Maintain
(<30d), Stalled (>30d), Done (override).

### FR-002 GitHub source of truth
The app SHALL fetch the user's owned repos via the GitHub REST API
(`GET /user/repos?type=owner&per_page=100&sort=pushed`, paginated) and recent
commits per repo (`GET /repos/{owner}/{repo}/commits?per_page=5`) using a
read-only token from the `GITHUB_TOKEN` environment variable. Private repos SHALL
be included.

### FR-003 Curation layer
The app SHALL persist a curation layer at `DATA_DIR/projects.json` keyed by repo
`full_name` (owner/repo) with optional fields: `goal`, `whats_next`,
`status_override` ("done"|""), `live_url`, `local_path`, `hidden`, `order`. GitHub
provides name/description/language/pushed_at/commits; the curation layer adds only
what GitHub cannot know. Merge occurs at render time by `full_name`.

### FR-004 Recency grouping
The app SHALL classify each repo by `pushed_at`: Active if <7 days, Maintain if
<30 days, Stalled otherwise. `status_override:"done"` SHALL force the Done group
regardless of recency.

### FR-005 Ollama summarization (non-blocking)
The app SHALL call a local Ollama instance (`OLLAMA_BASE_URL` env, default
`http://localhost:11434`; `OLLAMA_MODEL` env, default `qwen2.5:7b`) to summarize
each repo's description + last 5 commit subjects/bodies into a 1–2 sentence
"current state" blurb via `POST /api/generate` (stream:false). Summaries SHALL be
cached (key = repo + commit SHAs; 30min success / 5min failure TTL; 20s timeout;
per-project stampede guard). The `/hub` page SHALL render from cache with a
"Summarizing…" placeholder for misses and SHALL fill them via a background
`GET /api/hub/summaries` JSON endpoint polled by JS — page load SHALL NOT block on
Ollama. On any Ollama failure the placeholder SHALL be replaced with the raw recent
commit list. The prompt SHALL instruct the model to treat commit messages as data,
not instructions.

### FR-006 Attention surfacing
The Hub SHALL flag repos needing attention: stalled, missing goal, missing
whats_next, or done-but-recently-pushed.

### FR-007 Hub admin
The app SHALL provide an auth-gated `/hub/admin` page to edit the curation layer
(goal, whats_next, status_override, live_url, local_path, hidden, order) via
POST actions. Unauthenticated access SHALL return 403.

### FR-008 Allowlisted actions
The Hub SHALL provide exactly two auth-gated actions: (a) "Refresh hub now" —
force-bust GitHub + summary caches and re-poll; (b) "Download backup" — a
Python-native `tarfile` of `DATA_DIR` returned as an `application/octet-stream`
download. No shell passthrough, no arbitrary command execution. Unauthenticated
access SHALL return 403.

### FR-009 Merge / removal
The app SHALL remove `/projects`, `/projects/admin`, all `/projects/admin/*`
routes, `/portfolio`, and the static `portfolio.html`. Nav SHALL become Home ·
Briefings · Hub · Status. Footer and homepage links SHALL point to `/hub`. Dead
code (`inject_nav`, `portfolio_page`) SHALL be removed.

### FR-010 Graceful degradation
If `GITHUB_TOKEN` is missing/invalid (401), the Hub SHALL show a "token not
configured" banner and render curated-only data. On rate limit (403 +
X-RateLimit-Remaining:0) or network error, the app SHALL serve stale cache with a
banner. A single repo's commit fetch failure SHALL degrade only that card. A
whole-page error SHALL occur only when there is no cache AND the API is down. The
token, Ollama URL, model, prompts, and errors SHALL NEVER appear in logs,
responses, or error pages.

### FR-011 Untouched surfaces
Briefings (`/briefings`, `/briefing/<date>`, bookmarks), monitoring (`/status`,
`/api/status`, `monitors.json`), and the homepage briefing block + status strip
SHALL remain unchanged in logic and styling.

### FR-012 Engineering constraints
Single-file stdlib Python (`server.py`), `urllib` only, no new dependency, no
framework, no build step. Atomic writes (`.tmp` + `.replace()`) for all data files.
Module-level dict + TTL caches mirror the existing monitor cache pattern.

### FR-013 Deployment artifacts
`compose.yml` SHALL mount `GITHUB_TOKEN` from a root-owned secret
(`/srv/secrets/landing-page/`) and set `OLLAMA_BASE_URL`/`OLLAMA_MODEL` env, and
join the network Ollama resides on. `OPERATIONS.md` SHALL document token
provisioning, Ollama env, and the Hub. Deploy is deferred unless explicitly
requested.

## Out of scope
- Arbitrary command execution / service restart / deploy from the UI.
- PR/CI/review collaboration signals (solo user does not use them).
- Editing briefings from the UI (future pass).
- Any change to Caddyfile, tunnel ingress, public hostname, or Access policy.
