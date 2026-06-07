# CLAUDE.md

This file is the working technical guide for agents modifying this repository.
Last verified against the codebase: **2026-06-07**.

## Project Purpose

The Gép-Coop Price Agent helps procurement staff compare supplier prices and stock.
A buyer enters an internal Gép-Coop part number. The application:

1. Looks up every mapped supplier part number in Supabase.
2. Lets the buyer select which available suppliers to query.
3. Runs supplier-specific Playwright scrapers server-side.
4. Normalises prices to one piece (`price_per_db`).
5. Converts EUR prices to HUF with an admin-maintained manual exchange rate.
6. Streams progress and results to the browser with Server-Sent Events.
7. Ranks comparable prices and recommends the cheapest supplier.

There are currently **11 implemented supplier integrations**.

## Architecture

```
[Single-file browser UI: ui/index.html]
             |
             | Bearer auth + JSON/SSE
             v
[FastAPI: main.py]
  |-- authentication and admin operations
  |-- mapping preview and query orchestration
  |-- max 4 concurrent scraper executions
  |-- recommendation and run logging
  |-- supplier deep-links and shared login helper
  |
  |-- agent/tools.py
  |     |-- Supabase article mapping lookup
  |     |-- supplier scraper dispatch
  |     `-- per-piece and EUR-to-HUF normalisation
  |
  |-- browser/supplier_<id>.py
  |     `-- Playwright login, search, price and stock extraction
  |
  `-- Supabase
        |-- article_mapping
        |-- app_users
        |-- query_runs
        |-- supplier_info_items
        `-- Storage: homepage information JSON
```

Important runtime properties:

- `SCRAPER_LIMIT = asyncio.Semaphore(4)` limits Chromium load across users.
- Scrapers use headless Chromium and run on the machine hosting FastAPI.
- Authentication tokens are stored only in the in-memory `sessions` dictionary.
  All users must log in again after a server restart.
- The UI is served with `Cache-Control: no-store`.
- The application depends on Supabase for mappings and authentication. The old CSV
  fallback is not active in the current lookup flow.

## Project Structure

```text
main.py
  FastAPI routes, auth, admin functions, query orchestration, recommendation,
  supplier credentials, deep-links, homepage info and Supabase run logging.

agent/tools.py
  Mapping lookup, autocomplete cache, supplier dispatch, price normalisation.

browser/
  messages.py
    Canonical buyer-facing not-found / not-priced messages.
  session_utils.py
    Playwright storage_state persistence and freshness checks.
  supplier_<id>.py
    One scraper per implemented supplier.

ui/index.html
  Entire frontend: login, mapping preview, supplier selection, progress,
  result cards, feedback, webshop login helper and admin panel.

deploy/
  Supabase SQL migrations, systemd unit and Hetzner setup script.

assets/
  logo.png
  sessions/                 Runtime Playwright sessions, gitignored.
  homepage_info.json        Runtime local mirror, gitignored.

docs/webshop_utmutato.md
  Operational webshop notes.

Dockerfile
docker-compose.yml
HETZNER.md
SETUP.md
```

`WORK/` is a local, gitignored working area for test files, issues and generated
offers. Do not assume files under `WORK/` belong in commits.

## Suppliers

Supplier IDs are names, not historical values such as `supplier_a`.
Registration is split between `SUPPLIER_META` in `main.py` and
`_IMPLEMENTED_SUPPLIERS` plus dispatch logic in `agent/tools.py`.

| ID | Site | Env prefix | Returned currency | Extra login field |
|---|---|---|---|---|
| `csavarda` | csavarda.hu | `SUPPLIER_A` | HUF | none |
| `irontrade` | irontrade.hu | `SUPPLIER_B` | HUF | none |
| `koelner` | webshop.koelner.hu | `SUPPLIER_C` | HUF | none |
| `mekrs` | eshop.mekrs.cz | `SUPPLIER_D` | EUR, explicitly enforced | none |
| `fabory` | fabory.com/hu | `SUPPLIER_E` | HUF | none |
| `reyher` | rio.reyher.de | `SUPPLIER_F` | EUR | `CUSTOMER_CODE` |
| `hopefix` | hopefix.cz | `SUPPLIER_G` | EUR | none |
| `fastbolt` | fbonline.fastbolt.com | `SUPPLIER_H` | EUR | `SHORTNAME` |
| `schaefer` | shop.schaefer-peters.com | `SUPPLIER_I` | EUR | none |
| `kingb2b` | kingb2b.it | `SUPPLIER_J` | EUR | none |
| `wasishop` | wasishop.de | `SUPPLIER_K` | EUR | none |

