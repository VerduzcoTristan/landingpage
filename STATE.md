# STATE — landingpage
**Phase:** maintain
**Goal:** Control Center serves daily briefings, live monitoring, and a GitHub-sourced, Ollama-summarized project Hub behind Cloudflare Access
**Current milestone:** Hub overhaul completed and verified locally on 2026-07-19; production remains unchanged pending an explicit deploy
**Next action:** When deployment is approved, provision the GitHub secret, connect Ollama to `proxy_net`, deploy the reviewed commits, and validate through Cloudflare Access
**Blocked on:** nothing

## Log (newest first, one line per session)
- 2026-07-19: GitHub/Ollama Hub overhaul completed locally without deployment. GitHub repo/activity ingestion, non-blocking Ollama summaries with raw-commit fallback, curated-only fallback, auth-gated curation/refresh/backup, old-route removal, Compose secret/config wiring, and operations docs are complete; 126 unit tests, 27 live smoke checks, Compose validation, compilation, and the repo-wide legacy-brand scan pass.
- 2026-07-14: Control Center overhaul shipped. Briefings are primary (7 live homepage rows, 27 archive cards); Control Center and Caddy monitors are healthy; production project add/update/hide/delete passed with data restored; all 24 retained/removed route checks passed; Cloudflare Access returns 302 before content; operations, backup, Compose hardening, two-network wiring, and legacy-code deletion audited green.
- 2026-07-11: M1 SHIPPED. Milestone gate verified: unauthenticated /portfolio → 302 to Access login with zero content leak; authenticated view renders dashboard with nav and all cards. publish-dashboard.bat validated 3x (two real publishes d6886e9/26bfe3e + clean no-op). Ledger fully ticked in docs/deploy-facts.md.
- 2026-07-11: Prompts C+D done. C: /portfolio deployed (a32b603), container rebuilt, in-container 200/200. D: publish-dashboard.bat created in Skills and tested end to end (real commit d6886e9 + rebuild, then clean no-op run). Codex's STATE.md edit corrupted the file's encoding (cp1252 mojibake), which leaked into the live dashboard — fixed and republished by Claude review session. Remaining: phone gate.
- 2026-07-11: Prompts A+B done. A: server tree reconciled (c801a91), systemd unit gone, cloudflared→caddy:80→landing-page. B: /portfolio route+nav+gate written by Codex, stopped on cp1252/`→` banner crash — verified locally in planning session with PYTHONUTF8=1 (200, no placeholders). Stop rules loosened per feedback: agents resolve local obstacles themselves; STOP reserved for production-safety lines.
- 2026-07-10: Prompt sequence rewritten as v2 — 4 self-contained prompts (A reconcile, B route, C deploy, D publish.bat), zero fill-ins/pasting; persistent memory file docs/deploy-facts.md seeded with all verified facts (ssh alias `server`, BAKED, no host port, auth pattern). Auth question resolved without dashboard check: is_authenticated() just validates the Access header, so /portfolio gets the gate unconditionally.
- 2026-07-10: Prompt 4 attempted out of order — correctly stopped at step 1. Confirmed: Prompt 2 done (portfolio.html has all 3 placeholders once each), Prompt 3 NOT done (server.py has no /portfolio route), Prompt 1b diff still not captured. .claude/ scare was a false alarm (globally gitignored on desktop, Codex env lacks that config). Prompt 4 step 1 rewritten with an explicit file allowlist + "server.py must be modified" gate.
- 2026-07-10: Prompt 1 ran — verdict BAKED; stopped on dirty server tree (briefing_archive.py, compose.yml modified on-box; likely the live bind-mount config that never got committed). Desktop synced to 4df0883 (containerize commit was on GitHub). Discovered container has NO published host port (expose 3002 on proxy_net) — wrote Prompt 1b and fixed Prompt 4's curl steps to use docker exec.
- 2026-07-10: Codex prompt sequence written (docs/portfolio-deploy-prompts.md) — 5 prompts, M1 gate = phone verification behind Access.
- 2026-07-10: Chosen as active build project, displacing LLM-Router (explicit decision per operating manual §1). Confirmed templates are read per request — updates need no service restart.

## Parked (ideas/questions deliberately not being acted on)
- README deploy section is stale — describes bare systemd, production is a Docker container; rewrite after M1 from docs/deploy-facts.md
- Cloudflare Access path-scope check (Zero Trust dashboard) — no longer blocking since /portfolio is app-gated; still worth confirming someday
- BAKED confirmed → bind-mount portfolio.html so dashboard updates skip rebuilds (post-M1 improvement)
- M2: auto-publish on session end (Windows scheduled task: regenerate + push)
- Live server-side regeneration from git-pulled STATE.md files — probably never; desktop push is fine
- Hermes Kanban UI — separate project, needs spec (Plan item 6) first
