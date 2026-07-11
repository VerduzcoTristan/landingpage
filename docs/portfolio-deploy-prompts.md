# /portfolio deploy — copy-paste prompt sequence (v2)

**Goal:** Portfolio dashboard live at `devmclovin.com/portfolio` behind Cloudflare Access, updated by one command from the desktop.

**How to run:** Prompts pasted **verbatim** into a **fresh Codex conversation** in the stated working directory. Nothing to fill in, nothing to paste between prompts. Persistent state lives in the project memory file `C:\Users\Admin\Desktop\Code\Active\landingpage\docs\deploy-facts.md` — every prompt reads it first and writes its results back before finishing, so each fresh conversation self-orients.

**Obstacle policy (applies to every prompt):** local/desktop obstacles — environment quirks, encoding errors, ports in use, tool differences, files in an unexpected-but-explainable state — are yours to diagnose and resolve; do so, keep going, and record what you found and did in the memory file. Reserve STOP for the short list each prompt names: those are production-safety lines (touching the server outside the allowed commands, weakening auth, resolving git conflicts on the production box), not puzzles to route around. When you resolve an obstacle, prefer changing your *procedure* (env vars, different invocation, different check) over changing project code beyond the task's scope.

Already done (recorded in the memory file): server verification (**BAKED**, no published host port, cloudflared → `http://caddy:80` → landing-page), server tree reconciled at `c801a91` (Prompt A ✅), `/portfolio` route implemented and locally verified (Prompt B ✅), deployed and verified in-container (Prompt C ✅, commit `a32b603`), publish loop built and tested (Prompt D ✅ — `publish-dashboard.bat` in Skills). **Next up: the manual milestone gate (phone check).**

---

## Prompt A — Reconcile server tree + finish verification ✅ DONE 2026-07-11

Completed — server reconciled at `c801a91`, systemd unit gone, cloudflared → caddy → landing-page, in-container HTTP 200/404. Kept for reference; do not re-run.

Fresh Codex conversation, working directory `C:\Users\Admin\Desktop\Code\Active\landingpage`. Needs shell + SSH.

```text
Working directory: C:\Users\Admin\Desktop\Code\Active\landingpage (Windows desktop). FIRST ACTION: read docs\deploy-facts.md in full — it is this project's persistent memory; its facts override anything in this prompt. All server commands run remotely as: ssh server '<command>' (the alias is configured; no password prompt).

Task: the server repo /srv/apps/landing-page/repo has uncommitted on-box edits (briefing_archive.py, compose.yml). Adopt them into git properly, then finish the verification items the memory file marks NOT YET VERIFIED.

Hard rules: never edit files on the server; never print environment values, tokens, or tunnel credentials (key names only; if a diff hunk contains a secret value, replace just the value with [REDACTED] and say so). The ONLY state-changing server commands allowed are the git stash / pull --ff-only / stash drop in step 5. No rebuilds, no restarts, no docker changes, nothing else.

1. Desktop preflight: `git status --short` — allowed untracked entries are STATE.md, docs/portfolio-deploy-prompts.md, portfolio.html only (docs/deploy-facts.md is gitignored and must stay that way); anything else, or any modified tracked file → STOP and record it in the memory file. Then `git pull --ff-only`.
2. Capture the server's uncommitted work: ssh server 'git -C /srv/apps/landing-page/repo status --short' and ssh server 'git -C /srv/apps/landing-page/repo diff'. If the tree is already clean, record that and skip to step 6.
3. Review the diff against expectations in the memory file: compose.yml should gain exactly the two read-only bind mounts and the BRIEFING_DB_READ_ONLY environment key; briefing_archive.py changes may be any size. If the compose.yml diff touches anything else — ports, networks, security_opt, cap_drop, new services — or any file beyond those two is modified: STOP, write the full diff into the memory file's Results log, and report.
4. Adopt on the desktop: save the diff to a temp file, `git apply` it in the desktop repo, confirm `git diff` reproduces it, then commit ONLY briefing_archive.py and compose.yml with message "chore: adopt on-box container config (bind mounts + BRIEFING_DB_READ_ONLY)" and push.
5. Clean the server tree: ssh server 'git -C /srv/apps/landing-page/repo stash', then '... pull --ff-only', then '... status --short'. If the pull fails or the tree is not clean afterwards: STOP and record — do NOT resolve anything on the server. If clean: confirm '... log --oneline -1' matches the commit you pushed, then '... stash drop'. Do NOT rebuild or restart the container — the running one already uses this config; Prompt C's rebuild normalizes the image.
6. Finish verification (read-only):
   a. ssh server 'systemctl status devmclovin-landing --no-pager' — does the old bare-metal unit exist, is it active? If it is RUNNING alongside the container, flag it loudly in your report and the memory file.
   b. cloudflared: find how it runs (ssh server 'systemctl cat cloudflared' for a config path, or 'docker ps' if it is a container) and report the ingress rule for devmclovin.com — hostname and the service URL it forwards to. Expected per the memory file: http://landing-page:3002 over the proxy_net docker network. Confirm or correct.
   c. HTTP from inside the container (no host port exists — use the docker exec pattern from the memory file) for path '/' (expect 200) and '/portfolio' (expect 404 today). Report both codes.
7. Persist: append a "## Prompt A results (<date>)" section to docs\deploy-facts.md — reconciled commit hash (or "tree was already clean"), the systemd answer, the cloudflared ingress answer, both HTTP codes, and anything that surprised you — then tick the Prompt A checkbox in the Status ledger. Do not commit the memory file. Finally, summarize the same in chat.
```