`ferdinand_part_no` is supported by mapping import/export, but Ferdinand is not
in `_IMPLEMENTED_SUPPLIERS`, has no scraper, and is therefore not queried.

## Configuration and Secrets

The root `.env` file is the current source of supplier credentials and runtime
settings. It is gitignored and must never be committed.

Required core variables:

```text
SUPABASE_URL
SUPABASE_KEY
PRIMARY_ADMIN_USERNAME        Optional; defaults in main.py.
EUR_TO_HUF_RATE               Optional; defaults to 400.
```

Each supplier uses:

```text
SUPPLIER_<A..K>_URL
SUPPLIER_<A..K>_USERNAME
SUPPLIER_<A..K>_PASSWORD
```

Additional fields:

```text
SUPPLIER_F_CUSTOMER_CODE
SUPPLIER_H_SHORTNAME
```

Homepage information storage can be overridden with:

```text
SUPABASE_INFO_BUCKET          Default: internal-docs
SUPABASE_INFO_PATH            Default: homepage/info.json
```

### Credential update behaviour

Admin supplier saves call `_update_env_file()`:

- updates the root `.env`;
- updates `os.environ` immediately;
- updates the in-memory `SUPPLIER_CREDS`;
- does not require a server restart for subsequent scraper runs.

The password field is intentionally blank when the admin form loads. An empty
submitted password preserves the existing password. In Docker, `.env` is mounted
at `/app/.env`, so admin changes persist to the host file.

`GET /supplier/login-info` returns shared webshop login URLs and credentials to
any authenticated application user. Treat access to the application as access
to these shared supplier credentials.

## Supabase Data Model

### `article_mapping`

Wide mapping table:

```text
gepcoop_part_no
name
csavarda_part_no
irontrade_part_no
koelner_part_no
mekrs_part_no
fabory_part_no
ferdinand_part_no
reyher_part_no
hopefix_part_no
fastbolt_part_no
schaefer_part_no
kingb2b_part_no
wasishop_part_no
```

`lookup_mapping_all()` returns only non-empty mappings whose supplier is in
`_IMPLEMENTED_SUPPLIERS`.

Admin mapping upload:

- accepts CSV or XLSX;
- recognises Hungarian aliases such as `Gépcoop cikkszám`, `Cikknév`,
  `Iron trade`, `Schafer`, `King` and `Wasi`;
- performs a full replacement: deletes current rows, then upserts in batches of 500;
- uses `gepcoop_part_no` as the conflict key.

Autocomplete loads all `gepcoop_part_no` values in pages of 1000 and caches them
in process memory for 300 seconds.

### `app_users`

Created by `deploy/supabase_app_users.sql`.

- Usernames are primary keys.
- Passwords use PBKDF2-HMAC-SHA256 with 240,000 iterations.
- Roles: normal user, admin and primary admin.
- User deletion is soft deletion through `is_active=false` and `deleted_at`.
- A unique partial index allows only one row with `is_primary=true`.
- The table must already contain at least one active admin; the application does
  not bootstrap an empty table automatically.

### `query_runs`

Stores query status, suppliers, duration, username, errors and recommendation
feedback. Required incremental migrations:

- `deploy/supabase_query_runs_username.sql`
- `deploy/supabase_query_runs_feedback.sql`

Run statuses are `ok`, `partial` or `error`.

### `supplier_info_items`

Created by `deploy/supabase_supplier_info_items.sql`.

Stores up to five active ordering-information rows per supplier:

```text
supplier_id
label
value
sort_order
is_active
updated_at
updated_by
```

The admin can add, edit, delete and reorder rows in the Webshop login tab.
Blank label/value pairs are ignored. The database trigger and backend both
enforce the maximum of five active rows.

Active rows are attached to lookup and query results as `info_items`. Result
cards show the first two rows and put additional rows in a collapsible section.
No block is rendered when the list is empty.

### Homepage information

The admin-editable homepage message is stored as JSON in Supabase Storage:

```text
bucket: internal-docs
path:   homepage/info.json
```

The application also writes `assets/homepage_info.json` as a local fallback and
mirror. Empty text hides the block. This replaces the previous managed-guide-PDF
workflow; there are no current `/guide/pdf` or `/admin/guide/upload` routes.

## Authentication and Authorisation

- `POST /login` validates `app_users` and returns a random bearer token.
- Tokens are held only in memory and are not JWTs.
- `GET /me` refreshes username and role information.
- Normal protected endpoints call `_get_username()`.
- Admin endpoints call `_get_admin()`.
- The primary admin is resolved from `PRIMARY_ADMIN_USERNAME`, then the
  `is_primary` row, then a sole active admin fallback.
