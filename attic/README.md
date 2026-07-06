# attic/

Standalone servers parked here by the 2026-07 landing-page redesign (M1).

Per the plan's two-tier deletion rule, these "runnable servers" may only be
deleted outright once **Milestone 0** (the production-box preflight) has
confirmed no `systemd` unit references them:

```bash
ls /etc/systemd/system/ | grep -iE 'ollama|inbox|runbook|notes|backup'
grep -rl 'inbox_server|runbook_server|notes_proxy|ollama_api|backups_api' /etc/systemd/system/ 2>/dev/null
```

M0 could not be run in the redesign environment (Windows dev checkout, not the
production box), so the conservative fallback — `git mv` to `attic/` instead of
deletion — was taken. On the box, once the greps above return nothing, these can
be `git rm`'d:

| File | Was on port | Superseded by (in `server.py`) |
|---|---|---|
| `ollama_api.py` | 3097 | built-in `/api/ollama/*` |
| `ollama_api_server.py` | 3004 | built-in `/api/ollama/*` |
| `inbox_server.py` | 8001 | `/inbox` + new `/api/inbox/*` proxy |
| `runbook_server.py` | 3009 | `/runbooks` (via `runbook_data.py`) |
| `notes_proxy.py` | 3005→8123 | `_proxy_notes` (→8081) |
| `backups_api.py` | 8091 | removed — served hardcoded sample data |
