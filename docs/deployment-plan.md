# TemporalCloak — Hostinger VPS Deployment Plan

## Overview

Deploy TemporalCloak's Demo 2 (HTTP steganography) as a public web service on a Hostinger KVM VPS. Visitors hit a web page, click "try it," and receive an image with a secret quote hidden in the chunk timing. A companion client script decodes the hidden message.

Demo 1 (raw TCP) is not suitable for public internet deployment — firewalls block non-standard ports and raw TCP timing is unreliable over long distances.

### Cost

| Item | Cost |
|------|------|
| Hostinger KVM 1 VPS (1 vCPU, 4GB RAM, 50GB SSD) | ~$5-7/mo |
| Domain (optional, via Hostinger) | ~$1/mo |
| Let's Encrypt TLS | Free |

---

## 1. Code Changes (Done)

All local code changes are complete and tested (53 tests passing).

### 1.1 `config.py` — Centralized Deployment Config

All deployment settings read from environment variables with sensible defaults. Production values are documented as comments at the bottom of the file. See `config.py` for the full reference.

| Env Var | Default | Production |
|---------|---------|------------|
| `TC_HOST` | `0.0.0.0` | `0.0.0.0` |
| `TC_PORT` | `8888` | `443` |
| `TC_TLS_CERT` | _(empty)_ | `/etc/letsencrypt/live/temporalcloak.cloud/fullchain.pem` |
| `TC_TLS_KEY` | _(empty)_ | `/etc/letsencrypt/live/temporalcloak.cloud/privkey.pem` |
| `TC_BIT_1_DELAY` | `0.00` | `0.05` |
| `TC_BIT_0_DELAY` | `0.10` | `0.30` |
| `TC_MIDPOINT` | `0.05` | `0.175` |

When `TC_TLS_CERT` and `TC_TLS_KEY` are set, the server automatically enables HTTPS and starts an HTTP→HTTPS redirect on port 80. When unset, it runs plain HTTP (for local dev).

### 1.2 `temporal_cloak/const.py` — Configurable Timing

Timing constants now read from `TC_BIT_1_DELAY`, `TC_BIT_0_DELAY`, and `TC_MIDPOINT` environment variables. Localhost defaults are unchanged so local demos and tests work without any env vars.

**Internet timing rationale:** Jitter is typically 10-50ms. A 250ms gap between bit values (50ms vs 300ms) provides enough margin for the adaptive threshold. These values should be validated after deployment (see Section 5).

### 1.3 `demos/temporal_cloak_web.py` — Production Server

Rewritten with:
- **Routes:** `GET /api/image` (steganography), `GET /api/health` (monitoring), `GET /` (static landing page via `StaticFileHandler`)
- **TLS:** Automatic when `TC_TLS_CERT`/`TC_TLS_KEY` are set (Tornado-native `ssl_options`)
- **HTTP→HTTPS redirect** on port 80 when TLS is active
- **Absolute paths** via `config.py` (no relative path issues under systemd)
- **Logging** via Python `logging` module (replaces `print()`)
- **Startup banner** logs bound address, port, and active timing constants

### 1.4 CLI Decoder

The `temporal-cloak` CLI replaces the old `temporal_cloak_cli_decoder.py` script.

```bash
# Local
uv run temporal-cloak decode http://localhost:8888/api/image

# Production
uv run temporal-cloak decode https://temporalcloak.cloud/api/image
```

### 1.5 Quotes Encoding

- `content/quotes/quotes.json` re-encoded from Windows-1252 to UTF-8
- `QuoteProvider` updated to read `encoding="utf-8"`

### 1.6 `static/index.html` — Placeholder Landing Page

Minimal placeholder served at `/`. Full design to be done separately.

---

## 2. Server Architecture

```
Internet                           Hostinger VPS
────────                           ─────────────────────────────────
Browser ──── HTTPS (443) ────────► Tornado (direct, no nginx)
                                     ├── GET /           → landing page (static HTML)
                                     ├── GET /api/image  → steganography endpoint
                                     │                     (streams image with timing)
                                     └── GET /api/health → health check

Client CLI ──── HTTPS (443) ───► Tornado
  (temporal-cloak decode)                    └── GET /api/image  → decode timing from chunks
```

### Why No Nginx/Reverse Proxy