- Password changes and user deletion invalidate that user's active in-memory sessions.

## HTTP API

### Public static/runtime

- `GET /` - single-file UI
- `GET /logo.png`
- `GET /health`

### Authenticated user

- `POST /login`
- `GET /me`
- `GET /homepage-info`
- `GET /query/lookup?internal_part_no=`
- `GET /query/stream?internal_part_no=&suppliers=`
- `POST /query/feedback`
- `GET /parts`
- `GET /parts/search?q=`
- `POST /supplier/open`
- `GET /supplier/login-info`
- `POST /reyher/open`

`POST /reyher/open` starts a headed browser on the server and is only useful
when the server is running on the buyer's own graphical machine.

### Admin

- `POST /admin/homepage-info`
- `GET /admin/mapping`
- `POST /admin/upload-mapping`
- `GET /admin/mapping-template`
- `DELETE /admin/mapping`
- `GET /admin/runs`
- `GET /admin/runs/export` - XLSX export
- `GET /admin/runs/chart?range=week|month|all`
- `GET /admin/fx-settings`
- `POST /admin/fx-settings`
- `GET /admin/suppliers`
- `POST /admin/update-supplier`
- `GET /admin/users`
- `GET /admin/app-user`
- `POST /admin/update-app-user`
- `POST /admin/users`
- `POST /admin/update-user-password`
- `POST /admin/users/{username}/admin`
- `DELETE /admin/users/{username}`
- `POST /supplier/update-password`

## Query Flow and SSE

`GET /query/lookup` performs the non-scraping preview:

- product name;
- available supplier IDs and part numbers;
- current supplier `info_items`;
- unavailable suppliers.

`GET /query/stream`:

1. Repeats mapping lookup.
2. Applies the optional comma-separated supplier filter.
3. Runs selected suppliers concurrently under `SCRAPER_LIMIT`.
4. Emits supplier progress.
5. Attaches current active `info_items`.
6. Computes recommendation.
7. Logs the run to `query_runs`.
8. Emits the final result.

SSE event types:

- `progress`
- `password_required`
- `result`
- `error`

Login-related scraper errors are detected with keyword matching and emitted as
`password_required`, allowing admins to jump to supplier credential management.

## Scraper Contract

Every `browser/supplier_<id>.py` exposes:

```python
async def fetch_price(supplier_part_no: str, on_progress=None) -> dict:
    ...
```

Standard successful raw response:

```json
{
  "supplier_part_no": "9250240 25/18",
  "price_raw": 4.78,
  "price_unit_qty": 100,
  "currency": "EUR",
  "unit": "db",
  "stock": 73200,
  "queried_at": "2026-06-01T09:55:30",
  "product_url": "https://..."
}
```

`product_url` is optional and currently captured by some scrapers when the exact
detail page is known. The frontend prefers it over a generated search URL.

`stock` may be:

- integer quantity;
- location dictionary;
- human-readable string such as `Raktáron`;
- `None`.

Scraper responsibilities:

1. Restore a saved session when fresh.
2. Verify that the restored session is still authenticated.
3. Invalidate and recreate stale/invalid sessions.
4. Log in using `.env` credentials.
5. Search for an exact supplier part number.
6. Distinguish product-not-found from product-found-without-price.
7. Return raw price and its quantity basis, never a pre-normalised guess.
8. Close Chromium in a `finally` path.

Most sessions use a 20-hour freshness window. Reyher uses 23 hours. Koelner has
site-specific restore logic but still persists through `session_utils.py`.

## Canonical Buyer Feedback

`browser/messages.py` defines:

```python
MSG_NOT_FOUND = "jelenleg nem találom a webshopban a terméket"
MSG_NOT_PRICED = "a webshopban elérhető a termék, de nincs beárazva"
```

Use `MSG_NOT_FOUND` only when the mapped supplier part cannot be found after
login/search. Use `MSG_NOT_PRICED` when the exact product is present but no
numeric price is available.

`has_numeric_price()` treats placeholders such as POA, request-only text, dashes
or empty values as not priced.

Reyher normally falls back to a manual card for unexpected automation errors,
but these two canonical outcomes are deliberately re-raised and shown directly.

## Price Normalisation and Recommendation

Scrapers return the supplier's raw price and price basis:

```python
price_per_db = price_raw / price_unit_qty
```

For non-HUF currencies, only EUR is currently supported:

```python
price_per_db_huf = price_per_db * EUR_TO_HUF_RATE
fx_huf_rate = EUR_TO_HUF_RATE
```

The default exchange rate is 400 HUF/EUR if the environment value is absent,
invalid or non-positive. Admin changes update `.env` and `os.environ` immediately.

