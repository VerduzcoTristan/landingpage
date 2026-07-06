#!/usr/bin/env python3
"""Backup Status API — serves /api/backups/status with Hermes + GitHub backup data."""

from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Backup Status API", version="0.1.0")

# ─── Hardcoded sample data ──────────────────────────────────────────────────
# Replace this block with real backup service integration later.
SAMPLE_BACKUPS = [
    {
        "type": "hermes",
        "lastBackupTime": "2026-06-28T09:00:00Z",
        "lastSuccess": True,
        "repoLink": "https://github.com/VerduzcoTristan/Hermes-backup",
        "changedFilesCount": 7,
        "backupSize": "42.3 MB",
        "restoreInstructionsLink": "https://github.com/VerduzcoTristan/Hermes-backup/blob/main/README.md",
    },
    {
        "type": "github",
        "lastBackupTime": "2026-06-28T08:00:00Z",
        "lastSuccess": True,
        "repoLink": "https://github.com/VerduzcoTristan/Hermes-backup/tree/main/projects",
        "changedFilesCount": 12,
        "backupSize": "1.2 GB",
        "restoreInstructionsLink": "https://github.com/VerduzcoTristan/Hermes-backup/blob/main/RESTORE.md",
    },
]
# ────────────────────────────────────────────────────────────────────────────


@app.get("/api/backups/status")
async def backup_status():
    """Return backup status for both Hermes and GitHub backups."""
    return JSONResponse(content={"backups": SAMPLE_BACKUPS})


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8091)
