# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Automates supplier price and stock lookups for Gép-Coop procurement staff. A buyer enters an internal
Gép-Coop part number; the system translates it to each supplier's part number, scrapes the supplier
webshops via Playwright (server-side, headless), normalises every price to **per 1 db (piece)**, and
returns a side-by-side comparison plus a purchase recommendation. There are currently **11 integrated
suppliers**.

## Architecture

**FastAPI backend + browser UI + per-supplier Playwright scrapers + Supabase.**

```
[Browser UI] ──GET /query/stream (SSE)──▶ [FastAPI: main.py]
                                            ├── lookup_mapping_all()     → Supabase article_mapping
                                            ├── fetch_supplier_price()   → browser/supplier_*.py (Playwright)
                                            │     └── normalise to price_per_db (+ EUR→HUF for FX suppliers)
                                            └── compute_recommendation() → cheapest, HUF-comparable
```

- The backend orchestrates the workflow but never invents prices or stock.
- Up to **4 scrapers run concurrently** (`SCRAPER_LIMIT = asyncio.Semaphore(4)` in `main.py`).
- The scrapers run wherever the server runs (headless Chromium); results stream to the buyer's
  browser over Server-Sent Events. The buyer's browser is a thin client.
- If a scraper fails, the per-supplier card shows a human-readable message (see Buyer Feedback below).

## Project Structure

```
main.py                     # FastAPI app: auth, query stream, recommendation, deep-links, admin, FX
agent/
  └── tools.py              # Supabase mapping lookup + supplier dispatch + price normalisation
browser/
  ├── supplier_<id>.py      # One Playwright scraper per supplier (11 of them)
  ├── session_utils.py      # storage_state session load/save/freshness helpers
  └── messages.py           # Canonical buyer-facing feedback messages (MSG_NOT_FOUND / MSG_NOT_PRICED)
ui/
  └── index.html            # Single-file frontend (login, query, results, admin panel, webshop-login helper)
assets/
  ├── sessions/             # Per-supplier saved login sessions (storage_state JSON)
  └── logo.png
deploy/                     # Supabase SQL migrations + systemd/server setup
docs/webshop_utmutato.md    # Per-webshop login & search guide (operational)
Dockerfile, docker-compose.yml, HETZNER.md, SETUP.md
```

## Suppliers

`supplier_id` is the **supplier name** (not `supplier_a`). Defined in `SUPPLIER_META` (`main.py`), each
mapped to a `SUPPLIER_<A..K>_*` env prefix. Two suppliers have an extra credential field.

| supplier_id | site | env prefix | currency | extra field |
|---|---|---|---|---|
| csavarda | csavarda.hu | SUPPLIER_A | HUF | — |
| irontrade | irontrade.hu | SUPPLIER_B | HUF | — |
| koelner | webshop.koelner.hu | SUPPLIER_C | HUF | — |
| mekrs | eshop.mekrs.cz | SUPPLIER_D | EUR (forced) | — |
| fabory | fabory.com/hu | SUPPLIER_E | HUF | — |
| reyher | rio.reyher.de | SUPPLIER_F | EUR | customer_code |
| hopefix | hopefix.cz | SUPPLIER_G | EUR | — |
| fastbolt | fbonline.fastbolt.com | SUPPLIER_H | EUR | shortname |
| schaefer | shop.schaefer-peters.com | SUPPLIER_I | EUR | — |
| kingb2b | kingb2b.it | SUPPLIER_J | EUR | — |
| wasishop | wasishop.de | SUPPLIER_K | EUR | — |

## Data Source — Supabase (CSV fallback disabled)

Article mapping lives in the Supabase **`article_mapping`** table — a wide table:

```
gepcoop_part_no | name | csavarda_part_no | irontrade_part_no | koelner_part_no | … | wasishop_part_no
```

Column convention: `{supplier_id}_part_no`. `agent/tools.py` provides:
- `lookup_mapping_all(part)` → list of `{supplier_id, supplier_part_no, supplier_url}` for every
  supplier that has a non-empty part number AND an implemented scraper (`_IMPLEMENTED_SUPPLIERS`).
- `get_all_part_numbers()` / `search_part_numbers(q)` → autocomplete (paginated, 300 s cache).

Requires `SUPABASE_URL` / `SUPABASE_KEY`. Other Supabase tables/buckets: `app_users` (auth),
`query_runs` (run log + feedback), and a storage bucket for the managed guide PDF.

## Authentication

App users are stored in Supabase `app_users` (PBKDF2-SHA256 hashes). `POST /login` issues an in-memory
bearer token (`sessions` dict in `main.py`). Roles: regular user, admin, and a single **primary admin**.
Most endpoints require a user token; `/admin/*` require an admin token; the guide upload requires the
primary admin.

## HTTP API (main.py)

- `GET /` UI · `GET /logo.png` · `GET /health`
- `POST /login` · `GET /me`
- `GET /query/lookup?internal_part_no=` — mapping preview (name + supplier part numbers), no scraping
- `GET /query/stream?internal_part_no=&suppliers=` — **SSE**; emits `progress`, `result`, `error`,
  `password_required` events; runs all (or selected) scrapers in parallel and logs the run
- `POST /query/feedback` — thumbs up/down on a recommendation (stored on `query_runs`)
- `GET /parts`, `GET /parts/search?q=` — part-number autocomplete
- `POST /supplier/open` — returns a **product deep-link URL** the frontend opens in the buyer's own
  browser tab (+ `is_product_link` flag)
- `GET /supplier/login-info` — per-webshop login URL + shared credentials for the "log in to webshops"
  helper (any logged-in user)