`compute_recommendation()`:

- excludes errors and results without usable prices;
- ranks HUF prices directly and EUR prices through `price_per_db_huf`;
- returns the cheapest supplier;
- reports the difference and saving percentage against second place;
- adds a stock warning when the runner-up's numeric stock is more than double
  the winner's stock;
- does not incorporate ordering-information rows into ranking.

## Product Links and Webshop Login Helper

The result-card button uses this order:

1. Open scraper-provided `product_url` when available.
2. Otherwise call `POST /supplier/open` and use `_SUPPLIER_SEARCH_URLS`.
3. For suppliers without a real part-specific URL, open the home/portal and copy
   the supplier part number to the clipboard.

Hopefix and KingB2B currently use home/portal fallback URLs. Other registered
suppliers have search/deep-link templates.

The Webshop login helper calls `GET /supplier/login-info`, opens supplier login
pages in the buyer's browser and supports copying shared credentials. Browser
password storage is expected to handle future autofill.

## Frontend Behaviour

`ui/index.html` is intentionally a single-file application with embedded CSS and
JavaScript. There is no frontend build step.

Main buyer workflow:

- session bootstrap from `sessionStorage`;
- optional homepage announcement;
- autocomplete and mapping preview;
- supplier filtering;
- per-supplier progress pipeline;
- recommendation card and feedback;
- supplier result cards with ordering information;
- product/deep-link opening;
- webshop login helper.

Admin tabs:

- Admin account
- Users
- Webshop login
  - homepage information
  - supplier credentials
  - supplier ordering information
- Part-number mapping
- Run log
- Exchange rate

Escape dynamic user/database text with `escHtml()` or `escAttr()` before adding
it to template strings.

## Running Locally

Direct Python development:

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Local URL:

```text
http://localhost:8000/
```

Docker:

```bash
docker compose up -d --build
```

Docker URL:

```text
http://localhost:8080/
```

The Docker image is based on the Playwright Python `v1.58.0-noble` image.
`docker-compose.yml` mounts both `assets/` and `.env`, allocates 256 MB shared
memory and uses `restart: unless-stopped`.

## Deployment

Hetzner deployment uses Docker Compose under `/opt/price_agent` and the
`price-agent.service` systemd unit.

Useful server commands:

```bash
systemctl status price-agent
systemctl restart price-agent
journalctl -u price-agent -f
docker compose -f /opt/price_agent/docker-compose.yml logs -f
```

Code-only updates still require rebuilding/recreating the container because the
repository source is copied into the Docker image:

```bash
cd /opt/price_agent
git pull
docker compose up -d --build
```

SSE reverse proxies must disable buffering.

Headed Playwright helpers open windows on the server, not on a remote buyer's
computer. Headless scraping and returned browser deep-links work remotely.

## Database Migrations

Run SQL files through the Supabase SQL Editor when provisioning or upgrading:

```text
deploy/supabase_app_users.sql
deploy/supabase_query_runs_username.sql
deploy/supabase_query_runs_feedback.sql
deploy/supabase_supplier_info_items.sql
```

The repository does not contain the original `article_mapping` or `query_runs`
table creation migrations, so those base tables must already exist.

## Adding a Supplier

1. Add `{supplier_id}_part_no` to `article_mapping` and the mapping upload column list.
2. Add supplier metadata and environment prefix to `SUPPLIER_META`.
3. Add its default URL to `_SUPPLIER_URLS` in `agent/tools.py`.
4. Create `browser/supplier_<id>.py` with the standard async contract.
5. Add the ID to `_IMPLEMENTED_SUPPLIERS`.
6. Add dispatch logic in `fetch_supplier_price()`.
7. Add search and login URLs in `main.py`.
8. Add headed/session maps only if that local-only flow is required.
9. Add the supplier to frontend filters/labels.
10. Verify raw unit quantity, currency, stock parsing, canonical errors, session
    reuse and product-link behaviour.

## Validation Before Finishing Changes

At minimum:

```bash
python3 -m py_compile main.py agent/tools.py browser/*.py
awk '/<script>/{flag=1;next}/<\/script>/{flag=0}flag' ui/index.html | node --check -
```

For scraper changes, run the affected scraper with a known mapped supplier part
number and inspect login, exact-match, price basis, stock and final URL.

For API/UI changes, start the local server and check:

```text
GET /health
login
mapping preview
selected-supplier query
admin save/reload
```

Do not commit:

- `.env`;
- runtime session JSON;
- `assets/homepage_info.json`;
- files under `WORK/`;
- unrelated user changes already present in the worktree.
