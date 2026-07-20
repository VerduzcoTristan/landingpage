"""Optional stdlib Ollama client and summary cache for Hub activity."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request


class OllamaClient:
    def __init__(self, cache_ttl: int = 86400):
        self.cache_ttl = cache_ttl
        self.cache: dict = {}
        self.lock = threading.RLock()

    @staticmethod
    def base_url() -> str:
        return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")

    @staticmethod
    def model() -> str:
        return os.environ.get("OLLAMA_MODEL", "qwen2.5:7b").strip()

    def summarize(self, repo: dict) -> str | None:
        commits = repo.get("commits") if isinstance(repo, dict) else []
        if not isinstance(commits, list) or not commits:
            return None
        subjects = []
        for commit in commits[:5]:
            if not isinstance(commit, dict):
                continue
            subject = commit.get("subject")
            if isinstance(subject, str) and subject.strip():
                subjects.append(f"- {subject.strip()}")
        if not subjects:
            return None
        prompt = (
            "Summarize what this project is doing recently in ONE short plain-English "
            "sentence (max 20 words), based only on these recent commit subjects. "
            "No preamble, no quotes:\n" + "\n".join(subjects)
        )
        payload = json.dumps({
            "model": self.model(), "prompt": prompt, "stream": False
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url()}/api/generate", data=payload, method="POST"
        )
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            text = data.get("response") if isinstance(data, dict) else ""
            if not isinstance(text, str):
                return None
            text = re.sub(r"\s+", " ", text).strip()
            return text[:200] or None
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, TypeError):
            return None

    def fill(self, repos: list[dict]) -> None:
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            full_name = repo.get("full_name")
            full_name = full_name.strip() if isinstance(full_name, str) else ""
            if not full_name:
                continue
            now = time.time()
            with self.lock:
                cached = self.cache.get(full_name)
                if cached and now - cached.get("ts", 0) < self.cache_ttl:
                    continue
            summary = self.summarize(repo)
            if summary:
                with self.lock:
                    self.cache[full_name] = {"summary": summary, "ts": now}

    def snapshot(self, repos: list[dict]) -> tuple[dict, list[str]]:
        now = time.time()
        summaries = {}
        pending = []
        with self.lock:
            for repo in repos:
                if not isinstance(repo, dict):
                    continue
                full_name = repo.get("full_name")
                full_name = full_name.strip() if isinstance(full_name, str) else ""
                if not full_name:
                    continue
                cached = self.cache.get(full_name)
                if cached and now - cached.get("ts", 0) < self.cache_ttl:
                    summaries[full_name] = cached.get("summary")
                else:
                    summaries[full_name] = None
                    pending.append(full_name)
        return summaries, pending

    def invalidate(self) -> None:
        with self.lock:
            self.cache.clear()


DEFAULT_CLIENT = OllamaClient()
