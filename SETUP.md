# Gép-Coop Price Agent — Setup Guide

## What you need (one time install)

| Software | Download | Notes |
|---|---|---|
| Docker Desktop | docker.com/products/docker-desktop | Skip account creation, click "Continue without signing in" |
| Git for Windows | git-scm.com/download/win | Click Next on everything, default settings |
| WSL2 (Windows only) | Built into Windows 10/11 | Docker Desktop will ask for it automatically |

---

## Step 1 — Install Docker Desktop

1. Download from **docker.com/products/docker-desktop**
2. Install → restart if asked
3. Open Docker Desktop → wait until bottom left shows **"Engine running"** (green dot)
4. Verify in terminal:
```
docker --version
```
Should print: `Docker version 29.x.x`

---

## Step 2 — Clone the repository

Open Command Prompt or PowerShell and navigate where you want the project:
```
cd C:\Users\YourName\Desktop
```

Clone:
```
git clone https://github.com/matenagy1990/gepcoop-price-agent.git
cd gepcoop-price-agent
```

---

## Step 3 — Add the .env file

The `.env` file contains all passwords and API keys. It is NOT on GitHub (intentionally).

Get it from the project owner and place it inside the `gepcoop-price-agent` folder.

**Important on Windows:** the file must be named `.env` (with the dot).
If it was saved as `env` (without dot), rename it in PowerShell:
```
Rename-Item env .env
```

Verify it is there:
```
dir /a
```
You should see `.env` in the list.

---

## Step 4 — Start the app

From inside the `gepcoop-price-agent` folder:
```
docker compose up -d
```

**First run:** takes ~5 minutes (downloads the Playwright Docker image, ~1.5 GB).
**After that:** starts in seconds.

When done, open browser and go to:
```
http://localhost:8080
```

Log in with:
- **Username:** `gepcoop`
- **Password:** `Beszerzes2026!`

---

## Useful commands

```bash
docker compose up -d          # start the app
docker compose down           # stop the app
docker compose restart        # restart (e.g. after changing .env)
docker compose logs -f        # watch live logs
docker compose up -d --build  # rebuild image (needed after code changes)
git pull                      # get latest code from GitHub
```

---

## Update the app (after code changes)

When the project owner pushes new code to GitHub:
```
git pull
docker compose down
docker compose up -d --build
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `git is not recognized` | Install Git from git-scm.com, reopen terminal |
| `no configuration file provided` | You are not inside the `gepcoop-price-agent` folder — run `cd gepcoop-price-agent` |
| `.env` not found / named `env` | Rename: `Rename-Item env .env` |
| `Engine running` not showing | Wait 60 seconds, Docker is still starting |
| Login fails | Username: `gepcoop` Password: `Beszerzes2026!` |
| Playwright / Chromium error | Run `git pull` then `docker compose up -d --build` to get latest image version |
| App stopped after PC restart | Run `docker compose up -d` again in the project folder |

---

## How the mapping CSV works

The file `assets/mapping.csv` links Gép-Coop internal part numbers to supplier part numbers:

```
gepcoop_part_no, csavarda_part_no, irontrade_part_no, koelner_part_no, mekrs_part_no
934128ZN,        934012000000801000, 934012000000801000, 00514,         10000.14.01.120.000
```

To add or update part numbers: log in as admin and upload a new CSV file.
The new CSV must have the same column names. It takes effect immediately — no restart needed.

---

## Remote access for colleagues (Tailscale)

If colleagues at other locations need access:

1. Create a free account at **tailscale.com**
2. Install Tailscale on the host computer and on each colleague's device
3. Note the Tailscale IP of the host computer (`tailscale ip` in terminal)
4. Colleagues open: `http://100.x.x.x:8080` in their browser

See the full Tailscale setup in the deployment plan.