**Success looks like:** server tree clean at the new commit, systemd unit absent/inactive, cloudflared → `http://landing-page:3002`, HTTP 200 / 404. Any STOP: bring it back to the planning session.

---

## Prompt B — /portfolio route in server.py ✅ DONE 2026-07-11

Completed — route + nav + auth gate implemented and locally verified (`/portfolio` 200, no literal placeholders; the earlier "could not run" was just the cp1252 console vs the `→` banner, fixed with `PYTHONUTF8=1`). The diff sits uncommitted in server.py; Prompt C commits it. Kept for reference; do not re-run.

Fresh Codex conversation, working directory `C:\Users\Admin\Desktop\Code\Active\landingpage`. No server contact.

```text
Working directory: C:\Users\Admin\Desktop\Code\Active\landingpage (Windows desktop). FIRST ACTION: read docs\deploy-facts.md in full — this project's persistent memory; its facts override anything in this prompt. Abort with a report if its Status ledger does not show Prompt A complete. No SSH, no server contact in this task.

server.py is a very large single-file Python stdlib HTTP server. Do NOT refactor, reformat, reorganize, or "improve" anything in it. Minimal additive diff only.

Task: serve portfolio.html at /portfolio, exactly the way existing template pages are served.

1. Read the /status-board handler (serves status-board.html via inject_nav; see the memory file for landmarks). Add a /portfolio route copying that pattern exactly: read portfolio.html from the same directory PER REQUEST (no caching, no module-level read_text), inject_nav with active nav id "portfolio".
2. Gate the route exactly like /models does (~line 7795 per the memory file): if not is_authenticated(self): respond 403 with _UNAUTH_PAGE and return. This gate is REQUIRED — do not omit or weaken it for any reason. is_authenticated has a localhost bypass, so local testing works with the gate in place.
3. Add the nav link ONLY in render_nav() — nav is never edited inside templates.
4. portfolio.html already exists in the repo, generated externally — do not edit it. Confirm .gitignore does not exclude it.
5. Test locally: start `python server.py 3102`, request http://localhost:3102/portfolio — expect 200 with real nav HTML and ZERO literal __SITE_NAV__ / __SITE_NAV_CSS__ / __SITE_NAV_JS__ strings in the body. Also request /status-board — expect 200 (other data-driven pages may degrade on this dev machine; only /portfolio and the nav must work). Stop the test server afterwards.
6. Do NOT commit anything — the deploy prompt commits. Persist: append "## Prompt B results (<date>)" to docs\deploy-facts.md with the diff (it should be roughly 10–25 added lines: one route branch, one nav entry), the two test status lines, and confirmation the gate is present; tick the Prompt B ledger checkbox. Summarize in chat and show the full diff.

STOP and record in the memory file instead of proceeding if: a /portfolio route already exists; the status-board pattern differs materially from what this prompt describes; .gitignore excludes the html; or the local test cannot run at all (record the exact error, do not work around it).
```

