#!/usr/bin/env bash
# One-shot setup: Add devmclovin.com to Cloudflare tunnel + enable landing page service
# Run with: sudo bash /home/ubuntu/devmclovin-landing/setup.sh
set -euo pipefail

echo "=== devmclovin.com Landing Page Setup ==="
echo ""

# 1. Add devmclovin.com ingress to tunnel config
CONFIG="/etc/cloudflared/config.yml"
BACKUP="${CONFIG}.bak.$(date +%Y%m%d%H%M%S)"

if grep -q "^- hostname: devmclovin.com$" "$CONFIG"; then
    echo "✓ devmclovin.com already in tunnel config"
else
    cp "$CONFIG" "$BACKUP"
    echo "✓ Backed up config to $BACKUP"
    sed -i '/- hostname: hermes.devmclovin.com/,/http_status:404/{/http_status:404/i\  - hostname: devmclovin.com\n    service: http://localhost:3002
}' "$CONFIG"
    echo "✓ Added devmclovin.com → localhost:3002 to tunnel ingress"
fi

# 2. Install systemd service for landing page server
SERVICE_FILE="/home/ubuntu/devmclovin-landing/devmclovin-landing.service"
cp "$SERVICE_FILE" /etc/systemd/system/devmclovin-landing.service
systemctl daemon-reload
systemctl enable --now devmclovin-landing.service
echo "✓ Landing page service installed and started"

# 3. Restart tunnel (cloudflared doesn't support reload)
systemctl restart cloudflared
echo "✓ Tunnel restarted"

echo ""
echo "=== Done! ==="
echo "Visit: https://devmclovin.com"
echo "Hermes: https://hermes.devmclovin.com"
