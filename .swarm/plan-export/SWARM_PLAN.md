<!--
AUTO-GENERATED EXPORT/CHECKPOINT SNAPSHOT — DO NOT EDIT
This file is NOT the live plan. It is a derived export artifact.
- .swarm/plan-ledger.jsonl is the authoritative source of plan state.
- .swarm/plan.json and .swarm/plan.md are derived projections.
Regenerated on: save_plan and phase_complete.
-->
# Projects/Portfolio Hub (GitHub-sourced, Ollama-summarized)
Swarm: default
Phase: 1 [PENDING] | Updated: 2026-07-17T18:01:52.855Z

---
## Phase 1: Hub Build [PENDING]
- [ ] 1.1: Scaffold Hub data + curation layer: HUB_FILE, load_hub/save_hub/update_hub with new curation schema keyed by full_name, legacy projects.json migration. Add functions only; do not remove old projects functions yet. [MEDIUM]
- [ ] 1.2: GitHub client (inline): fetch_all_repos (paginated /user/repos), fetch_recent_commits (per repo, 5 commits), classify_recency (H4), module cache _GH_CACHE (600s TTL, stale-while-revalidate). Token from GITHUB_TOKEN env, Authorization: token header. Never log/echo token. [LARGE]
- [ ] 1.3: Ollama client (inline) + lazy /api/hub/summaries endpoint: call_ollama_generate (POST /api/generate stream:false, 20s timeout), get_project_summary (cache keyed by repo+SHAs, 30min/5min TTL, stampede guard), build_summary_prompt (injection guard), format_commit. Plus GET /api/hub/summaries computing missing summaries without blocking /hub. [LARGE]
- [ ] 1.4: Hub render (/hub): hub_page() merges GitHub data with curation layer by full_name, groups Active/Maintain/Stalled/Done (H4), renders cards with name/description/language/relative last-push/Ollama summary(or fallback)/goal/whats_next/links/attention flags. Empty state when GitHub unavailable + no curation. [LARGE]
- [ ] 1.5: Hub admin (/hub/admin): hub_admin_page() auth-gated, lists repos with forms to edit goal/whats_next/status_override(done)/live_url/local_path/hidden/order, POSTing to update_hub actions. Reuse existing auth + form styling. [MEDIUM]
- [ ] 1.6: Hub Actions (allowlisted, H7): POST /hub/action/refresh (force-bust _GH_CACHE + summary cache) and POST /hub/action/backup (Python-native tarfile of DATA_DIR returned as octet-stream download). Both auth-gated. No shell passthrough. [MEDIUM]
- [ ] 1.7: Route wiring + nav + footer/homepage links + delete old surfaces + dead code: add /hub, /hub/admin, /hub/action/*, /api/hub/summaries; remove /projects, /projects/admin, /projects/admin/*, /portfolio. Update render_nav to Home/Briefings/Hub/Status. Fix footer in html_page() and homepage links in home_page() to /hub. Delete portfolio.html. Remove inject_nav() and portfolio_page(). [LARGE]
- [ ] 1.8: Smoke + compile + local verify: py_compile all .py; run server.py 3102 with GITHUB_TOKEN unset (banner) and set; update scripts/smoke.py matrix per H12 and run green. [MEDIUM]
- [ ] 1.9: Compose + OPERATIONS update (H9/H10): compose.yml add GITHUB_TOKEN secret mount + OLLAMA_BASE_URL/OLLAMA_MODEL env + network for Ollama. OPERATIONS.md document token provisioning + Ollama env + Hub. [MEDIUM]
- [ ] 1.10: Definition-of-done check + STATE.md log (no deploy): walk all DoD items; update STATE.md log. Do NOT deploy unless explicitly requested. [SMALL]