**Success looks like:** a ~10–25 line additive diff, both test URLs 200, auth gate present. A big diff is a failed step even if it works — re-run with "minimal diff" emphasized.

---

## Prompt C — Deploy and verify ✅ DONE 2026-07-11

Completed — deployed `a32b603`, fresh image, container Up (healthy), in-container `/portfolio` and `/` both 200. Kept for reference; do not re-run.

Fresh Codex conversation, working directory `C:\Users\Admin\Desktop\Code\Active\landingpage`. Needs shell + SSH. Touches production.

```text
Working directory: C:\Users\Admin\Desktop\Code\Active\landingpage (Windows desktop); server commands run as: ssh server '<command>'. FIRST ACTION: read docs\deploy-facts.md in full — persistent project memory; its facts (paths, names, deploy command) are the only source of truth, never guessed values. Abort with a report if the Status ledger does not show Prompt A and Prompt B complete.

Task: deploy the /portfolio page. Steps in order, stopping at any failure.

1. Desktop preflight: `git status --short` — expected: modified server.py and .gitignore; untracked portfolio.html, STATE.md, docs/portfolio-deploy-prompts.md. Deviations are yours to handle, not stop on: extra untracked files (including .claude/) are simply left out of the commit and noted in the memory file; extra modified tracked files are likewise left uncommitted and noted. docs/deploy-facts.md is gitignored — never commit it. Sanity checks: server.py's diff is the /portfolio route + nav entry + is_authenticated gate described in the memory file (Prompt B results) and nothing more; portfolio.html contains each of __SITE_NAV_CSS__, __SITE_NAV__, __SITE_NAV_JS__ exactly once. If a sanity check fails, diagnose and fix it locally using the memory file before proceeding — the one hard rule is that the auth gate ships with the route.
2. Server preflight: ssh server 'git -C /srv/apps/landing-page/repo status --short'. Untracked noise there is ignorable (note it); MODIFIED tracked files mean someone edited the box again since reconciliation — that is a real STOP (never fix production files in place). Record ROLLBACK-COMMIT: ssh server 'git -C /srv/apps/landing-page/repo log --oneline -1' (expect c801a91).
3. Desktop: `git add server.py .gitignore portfolio.html STATE.md docs/portfolio-deploy-prompts.md` (exactly these), commit with message "feat: /portfolio dashboard page (template + route)", push.
4. Server deploy (the DEPLOY-COMMAND from the memory file): ssh server 'cd /srv/apps/landing-page/repo && git pull --ff-only && docker compose up -d --build landing-page'. STOP on merge conflict or non-fast-forward — never resolve conflicts on the production box.
5. Verify: ssh server 'docker ps' shows landing-page Up; ssh server 'docker logs landing-page --tail 30' shows a clean start; confirm the image was actually rebuilt from fresh code — ssh server 'docker inspect --format {{.Created}} repo-landing-page' must be from the last few minutes (a cached stale image looks exactly like "deploy did nothing"). Then HTTP from inside the container (docker exec pattern from the memory file): '/portfolio' → expect 200 (the localhost bypass applies inside the container — 200 here does NOT mean the page is public) and '/' → 200.
6. On container failure or crash-loop: ssh server 'docker logs landing-page --tail 50', report the error, then roll back — ssh server 'git -C /srv/apps/landing-page/repo reset --hard <ROLLBACK-COMMIT>' followed by the same compose up -d --build command, confirm Up — then STOP. Never debug on the server.
7. Persist: append "## Prompt C results (<date>)" to docs\deploy-facts.md — deployed commit hash, ROLLBACK-COMMIT, the docker ps line, image Created timestamp, both HTTP codes — and tick the Prompt C ledger checkbox. Report the same in chat, ending with: "Manual milestone gate is next: phone check per the prompts doc."

Never: edit files on the server, weaken or bypass Cloudflare Access or the app auth gate, modify the Dockerfile/compose beyond what git pull brings, restart cloudflared or any other container, prune or remove images. Do not test the public URL — the human does that.
```

