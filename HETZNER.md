# Price Agent a Hetzner szerveren

Utolsó dokumentációfrissítés: **2026-08-14**. Az éles commit ellenőrzése:
`git -C /opt/price_agent rev-parse --short HEAD`.

Colleagues can access the app from any device at a stable internet URL.
Budget: ~€22.59/month (Hetzner CPX42).

---

## What you get

| Component | Details |
|---|---|
| Hetzner project | [Price Agent project dashboard](https://console.hetzner.com/projects/14917603/dashboard) — Hetzner login and project membership required |
| Server | Hetzner CPX42 — 8 vCPU (AMD), 16 GB RAM, 320 GB SSD |
| Public IP | `178.104.208.200` |
| OS | Ubuntu 26.04 LTS |
| App | FastAPI + Playwright/Chromium, running in Docker |
| Price Agent | `https://178.104.208.200/` |
| Batch Agent | `https://178.104.208.200/batch-agent/` |
| Public ports | `22`, `80`, `443` |
| Internal-only ports | `127.0.0.1:8080`, `127.0.0.1:8001` |
| TLS | Let's Encrypt short-lived IP certificate, automatic renewal |
| Auto-restart | Yes — survives reboots and crashes |

The root Docker Compose stack runs two containers: `price-agent` and
`batch-price-agent`. Nginx runs on the host and routes public HTTPS traffic to
their localhost-only ports.

---

## Step 1 — Create a Hetzner account and a server

1. Register at **hetzner.com/cloud**
2. Create a new **Project**, then click **+ Add Server**
  - **Image:** current Ubuntu image offered by Hetzner
   - **Type:** CPX42 (€22.59/month)
   - **SSH keys:** paste your public key (`~/.ssh/id_rsa.pub` or `~/.ssh/id_ed25519.pub`).
     If you don't have one, generate it on your Mac:
     ```bash
     ssh-keygen -t ed25519 -C "hetzner"
     cat ~/.ssh/id_ed25519.pub   # copy this into Hetzner
     ```
3. Click **Create & Buy Now**. Note the IP address (e.g. `65.21.10.42`).

---

## Step 2 — Connect to the server

On your Mac (the current production server):
```bash
ssh -i ~/.ssh/id_ed25519 root@178.104.208.200
```

The server has the project SSH public key in `root/.ssh/authorized_keys`, so
routine maintenance must use key authentication instead of a shared root
password. If Hetzner access is missing, first verify that the logged-in Hetzner
account is a member of the linked project shown above.

---

## Step 3 — Run the automated setup script

The `deploy/setup-server.sh` script installs Docker, clones the repo, installs
the systemd service, and starts the app in one go.

**Run on the server:**
```bash
curl -fsSL https://raw.githubusercontent.com/matenagy1990/gepcoop-price-agent/main/deploy/setup-server.sh | bash
```

The script will pause and ask you to create the `.env` file before continuing.
At that point, open a **second terminal** and copy your local `.env` to the server:
```bash
# Run this on your Mac (second terminal window):
scp /path/to/gepcoop-price-agent/.env root@65.21.10.42:/opt/price_agent/.env
```

Then press **Enter** in the first terminal to continue.

> **First run:** Docker downloads the Playwright image (~1.5 GB) — takes ~3–5 minutes.

---

## Step 4 — Verify the app is running

```bash
systemctl status price-agent
```

Should show `active (running)`.

Az első HTTP-ellenőrzéshez nyisd meg:
```
http://65.21.10.42
```

Log in and test a part number lookup.

Az Nginxet a telepítő automatikusan beállítja. A Docker `8080` és `8001`
portjai kizárólag a szerver saját `127.0.0.1` címén érhetők el.

---

## Step 5 — HTTPS aktiválása domain nélkül

Futtasd:

```bash
cd /opt/price_agent
sudo bash deploy/enable-https.sh 178.104.208.200
```

Ez hatnapos Let's Encrypt IP-tanúsítványt kér, bekapcsolja a HTTPS
átirányítást és az automatikus megújítást, majd a
tűzfalon csak az SSH (`22`), HTTP (`80`) és HTTPS (`443`) portokat hagyja
elérhetően. A `8080` és `8001` portokat kívülről lezárja.

Az IP-tanúsítvány körülbelül hatnapos. Ez nem jelent kézi karbantartást:
a Snapből telepített Certbot időzítő automatikusan megújítja, a deploy hook
pedig újratölti az Nginxet.

---

## Ongoing maintenance

| Task | Command (run on server) |
|---|---|
| Check status | `systemctl status price-agent` |
| View live logs | `journalctl -u price-agent -f` |
| Restart app | `systemctl restart price-agent` |
| Update to latest code | `cd /opt/price_agent && git pull --ff-only origin main && docker compose up -d --build` |
| Full rebuild | `cd /opt/price_agent && git pull --ff-only origin main && docker compose build && docker compose up -d && systemctl restart price-agent` |
| Certificate state | `certbot certificates` |
| Renewal timer | `systemctl status snap.certbot.renew.timer` |
| Renewal test | `certbot renew --cert-name 178.104.208.200 --dry-run` |
| Firewall | `ufw status numbered` |
| Listening ports | `ss -lntp \| grep -E ':(80\|443\|8080\|8001) '` |

---

## Update workflow (after code changes)

Az éles frissítés csak a lokálisan reprodukált, lokálisan javított és tesztelt,
majd GitHubon review-zott/merge-ölt commitból történhet. Kötelező sorrend:

```text
local reproduce → local fix/test → GitHub review/merge → server deploy → production verification
```

Tracked alkalmazásfájlt közvetlenül a szerveren nem szerkesztünk. Ha a szerver
HEAD-je nem egyezik az `origin/main` kiadásra szánt commitjával, a deploy megáll.

```bash
# On the server:
cd /opt/price_agent
git pull --ff-only origin main
docker compose up -d --build
systemctl restart price-agent
```

Docker uses `restart: unless-stopped`. Because the repository source is copied
into the image, code changes should also be followed by a rebuild/recreate on
the server. If you changed `requirements.txt` or the Dockerfile, rebuild first:
```bash
cd /opt/price_agent
git pull --ff-only origin main
docker compose build
docker compose up -d
systemctl restart price-agent
```

Always deploy a reviewed commit already present on `origin/main`. Do not edit
tracked application files directly on the server. Production `.env` and
`assets/sessions/` remain server-local and must never be committed.

After deployment:

```bash
git rev-parse --short HEAD
docker compose ps
curl -sS https://178.104.208.200/health
curl -sS -o /dev/null -w '%{http_code}\n' https://178.104.208.200/batch-agent/
```

## Reboot and process-health checks

The server was rebooted and checked on **2026-08-14** after an accumulation of
zombie processes. A reboot cleared them. Both Compose services now use
`init: true`, so Docker's Tini process is PID 1 and reaps orphaned Chromium
children after Playwright runs. For future checks:

```bash
ps -eo stat= | awk '$1 ~ /^Z/ {count++} END {print count+0}'
uptime
systemctl status price-agent --no-pager
docker compose -f /opt/price_agent/docker-compose.yml ps
```

If the zombie count grows continuously despite Tini, identify the parent
process before rebooting and verify `docker compose config` still shows
`init: true` for both services. A planned reboot is acceptable after confirming
both applications restart and both HTTPS checks above succeed.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ssh: Connection refused` | Wait 60 s after server creation, then retry |
| `active (running)` but browser shows nothing | Check `journalctl -u price-agent -f` for errors |
| Docker image download stuck | Wait — Playwright image is ~1.5 GB on first pull |
| `BrowserType.launch: Executable doesn't exist` | Pull latest code and rebuild the Docker image so Playwright package and base image versions match |
| Login fails | Ellenőrizd az aktív felhasználót az admin felületen és a szerver naplóját |
| App stopped after reboot | `systemctl start price-agent` (shouldn't happen — auto-start is enabled) |
| Need to update `.env` on server | `nano /opt/price_agent/.env`, then `systemctl restart price-agent` |
| HTTPS certificate renewal fails | Check `journalctl -u snap.certbot.renew.service` and ensure port 80 remains publicly reachable |
| Direct `:8080` or `:8001` URL times out | Expected production behaviour; use the HTTPS URLs above |
| KingB2B shows an empty search despite a valid mapping | Pull the release containing clean-session retry and RD3 response synchronisation; invalidate only `assets/sessions/kingb2b_session.json` if recovery still fails |
| Wasishop appears logged in but returns wrong/empty data | Pull the release containing logout-marker authentication and exact-card parsing; invalidate only `assets/sessions/wasishop_session.json` if required |
