"""Non-blocking, bounded stdlib GitHub client for Hub repository data."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class GitHubClient:
    """Maintain a stale-while-refreshing snapshot of owned repositories."""

    def __init__(
        self,
        api_url: str = "https://api.github.com",
        cache_ttl: int = 600,
        failure_ttl: int = 60,
        max_workers: int = 4,
        max_files: int = 20,
        max_patch_chars: int = 2000,
        max_total_patch_chars: int = 12000,
        max_response_bytes: int = 2_000_000,
        insight_loader: Callable[[], dict] | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.cache_ttl = cache_ttl
        self.failure_ttl = failure_ttl
        self.max_workers = max(1, min(int(max_workers), 4))
        self.max_files = max(1, min(int(max_files), 50))
        self.max_patch_chars = max(0, min(int(max_patch_chars), 5000))
        self.max_total_patch_chars = max(0, min(int(max_total_patch_chars), 30000))
        self.max_response_bytes = max(1024, min(int(max_response_bytes), 5_000_000))
        self._insight_loader = insight_loader
        self._cache = {"repos": None, "ts": 0.0}
        self._state = "idle"
        self._banner: str | None = None
        self._version = 0
        self._last_attempt = 0.0
        self._thread: threading.Thread | None = None
        self._forced_repos: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def token() -> str:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            return token
        token_file = os.environ.get("GITHUB_TOKEN_FILE", "").strip()
        if not token_file:
            return ""
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def request(self, url: str):
        token = self.token()
        if not token:
            return None
        request = urllib.request.Request(url)
        request.add_header("Authorization", f"token {token}")
        request.add_header("Accept", "application/vnd.github+json")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    return None
                return json.loads(body.decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, TypeError):
            return None

    def fetch_all_repos(self) -> list[dict] | None:
        if not self.token():
            return None
        repos = []
        for page in range(1, 11):
            data = self.request(
                f"{self.api_url}/user/repos?type=owner&per_page=100"
                f"&sort=pushed&direction=desc&page={page}"
            )
            if not isinstance(data, list):
                return None
            repos.extend(data)
            if len(data) < 100:
                return repos
        return repos

    def set_insight_loader(self, loader: Callable[[], dict] | None) -> None:
        self._insight_loader = loader

    def _known_insights(self) -> dict:
        if self._insight_loader is None:
            return {}
        try:
            value = self._insight_loader()
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _sha(value) -> str:
        return value.strip()[:64] if isinstance(value, str) else ""

    def _extract_changes(self, data: dict, fallback_sha: str = "") -> dict | None:
        if not isinstance(data, dict):
            return None
        files = data.get("files") if isinstance(data.get("files"), list) else []
        head_commit = data.get("head_commit") if isinstance(data.get("head_commit"), dict) else {}
        commits = data.get("commits") if isinstance(data.get("commits"), list) else []
        head_sha = self._sha(head_commit.get("sha")) or self._sha(data.get("sha"))
        if not head_sha and commits:
            last = commits[-1] if isinstance(commits[-1], dict) else {}
            head_sha = self._sha(last.get("sha"))
        head_sha = head_sha or self._sha(fallback_sha)

        changed_files = []
        prompt_files = []
        patch_budget = self.max_total_patch_chars
        for item in files[:self.max_files]:
            if not isinstance(item, dict):
                continue
            path = self._text(item.get("filename"))[:500]
            if not path:
                continue
            status = self._text(item.get("status")).lower()
            if status not in {"added", "modified", "removed", "renamed"}:
                status = "modified"
            try:
                additions = max(0, int(item.get("additions", 0)))
            except (TypeError, ValueError, OverflowError):
                additions = 0
            try:
                deletions = max(0, int(item.get("deletions", 0)))
            except (TypeError, ValueError, OverflowError):
                deletions = 0
            safe = {
                "path": path,
                "status": status,
                "additions": additions,
                "deletions": deletions,
            }
            changed_files.append(safe)
            patch = item.get("patch") if isinstance(item.get("patch"), str) else ""
            patch = patch[: min(self.max_patch_chars, patch_budget)]
            patch_budget -= len(patch)
            prompt_files.append({**safe, "patch": patch})

        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        try:
            additions = max(0, int(stats.get("additions", 0)))
        except (TypeError, ValueError, OverflowError):
            additions = sum(item["additions"] for item in changed_files)
        try:
            deletions = max(0, int(stats.get("deletions", 0)))
        except (TypeError, ValueError, OverflowError):
            deletions = sum(item["deletions"] for item in changed_files)
        if not additions and changed_files:
            additions = sum(item["additions"] for item in changed_files)
        if not deletions and changed_files:
            deletions = sum(item["deletions"] for item in changed_files)
        return {
            "head_sha": head_sha,
            "changed_files": changed_files,
            "additions": additions,
            "deletions": deletions,
            "prompt_files": prompt_files,
        }

    def _latest_commit_changes(self, owner: str, repo: str, branch: str) -> dict | None:
        safe_branch = urllib.parse.quote(branch or "main", safe="")
        data = self.request(f"{self.api_url}/repos/{owner}/{repo}/commits/{safe_branch}")
        return self._extract_changes(data) if isinstance(data, dict) else None

    def fetch_change_context(
        self, owner: str, repo: str, branch: str, previous_sha: str = ""
    ) -> dict | None:
        """Fetch bounded file changes without reading or returning commit messages."""
        safe_branch = urllib.parse.quote(branch or "main", safe="")
        if previous_sha:
            safe_base = urllib.parse.quote(previous_sha, safe="")
            compare = self.request(
                f"{self.api_url}/repos/{owner}/{repo}/compare/{safe_base}...{safe_branch}"
            )
            extracted = self._extract_changes(compare) if isinstance(compare, dict) else None
            return extracted or self._latest_commit_changes(owner, repo, branch)

        commits = self.request(
            f"{self.api_url}/repos/{owner}/{repo}/commits?sha={safe_branch}&per_page=6"
        )
        identities = [
            self._sha(item.get("sha")) for item in commits
            if isinstance(item, dict) and self._sha(item.get("sha"))
        ] if isinstance(commits, list) else []
        if len(identities) >= 2:
            safe_base = urllib.parse.quote(identities[-1], safe="")
            safe_head = urllib.parse.quote(identities[0], safe="")
            compare = self.request(
                f"{self.api_url}/repos/{owner}/{repo}/compare/{safe_base}...{safe_head}"
            )
            extracted = self._extract_changes(compare, fallback_sha=identities[0])
            if extracted:
                return extracted
        return self._latest_commit_changes(owner, repo, branch)

    @staticmethod
    def classify_recency(pushed_at: str, now: datetime | None = None) -> str:
        if not isinstance(pushed_at, str) or not pushed_at:
            return "stalled"
        try:
            pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            if pushed.tzinfo is None:
                pushed = pushed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return "stalled"
        delta = (now or datetime.now(timezone.utc)) - pushed
        if delta.days < 7:
            return "active"
        if delta.days < 30:
            return "maintain"
        return "stalled"

    @staticmethod
    def _text(value) -> str:
        return value.strip() if isinstance(value, str) else ""

    def _enrich_repo(self, repo: dict, known: dict | None = None) -> dict | None:
        if not isinstance(repo, dict):
            return None
        full_name = self._text(repo.get("full_name"))
        owner, separator, name = full_name.partition("/")
        if not separator or not owner or not name:
            return None
        pushed_at = self._text(repo.get("pushed_at"))
        branch = self._text(repo.get("default_branch")) or "main"
        known = known if isinstance(known, dict) else {}
        previous_sha = self._sha(known.get("head_sha"))
        previous_pushed_at = self._text(known.get("source_pushed_at"))
        unchanged = bool(previous_sha and pushed_at and pushed_at == previous_pushed_at)
        changes = None
        if not unchanged:
            try:
                changes = self.fetch_change_context(owner, name, branch, previous_sha)
            except Exception:
                changes = None
        if unchanged:
            change_status = "unchanged"
        elif changes and changes.get("changed_files"):
            change_status = "ready"
        elif changes and changes.get("head_sha"):
            change_status = "no_changes"
        else:
            change_status = "unavailable"
        return {
            "full_name": full_name,
            "name": self._text(repo.get("name")) or name,
            "description": self._text(repo.get("description")),
            "language": self._text(repo.get("language")) or None,
            "html_url": self._text(repo.get("html_url")),
            "default_branch": branch,
            "pushed_at": pushed_at,
            "recency": self.classify_recency(pushed_at),
            "head_sha": previous_sha if unchanged else (changes or {}).get("head_sha", ""),
            "change_status": change_status,
            "changed_files": (
                known.get("changed_files", []) if unchanged else (changes or {}).get("changed_files", [])
            ),
            "additions": (
                known.get("additions", 0) if unchanged else (changes or {}).get("additions", 0)
            ),
            "deletions": (
                known.get("deletions", 0) if unchanged else (changes or {}).get("deletions", 0)
            ),
            "change_context": None if unchanged else changes,
        }

    def _refresh(self) -> None:
        now = time.time()
        try:
            repos = self.fetch_all_repos()
            if repos is None:
                raise RuntimeError("GitHub fetch failed")
            known_insights = self._known_insights()
            with self._lock:
                forced_repos = set(self._forced_repos)

            def enrich(repo):
                full_name = self._text(repo.get("full_name")) if isinstance(repo, dict) else ""
                known = {} if full_name in forced_repos else known_insights.get(full_name, {})
                return self._enrich_repo(repo, known)

            with ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="hub-github"
            ) as pool:
                enriched = [entry for entry in pool.map(enrich, repos) if entry]
            result = {"repos": enriched, "status": "ok", "banner": None, "ts": now}
            with self._lock:
                self._cache = {"repos": result, "ts": now}
                self._state = "ready"
                self._banner = None
                self._version += 1
                self._forced_repos.difference_update(forced_repos)
        except Exception:
            with self._lock:
                self._state = "error"
                self._banner = (
                    "GitHub unavailable — showing cached data."
                    if self._cache["repos"] is not None
                    else "GitHub unavailable — unable to load repositories."
                )
                self._version += 1
        finally:
            with self._lock:
                self._thread = None

    def _start_refresh_unlocked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._state = "refreshing"
        self._banner = "Refreshing GitHub activity…"
        self._last_attempt = time.time()
        self._thread = threading.Thread(
            target=self._refresh, name="hub-github-refresh", daemon=True
        )
        self._thread.start()

    def get_repos(self, force: bool = False) -> dict:
        """Return immediately and trigger at most one background refresh."""
        now = time.time()
        with self._lock:
            cached = self._cache["repos"]
            fresh = cached is not None and now - self._cache["ts"] < self.cache_ttl
            if not self.token():
                self._state = "error"
                self._banner = (
                    "GitHub token not configured — showing cached data."
                    if cached is not None
                    else "GitHub token not configured. Set GITHUB_TOKEN to populate the Hub."
                )
            retry_allowed = self._state != "error" or now - self._last_attempt >= self.failure_ttl
            if self.token() and (force or (not fresh and retry_allowed)):
                self._start_refresh_unlocked()

            if cached is None:
                status = "token_missing" if not self.token() else self._state
                return {
                    "repos": [], "status": status, "banner": self._banner,
                    "ts": 0.0, "state": self._state, "version": self._version,
                }
            snapshot = dict(cached)
            snapshot.update(
                status="ok" if self._state == "ready" else self._state,
                banner=self._banner,
                state=self._state,
                version=self._version,
            )
            return snapshot

    def state(self) -> dict:
        with self._lock:
            cached = self._cache["repos"]
            return {
                "state": self._state,
                "version": self._version,
                "updated_at": self._cache["ts"] or None,
                "has_data": cached is not None,
            }

    def invalidate(self) -> None:
        with self._lock:
            self._cache["ts"] = 0.0
            if self._state != "refreshing":
                self._state = "idle"
                self._banner = None

    def invalidate_repo(self, full_name: str) -> None:
        """Force one repository to rebuild an initial change window on refresh."""
        full_name = full_name.strip() if isinstance(full_name, str) else ""
        if not full_name:
            return
        with self._lock:
            self._forced_repos.add(full_name)
            self._cache["ts"] = 0.0
            if self._state != "refreshing":
                self._state = "idle"
                self._banner = None

    def wait_for_refresh(self, timeout: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            return self._thread is None


DEFAULT_CLIENT = GitHubClient()
