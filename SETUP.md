# Gép-Coop Price Agent — Setup Guide

Last updated: **2026-08-14**.

The repository contains two applications sharing the same 14 supplier
scrapers:

- Price Agent: local `http://localhost:8080`
- Batch Price Agent: local `http://localhost:8001` (open it from the Price
  Agent application selector so it receives both required access tokens)

The Gép-Coopilot chat launcher and its admin ticket menu are currently hidden
with reversible frontend feature switches. Existing Copilot data is retained.

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

Never paste `.env` values into documentation, issues, commits or terminal
output shared with others. Supplier credentials are referenced only by their
`SUPPLIER_<letter>_USERNAME/PASSWORD` variable names.

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

Jelentkezz be az admin által létrehozott aktív felhasználóval. A felhasználók
a Supabase `app_users` táblájában vannak, és az admin felületen kezelhetők.
Fix éles felhasználónevet vagy jelszót nem tárolunk a dokumentációban.

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

Focused local checks after scraper/UI changes:

```bash
python3 -m py_compile browser/supplier_fabory.py browser/supplier_inoxmare.py browser/supplier_kingb2b.py browser/supplier_wasishop.py
./.venv/bin/python -m unittest discover -s tests -v
cd batch-price-agent && ../.venv/bin/python -m unittest discover -s tests -v
```

---

## Required change and release workflow

Minden hibajavításnál és funkciómódosításnál kötelező a következő sorrend:

1. A hibát a lokális repositoryban, a valós mappinggal reprodukáld.
2. A kódot kizárólag lokálisan módosítsd; az éles szerveren ne szerkessz tracked fájlt.
3. Futtasd a syntax-, unit- és releváns élő, csak olvasási ellenőrzéseket.
4. Csak sikeres helyi ellenőrzés után commitolj és pusholj külön GitHub branchre.
5. A változás review/merge után kerüljön az `origin/main` ágra.
6. A szerver kizárólag az `origin/main` ellenőrzött commitját húzza le.
7. Rebuild/restart után ellenőrizd a health endpointokat és ugyanazt a tesztterméket.

Röviden: **local reproduce → local fix/test → GitHub review/merge → server deploy → production verification**.

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
| Login fails | Ellenőrizd az admin felületen, hogy a felhasználó aktív-e, majd nézd meg a backend naplóját |
| Playwright / Chromium error | Run `git pull` then `docker compose up -d --build` to get latest image version |
| App stopped after PC restart | Run `docker compose up -d` again in the project folder |

---

## How the mapping CSV works

The Supabase `article_mapping` table links Gép-Coop internal part numbers to
supplier part numbers. The admin CSV upload updates that table; the CSV is an
import format, not the runtime source of truth.

```
gepcoop_part_no, csavarda_part_no, irontrade_part_no, koelner_part_no, mekrs_part_no
934128ZN,        934012000000801000, 934012000000801000, 00514,         10000.14.01.120.000
```

To add or update part numbers: log in as admin and upload a new CSV file.
The new CSV must have the same column names. It takes effect immediately — no restart needed.

---

## Production access

Az éles alkalmazás nem a fejlesztői számítógépről és nem Tailscale-en érhető el:

```text
Price Agent: https://178.104.208.200/
Batch Agent: https://178.104.208.200/batch-agent/
```

A szerveren az Nginx kezeli a HTTPS-t. A Docker `8080/8001` portjai csak
localhoston érhetők el, és a tűzfal kívülről is tiltja őket.

## Optional private test access with Tailscale

Ez csak külön, nem éles tesztgép esetén használható:

1. Create a free account at **tailscale.com**
2. Install Tailscale on the host computer and on each colleague's device
3. Note the Tailscale IP of the host computer (`tailscale ip` in terminal)
4. A Docker portkötést külön engedélyezni kell a Tailscale interfészre; az éles
   szerver konfigurációját emiatt ne módosítsd.

See the full Tailscale setup in the deployment plan.
