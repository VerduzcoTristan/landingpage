"""Thread-safe flat-file persistence for Hub curation data."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Callable


_EMPTY_ENTRY = {
    "goal": "",
    "whats_next": "",
    "status_override": "",
    "live_url": "",
    "local_path": "",
    "hidden": False,
    "order": 0,
}


def empty_entry() -> dict:
    return dict(_EMPTY_ENTRY)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _integer(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def normalise_hub(raw) -> dict:
    """Return a complete, safe curation mapping from arbitrary decoded JSON."""
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        full_name = _text(key)
        if not full_name:
            continue
        entry = value if isinstance(value, dict) else {}
        status = _text(entry.get("status_override")).lower()
        clean[full_name] = {
            "goal": _text(entry.get("goal")),
            "whats_next": _text(entry.get("whats_next")),
            "status_override": "done" if status == "done" else "",
            "live_url": _text(entry.get("live_url")),
            "local_path": _text(entry.get("local_path")),
            "hidden": _boolean(entry.get("hidden")),
            "order": _integer(entry.get("order"), 0),
        }
    return clean


class HubStore:
    """Locked JSON store with legacy migration and atomic replacement."""

    def __init__(self, path: Path, legacy_path: Path | None = None):
        self.path = Path(path)
        self.legacy_path = Path(legacy_path) if legacy_path else None
        self._lock = threading.RLock()

    def migrate_legacy_path(self) -> None:
        with self._lock:
            if self.path.exists() or not self.legacy_path or not self.legacy_path.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.legacy_path, self.path)

    @staticmethod
    def _migrate_list(raw: list) -> dict:
        migrated = {}
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            full_name = ""
            repo_url = _text(item.get("repo_url"))
            if repo_url:
                match = re.search(r"github\.com/([^/]+/[^/#?]+)", repo_url)
                if match:
                    full_name = match.group(1)
            full_name = full_name or _text(item.get("name"))
            if not full_name:
                continue
            migrated[full_name] = {
                "goal": _text(item.get("description")),
                "whats_next": "",
                "status_override": "",
                "live_url": _text(item.get("url")),
                "local_path": "",
                "hidden": _boolean(item.get("hidden")),
                "order": _integer(item.get("order"), index),
            }
        return normalise_hub(migrated)

    def _load_unlocked(self) -> tuple[dict, bool]:
        if not self.path.exists():
            return {}, False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}, False
        if isinstance(raw, list):
            return self._migrate_list(raw), True
        return normalise_hub(raw), False

    def load(self) -> dict:
        with self._lock:
            data, migrated = self._load_unlocked()
            if migrated:
                self._save_unlocked(data)
            return data

    def _save_unlocked(self, hub: dict) -> None:
        clean = normalise_hub(hub)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(clean, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def save(self, hub: dict) -> None:
        with self._lock:
            self._save_unlocked(hub)

    def update(self, action: str, get: Callable[[str, str], object]) -> str:
        full_name = _text(get("full_name", ""))
        if not full_name:
            return "Repository identifier missing."
        with self._lock:
            hub, migrated = self._load_unlocked()
            if migrated:
                self._save_unlocked(hub)
            entry = dict(hub.get(full_name) or empty_entry())
            if action == "update":
                entry.update({
                    "goal": _text(get("goal", "")),
                    "whats_next": _text(get("whats_next", "")),
                    "status_override": (
                        "done" if _text(get("status_override", "")).lower() == "done" else ""
                    ),
                    "live_url": _text(get("live_url", "")),
                    "local_path": _text(get("local_path", "")),
                    "hidden": _boolean(get("hidden", "")),
                    "order": _integer(get("order", entry.get("order", 0)), entry.get("order", 0)),
                })
                hub[full_name] = entry
            elif action == "toggle-hide":
                entry["hidden"] = not _boolean(entry.get("hidden"))
                hub[full_name] = entry
            elif action == "delete":
                hub.pop(full_name, None)
            else:
                return "Unknown action."
            self._save_unlocked(hub)
        return "Hub updated."