**Success looks like:** fresh image timestamp, container Up, 200/200 from inside. If Codex starts debugging on the production box instead of rolling back, stop it — debug on the desktop, redeploy.

---

## Prompt D — One-command publish loop ✅ DONE 2026-07-11

Completed — `publish-dashboard.bat` created in Skills, tested end to end (real publish `d6886e9` + rebuild, then clean "dashboard unchanged" no-op). Note: the test's STATE.md edit corrupted that file's encoding (cp1252 mojibake) and the garbage shipped in the generated HTML; fixed and republished afterwards. Kept for reference; do not re-run.

Fresh Codex conversation, working directory `C:\Users\Admin\Desktop\Code\Active\Skills`.

```text
Working directory: C:\Users\Admin\Desktop\Code\Active\Skills (Windows desktop). FIRST ACTION: read C:\Users\Admin\Desktop\Code\Active\landingpage\docs\deploy-facts.md in full — persistent project memory. Abort with a report if its Status ledger does not show Prompt C complete.

Task: create publish-dashboard.bat (Windows batch, stdlib/git/ssh only, no new dependencies) — the one-command publish loop:

1. Run `python dashboard.py --site` (regenerates ..\landingpage\portfolio.html from the STATE.md files).
2. In ..\landingpage: if `git status --porcelain -- portfolio.html` shows a change, commit ONLY portfolio.html with message "dashboard: update YYYY-MM-DD" (real current date) and push. If no change, print "dashboard unchanged" and exit 0 without touching the server.
3. If a commit was pushed, go live (the site bakes source into the image, so HTML changes need the rebuild): run ssh server "cd /srv/apps/landing-page/repo && git pull --ff-only && docker compose up -d --build landing-page" directly from the bat, echoing its output. Exit nonzero if the ssh step fails, with a clear message that the commit is pushed but NOT live.

Safety: the script must never use `git add -A`, never commit anything besides portfolio.html, and never run any server command other than the exact go-live line above. Handle "not in a git repo" / ssh failure with readable error messages rather than silent exit.

Test end to end once for real: touch nothing, run it, expect "dashboard unchanged" — then append a trailing space to one Log line in any Active project's STATE.md, run it again, expect regenerate → commit → push → server rebuild, and verify with ssh server 'docker ps' that landing-page is Up. One real commit and one real rebuild are acceptable.

Persist: append "## Prompt D results (<date>)" to the memory file with the bat's final contents summary and the test transcript highlights; tick the Prompt D ledger checkbox. Summarize in chat.
```

**Success looks like:** run 1 prints "dashboard unchanged" and touches nothing; run 2 does the full regenerate → push → rebuild chain. Check the bat can't sweep up unrelated edits in the landingpage repo.

---

## Manual milestone gate (you, ~2 minutes, after Prompt C)

1. Phone, incognito/logged out: `https://devmclovin.com/portfolio` → must NOT render. Either the Cloudflare Access login or the site's 403 page counts as pass. **If the dashboard renders without auth, pull it immediately**: `ssh server 'git -C /srv/apps/landing-page/repo reset --hard HEAD~1 && cd /srv/apps/landing-page/repo && docker compose up -d --build landing-page'` — it exposes your goals and plans.
2. Log in via Access → dashboard renders with the site nav, both projects visible.
3. That's M1 shipped. Add the log line to STATE.md and tick the last ledger checkbox in docs/deploy-facts.md.

## Failure modes to watch across all prompts

- **Refactor creep:** server.py invites cleanup; every prompt forbids it. A big diff is a failed step regardless of whether it works.
- **Auth erosion:** the 403/redirect for unauthenticated requests is *success*. Any step that "fixes" it is a regression — reject.
- **Invented facts:** every path, name, and command must come from docs/deploy-facts.md. If output cites a value that isn't there, it was guessed.
- **Debugging on production:** the server only ever pulls, rebuilds, and rolls back. All debugging happens on the desktop.
- **Memory file hygiene:** deploy-facts.md is gitignored and must never be committed; no secret values in it, ever.
