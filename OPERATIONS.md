# Control Center operations

Control Center runs as the `landing-page` Docker Compose stack at
`/srv/apps/landing-page/`. The repository and `compose.yml` live in
`/srv/apps/landing-page/repo/`; persistent application data lives in the sibling
`/srv/apps/landing-page/data/` directory.

## Architecture

- `landing-page` is a non-root Python container (uid 10001) with a read-only root
  filesystem, all Linux capabilities dropped, and `no-new-privileges` enabled.
- The container joins internal `app_net` and shared external `proxy_net`. It does
  not publish a host port; Caddy reaches `landing-page:3002` on `proxy_net`, and
  the app reaches Ollama at the configured service URL on that same network.
- Caddy is fronted by the Cloudflare Tunnel. Its site address must retain the
  explicit `http://` prefix because the tunnel terminates TLS.
- The briefing database and cron output are mounted read-only. Hub curation,
  monitors, bookmarks, and generated impact cache files are written only beneath
  `/app/data`.

The public hostname is intentionally not committed. Caddy and the tunnel keep
serving the currently configured hostname; hostname or route changes belong in a
separate infrastructure change under `/srv/infra/`.

## Configuration and secrets

Create `/srv/apps/landing-page/repo/.env` with mode `0600`:

```dotenv
ALLOWED_HOSTS=<current-public-hostname>,localhost,127.0.0.1
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:7b
```

`ALLOWED_HOSTS` is a comma-separated exact-host allowlist. A missing or
unlisted `Host` header receives HTTP 421. The Compose default is suitable only
for local checks; production must include the hostname already used by Caddy.

Create a read-only GitHub PAT with repository access (including private
repositories that should appear in the Hub) and write only the token to
`/srv/secrets/landing-page/github_token`. Compose mounts it at
`/run/secrets/github_token`; the value never belongs in `.env`, the repository,
or the image. `GITHUB_TOKEN` remains supported only as a local-development
override.

`OLLAMA_BASE_URL` and `OLLAMA_MODEL` are non-secret settings. The default URL
expects the Ollama container to join the existing external `proxy_net` with the
network alias `ollama`. Joining Ollama to that network is an infrastructure
operation and must be completed in Ollama's own Compose stack; this repository
does not mutate `/srv/infra`.

## First-time host preparation

Run as an administrator on the server:

```sh
install -d -o 10001 -g 10001 -m 0750 /srv/apps/landing-page/data
install -d -o root -g root -m 0750 /srv/secrets/landing-page
install -o root -g 10001 -m 0440 /dev/null /srv/secrets/landing-page/github_token
docker network inspect proxy_net >/dev/null
```

Populate `github_token` through the server's secret-management workflow. It
remains root-owned on the host, with group-read permission for uid/gid 10001;
Compose exposes it read-only to that non-root container identity. Then confirm
Ollama resolves from `proxy_net` as `ollama`. Do not paste the token into shell
history or command output.

The two required briefing sources must exist on the host:

```text
/home/hermes/.hermes/data/briefings.db
/home/hermes/.hermes/cron/output/7dc1d641173d/
```

## Build and run

From `/srv/apps/landing-page/repo/`:

```sh
docker compose config --quiet
docker compose up -d --build landing-page
docker compose ps
```

The healthcheck calls `http://127.0.0.1:3002/` inside the container. A healthy
container should also answer:

```sh
docker compose exec -T landing-page python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:3002/health').read().decode())"
```

For local development without Docker:

```sh
ALLOWED_HOSTS=localhost,127.0.0.1 GITHUB_TOKEN_FILE=/path/to/github_token \
  OLLAMA_BASE_URL=http://localhost:11434 PYTHONUTF8=1 python server.py 3102
python scripts/smoke.py 3102
```

Omit `GITHUB_TOKEN_FILE` (and `GITHUB_TOKEN`) to exercise the Hub's curated-only
fallback. If Ollama is unavailable, Hub cards fall back to recent commit text
instead of failing the page.

## Deploy

Deploy only a reviewed commit on `main`:

```sh
cd /srv/apps/landing-page/repo
git pull --ff-only
docker compose up -d --build landing-page
docker compose ps
docker compose logs --tail=100 landing-page
```

Cloudflare Access should redirect an unauthenticated request before application
content is exposed. Do not change the Caddyfile, tunnel ingress, public hostname,
or Access policy as part of a normal application deploy.

## Logs and checks

```sh
cd /srv/apps/landing-page/repo
docker compose logs -f --tail=100 landing-page
docker compose exec -T landing-page python3 scripts/smoke.py 3002
docker compose exec -T landing-page python3 -m py_compile server.py briefing_archive.py scripts/smoke.py
```

The smoke script verifies all retained routes plus 404 responses for deleted
features, including `/hub` and the removal of `/projects` and `/portfolio`.
`/health` is the liveness endpoint; `/api/status` runs the configured monitor
checks and returns their live results.

## Data

Persistent files are under `/srv/apps/landing-page/data/`:

- `monitors.json` — live HTTP checks and links to existing monitoring tools.
- `projects.json` — Hub curation keyed by GitHub `owner/repository`, managed
  through `/hub/admin` (goals, next steps, ordering, visibility, and live links).
- `bookmarks.json` — saved briefing stories.

The container owns these files as uid 10001. Do not store user data inside the
repository or container layer.

## Backup and restore

Create a logical export:

```sh
sudo /srv/apps/landing-page/repo/scripts/export-data.sh
```

The script writes an atomic, mode-0600 tarball beneath
`/srv/backups/exports/landing-page/`. To restore, stop the app, inspect the
archive, extract it into an empty data directory, restore ownership, then start
and smoke-test the app:

```sh
cd /srv/apps/landing-page/repo
docker compose stop landing-page
tar -tzf /srv/backups/exports/landing-page/<archive>.tar.gz
sudo tar -xzf /srv/backups/exports/landing-page/<archive>.tar.gz \
  -C /srv/apps/landing-page/data
sudo chown -R 10001:10001 /srv/apps/landing-page/data
docker compose up -d landing-page
docker compose exec -T landing-page python3 scripts/smoke.py 3002
```

Restoring replaces user-managed state; take a fresh export first and confirm the
selected archive before extraction.

## Troubleshooting

- HTTP 421: add the request hostname to `ALLOWED_HOSTS` in `.env`, then recreate
  the container.
- Empty monitoring board: create a valid `data/monitors.json`; check
  `/api/status` from inside the container.
- Missing briefings: verify both read-only host mounts exist and are readable by
  Docker; the UI should still render a graceful empty state.
- Hub says the GitHub token is not configured: verify the root-owned
  `/srv/secrets/landing-page/github_token` exists, then recreate the container.
- Hub has repositories but no generated summaries: verify Ollama is running,
  shares `proxy_net` with alias `ollama`, and the configured model is installed.
- Permission errors beneath `/app/data`: restore uid/gid 10001 ownership on the
  host data directory.
