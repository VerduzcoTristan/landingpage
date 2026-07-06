# devmclovin.com — landing page

A single-file Python 3 **stdlib** HTTP server (`server.py`, no framework, no build
step) that powers [devmclovin.com](https://devmclovin.com): a personal command
center with daily briefings, service health, Hermes admin, and a set of LLM tools.
It runs behind a Cloudflare Tunnel + Cloudflare Access.

There is **no build step and no dependency install** — everything is Python
standard library plus a few sibling modules in this repo (`runbook_data.py`,
`cloudflare_api.py`). Pages are Python functions that return HTML strings; six
pages are served from standalone `*.html` templates with the shared nav injected
at request time (see "Shared nav" below).

## Ports / services

`server.py` proxies several same-origin `/api/*` paths to local services. The
table below is the source of truth for what talks to what. Items marked
**VERIFY** should be confirmed on the production box (`ss -tlnp` / `systemctl`)
before relying on them — they are read from the code, not from a live host.

| Port  | Service                     | Reached via `server.py`                    | Notes |
|-------|-----------------------------|--------------------------------------------|-------|
| 3002  | This landing page           | —                                          | `ExecStart … server.py 3002` |
| 9091  | `api_server.py` (status)    | `/api/status`, `/api/service/*` → `_proxy_api` | service health board data |
| 8081  | Notes API                   | `/api/notes/*` → `_proxy_notes` (**VERIFY**) | Bearer `NOTES_API_TOKEN` |
| 8000  | Agent Inbox API             | `/api/inbox/*` → `_proxy_inbox` (**VERIFY**) | upstream paths under `/api/v1/*`, no auth |
| 8092  | Commands API                | `/api/proxy/commands/<action>` (**VERIFY**)  | `X-API-Key: COMMANDS_API_KEY` |
| 3003  | `router_server.py`          | standalone (not proxied)                   | LLM router dashboard (`router-dashboard.html`) |
| 11434 | Ollama                      | `/api/ollama/*` (server calls it directly) | local model runtime |

Auth: `is_authenticated()` gates `/models`, `/model-tuning`, `/llm-lab` (localhost
bypass + Cloudflare Access `Cf-Access-Authenticated-User-Email`). App-layer auth is
redundant **iff** Cloudflare Access covers `devmclovin.com/*` — **VERIFY** the CF
Access policy scope.

## Shared nav (no drift)

The top nav is defined once in `render_nav(active)`; its CSS/JS live in the
`NAV_CSS` / `NAV_JS` constants. `html_page()` uses them for server-rendered pages,
and `inject_nav()` fills the `__SITE_NAV__` / `__SITE_NAV_CSS__` / `__SITE_NAV_JS__`
placeholders in the six template pages (`notes.html`, `inbox.html`,
`status-board.html`, `model_comparison.html`, `model_tuning.html`, `llm_lab.html`).
Edit the nav in `render_nav()` only — never in a template.

## Secrets / `.env`

`server.py` reads two tokens from `~/.hermes/.env` via `_load_env_var()`, falling
back to the previous hardcoded defaults so nothing breaks if they are unset:

- `NOTES_API_TOKEN`  (Notes API bearer token)
- `COMMANDS_API_KEY` (Commands API `X-API-Key`)

To rotate: set both in `~/.hermes/.env`, update the upstream services to match,
then restart the landing page service.

## Run locally (dev)

```bash
python3 server.py 3102        # any non-3002 port to avoid the live service
# then browse http://localhost:3102/
```

Note: `server.py` reads live data from `~/.hermes` / `~/.devmclovin` and imports
`briefing_archive` from `~/.hermes/tools`, so a fully-featured run requires the
production home directories. `/models` degrades gracefully to an "unavailable"
state if the external `~/projects/model-price-comparison` DB is missing.

## Deploy / restart (production box)

The systemd unit and installer are in the repo:

```bash
systemctl cat devmclovin-landing          # confirm the deployed User=/paths
sudo systemctl restart devmclovin-landing # restart after pulling changes
journalctl -u devmclovin-landing -n 100   # logs (also at /logs)
```

`devmclovin-landing.service` / `setup.sh` reference `User=ubuntu` and
`/home/ubuntu/devmclovin-landing`. **VERIFY** these match the live unit before
re-running `setup.sh`; do not edit the running unit from this repo.

## Repo layout

- `server.py` — the whole site.
- `api_server.py`, `notes_api_server.py` — sibling services (not started by this repo).
- `router_server.py`, `router_metrics.py`, `router-dashboard.html` — standalone router dashboard (port 3003).
- `runbook_data.py` — `/runbooks` content. `cloudflare_api.py` — tunnel data.
- `*.html` — the six injected template pages.
- `docs/` — plans and the agent inbox guide. `attic/` — parked standalone servers (see `attic/README.md`).
