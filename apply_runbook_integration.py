#!/usr/bin/env python3
"""
Runbook integration for server.py — self-healing script.

Run this script anytime to ensure the runbooks page is integrated into
the main devmclovin.com landing page server. It applies 3 changes:

1. Import: from runbook_data import runbooks_page
2. Nav link: ("/runbooks", "Runbooks", "runbooks")
3. Route: elif path == "/runbooks": ...

Usage:
    python3 /home/hermes/devmclovin-landing/apply_runbook_integration.py

After applying, restart the devmclovin-landing service:
    systemctl --user restart devmclovin-landing

If the service can't start due to port conflicts (concurrent workers):
    systemctl --user stop devmclovin-landing
    fuser -k 3002/tcp
    systemctl --user start devmclovin-landing
"""

import sys
import os
import shutil

SERVER_PY = "/home/hermes/devmclovin-landing/server.py"
BACKUP_SUFFIX = ".bak.runbook"

def apply_changes(content: str) -> tuple[str, list[str]]:
    """Apply the 3 integration changes. Returns (new_content, log_messages)."""
    log = []
    changes = 0

    # 1. Import: after "from pathlib import Path" add "from runbook_data import runbooks_page"
    if "from runbook_data import runbooks_page" not in content:
        old = "\nfrom pathlib import Path\n"
        if old in content:
            content = content.replace(old, f"\nfrom pathlib import Path\nfrom runbook_data import runbooks_page\n", 1)
            changes += 1
            log.append("  ✓ Added runbook_data import")
        else:
            log.append("  ✗ Could not find 'from pathlib import Path' for import insertion")

    # 2. Nav link: after Hermes link
    if '("/runbooks"' not in content:
        # Try pattern with models link
        old_patterns = [
            '\n        ("/hermes", "Hermes", "hermes"),\n        ("/models", "Models", "models"),',
            '\n        ("/hermes", "Hermes", "hermes"),\n        ("https://ssh.devmclovin.com", "SSH", "ssh"),',
            '\n        ("/hermes", "Hermes", "hermes"),\n        ("/notes", "Notes", "notes"),',
        ]
        for old in old_patterns:
            if old in content:
                new = old.replace(
                    '("/hermes", "Hermes", "hermes"),',
                    '("/hermes", "Hermes", "hermes"),\n        ("/runbooks", "Runbooks", "runbooks"),'
                )
                content = content.replace(old, new, 1)
                changes += 1
                log.append("  ✓ Added Runbooks nav link")
                break
        else:
            log.append("  ✗ Could not find nav_links pattern for insertion")

    # 3. Route: after /hermes handler
    if 'path == "/runbooks"' not in content:
        old_patterns = [
            'elif path == "/hermes":\n            content = hermes_page().encode()\n            self._respond(200, "text/html", content)\n        elif path == "/kanban":',
            'elif path == "/hermes":\n            content = hermes_page().encode()\n            self._respond(200, "text/html", content)\n        elif path == "/services":',
            'elif path == "/hermes":\n            content = hermes_page().encode()\n            self._respond(200, "text/html", content)\n        elif path == "/models":',
            'elif path == "/hermes":\n            content = hermes_page().encode()\n            self._respond(200, "text/html", content)\n        elif path == "/notes":',
        ]
        for old in old_patterns:
            if old in content:
                new = (
                    'elif path == "/hermes":\n'
                    '            content = hermes_page().encode()\n'
                    '            self._respond(200, "text/html", content)\n'
                    '        elif path == "/runbooks":\n'
                    '            content = runbooks_page().encode()\n'
                    '            self._respond(200, "text/html", content)\n'
                    '        elif path == "/kanban":'
                )
                content = content.replace(old, new, 1)
                changes += 1
                log.append("  ✓ Added /runbooks route")
                break
        else:
            log.append("  ✗ Could not find route pattern for insertion")

    return content, log


def main():
    print("=== Runbook Integration ===")

    if not os.path.exists(SERVER_PY):
        print(f"ERROR: {SERVER_PY} not found")
        sys.exit(1)

    # Read current content
    with open(SERVER_PY) as f:
        content = f.read()

    # Check current state
    has_import = "from runbook_data import runbooks_page" in content
    has_nav = '("/runbooks"' in content
    has_route = 'path == "/runbooks"' in content

    if has_import and has_nav and has_route:
        print("Runbook integration already complete. Nothing to do.")
        # Still check syntax
        try:
            compile(content, SERVER_PY, "exec")
            print("Syntax: OK")
        except SyntaxError as e:
            print(f"Syntax ERROR at line {e.lineno}: {e.msg}")
        return

    print(f"State: import={'✓' if has_import else '✗'}, nav={'✓' if has_nav else '✗'}, route={'✓' if has_route else '✗'}")

    # Back up
    backup = SERVER_PY + BACKUP_SUFFIX
    shutil.copy2(SERVER_PY, backup)
    print(f"Backup: {backup}")

    # Apply changes
    new_content, log = apply_changes(content)
    for msg in log:
        print(msg)

    if new_content == content:
        print("No changes made.")
        return

    # Check syntax
    try:
        compile(new_content, SERVER_PY, "exec")
        print("Syntax: OK")
    except SyntaxError as e:
        print(f"Syntax ERROR at line {e.lineno}: {e.msg}")
        print("Aborting — backup preserved.")
        sys.exit(1)

    # Write atomically
    tmp = SERVER_PY + ".tmp"
    with open(tmp, 'w') as f:
        f.write(new_content)
    os.replace(tmp, SERVER_PY)
    print(f"✓ Integration applied to {SERVER_PY}")
    print()
    print("Next: restart the service:")
    print("  systemctl --user restart devmclovin-landing")
    print()
    print("If port 3002 is busy (concurrent workers):")
    print("  systemctl --user stop devmclovin-landing")
    print("  fuser -k 3002/tcp")
    print("  systemctl --user start devmclovin-landing")
    print()
    print("Standalone runbook server (always available):")
    print("  http://localhost:3009/runbooks")
    print("  systemctl --user status runbook-server")


if __name__ == "__main__":
    main()
