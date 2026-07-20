#!/usr/bin/env python3
"""Small HTTP smoke test for the Control Center route surface."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


# Retained routes are regression checks; removed surfaces must stay gone.
ROUTES = {
    "/": 200,
    "/briefings": 200,
    "/briefing/1970-01-01": 200,
    "/status": 200,
    "/api/status": 200,
    "/hub": 200,
    "/api/hub/state": 200,
    "/api/hub/insights": 200,
    "/api/hub/summaries": 404,
    # Localhost is intentionally authenticated by the server's development bypass.
    "/hub/admin": 200,
    "/hub/admin/delete": 404,
    "/health": 200,
    "/projects": 404,
    "/projects/admin": 404,
    "/projects/admin/update": 404,
    "/portfolio": 404,
    "/notes": 404,
    "/inbox": 404,
    "/models": 404,
    "/model-tuning": 404,
    "/llm-lab": 404,
    "/hermes": 404,
    "/cron": 404,
    "/kanban": 404,
    "/tunnel": 404,
    "/logs": 404,
    "/disk-cleanup": 404,
    "/runbooks": 404,
    "/bookmarks": 404,
    "/api/briefings/search": 404,
    "/models.js": 404,
}


def fetch(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "control-center-smoke/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def post(url: str, values: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode("utf-8"),
        headers={"User-Agent": "control-center-smoke/1",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", type=int, default=3102)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    failures: list[str] = []
    base_url = f"http://{args.host}:{args.port}"
    for path, expected in ROUTES.items():
        try:
            status, body = fetch(base_url + path, args.timeout)
        except Exception as error:
            failures.append(f"{path}: request failed: {error}")
            print(f"FAIL {path} request failed: {error}")
            continue

        problems: list[str] = []
        if status != expected:
            problems.append(f"expected {expected}, got {status}")
        legacy_brand = b"dev" + b"mclovin"
        if status == 200 and legacy_brand in body.lower():
            problems.append("200 response contains legacy brand")
        if status == 200 and (b">None<" in body or b">None</" in body):
            problems.append("200 response renders a Python None value")
        if path == "/api/hub/state" and status == 200:
            try:
                state = json.loads(body)
                if state.get("state") not in {"idle", "refreshing", "ready", "error"}:
                    problems.append("invalid Hub refresh state")
            except (ValueError, TypeError):
                problems.append("Hub state is not valid JSON")
        if path == "/api/hub/insights" and status == 200:
            try:
                insights = json.loads(body)
                if set(insights) != {"insights", "states", "pending"}:
                    problems.append("unexpected Projects insight response shape")
            except (ValueError, TypeError):
                problems.append("Projects insights are not valid JSON")
        if path == "/" and status == 200:
            for marker in (b"Today's Briefing", b"Monitoring", b"Focus projects"):
                if marker not in body:
                    problems.append(f"homepage missing {marker.decode()}")
        if path == "/hub" and status == 200:
            if b"<h1>Projects</h1>" not in body:
                problems.append("Projects heading missing")
            if (b'data-hub-filter="focus"' not in body and b"No projects yet" not in body
                    and b"Loading GitHub activity" not in body):
                problems.append("Projects Focus filter missing")
        if path == "/hub/admin" and status == 200:
            markers = [b"Manage Projects", b'name="csrf_token"']
            if b"No projects to curate yet" not in body:
                markers.append(b'id="admin-repo-search"')
            for marker in markers:
                if marker not in body:
                    problems.append(f"Projects admin missing {marker.decode()}")

        if problems:
            failures.append(f"{path}: {'; '.join(problems)}")
            print(f"FAIL {path} {'; '.join(problems)}")
        else:
            print(f"PASS {path} {status}")

    for path in ("/hub/admin/update", "/hub/admin/delete",
                 "/hub/admin/refresh", "/hub/admin/regenerate", "/hub/admin/backup"):
        try:
            status, _ = post(base_url + path, {"csrf_token": "invalid", "full_name": "smoke/check"},
                             args.timeout)
        except Exception as error:
            failures.append(f"POST {path}: request failed: {error}")
            print(f"FAIL POST {path} request failed: {error}")
            continue
        if status != 403:
            failures.append(f"POST {path}: expected CSRF rejection 403, got {status}")
            print(f"FAIL POST {path} expected CSRF rejection 403, got {status}")
        else:
            print(f"PASS POST {path} 403 invalid CSRF")

    if failures:
        print(f"\n{len(failures)} smoke check(s) failed", file=sys.stderr)
        return 1
    print(f"\n{len(ROUTES) + 5} smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
