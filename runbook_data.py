"""Runbook data and page rendering for devmclovin.com."""

import html as html_mod

# ── Runbook entries ──

RUNBOOK_ENTRIES = [
    # ── Services (4) ──
    {
        "title": "Restart Cloudflare Tunnel",
        "command": "sudo systemctl restart cloudflared",
        "description": "Restart the Cloudflare tunnel service after config changes or connection drops.",
        "category": "services",
    },
    {
        "title": "Check HTTP Server Status",
        "command": "systemctl status hermes-landing --no-pager",
        "description": "Check whether the devmclovin.com landing page server is running and healthy.",
        "category": "services",
    },
    {
        "title": "Restart Hermes Agent",
        "command": "sudo systemctl restart hermes-agent",
        "description": "Restart the Hermes AI agent service after a crash, freeze, or config update.",
        "category": "services",
    },
    {
        "title": "Check Docker Container Status",
        "command": "docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'",
        "description": "List all Docker containers and their current status — look for Exited or unhealthy states.",
        "category": "services",
    },
    # ── Troubleshooting (4) ──
    {
        "title": "Debug High CPU Usage",
        "command": "top -b -n 1 | head -20",
        "description": "Identify processes consuming the most CPU when the server feels sluggish.",
        "category": "troubleshooting",
    },
    {
        "title": "Find Large Files",
        "command": "du -ah / 2>/dev/null | sort -rh | head -20",
        "description": "Locate the largest files and directories eating up disk space. Run from target mount point.",
        "category": "troubleshooting",
    },
    {
        "title": "Check DNS Resolution",
        "command": "dig devmclovin.com +short && dig puzzlelabs.app +short",
        "description": "Verify DNS is resolving correctly for both domains. Should return Cloudflare proxy IPs.",
        "category": "troubleshooting",
    },
    {
        "title": "Test Internet Connectivity",
        "command": "curl -s -o /dev/null -w '%{http_code}' https://1.1.1.1 && ping -c 3 1.1.1.1",
        "description": "Quick connectivity check — HTTP and ICMP to Cloudflare DNS. 200 + 0% loss = healthy.",
        "category": "troubleshooting",
    },
    # ── Maintenance (3) ──
    {
        "title": "Update System Packages",
        "command": "sudo apt update && sudo apt upgrade -y",
        "description": "Refresh package index and install available updates. Review held-back packages afterward.",
        "category": "maintenance",
    },
    {
        "title": "Clean Package Cache",
        "command": "sudo apt clean && sudo journalctl --vacuum-time=7d",
        "description": "Free disk space by clearing the APT cache and trimming systemd journals older than 7 days.",
        "category": "maintenance",
    },
    {
        "title": "Check Disk Health (SMART)",
        "command": "sudo smartctl -a /dev/sda 2>/dev/null || sudo smartctl -a /dev/nvme0",
        "description": "Inspect S.M.A.R.T. data for the primary disk — look for Reallocated_Sector_Ct and temperature.",
        "category": "maintenance",
    },
    # ── Hardware (3) ──
    {
        "title": "Check GPU Status",
        "command": "nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv",
        "description": "Report GPU model, temperature, utilization, and VRAM usage. Useful before starting ML workloads.",
        "category": "hardware",
    },
    {
        "title": "Check CPU Temperature",
        "command": "sensors 2>/dev/null || cat /sys/class/thermal/thermal_zone*/temp",
        "description": "Read CPU / motherboard temperature sensors. High temps (>80°C) may indicate cooling issues.",
        "category": "hardware",
    },
    {
        "title": "Check Memory Usage",
        "command": "free -h && echo '---' && vmstat -s | head -10",
        "description": "Show RAM and swap usage plus top-level VM stats. Look for high swap usage as a warning sign.",
        "category": "hardware",
    },
]


