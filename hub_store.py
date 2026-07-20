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
    "current_override": "",
    "whats_next": "",
    "status_override": "",
    "live_url": "",
    "local_path": "",
    "pinned": False,
    "hidden": False,
    "order": 0,
}

_INSIGHT_STATES = {"pending", "updating", "ready", "stale", "no_changes", "unavailable"}
_FAILURE_KINDS = {"", "github", "ollama", "invalid_response", "no_changes"}
_CONFIDENCE_LEVELS = {"", "low", "medium", "high"}
_MAX_CHANGED_FILES = 20
_MAX_HISTORY = 5


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


def _non_negative_integer(value, default: int = 0) -> int:
    return max(0, _integer(value, default))


def _bounded_text(value, limit: int) -> str:
    return _text(value)[:limit]


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
            "current_override": _text(entry.get("current_override")),
            "whats_next": _text(entry.get("whats_next")),
            "status_override": "done" if status == "done" else "",
            "live_url": _text(entry.get("live_url")),
            "local_path": _text(entry.get("local_path")),
            "pinned": _boolean(entry.get("pinned")),
            "hidden": _boolean(entry.get("hidden")),
            "order": _integer(entry.get("order"), 0),
        }
    return clean


def _normalise_changed_files(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    clean = []
    for item in value[:_MAX_CHANGED_FILES]:
        if not isinstance(item, dict):
            continue
        path = _bounded_text(item.get("path"), 500)
        if not path:
            continue
        status = _bounded_text(item.get("status"), 32).lower()
        clean.append({
            "path": path,
            "status": status if status in {"added", "modified", "removed", "renamed"} else "modified",
            "additions": _non_negative_integer(item.get("additions")),
            "deletions": _non_negative_integer(item.get("deletions")),
        })
    return clean


def _normalise_history(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    clean = []
    for item in value[:_MAX_HISTORY]:
        if not isinstance(item, dict):
            continue
        current_state = _bounded_text(item.get("current_state"), 1000)
        next_step = _bounded_text(item.get("next_step"), 1000)
        if not current_state and not next_step:
            continue
        confidence = _bounded_text(item.get("confidence"), 16).lower()
        clean.append({
            "current_state": current_state,
            "next_step": next_step,
            "confidence": confidence if confidence in _CONFIDENCE_LEVELS else "",
            "head_sha": _bounded_text(item.get("head_sha"), 64),
            "generated_at": _bounded_text(item.get("generated_at"), 64),
        })
    return clean


def normalise_insight(raw) -> dict:
    """Return one display-safe generated insight; unknown fields are discarded."""
    entry = raw if isinstance(raw, dict) else {}
    confidence = _bounded_text(entry.get("confidence"), 16).lower()
    state = _bounded_text(entry.get("state"), 32).lower()
    failure_kind = _bounded_text(entry.get("failure_kind"), 32).lower()
    return {
        "current_state": _bounded_text(entry.get("current_state"), 1000),
        "next_step": _bounded_text(entry.get("next_step"), 1000),
        "confidence": confidence if confidence in _CONFIDENCE_LEVELS else "",
        "head_sha": _bounded_text(entry.get("head_sha"), 64),
        "source_pushed_at": _bounded_text(entry.get("source_pushed_at"), 64),
        "changed_files": _normalise_changed_files(entry.get("changed_files")),
        "additions": _non_negative_integer(entry.get("additions")),
        "deletions": _non_negative_integer(entry.get("deletions")),
        "generated_at": _bounded_text(entry.get("generated_at"), 64),
        "state": state if state in _INSIGHT_STATES else "unavailable",
        "failure_kind": failure_kind if failure_kind in _FAILURE_KINDS else "",
        "history": _normalise_history(entry.get("history")),
    }


def normalise_insights(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        full_name = _bounded_text(key, 300)
        if full_name:
            clean[full_name] = normalise_insight(value)
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
                "current_override": "",
                "whats_next": "",
                "status_override": "",
                "live_url": _text(item.get("url")),
                "local_path": "",
                "pinned": False,
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
                    "current_override": _text(get("current_override", "")),
                    "whats_next": _text(get("whats_next", "")),
                    "status_override": (
                        "done" if _text(get("status_override", "")).lower() == "done" else ""
                    ),
                    "live_url": _text(get("live_url", "")),
                    "local_path": _text(get("local_path", "")),
                    "pinned": _boolean(get("pinned", "")),
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


class InsightStore:
    """Locked generated-insight store with capped history and atomic writes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load_unlocked(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return normalise_insights(raw)

    def load(self) -> dict:
        with self._lock:
            return self._load_unlocked()

    def _save_unlocked(self, insights: dict) -> None:
        clean = normalise_insights(insights)
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

    def save(self, insights: dict) -> None:
        with self._lock:
            self._save_unlocked(insights)

    @staticmethod
    def _snapshot(entry: dict) -> dict | None:
        if not entry.get("current_state") and not entry.get("next_step"):
            return None
        return {
            "current_state": entry.get("current_state", ""),
            "next_step": entry.get("next_step", ""),
            "confidence": entry.get("confidence", ""),
            "head_sha": entry.get("head_sha", ""),
            "generated_at": entry.get("generated_at", ""),
        }

    def put_generated(self, full_name: str, record: dict) -> dict | None:
        """Persist a generated record and retain up to five distinct predecessors."""
        full_name = _bounded_text(full_name, 300)
        if not full_name:
            return None
        with self._lock:
            insights = self._load_unlocked()
            previous = insights.get(full_name)
            clean = normalise_insight(record)
            history = list(previous.get("history", [])) if previous else []
            snapshot = self._snapshot(previous) if previous else None
            changed = previous and (
                previous.get("head_sha") != clean.get("head_sha")
                or previous.get("current_state") != clean.get("current_state")
                or previous.get("next_step") != clean.get("next_step")
            )
            if changed and snapshot:
                history.insert(0, snapshot)
            clean["history"] = _normalise_history(history)
            insights[full_name] = clean
            self._save_unlocked(insights)
            return clean

    def set_state(self, full_name: str, state: str, failure_kind: str = "") -> dict | None:
        """Update lifecycle metadata without discarding the last-good text."""
        full_name = _bounded_text(full_name, 300)
        if not full_name:
            return None
        with self._lock:
            insights = self._load_unlocked()
            current = dict(insights.get(full_name) or {})
            current["state"] = state
            current["failure_kind"] = failure_kind
            clean = normalise_insight(current)
            insights[full_name] = clean
            self._save_unlocked(insights)
            return clean

    def remove(self, full_name: str) -> None:
        full_name = _bounded_text(full_name, 300)
        if not full_name:
            return
        with self._lock:
            insights = self._load_unlocked()
            insights.pop(full_name, None)
            self._save_unlocked(insights)
