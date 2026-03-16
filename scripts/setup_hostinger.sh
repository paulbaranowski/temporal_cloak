#!/usr/bin/env bash
#
# Full server setup for TemporalCloak on a fresh Hostinger VPS (Ubuntu 24.04).
# Run as root: sudo bash scripts/setup_hostinger.sh temporalcloak.cloud
#
# Prerequisites:
#   - Fresh Ubuntu 24.04 VPS
#   - DNS A record for the domain already pointing at this server's IP
#   - Running as root (or with sudo)
#
# What this script does:
#   1. Installs system packages (Python 3.13, sqlite3, certbot)
#   2. Configures firewall (SSH, HTTP, HTTPS only)
#   3. Creates the temporalcloak user
#   4. Installs uv and clones the repo as that user
#   5. Obtains TLS certificate via Let's Encrypt
#   6. Grants cert access to the temporalcloak user
#   7. Installs the systemd service
#   8. Starts the server and runs a health check

set -euo pipefail

DOMAIN="${1:-}"
REPO_URL="https://github.com/paulbaranowski/TemporalCloak.git"
APP_DIR="/home/temporalcloak/app"
SERVICE_FILE="/etc/systemd/system/temporalcloak.service"

if [ -z "$DOMAIN" ]; then
    echo "Usage: sudo bash $0 <domain>"
    echo "Example: sudo bash $0 temporalcloak.cloud"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: this script must be run as root"
    exit 1
fi

echo "========================================="
echo "Setting up TemporalCloak for: $DOMAIN"
echo "========================================="

# -----------------------------------------------
# 1. System packages
# -----------------------------------------------
echo ""
echo "--- Installing system packages ---"
apt update && apt upgrade -y
add-apt-repository ppa:deadsnakes/ppa -y
apt install -y python3.13 python3.13-venv python3.13-dev sqlite3 certbot

# Install DuckDB CLI for the temporalcloak user
su - temporalcloak -c 'curl -fsSL https://install.duckdb.org | sh' || echo "DuckDB install failed, continuing..."

# -----------------------------------------------
# 2. Firewall
# -----------------------------------------------
echo ""
echo "--- Configuring firewall ---"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# -----------------------------------------------
# 3. Create app user
# -----------------------------------------------
echo ""
echo "--- Creating temporalcloak user ---"
if id temporalcloak &>/dev/null; then
    echo "User temporalcloak already exists, skipping"
else
    useradd -m -s /bin/bash temporalcloak
fi

# -----------------------------------------------
# 4. Install uv and clone repo as temporalcloak user
# -----------------------------------------------
echo ""
echo "--- Installing uv for temporalcloak user ---"
su - temporalcloak -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

echo ""
echo "--- Cloning repo and installing dependencies ---"
if [ -d "$APP_DIR" ]; then
    echo "App directory already exists, pulling latest"
    su - temporalcloak -c "cd $APP_DIR && git pull origin main"
else
    su - temporalcloak -c "git clone $REPO_URL $APP_DIR"
fi
su - temporalcloak -c "cd $APP_DIR && ~/.local/bin/uv sync"

# -----------------------------------------------
# 5. TLS certificate via Let's Encrypt
# -----------------------------------------------
echo ""
echo "--- Obtaining TLS certificate for $DOMAIN ---"
certbot certonly --standalone --non-interactive --agree-tos \
    --register-unsafely-without-email -d "$DOMAIN"

# -----------------------------------------------
# 6. Grant cert access to temporalcloak user
# -----------------------------------------------
echo ""
echo "--- Granting certificate access ---"
chmod 750 /etc/letsencrypt/live/
chmod 750 /etc/letsencrypt/archive/
chmod 750 "/etc/letsencrypt/live/$DOMAIN/"
chmod 750 "/etc/letsencrypt/archive/$DOMAIN/"

chgrp temporalcloak "/etc/letsencrypt/live/$DOMAIN/"
chgrp temporalcloak "/etc/letsencrypt/archive/$DOMAIN/"
chgrp temporalcloak "/etc/letsencrypt/archive/$DOMAIN/"*
chmod 640 "/etc/letsencrypt/archive/$DOMAIN/privkey"*.pem

# Auto-renewal hook to restart the service after cert renewal
mkdir -p /etc/letsencrypt/renewal-hooks/post
cat > /etc/letsencrypt/renewal-hooks/post/restart-temporalcloak.sh << 'HOOK'
#!/bin/bash
systemctl restart temporalcloak
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/post/restart-temporalcloak.sh

# -----------------------------------------------
# 7. Systemd service
# -----------------------------------------------
echo ""
echo "--- Installing systemd service ---"
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=TemporalCloak Steganography Server
After=network.target

[Service]
Type=simple
User=temporalcloak
WorkingDirectory=$APP_DIR
Environment=TC_HOST=0.0.0.0
Environment=TC_PORT=443
Environment=TC_TLS_CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
Environment=TC_TLS_KEY=/etc/letsencrypt/live/$DOMAIN/privkey.pem
Environment=TC_BIT_1_DELAY=0.05
Environment=TC_BIT_0_DELAY=0.30
Environment=TC_MIDPOINT=0.175
ExecStart=/home/temporalcloak/.local/bin/uv run python demos/temporal_cloak_web.py
Restart=always
RestartSec=5

# Allow binding to port 443 without root
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable temporalcloak
systemctl start temporalcloak

# -----------------------------------------------
# 8. Health check
# -----------------------------------------------
echo ""
echo "--- Waiting for server to start ---"
sleep 3

if curl -sf -k "https://localhost/api/health" > /dev/null 2>&1; then
    echo "Health check passed!"
    curl -sk "https://localhost/api/health"
    echo ""
else
    echo "WARNING: Health check failed. Check logs with:"
    echo "  journalctl -u temporalcloak -e --no-pager -n 30"
fi

echo ""
echo "========================================="
echo "Setup complete!"
echo "  Server: https://$DOMAIN"
echo "  Health: https://$DOMAIN/api/health"
echo "  Logs:   journalctl -u temporalcloak -f"
echo "  DB:     sqlite3 $APP_DIR/data/links.db"
echo "========================================="
