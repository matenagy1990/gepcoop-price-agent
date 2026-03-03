# Deploy Price Agent to Hetzner Cloud

Colleagues can access the app from any device at a stable internet URL.
Budget: ~€4.51/month (Hetzner CX22).

---

## What you get

| Component | Details |
|---|---|
| Server | Hetzner CX22 — 2 vCPU, 4 GB RAM, 40 GB SSD |
| OS | Ubuntu 22.04 |
| App | FastAPI + Playwright/Chromium, running in Docker |
| URL | `http://<server-ip>:8080` |
| Auto-restart | Yes — survives reboots and crashes |

Everything runs in the existing Docker container (same image as local).
No code changes are needed.

---

## Step 1 — Create a Hetzner account and a server

1. Register at **hetzner.com/cloud**
2. Create a new **Project**, then click **+ Add Server**
   - **Image:** Ubuntu 22.04
   - **Type:** CX22 (€4.51/month)
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

Open a browser and navigate to:
```
http://65.21.10.42:8080
```

Log in and test a part number lookup.

---

## Step 5 — (Optional) Restrict firewall

Allow only the app port and SSH:
```bash
ufw allow 22/tcp      # SSH — keep this open!
ufw allow 8080/tcp    # Price Agent UI
ufw enable
```

---

## Ongoing maintenance

| Task | Command (run on server) |
|---|---|
| Check status | `systemctl status price-agent` |
| View live logs | `journalctl -u price-agent -f` |
| Restart app | `systemctl restart price-agent` |
| Update to latest code | `git -C /opt/price_agent pull && systemctl restart price-agent` |
| Full rebuild | `cd /opt/price_agent && docker compose up -d --build && systemctl restart price-agent` |

---

## Update workflow (after code changes)

```bash
# On the server:
git -C /opt/price_agent pull
systemctl restart price-agent
```

Docker uses `restart: unless-stopped`, so after a `git pull` a simple service
restart picks up the latest image/code.

If you changed `requirements.txt` or the Dockerfile, rebuild first:
```bash
cd /opt/price_agent
docker compose build
systemctl restart price-agent
```

---

## (Optional later) Add a domain name

1. Buy a domain (e.g. `price.gepcoop.hu`) and point its **A record** to the server IP.
2. Install Nginx as a reverse proxy:
   ```bash
   apt install -y nginx
   ```
3. Create `/etc/nginx/sites-available/price-agent`:
   ```nginx
   server {
       listen 80;
       server_name price.gepcoop.hu;

       location / {
           proxy_pass http://127.0.0.1:8080;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           # Required for SSE (streaming query results):
           proxy_buffering off;
           proxy_cache off;
       }
   }
   ```
4. Enable and reload:
   ```bash
   ln -s /etc/nginx/sites-available/price-agent /etc/nginx/sites-enabled/
   nginx -t && systemctl reload nginx
   ```
5. (Recommended) Add HTTPS with Let's Encrypt:
   ```bash
   apt install -y certbot python3-certbot-nginx
   certbot --nginx -d price.gepcoop.hu
   ```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ssh: Connection refused` | Wait 60 s after server creation, then retry |
| `active (running)` but browser shows nothing | Check `journalctl -u price-agent -f` for errors |
| Docker image download stuck | Wait — Playwright image is ~1.5 GB on first pull |
| Login fails | Username: `gepcoop` Password: `Beszerzes2026!` |
| App stopped after reboot | `systemctl start price-agent` (shouldn't happen — auto-start is enabled) |
| Need to update `.env` on server | `nano /opt/price_agent/.env`, then `systemctl restart price-agent` |