Nginx and other reverse proxies **buffer HTTP responses** by default. Even with `proxy_buffering off`, they can introduce variable latency between chunks. Since TemporalCloak's entire mechanism depends on precise inter-chunk timing, the Tornado server must face the internet directly.

This means Tornado handles TLS termination itself (via `ssl_options`).

### Why Not Demo 1

Demo 1 uses raw TCP on port 1234 via `TemporalCloakServer` and `TemporalCloakClient`. Problems for public deployment:
- Many corporate/school firewalls block non-standard ports
- Raw TCP timing is more variable than HTTP chunked responses over the internet
- No browser-based client possible (browsers can't open raw TCP sockets)
- No TLS wrapper without extra tooling (stunnel, etc.)

---

## 3. Hostinger VPS Setup

### 3.1 Provision the VPS

- **Plan:** KVM 1 (cheapest tier — more than enough)
- **OS:** Ubuntu 24.04 LTS
- **Region:** Pick closest to your primary audience (US/EU)
- **Access:** SSH key authentication (disable password login)

### 3.2 Initial Server Configuration

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.13+ (Ubuntu 24.04 ships 3.12, so use deadsnakes PPA)
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt install python3.13 python3.13-venv python3.13-dev -y

# Install uv (project uses uv, not pip)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install sqlite3 CLI (needed for querying the links database)
sudo apt install sqlite3 -y

# Install DuckDB CLI
# DuckDB can also query SQLite files directly: duckdb -c "SELECT * FROM sqlite_scan('path/to/db', 'table')"
curl -fsSL https://install.duckdb.org | sh

# Install certbot for TLS
sudo apt install certbot -y

# Firewall — only allow SSH, HTTP, HTTPS
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

### 3.3 Deploy the Code

```bash
# Create app user
sudo useradd -m -s /bin/bash temporalcloak
sudo su - temporalcloak

# Clone the repo
git clone https://github.com/paulbaranowski/TemporalCloak.git ~/app
cd ~/app

# Install dependencies
uv sync
```

---

## 4. TLS Setup (Let's Encrypt)

### 4.1 Get a Certificate

```bash
# Get cert (standalone mode — certbot runs its own temp server on port 80)
sudo certbot certonly --standalone -d temporalcloak.cloud

# Certs land in /etc/letsencrypt/live/temporalcloak.cloud/
#   fullchain.pem  — certificate + intermediates
#   privkey.pem    — private key
```

### 4.2 Grant Certificate Access to the App User

Let's Encrypt certs are owned by root. The `temporalcloak` user needs read access to load them at startup:

```bash
# Allow traversal into the live/ and archive/ directories
sudo chmod 750 /etc/letsencrypt/live/
sudo chmod 750 /etc/letsencrypt/archive/
sudo chmod 750 /etc/letsencrypt/live/temporalcloak.cloud/
sudo chmod 750 /etc/letsencrypt/archive/temporalcloak.cloud/

# Grant group read on the private key (the most restrictive file)
sudo chgrp temporalcloak /etc/letsencrypt/live/temporalcloak.cloud/
sudo chgrp temporalcloak /etc/letsencrypt/archive/temporalcloak.cloud/
sudo chgrp temporalcloak /etc/letsencrypt/archive/temporalcloak.cloud/*
sudo chmod 640 /etc/letsencrypt/archive/temporalcloak.cloud/privkey*.pem
```

### 4.3 Auto-Renewal

```bash
# Certbot auto-renews via systemd timer (installed by default)
# Add a renewal hook to restart Tornado after cert renewal
sudo tee /etc/letsencrypt/renewal-hooks/post/restart-temporalcloak.sh << 'EOF'
#!/bin/bash
systemctl restart temporalcloak
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/post/restart-temporalcloak.sh
```

No code-level TLS configuration needed — setting `TC_TLS_CERT` and `TC_TLS_KEY` environment variables is all it takes. The server handles TLS setup and HTTP→HTTPS redirect automatically.

---

## 5. Systemd Service

Create `/etc/systemd/system/temporalcloak.service`:

```ini
[Unit]
Description=TemporalCloak Steganography Server
After=network.target

[Service]
Type=simple
User=temporalcloak
WorkingDirectory=/home/temporalcloak/app
Environment=TC_HOST=0.0.0.0
Environment=TC_PORT=443
Environment=TC_TLS_CERT=/etc/letsencrypt/live/temporalcloak.cloud/fullchain.pem
Environment=TC_TLS_KEY=/etc/letsencrypt/live/temporalcloak.cloud/privkey.pem
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
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable temporalcloak
sudo systemctl start temporalcloak
```

---

## 6. Testing & Calibration

### 6.1 Local Testing

```bash
# Run full test suite (53 tests)
uv run python -m unittest discover -s tests -v

# Smoke test server locally
uv run python demos/temporal_cloak_web.py
# In another terminal:
uv run temporal-cloak decode http://localhost:8888/api/image
```

### 6.2 Internet Timing Calibration

After deploying to the VPS, run the client from your local machine:

```bash
uv run temporal-cloak decode https://temporalcloak.cloud/api/image
```

Check:
- **Does the message decode correctly?** If not, widen the delay gap.
- **What's the observed jitter?** The client's debug output shows actual inter-chunk delays.
- **Test from different networks** — home WiFi, mobile hotspot, coffee shop.

### 6.3 Tuning

If decoding fails over the internet, adjust via the systemd environment variables — no code changes needed:

1. Increase `TC_BIT_0_DELAY` (try `0.40`, then `0.50`)
2. Keep `TC_BIT_1_DELAY` at `0.05` (not zero — zero delay can cause chunks to merge)
3. Recalculate `TC_MIDPOINT` as `(BIT_0 + BIT_1) / 2`
4. Restart: `sudo systemctl restart temporalcloak`

**Tradeoff:** Wider delays = more reliable decoding, but slower transmission. A 200-character quote at 0.3s/bit takes ~8 seconds. At 0.5s/bit, ~13 seconds. Both are acceptable for a demo where the user is waiting for an image download anyway.

---

## 7. Monitoring & Maintenance

### 7.1 Logs

```bash
# View live logs
sudo journalctl -u temporalcloak -f

# View recent errors
sudo journalctl -u temporalcloak --since "1 hour ago" -p err
```

### 7.2 Health Check

The `/api/health` endpoint returns:

```json
{"status": "ok", "uptime": 3600}
```

Use a free uptime monitor (UptimeRobot, Hetrixtools) to ping this every 5 minutes.

### 7.3 Updates

```bash
cd /home/temporalcloak/app
git pull origin main
sudo systemctl restart temporalcloak
```

---

## 8. Security Considerations

| Concern | Mitigation |
|---------|------------|
| DDoS / abuse | Tornado is single-threaded; add rate limiting (e.g., 10 req/min per IP) |
| Path traversal | `ImageProvider` and `QuoteProvider` use hardcoded paths, not user-supplied |
| TLS configuration | Use modern ciphers only (TLS 1.2+), certbot handles this |
| No auth needed | This is a public demo — no user accounts or sensitive data |
| Process isolation | Run as dedicated `temporalcloak` user, not root |
| Port exposure | Only 22, 80, 443 open via UFW |

---

## 9. Remaining Steps

| Step | What | Status |
|------|------|--------|
| 1 | Code changes (config, timing, server, client, quotes) | Done |
| 2 | Placeholder landing page | Done |
| 3 | Design and build full landing page | Not started (separate task) |
| 4 | Provision Hostinger VPS | Not started |
| 5 | Server setup (Python, uv, firewall, certbot) | Not started |
| 6 | Deploy code to VPS | Blocked by steps 4-5 |
| 7 | Domain + TLS setup (Let's Encrypt) | Blocked by step 6 |
| 8 | Systemd service setup | Blocked by step 6 |
| 9 | Internet timing calibration | Blocked by steps 6-8 |
| 10 | Deploy full landing page | Blocked by steps 3, 6 |

---

## 10. Open Questions

1. **Domain name** — Do you want to use a custom domain, or just the VPS IP address? A domain is needed for TLS (Let's Encrypt doesn't issue certs for bare IPs).

2. **Landing page scope** — Will the landing page just explain the concept, or also include a browser-based decoder? (A JS-based decoder would measure chunk timing in the browser via `fetch()` streaming, eliminating the need for the Python client script.)

3. **Image content** — Keep the current 10 stock images, or curate a different set for the public demo?
