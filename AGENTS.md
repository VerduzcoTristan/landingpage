# AGENTS.md — Control Center (formerly "devmclovin landing page")

Instructions for any coding agent (Claude Code, Codex, or other) working in
this repo. Claude Code users: keep `CLAUDE.md` as a symlink to this file
(`ln -s AGENTS.md CLAUDE.md`).

## What this project is

A personal control center / dashboard for Tristan, served from his home server.
It is NOT a public marketing site. Optimize for daily personal utility, not
visitors. Primary daily use today: **briefings**. Secondary: **server
monitoring**. Aspirational: **projects and portfolio pages** (exist, unused,
need real management UI).

## Branding — hard rule

- Remove ALL references to "devmclovin" (page titles, headers, copy, meta tags,
  config values, service names, repo strings, comments).
- Site identity: "Tristan" for personal identity, "Control Center" for the app
  itself. Page title format: `Control Center` or `<Page> — Control Center`.
- If a domain/hostname string is needed and the value is uncertain, use a
  config variable, not a hardcoded name. Flag hostname decisions in the plan.

## Deployment target (this is the new setup — configure for it)

The old deployment (systemd unit `devmclovin-landing.service`, app in
`/home/ubuntu/devmclovin-landing/`, port 3002) is DEPRECATED. Do not extend it.

Target architecture:

- App lives at `/srv/apps/landing-page/` with `repo/`, `compose.yaml`, `.env`,
  `data/`. Follow the `/srv/apps/_template/` skeleton if present.
- Runs as its own Docker Compose stack. One stack per app — never merge into a
  shared compose file.
- **Two-network pattern**: `app_net` (internal, app ↔ its own services only)
  and `proxy_net` (shared external network Caddy uses to reach the app). The
  app container joins both; databases/sidecars join only `app_net`. No host
  port publishing — Caddy reaches the container over `proxy_net`.
- Ingress: Caddy (config at `/srv/infra/caddy/Caddyfile`), fronted by a
  Cloudflare Tunnel (`cloudflared` → `http://caddy:80`). Sites declared with
  explicit `http://` prefix in the Caddyfile (Option A: tunnel terminates TLS).
- Secrets: root-owned files under `/srv/secrets/landing-page/`, injected via
  env/secret mounts. Never commit secrets. `.env` is gitignored.
- Container runs as non-root with minimal privileges.
- Backups: anything worth persisting goes in `data/` and should be exportable
  to `/srv/backups/exports/landing-page/` (logical dump script if there's a DB).

## Feature directives

**Briefings (primary — protect and polish)**
- This is the page Tristan actually uses every day. It should be the homepage
  or one click from it. Nothing may regress here without explicit sign-off.

**Server monitoring (fix)**
- Currently broken. Diagnose root cause before rewriting — do not rip out and
  replace if a config/endpoint fix suffices. Note that infra monitoring
  (uptime-kuma + dozzle) exists at `/srv/infra/monitoring/`; prefer integrating
  with or linking to existing sources over duplicating collection logic.

**Projects & Portfolio (rebuild for actual use)**
- Pages exist but are unused because there are no management controls. Goal:
  Tristan can add/edit/reorder/hide entries from the UI (or a dead-simple
  admin flow) without editing code. Propose the simplest storage that supports
  this (flat files/SQLite preferred over anything heavier).

**Everything else (default: remove)**
- The site is full of unused features. Bias strongly toward deletion. In the
  audit/plan phase, list every route/feature/component with a keep/remove
  recommendation. Anything not serving briefings, monitoring, projects,
  portfolio, or core navigation defaults to REMOVE.

## Homepage layout

Current layout is poor. Redesign around actual usage frequency:
briefings first and largest, monitoring status at a glance, links to
projects/portfolio. No filler sections, no placeholder content, no
social-proof/marketing blocks.

## Engineering conventions

- Simplicity over cleverness. Prefer the smallest stack that does the job.
  Match existing patterns in the repo before introducing new dependencies.
- No new framework, database, or service without listing it as a decision in
  the plan with a one-line justification.
- Every feature ships with: acceptance criteria met, error states handled,
  and a smoke test (at minimum a script or documented curl checks).
- Keep an `OPERATIONS.md` current: how to build, run, deploy, check logs,
  and where data/secrets live.

## Workflow — plan first, then build

1. **Plan pass (no code changes):** read the codebase, produce a plan listing
   files touched, approach, every design decision with your recommendation,
   and the keep/remove audit table. Wait for approval.
2. **Build pass:** execute the approved plan. Do not stop to ask about
   implementation details.

## Decision boundaries

- For implementation details not covered by the spec or this file: choose the
  simplest option consistent with existing repo patterns, document the choice
  in your output, and keep going. Do not stop to ask.
- STOP and ask only for decisions affecting: architecture, data models,
  external interfaces (Caddyfile/tunnel routes, ports, hostnames), deleting
  user data, or anything touching `/srv/secrets` or `/srv/infra`.

## Definition of done (for the current overhaul)

- Zero occurrences of "devmclovin" anywhere in the repo (verify:
  `grep -ri devmclovin .` returns nothing).
- App builds and runs via `docker compose up -d` from
  `/srv/apps/landing-page/`, reachable through Caddy on `proxy_net`.
- Briefings page works and is the primary surface.
- Server monitoring displays live, correct data.
- Projects and portfolio entries are manageable from the UI without code edits.
- Removed features are actually deleted (code gone, not hidden).
- OPERATIONS.md updated to match reality.

## Session continuity — required

- The plan pass writes its output to `PLAN.md`: a checkbox list of build
  steps in execution order, plus the keep/remove audit table and all
  ratified decisions. PLAN.md is the single source of truth for progress.
- During the build pass: complete one step, verify it, check it off in
  PLAN.md, then `git commit` with message "step N: <description>". Never
  batch multiple steps into one commit.
- Record any mid-run decision you make in a `## Decision log` section at
  the bottom of PLAN.md (one line each).
- Assume you can be killed at any moment. The repo must always be in a
  state where a fresh agent with no chat history can resume by reading
  AGENTS.md + PLAN.md + `git log`.
- On starting any session: read AGENTS.md and PLAN.md, run `git status`
  and `git log --oneline -10`, and continue from the first unchecked step.
  If PLAN.md does not exist, the plan pass has not happened yet — do it.
  If there are uncommitted changes, reconcile them against the current
  step before proceeding.
