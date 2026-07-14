#!/usr/bin/env python3
"""Small HTTP smoke test for the Control Center route surface."""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request


# This is the pre-overhaul baseline. Each removal step updates the matching
# expected status from 200 to 404; surviving routes remain regression checks.
ROUTES = {
    "/": 200,
    "/briefings": 200,
    "/briefing/1970-01-01": 200,
    "/status": 200,
    "/projects": 200,
    "/portfolio": 200,
    "/health": 200,
    "/notes": 404,
    "/inbox": 404,
    "/models": 404,
    "/model-tuning": 404,
    "/llm-lab": 404,
    "/hermes": 200,
    "/cron": 200,
    "/tunnel": 200,
    "/logs": 200,
    "/disk-cleanup": 200,
    "/runbooks": 200,
    "/bookmarks": 200,
    "/api/briefings/search": 200,
}


def fetch(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "control-center-smoke/1"})
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
    parser.add_argument(
        "--allow-legacy-brand",
        action="store_true",
        help="baseline-only escape hatch until the rebrand step removes legacy copy",
    )
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
        if status == 200 and b"devmclovin" in body.lower():
            if args.allow_legacy_brand:
                print(f"WARN {path} contains legacy brand")
            else:
                problems.append("200 response contains legacy brand")

        if problems:
            failures.append(f"{path}: {'; '.join(problems)}")
            print(f"FAIL {path} {'; '.join(problems)}")
        else:
            print(f"PASS {path} {status}")

    if failures:
        print(f"\n{len(failures)} smoke check(s) failed", file=sys.stderr)
        return 1
    print(f"\n{len(ROUTES)} smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
