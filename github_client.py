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


class GitHubClient:
    """Maintain a stale-while-refreshing snapshot of owned repositories."""

    def __init__(
        self,
        api_url: str = "https://api.github.com",
        cache_ttl: int = 600,
        failure_ttl: int = 60,
        max_workers: int = 4,
    ):
        self.api_url = api_url.rstrip("/")
        self.cache_ttl = cache_ttl
        self.failure_ttl = failure_ttl
        self.max_workers = max(1, min(int(max_workers), 4))
        self._cache = {"repos": None, "ts": 0.0}
        self._state = "idle"
        self._banner: str | None = None
        self._version = 0
        self._last_attempt = 0.0
        self._thread: threading.Thread | None = None
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
                return json.loads(response.read().decode("utf-8"))
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

    def fetch_recent_commits(self, owner: str, repo: str, branch: str) -> list[dict]:
        safe_branch = urllib.parse.quote(branch or "main", safe="")
        data = self.request(
            f"{self.api_url}/repos/{owner}/{repo}/commits?sha={safe_branch}&per_page=5"
        )
        if not isinstance(data, list):
            return []
        commits = []
        for item in data[:5]:
            if not isinstance(item, dict):
                continue
            commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
            message = commit.get("message") if isinstance(commit.get("message"), str) else ""
            sha = item.get("sha") if isinstance(item.get("sha"), str) else ""
            commits.append({
                "sha": sha[:8],
                "subject": message.split("\n", 1)[0].strip(),
                "body": " ".join(
                    line.strip() for line in message.split("\n")[1:] if line.strip()
                )[:400],
            })
        return commits

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

    def _enrich_repo(self, repo: dict) -> dict | None:
        if not isinstance(repo, dict):
            return None
        full_name = self._text(repo.get("full_name"))
        owner, separator, name = full_name.partition("/")
        if not separator or not owner or not name:
            return None
        pushed_at = self._text(repo.get("pushed_at"))
        branch = self._text(repo.get("default_branch")) or "main"
        try:
            commits = self.fetch_recent_commits(owner, name, branch)
        except Exception:
            commits = []
        return {
            "full_name": full_name,
            "name": self._text(repo.get("name")) or name,
            "description": self._text(repo.get("description")),
            "language": self._text(repo.get("language")) or None,
            "html_url": self._text(repo.get("html_url")),
            "default_branch": branch,
            "pushed_at": pushed_at,
            "recency": self.classify_recency(pushed_at),
            "commits": commits,
        }

    def _refresh(self) -> None:
        now = time.time()
        try:
            repos = self.fetch_all_repos()
            if repos is None:
                raise RuntimeError("GitHub fetch failed")
            with ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="hub-github"
            ) as pool:
                enriched = [entry for entry in pool.map(self._enrich_repo, repos) if entry]
            result = {"repos": enriched, "status": "ok", "banner": None, "ts": now}
            with self._lock:
                self._cache = {"repos": result, "ts": now}
                self._state = "ready"
                self._banner = None
                self._version += 1
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

    def wait_for_refresh(self, timeout: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            return self._thread is None


DEFAULT_CLIENT = GitHubClient()
