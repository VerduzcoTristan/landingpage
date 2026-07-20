"""Small stdlib GitHub client for Hub repository and activity data."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


class GitHubClient:
    def __init__(self, api_url: str = "https://api.github.com", cache_ttl: int = 600):
        self.api_url = api_url.rstrip("/")
        self.cache_ttl = cache_ttl
        self._cache = {"repos": None, "ts": 0.0}
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
            url = (
                f"{self.api_url}/user/repos?type=owner&per_page=100"
                f"&sort=pushed&direction=desc&page={page}"
            )
            data = self.request(url)
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
            commits.append({
                "sha": item.get("sha", "")[:8] if isinstance(item.get("sha"), str) else "",
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

    def _stale_or_error(self, status: str, banner: str, now: float) -> dict:
        with self._lock:
            cached = self._cache["repos"]
        if cached is not None:
            stale = dict(cached)
            stale.update(status=status, banner=banner)
            return stale
        return {"repos": [], "status": status, "banner": banner, "ts": now}

    def get_repos(self, force: bool = False) -> dict:
        now = time.time()
        with self._lock:
            cached = self._cache["repos"]
            fresh = cached is not None and now - self._cache["ts"] < self.cache_ttl
        if fresh and not force:
            return cached
        if not self.token():
            with self._lock:
                has_cache = self._cache["repos"] is not None
            return self._stale_or_error(
                "token_missing",
                (
                    "GitHub token not configured — showing cached data."
                    if has_cache
                    else "GitHub token not configured. Set GITHUB_TOKEN to populate the Hub."
                ),
                now,
            )
        repos = self.fetch_all_repos()
        if repos is None:
            return self._stale_or_error(
                "error", "GitHub unavailable — showing cached data.", now
            )
        enriched = []
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            full_name = self._text(repo.get("full_name"))
            if not full_name:
                continue
            owner, separator, name = full_name.partition("/")
            if not separator or not owner or not name:
                continue
            pushed_at = self._text(repo.get("pushed_at"))
            branch = self._text(repo.get("default_branch")) or "main"
            enriched.append({
                "full_name": full_name,
                "name": self._text(repo.get("name")) or name,
                "description": self._text(repo.get("description")),
                "language": self._text(repo.get("language")) or None,
                "html_url": self._text(repo.get("html_url")),
                "default_branch": branch,
                "pushed_at": pushed_at,
                "recency": self.classify_recency(pushed_at),
                "commits": self.fetch_recent_commits(owner, name, branch),
            })
        result = {"repos": enriched, "status": "ok", "banner": None, "ts": now}
        with self._lock:
            self._cache = {"repos": result, "ts": now}
        return result

    def invalidate(self) -> None:
        with self._lock:
            self._cache = {"repos": None, "ts": 0.0}


DEFAULT_CLIENT = GitHubClient()