- `POST /reyher/open` — headed (visible) Playwright browser for Reyher (local-only; see Deployment)
- `GET /guide/pdf`, `POST /admin/guide/upload` — managed internal guide PDF (Supabase Storage)
- `/admin/*` — mapping view/upload/template, run log + CSV export + chart, FX settings, supplier
  credentials, app-user management, supplier password update

## Browser Scraper Pattern

Each `browser/supplier_<id>.py` exposes:

```python
async def fetch_price(supplier_part_no: str, on_progress=None) -> dict
```

It must: restore a saved session or log in (credentials from `.env`), search for `supplier_part_no`,
and return the **raw** values:

```json
{ "supplier_part_no": "...", "price_raw": 249.60, "price_unit_qty": 1000,
  "currency": "HUF", "unit": "db", "stock": 114000, "queried_at": "2026-..." }
```

- `stock` may be an `int`, a `dict` (e.g. csavarda `{budapest, vecsés}`), or a string (`"Raktáron"`/`"X"`).
- Sessions are saved as `storage_state` JSON in `assets/sessions/` via `browser/session_utils.py`.
- CSS selectors are hardcoded per scraper; when a site changes layout, only that scraper changes.
- Raise the **canonical feedback messages** (below) for the two buyer-facing outcomes.

## Buyer Feedback (browser/messages.py)

Two standardized messages, applied consistently across all 11 scrapers:

- `MSG_NOT_FOUND` = **"jelenleg nem találom a webshopban a terméket"** — the part maps to a supplier
  code, but the webshop search returns no matching product after login.
- `MSG_NOT_PRICED` = **"a webshopban elérhető a termék, de nincs beárazva"** — the product is found, but
  no usable price is shown (price field holds text/placeholder or is empty; guard with
  `has_numeric_price()`).

Principle: failures **before** the product is located → not found; failures **after** it is located but
with no numeric price → not priced. (Reyher: `agent/tools.py` re-raises these two instead of its manual
fallback, so buyers see the real outcome.)

## Price Normalisation (critical business rule)

Every supplier quotes in different units; all prices are normalised to **per 1 db** so suppliers are
comparable. The scraper returns raw values; `fetch_supplier_price` in `agent/tools.py` computes:

```python
price_per_db = price_raw / price_unit_qty      # e.g. 249.60 / 1000 = 0.2496
```

For non-HUF (EUR) suppliers it also adds a HUF-comparable price using the **admin-set manual rate**
(`EUR_TO_HUF_RATE` env, default 400; editable via `/admin/fx-settings`):

```python
price_per_db_huf = price_per_db * eur_to_huf_rate
fx_huf_rate      = eur_to_huf_rate
```

`fetch_supplier_price` output:
```json
{ "supplier_part_no": "...", "price_per_db": 0.2496, "price_raw": 249.60,
  "price_unit_qty": 1000, "currency": "HUF", "unit": "db", "stock": 114000,
  "queried_at": "2026-...",
  "price_per_db_huf": 1.85, "fx_huf_rate": 400.0 }   // last two only for EUR suppliers
```

`compute_recommendation` (`main.py`) ranks all suppliers that returned a usable price (EUR converted to
HUF) and returns the cheapest with a Hungarian explanation and the savings vs the runner-up.

## "Tovább a honlapra" — product deep-links + webshop login helper

The result card's button opens the supplier's **product/search page in the buyer's own browser**, so it
works both locally and when the app is hosted. Deep-link templates live in `_SUPPLIER_SEARCH_URLS`
(`main.py`); when a buyer is logged in to that webshop in their browser, the page shows the authenticated
price.

- 9 suppliers have a real product/search deep-link (csavarda, irontrade, koelner, fabory, fastbolt,
  reyher `?sku=`, mekrs, wasishop, schaefer `?query=`).
- **kingb2b** and **hopefix** are JS-only SPAs with no product deep-link → the button opens the home/
  portal and copies the part number to the clipboard (`is_product_link=false`).
- The server cannot auto-fill cross-origin logins, so a **"Webshop belépés" panel** (`/supplier/login-info`
  + UI) opens each webshop's login page and copies the shared company credentials, so the buyer logs in
  once per webshop in their browser. After that, the deep-links land on the authenticated product.

## Running the Project

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --reload          # serves UI + API on http://localhost:8000/
```

Needs `.env` with `SUPABASE_URL`, `SUPABASE_KEY`, and `SUPPLIER_<A..K>_*` credentials.

## Deployment

Hosted on Hetzner via Docker (`Dockerfile`, `docker-compose.yml`, `deploy/`, `HETZNER.md`); buyers use it
from their own browsers. Headless scraping and product deep-links work fully when hosted. The **headed**
(visible) browser flows — `POST /reyher/open` and `_supplier_open_headed` — open a window on the *server*,
so they are only useful when the server runs on the buyer's own machine.

## Adding a New Supplier

1. Add a `{supplier_id}_part_no` column + values to Supabase `article_mapping` (the admin mapping upload
   maps Hungarian column aliases → snake_case names).
2. Add `SUPPLIER_X_URL/USERNAME/PASSWORD` (+ any extra field) to `.env` and register the supplier in
   `SUPPLIER_META` (`main.py`).
3. Create `browser/supplier_<id>.py` exposing `fetch_price(...)`, returning the standard raw dict and
   raising `MSG_NOT_FOUND` / `MSG_NOT_PRICED` at the right points.
4. Add the id to `_IMPLEMENTED_SUPPLIERS` and the dispatch in `agent/tools.py`.
5. Add a product deep-link to `_SUPPLIER_SEARCH_URLS` and a login URL to `_SUPPLIER_LOGIN_URLS` (`main.py`).
6. If the supplier needs a headed/session flow, add its session file + URLs to the maps in
   `_supplier_open_headed`.
