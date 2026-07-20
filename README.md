# Control Center

Tristan's private home-server dashboard for daily briefings, live service health,
and living project state. Projects update automatically from GitHub code changes;
a local Ollama model explains where each project is and suggests one next step,
while manual overrides remain available. The app uses only Python's standard
library and has no frontend build step.

## Run locally

```powershell
# Terminal 1
$env:ALLOWED_HOSTS = "localhost,127.0.0.1"
python server.py 3102

# Terminal 2
python scripts/smoke.py 3102
```

Runtime state, including generated project insights and manual curation, belongs
under `data/` (or the directory selected by `DATA_DIR`) and is intentionally not
committed. Production runs as the dedicated Docker Compose stack behind Caddy
and Cloudflare Access.

See `OPERATIONS.md` for build, deployment, monitoring, data, secrets, logs, and
backup procedures.
