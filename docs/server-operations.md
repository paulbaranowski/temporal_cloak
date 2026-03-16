# TemporalCloak — Server Operations Runbook

Quick reference for diagnosing errors, managing the service, and querying the database on the Hostinger VPS.

---

## Connecting to the Server

```bash
ssh -i ~/.ssh/<keyfile> root@<HOSTINGER_IP>
```

Then switch to the app user:

```bash
sudo su - temporalcloak
cd ~/app
```

The app lives at `/home/temporalcloak/app`.

---

## Diagnosing Errors

### View recent logs

```bash
# Last 50 log lines
sudo journalctl -u temporalcloak -e --no-pager -n 50

# Errors only, last hour
sudo journalctl -u temporalcloak --since "1 hour ago" -p err --no-pager

# Follow logs in real-time (useful while restarting in another terminal)
sudo journalctl -u temporalcloak -f
```

### Check service status

```bash
sudo systemctl status temporalcloak
```

Key fields to look at:
- **Active** — `active (running)` is healthy. `activating (auto-restart)` means it keeps crashing.
- **Main PID** — `code=exited, status=1/FAILURE` means the process crashed. Check logs for the traceback.

### Common problems

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `status=1/FAILURE` loop | Python exception at startup (import error, missing file, bad config) | Check `journalctl` for the traceback |
| `Address already in use` | Port 443 already bound (another process or previous instance didn't stop) | `sudo lsof -i :443` to find the process, then `sudo kill <PID>` |
| TLS errors | Expired or missing certificate | `sudo certbot renew` then restart |
| `ModuleNotFoundError` | Dependencies out of sync | `cd /home/temporalcloak/app && ~/.local/bin/uv sync` |
| Permission denied on cert files | `temporalcloak` user lost read access | Re-run cert permission commands from `docs/deployment-plan.md` Section 4.2 |

### Health check

```bash
# From the server itself
curl -k https://localhost/api/health

# From your local machine
curl https://temporalcloak.cloud/api/health
```

Returns `{"status": "ok", "uptime": <seconds>}`.

---

## Managing the Service

### Restart

```bash
sudo systemctl restart temporalcloak
```

### Stop / Start

```bash
sudo systemctl stop temporalcloak
sudo systemctl start temporalcloak
```

### View the service file

```bash
cat /etc/systemd/system/temporalcloak.service
```

### Edit the service file

```bash
sudo nano /etc/systemd/system/temporalcloak.service
sudo systemctl daemon-reload
sudo systemctl restart temporalcloak
```

Always run `daemon-reload` after editing the service file.

### Deploy latest code manually

```bash
cd /home/temporalcloak/app
git pull origin main
~/.local/bin/uv sync
sudo systemctl restart temporalcloak
```

Note: pushes to `main` trigger automatic deployment via GitHub Actions (`.github/workflows/deploy.yml`).

---

## Querying the Database

The app uses SQLite. The database file location is controlled by the `TC_DB_PATH` env var (default: `/home/temporalcloak/app/data/links.db`).

### Prerequisites

The `sqlite3` CLI must be installed on the server:

```bash
sudo apt install sqlite3 -y
```

### Open the database

```bash
sqlite3 /home/temporalcloak/app/data/links.db
```

### Schema

The `links` table stores shareable steganography links:

```
link_id          TEXT PRIMARY KEY   -- 8-char hex ID (e.g. "a1b2c3d4")
message          TEXT               -- the hidden message
image_path       TEXT               -- full path to the image file
image_filename   TEXT               -- just the filename
created_at       REAL               -- unix timestamp
burn_after_reading INTEGER          -- 1 = delete after first decode
delivered        INTEGER            -- 1 = has been decoded
```

### Common queries

```sql
-- List all links
SELECT link_id, message, image_filename, datetime(created_at, 'unixepoch') AS created, burn_after_reading, delivered FROM links;

-- Count total links
SELECT COUNT(*) FROM links;

-- Find a specific link
SELECT * FROM links WHERE link_id = 'a1b2c3d4';

-- Links created today
SELECT * FROM links WHERE created_at > strftime('%s', 'now', '-1 day');

-- Undelivered links
SELECT link_id, message, image_filename FROM links WHERE delivered = 0;

-- Delete a specific link
DELETE FROM links WHERE link_id = 'a1b2c3d4';

-- Delete all delivered links older than 7 days
DELETE FROM links WHERE delivered = 1 AND created_at < strftime('%s', 'now', '-7 days');
```

### One-liner queries (without entering the sqlite3 shell)

```bash
# Count links
sqlite3 /home/temporalcloak/app/data/links.db "SELECT COUNT(*) FROM links;"

# List recent links
sqlite3 -header -column /home/temporalcloak/app/data/links.db \
  "SELECT link_id, message, delivered FROM links ORDER BY created_at DESC LIMIT 10;"
```

### Using DuckDB with the SQLite database

DuckDB can query SQLite files directly without importing. This is useful for more complex analytics or joins that would be cumbersome in the sqlite3 CLI.

```bash
# Open an interactive DuckDB shell attached to the SQLite database
duckdb -c "ATTACH '/home/temporalcloak/app/data/links.db' AS db (TYPE sqlite); USE db;"

# One-liner queries
duckdb -c "
  ATTACH '/home/temporalcloak/app/data/links.db' AS db (TYPE sqlite);
  SELECT * FROM db.links ORDER BY created_at DESC LIMIT 10;
"

# Aggregate stats
duckdb -c "
  ATTACH '/home/temporalcloak/app/data/links.db' AS db (TYPE sqlite);
  SELECT
    COUNT(*) AS total,
    SUM(delivered) AS delivered,
    SUM(burn_after_reading) AS burn_links,
    MIN(to_timestamp(created_at)) AS oldest,
    MAX(to_timestamp(created_at)) AS newest
  FROM db.links;
"

# Export to CSV
duckdb -c "
  ATTACH '/home/temporalcloak/app/data/links.db' AS db (TYPE sqlite);
  COPY (SELECT * FROM db.links) TO '/tmp/links_export.csv' (HEADER);
"
```