def runbooks_page() -> str:
    """Render the runbooks page as a full HTML document."""
    entries = RUNBOOK_ENTRIES

    # Count per category for filter pills
    counts = {}
    for entry in entries:
        cat = entry["category"]
        counts[cat] = counts.get(cat, 0) + 1

    # ── Category filter pills ──
    pills = '<button class="runbook-nav-cat active" data-cat="all" onclick="filterRunbookCategory(\'all\')">All <span class="count">({})</span></button>'.format(
        len(entries)
    )
    for cat in ["services", "troubleshooting", "maintenance", "hardware"]:
        n = counts.get(cat, 0)
        pills += '<button class="runbook-nav-cat" data-cat="{}" onclick="filterRunbookCategory(\'{}\')">{} <span class="count">({})</span></button>'.format(
            cat, cat, cat, n
        )

    # ── Entry cards ──
    cards = ""
    for entry in entries:
        title = html_mod.escape(entry["title"])
        desc = html_mod.escape(entry["description"])
        cmd = entry["command"]
        cmd_escaped = html_mod.escape(cmd)
        cmd_attr = html_mod.escape(cmd, quote=True)
        cat = entry["category"]
        cards += (
            '<div class="runbook-entry" data-category="{}">'
            "<h3>{}</h3>"
            "<p>{}</p>"
            "<pre>{}</pre>"
            '<button class="runbook-copy-btn" data-command="{}" aria-label="Copy command to clipboard" onclick="copyRunbookCommand(this)">📋 Copy</button>'
            "</div>"
        ).format(cat, title, desc, cmd_escaped, cmd_attr)

    # ── JavaScript ──
    js = """<script>
function copyRunbookCommand(btn) {
    var cmd = btn.getAttribute('data-command');
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cmd).then(function() {
            btn.textContent = '✓ Copied!';
            setTimeout(function() { btn.textContent = '📋 Copy'; }, 2000);
        }).catch(function() {
            _fallbackCopy(btn, cmd);
        });
    } else {
        _fallbackCopy(btn, cmd);
    }
}
function _fallbackCopy(btn, cmd) {
    var textarea = document.createElement('textarea');
    textarea.value = cmd;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    document.body.removeChild(textarea);
    btn.textContent = '✓ Copied!';
    setTimeout(function() { btn.textContent = '📋 Copy'; }, 2000);
}
function filterRunbookCategory(cat) {
    var pills = document.querySelectorAll('.runbook-nav-cat');
    for (var i = 0; i < pills.length; i++) {
        pills[i].classList.remove('active');
        if (pills[i].getAttribute('data-cat') === cat) {
            pills[i].classList.add('active');
        }
    }
    var entries = document.querySelectorAll('.runbook-entry');
    for (var i = 0; i < entries.length; i++) {
        if (cat === 'all' || entries[i].getAttribute('data-category') === cat) {
            entries[i].style.display = '';
        } else {
            entries[i].style.display = 'none';
        }
    }
}
</script>"""

    body = (
        '<h1 style="margin-top:0">📋 Runbooks</h1>'
        '<p style="color:var(--text-muted);margin-bottom:1.5rem">'
        "Common server commands — click to copy.</p>"
        '<div class="runbook-nav">{}</div>'
        '<div class="runbook-grid">{}</div>'
        "{}"
    ).format(pills, cards, js)

    # ── Self-contained HTML shell with dark theme ──
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        "<title>📋 Runbooks — devmclovin</title>\n"
        "<style>\n"
        ":root{--bg:#0d1117;--text:#e6edf3;--text-muted:#8b949e;"
        "--card-bg:#161b22;--border:#30363d;--accent:#7c3aed;--green:#3fb950;"
        "--warning:#d2991d;--danger:#f85149;}"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:var(--bg);color:var(--text);margin:0;padding:0}"
        "nav{display:flex;align-items:center;gap:1rem;padding:1rem 2rem;"
        "background:var(--card-bg);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;min-height:60px;box-sizing:border-box}"
        "nav a{color:var(--text-muted);text-decoration:none;font-size:0.9rem}"
        "nav a:hover,nav a.active{color:var(--text)}"
        "nav .logo{font-weight:700;font-size:1.1rem;color:var(--text)}"
        "nav .logo span{color:var(--accent)}"
        "nav .links{display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap}"
        ".nav-dropdown{position:relative;display:flex;align-items:center}.nav-more-summary{color:var(--text-muted);font-size:.9rem;cursor:pointer;padding:.5rem 0;white-space:nowrap;list-style:none;user-select:none}.nav-more-summary::-webkit-details-marker{display:none}.nav-more-summary::after{content:\" ▾\";font-size:.7rem}.nav-more-summary:hover,.nav-more-summary.active{color:var(--text)}.nav-dropdown-menu{position:absolute;top:100%;right:0;background:var(--card-bg);border:1px solid var(--border);border-radius:10px;padding:.4rem 0;min-width:210px;z-index:150;box-shadow:0 8px 30px rgba(0,0,0,.4);display:flex;flex-direction:column}.nav-dropdown-menu a{padding:.5rem 1rem!important}.nav-dropdown-menu a:hover{background:rgba(124,58,237,.1)}.nav-menu-label{padding:.35rem 1rem .2rem;color:var(--text-muted);font-size:.65rem;text-transform:uppercase;letter-spacing:.08em;opacity:.75}.hermes-btn{background:var(--accent);color:#fff!important;padding:.4rem 1rem!important;border-radius:6px;font-weight:600}"
        ".container{max-width:1200px;margin:0 auto;padding:2rem}"
        "footer{text-align:center;color:var(--text-muted);font-size:0.8rem;"
        "padding:2rem;border-top:1px solid var(--border);margin-top:3rem}"
        ".runbook-nav{display:flex;gap:0.5rem;margin-bottom:1rem;flex-wrap:wrap}"
        ".runbook-nav-cat{background:var(--card-bg);border:1px solid var(--border);"
        "color:var(--text-muted);padding:0.4rem 1rem;border-radius:20px;cursor:pointer;"
        "font-size:0.85rem;transition:all 0.15s}"
        ".runbook-nav-cat:hover{border-color:var(--accent);color:var(--text)}"
        ".runbook-nav-cat.active{background:var(--accent);color:#fff;border-color:var(--accent)}"
        ".runbook-nav-cat .count{opacity:0.7;margin-left:0.25rem}"
        ".runbook-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:1rem}"
        ".runbook-entry{background:var(--card-bg);border:1px solid var(--border);"
        "border-radius:8px;padding:1.25rem}"
        ".runbook-entry h3{color:var(--accent);margin:0 0 0.5rem 0;font-size:1rem}"
        ".runbook-entry p{color:var(--text-muted);margin:0 0 0.75rem 0;font-size:0.85rem}"
        ".runbook-entry pre{background:#0d1117;border:1px solid var(--border);"
        "border-radius:4px;padding:0.75rem;font-size:0.82rem;overflow-x:auto;"
        "color:var(--green);margin:0 0 0.75rem 0}"
        ".runbook-copy-btn{background:var(--card-bg);border:1px solid var(--border);"
        "color:var(--text-muted);padding:0.4rem 1rem;border-radius:6px;cursor:pointer;"
        "font-size:0.85rem;transition:all 0.15s}"
        ".runbook-copy-btn:hover{border-color:var(--accent);color:var(--text)}"
        "@media (max-width:480px){body{overflow-x:hidden}"
        "nav{padding:0 .75rem!important;height:auto!important;min-height:52px!important}"
        "nav .links{flex-wrap:nowrap!important;overflow-x:auto!important;-webkit-overflow-scrolling:touch;gap:.75rem!important;scrollbar-width:none}"
        "nav .links::-webkit-scrollbar{display:none}"
        "nav .links a{font-size:.8rem!important;padding:.45rem 0!important}"
        "nav .logo{font-size:1.05rem!important}"
        ".container{padding:1rem!important}"
        "button,.runbook-nav-cat,.runbook-copy-btn{min-height:44px}"
        ".runbook-nav-cat{padding:.5rem .85rem;font-size:.78rem}"
        ".runbook-grid{grid-template-columns:1fr!important}"
        ".runbook-entry pre{font-size:.75rem}"
        "h1{font-size:1.5rem!important}}"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<nav>\n"
        '<a href="/" class="logo">dev<span>mclovin</span></a>\n'
        '<div class="links">\n'
        '<a href="/">Home</a>\n'
        '<a href="/briefings">Briefings</a>\n'
        '<a href="/projects">Projects</a>\n'
        '<a href="/hermes">Hermes</a>\n'
        '<details class="nav-dropdown"><summary class="nav-more-summary active">Tools</summary><div class="nav-dropdown-menu">\n'
        '<div class="nav-menu-label">Status & workflow</div>\n'
        '<a href="/status">Status</a>\n'
        '<a href="/notes">Notes</a>\n'
        '<a href="/inbox">Inbox</a>\n'
        '<a href="/runbooks" class="active">Runbooks</a>\n'
        '</div></details>\n'
        '<details class="nav-dropdown"><summary class="nav-more-summary">System</summary><div class="nav-dropdown-menu">\n'
        '<div class="nav-menu-label">Operations</div>\n'
        '<a href="/cron">Cron Jobs</a>\n'
        '<a href="/models">Models</a>\n'
        '<a href="/disk-cleanup">Disk</a>\n'
        '<a href="/tunnel">Tunnel</a>\n'
        '<a href="/logs">Logs</a>\n'
        '</div></details>\n'
        '<a href="https://ssh.devmclovin.com" class="hermes-btn">SSH</a>\n'
        "</div>\n"
        "</nav>\n"
        '<div class="container">\n'
        + body +
        "</div>\n"
        "<script>\n"
        "(function(){function setupNavDropdowns(){var dropdowns=Array.prototype.slice.call(document.querySelectorAll('details.nav-dropdown'));dropdowns.forEach(function(dropdown){dropdown.addEventListener('toggle',function(){if(dropdown.open){dropdowns.forEach(function(other){if(other!==dropdown)other.open=false;});}});});document.addEventListener('click',function(e){if(!e.target.closest('details.nav-dropdown')){dropdowns.forEach(function(dropdown){dropdown.open=false;});}});document.addEventListener('keydown',function(e){if(e.key==='Escape'){dropdowns.forEach(function(dropdown){dropdown.open=false;});}});}if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',setupNavDropdowns);}else{setupNavDropdowns();}})();\n"
        "</script>\n"
        "<footer>\n"
        "devmclovin.com — more coming soon\n"
        "</footer>\n"
        "</body>\n"
        "</html>"
    )
