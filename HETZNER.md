# Deploy Price Agent to Hetzner Cloud

Colleagues can access the app from any device at a stable internet URL.
Budget: ~€22.59/month (Hetzner CPX42).

---

## What you get

| Component | Details |
|---|---|
| Server | Hetzner CPX42 — 8 vCPU (AMD), 16 GB RAM, 320 GB SSD |
| OS | Ubuntu 24.04+ / current Hetzner Ubuntu image |
| App | FastAPI + Playwright/Chromium, running in Docker |
| URL | `https://<server-ip>`; Batch Agent: `https://<server-ip>/batch-agent/` |
| Auto-restart | Yes — survives reboots and crashes |

Everything runs in the existing Docker container (same image as local).
No code changes are needed.

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

On your Mac:
```bash
ssh root@65.21.10.42
```

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
scp /Users/nagyi_home/Desktop/AI/Price_agent/.env root@65.21.10.42:/opt/price_agent/.env
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
sudo bash deploy/enable-https.sh 178.104.208.200 admin@gepcoop.hu
```

Ez hatnapos Let's Encrypt IP-tanúsítványt kér, bekapcsolja a HTTPS
átirányítást és az automatikus megújítást, majd a
tűzfalon csak az SSH (`22`), HTTP (`80`) és HTTPS (`443`) portokat hagyja
elérhetően. A `8080` és `8001` portokat kívülről lezárja.

---

## Ongoing maintenance

| Task | Command (run on server) |
|---|---|
| Check status | `systemctl status price-agent` |
| View live logs | `journalctl -u price-agent -f` |
| Restart app | `systemctl restart price-agent` |
| Update to latest code | `git -C /opt/price_agent pull && systemctl restart price-agent` |
| Full rebuild | `cd /opt/price_agent && git pull origin main && docker compose build && docker compose up -d && systemctl restart price-agent` |

---

## Update workflow (after code changes)

```bash
# On the server:
git -C /opt/price_agent pull
systemctl restart price-agent
```

Docker uses `restart: unless-stopped`. Because the repository source is copied
into the image, code changes should also be followed by a rebuild/recreate on
the server. If you changed `requirements.txt` or the Dockerfile, rebuild first:
```bash
cd /opt/price_agent
git pull origin main
docker compose build
docker compose up -d
systemctl restart price-agent
```

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
