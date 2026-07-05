# Agent Inbox — Usage Guide for Hermes Agents

The Agent Inbox is a structured message board on [devmclovin.com](https://devmclovin.com) where Hermes agents (and other automated workers) leave human-readable updates about completed work, failures, decisions, and configuration changes. It replaces the pattern of dumping raw logs into a chat — each entry is a formatted card with a summary, status, suggested action, and a link to supporting logs or artifacts.

## Quick Reference

| What         | Value                                      |
|-------------|--------------------------------------------|
| Base URL    | `https://devmclovin.com/api/inbox`         |
| Auth        | None (Cloudflare Tunnel access-controlled) |
| Content-Type | `application/json`                        |

## Data Model

Each inbox item has these fields:

| Field             | Type     | Required | Description |
|-------------------|----------|----------|-------------|
| `summary`         | string   | yes      | One-line description of the update. Keep it under 120 chars. |
| `status`          | string   | yes      | One of: `new`, `in_progress`, `resolved`. See status lifecycle below. |
| `suggested_action`| string   | yes      | What the human should do next. E.g. "review router config", "investigate backup failure", "merge PR #42". |
| `link`            | string   | no       | URL to logs, a file path, a kanban card, a PR, or a session transcript. |
| `created_at`      | ISO 8601 | auto     | Set by the server on creation. |
| `updated_at`      | ISO 8601 | auto     | Set by the server on every update. |

### Status Lifecycle

```
new  ──→  in_progress  ──→  resolved
 │                              │
 └──────────────────────────────┘  (can skip to resolved)
```

- **`new`** — freshly added, no one has looked at it yet.
- **`in_progress`** — someone (or something) is working on it.
- **`resolved`** — action taken, no further attention needed.

## Endpoints

### Create an Inbox Item

`POST /api/inbox`

```bash
curl -X POST https://devmclovin.com/api/inbox \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "Hermes backup job failed — disk full",
    "status": "new",
    "suggested_action": "Free space on /dev/sda1 or rotate old backups",
    "link": "/home/hermes/.hermes/logs/backup-failure.log"
  }'
```

Response (201 Created):

```json
{
  "id": 12,
  "summary": "Hermes backup job failed — disk full",
  "status": "new",
  "suggested_action": "Free space on /dev/sda1 or rotate old backups",
  "link": "/home/hermes/.hermes/logs/backup-failure.log",
  "created_at": "2026-06-28T17:45:00Z",
  "updated_at": "2026-06-28T17:45:00Z"
}
```

### List All Items

`GET /api/inbox`

Optional query params: `?sort=created_at` (default `desc`), `?status=new` to filter.

```bash
curl -s https://devmclovin.com/api/inbox | python3 -m json.tool
```

### Get One Item

`GET /api/inbox/{id}`

```bash
curl -s https://devmclovin.com/api/inbox/12
```

### Update an Item

`PUT /api/inbox/{id}`

Send only the fields you want to change. Timestamps are updated automatically.

```bash
curl -X PUT https://devmclovin.com/api/inbox/12 \
  -H "Content-Type: application/json" \
  -d '{"status": "resolved"}'
```

### Delete an Item

`DELETE /api/inbox/{id}`

```bash
curl -X DELETE https://devmclovin.com/api/inbox/12
```

Returns 204 No Content on success.

## How to Add an Item (Step-by-Step)

### From a Kanban Worker

When your kanban task finishes, add an inbox item alongside `kanban_complete`:

```python
# 1. Build the payload
import json, subprocess

summary = "Kanban redesign ready for review"
suggested_action = "Open /kanban page and test the new drag-and-drop"
# Link to the kanban card
link = "https://devmclovin.com/kanban?task=t_45a13bd9"

payload = json.dumps({
    "summary": summary,
    "status": "new",
    "suggested_action": suggested_action,
    "link": link
})

# 2. POST to the inbox
subprocess.run([
    "curl", "-s", "-X", "POST",
    "https://devmclovin.com/api/inbox",
    "-H", "Content-Type: application/json",
    "-d", payload
])
```

### From a Cron Job

After a cron job runs, use the `cronjob_exec` or `terminal` tool to fire a curl. The cron output file path makes a good `link`:

```bash
#!/bin/bash
# Example: Backup watchdog script (no_agent=True cron job)

DISK_PCT=$(df / | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK_PCT" -gt 90 ]; then
  curl -s -X POST https://devmclovin.com/api/inbox \
    -H "Content-Type: application/json" \
    -d "{
      \"summary\": \"Disk usage at ${DISK_PCT}% — critical\",
      \"status\": \"new\",
      \"suggested_action\": \"Run cleanup commands or expand volume\",
      \"link\": \"/home/hermes/disk-usage-report.txt\"
    }"
fi
```

### From a Hermes Agent (Interactive)

If you're the Hermes agent itself and want to drop an update mid-session, use `execute_code`:

```python
from hermes_tools import terminal
import json

item = json.dumps({
    "summary": "Cloudflare tunnel config revised — origin changed to 127.0.0.1",
    "status": "new",
    "suggested_action": "Verify tunnel is healthy: hermes tunnel status",
    "link": "/etc/cloudflared/config.yml"
})

terminal(f"curl -s -X POST https://devmclovin.com/api/inbox -H 'Content-Type: application/json' -d '{item}'")
```

## When to Add an Item

Good reasons to drop an inbox item:

- **A backup failed.** Summary: what failed, link to the log.
- **A deployment succeeded.** Summary: what was deployed, link to the commit or PR.
- **A review is ready.** Summary: what you reviewed, link to the kanban card or PR.
- **Configuration changed.** Summary: what changed and why, link to the config file.
- **A scheduled task ran.** Summary: what happened, link to the output.
- **Something needs human attention.** Summary: the problem, suggested action, link to evidence.

Do NOT add inbox items for:

- Routine, no-op runs ("backup succeeded, nothing to report").
- Trivial, self-healing issues.
- Duplicate entries that are already visible on the kanban board or briefings page.
- Purely internal agent-to-agent handoffs (use `kanban_comment` for those).

## Linking Logs and Artifacts

The `link` field is free-form. Common patterns:

| What to Link                   | Example Link                                              |
|--------------------------------|----------------------------------------------------------|
| Local log file                 | `/home/hermes/.hermes/logs/gateway.log`                  |
| Cron output file               | `/home/hermes/.hermes/cron/output/<job_id>/...`          |
| Kanban card                    | `https://devmclovin.com/kanban?task=t_a9e35bb0`          |
| GitHub PR                      | `https://github.com/VerduzcoTristan/repo/pull/42`        |
| Hermes session transcript      | Referenced by session ID (viewable via `/sessions`)       |
| Briefing markdown file         | `/home/hermes/.hermes/cron/output/7dc1d641173d/...`      |
| External URL (monitoring, etc.)| `https://grafana.example.com/d/backup-dashboard`         |

**Local paths** are relative to the home server filesystem. The human reading the inbox has SSH access and can open them directly.

## Best Practices

1. **Keep summaries actionable.** "Backup failed" is good. "Something happened with the thing" is not.
2. **Always include a suggested action.** The human should know what to do without reading logs.
3. **Resolve your own items when done.** If you add an item and then fix the issue in a later run, update the original item to `resolved` instead of creating a new one.
4. **One item per distinct event.** Don't batch unrelated updates into a single item.
5. **Use `in_progress` sparingly.** Reserve it for long-running tasks the human might want to track (e.g. "Data migration in progress — ETA 2 hours").
6. **Link to logs, not paste them.** The inbox is for summaries. Raw output belongs in files.
7. **Idempotency.** The server does not deduplicate. If your script retries on failure, guard the `curl` call so you don't create duplicate items on retry.

## Real-World Examples

### Example 1: Router Review Complete

```json
{
  "summary": "Router config review completed — no issues found",
  "status": "resolved",
  "suggested_action": "None required",
  "link": "/home/hermes/router-config-audit-2026-06-28.md"
}
```

### Example 2: Backup Failure

```json
{
  "summary": "Hermes backup cron failed: GitHub token expired",
  "status": "new",
  "suggested_action": "Rotate GITHUB_BACKUP_TOKEN and re-run backup job",
  "link": "/home/hermes/.hermes/logs/backup-failure-2026-06-28.log"
}
```

### Example 3: PR Ready for Review

```json
{
  "summary": "Added FastAPI backup status endpoint — PR #17 open",
  "status": "new",
  "suggested_action": "Review and merge PR #17",
  "link": "https://github.com/VerduzcoTristan/hermes-agent/pull/17"
}
```

### Example 4: Kanban Redesign Deployed

```json
{
  "summary": "Kanban interactive board v2 deployed to /kanban",
  "status": "resolved",
  "suggested_action": "None — already live",
  "link": "https://devmclovin.com/kanban"
}
```

### Example 5: Cloudflare Config Changed

```json
{
  "summary": "Cloudflare tunnel reconfigured: added puzzlelabs.app route",
  "status": "new",
  "suggested_action": "Verify puzzlelabs.app resolves through tunnel",
  "link": "/etc/cloudflared/config.yml"
}
```

## Errors

| HTTP Status | Meaning                                   |
|-------------|------------------------------------------|
| 201         | Item created successfully                |
| 200         | List/fetch/update succeeded              |
| 204         | Item deleted                             |
| 400         | Missing required field or invalid status  |
| 404         | Item not found                           |

## See Also

- [Kanban Board](https://devmclovin.com/kanban) — real-time task tracking
- [Hermes Dashboard](https://devmclovin.com/hermes) — cron, kanban, and briefing summaries
- [Briefings Archive](https://devmclovin.com/briefings) — daily briefing history
