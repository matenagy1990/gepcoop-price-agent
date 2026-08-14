# CLAUDE.md

This file is the working technical guide for agents modifying this repository.
Last updated against the codebase and production environment: **2026-08-14**.
Use `git rev-parse --short HEAD` locally and on `/opt/price_agent` instead of
keeping a stale commit hash in this document.

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

There are currently **14 implemented supplier integrations** (Vipa added 2026-06).

## Two-App Platform

This repository hosts **two separate applications**:

| App | Internal port | Production URL | Description |
|---|---:|---|---|
| Price Agent | 8080 | `https://178.104.208.200/` | Single part number lookup, real-time result cards |
| Batch Price Agent | 8001 | `https://178.104.208.200/batch-agent/` | Bulk lookup (up to 400 parts × 14 suppliers), matrix view + Excel export |

Both apps are served from the same Docker Compose stack (`docker-compose.yml` in the root).
The Batch Price Agent lives under `batch-price-agent/` and imports scrapers from this repo at runtime — see `batch-price-agent/README.md` for its full documentation.

The Price Agent's UI (`ui/index.html`) includes an **app-selector page** that
lets the buyer switch between the two apps. Locally the Batch tile navigates to
port 8001; in production it navigates to `/batch-agent/` on the same HTTPS
origin. Before navigation, the user must already have a valid Price Agent
session and must enter the separate `BATCH_ACCESS_PASSWORD`.

Current batch execution rule:

- 1-50 unique part numbers: only immediate execution is allowed.
- 51-400 unique part numbers: only scheduled execution is allowed.
- The backend enforces both rules; they are not only frontend restrictions.
- In production, scheduled runs are assigned to the next free Budapest-time
  slot starting at 23:30, then every 30 minutes through 04:30. Occupied or
  elapsed slots are skipped, and allocation continues on the next night.

## Architecture

```
[Single-file browser UI: ui/index.html]
             |
             | Bearer auth + JSON/SSE
             v
[FastAPI: main.py]
  |-- authentication and admin operations
  |-- mapping preview and query orchestration
  |-- max 4 concurrent scraper executions (SCRAPER_LIMIT = asyncio.Semaphore(4))
  |-- recommendation and run logging
  |-- supplier deep-links and shared login helper
  |-- dynamically loaded Gép-Coopilot issue-intake router
  |
  |-- agent/tools.py
  |     |-- Supabase article mapping lookup
  |     |-- supplier scraper dispatch
  |     `-- per-piece and EUR-to-HUF normalisation
  |
  |-- browser/supplier_<id>.py          (14 scrapers)
  |     |-- fetch_price()               single-part lookup (price agent)
  |     |-- fetch_prices()              batch lookup (batch agent, one browser session)
  |     |-- _login_or_restore()         shared: browser launch + session restore/login
  |     `-- _search_and_parse()         shared: search one part, return price+stock
  |
  `-- Supabase
        |-- article_mapping
        |-- app_users
        |-- query_runs
        |-- argip_price_list
        |-- gepcoop_stock
        |-- supplier_info_items
        |-- copilot_tasks / copilot_conversations / copilot_messages
        `-- Storage: homepage information JSON
```

Important runtime properties:

- `SCRAPER_LIMIT = asyncio.Semaphore(4)` limits Chromium load across users.
- Scrapers use headless Chromium and run on the machine hosting FastAPI.
- Authentication tokens are stored only in the in-memory `sessions` dictionary.
  All users must log in again after a server restart.
- Batch API calls require both the Price Agent bearer token and a separate
  12-hour Batch access ticket. The Batch backend validates both against the
  Price Agent before serving protected endpoints.
- The UI is served with `Cache-Control: no-store`.
- The application depends on Supabase for mappings and authentication.

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
  vipa_otp.py
    Vipa OTP (one-time token) login flow. Shared with batch agent.
  supplier_<id>.py
    One scraper per implemented supplier. Each exposes both fetch_price()
    (single lookup) and fetch_prices() (batch lookup, one session).

ui/index.html
  Entire frontend: login page, app-selector, mapping preview, supplier
  selection, progress, result cards, feedback, webshop login helper and
  admin panel. Single-file, no build step.

deploy/
  Supabase SQL migrations, systemd unit and Hetzner setup script.

assets/
  logo.png
  tile-single.jpg           App-selector tile image for Price Agent.
  tile-batch.jpg            App-selector tile image for Batch Agent.
  sessions/                 Runtime Playwright sessions, gitignored.
  homepage_info.json        Runtime local mirror, gitignored.

docs/webshop_utmutato.md
  Operational webshop notes.

batch-price-agent/
  Companion bulk-query application. See batch-price-agent/README.md.

Gép-Coopilot/
  copilot_module.py
    OpenAI-assisted Hungarian issue intake, task persistence and admin API.
  supabase_copilot_tables.sql
    Copilot task/conversation/message tables and indexes.
  MVP_hibafelvetel_admin_attekinto.md
    Product-level MVP specification and conversation requirements.

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
| `argip` | table-backed import | none | EUR | no browser login |
| `inoxmare` | inoxmare.com | `SUPPLIER_L` | EUR | none |
| `vipa` | vipafasteners.com | `SUPPLIER_VIPA` | EUR | OTP e-mail token |

`ferdinand_part_no` is supported by mapping import/export, but Ferdinand is not
in `_IMPLEMENTED_SUPPLIERS`, has no scraper, and is therefore not queried.

### Vipa (14th supplier — OTP login)

Vipa uses a one-time password delivered by e-mail (no static password).
The login flow is in `browser/vipa_otp.py` and shared between both apps.
The session is saved to `assets/sessions/vipa_session.json` with a 20-hour
freshness window. If the price agent logs in, the batch agent reuses the same
session and vice versa.

In the price agent UI, a Vipa OTP panel appears in the result card when Vipa
is the selected/running supplier and no live session exists.

Relevant env vars: `SUPPLIER_VIPA_URL`, `SUPPLIER_VIPA_USERNAME` (no password).

## Configuration and Secrets

The root `.env` file is the current source of supplier credentials and runtime
settings. It is gitignored and must never be committed.

Required core variables:

```text
SUPABASE_URL
SUPABASE_KEY             Must be supabase-py >= 2.15.0 compatible (sb_secret_* format).
PRIMARY_ADMIN_USERNAME   Optional; defaults in main.py.
EUR_TO_HUF_RATE          Optional; defaults to 400.
BATCH_ACCESS_PASSWORD    Second gate for authenticated Batch Agent users.
COPILOT_ENABLED          true/false feature flag for the Gép-Coopilot UI.
OPENAI_API_KEY           Required for model-assisted Copilot classification.
OPENAI_MODEL             Defaults to gpt-4o-mini.
```

Each supplier uses:

```text
SUPPLIER_<A..L>_URL
SUPPLIER_<A..L>_USERNAME
SUPPLIER_<A..L>_PASSWORD
```

Additional fields:

```text
SUPPLIER_F_CUSTOMER_CODE
SUPPLIER_H_SHORTNAME
SUPPLIER_VIPA_URL
SUPPLIER_VIPA_USERNAME
```

Homepage information storage can be overridden with:

```text
SUPABASE_INFO_BUCKET     Default: internal-docs
SUPABASE_INFO_PATH       Default: homepage/info.json
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
argip_part_no
vipa_part_no
inoxmare_part_no
```

`lookup_mapping_all()` returns only non-empty mappings whose supplier is in
`_IMPLEMENTED_SUPPLIERS`.
The `inoxmare_part_no` column is queried by the implemented Inoxmare scraper.
The `argip_part_no` column is queried through the separate `argip_price_list`
table populated from the customer-uploaded Argip Excel.

Admin mapping upload:

- accepts CSV or XLSX;
- recognises Hungarian aliases such as `Gépcoop cikkszám`, `Cikknév`,
  `Iron trade`, `Schafer`, `King`, `Wasi`, `Argip`, `Vipa` and `Inoxmare`;
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

Active rows are attached to lookup and query results as `info_items`.

### `argip_price_list`

Created by `deploy/supabase_argip_price_list.sql`.

Stores the separately uploaded Argip Excel price list. `argip_part_no` is the
lookup key used by the mapping table. The live comparison uses `base_price_eur`
for ranking. `price_lvl_1_eur` and `price_lvl_2_eur` plus their MOQ fields are
returned to the frontend as informational tiers only.

### `gepcoop_stock`

Created by `deploy/supabase_gepcoop_stock.sql`.

Stores the separately uploaded own-stock table shown as the dedicated Gép-Coop
result card. Uploads fully replace the table content.
Cards show the first two rows and put additional rows in a collapsible section.
No block is rendered when the list is empty.

### Homepage information

The admin-editable homepage message is stored as JSON in Supabase Storage:

```text
bucket: internal-docs
path:   homepage/info.json
```

The application also writes `assets/homepage_info.json` as a local fallback and
mirror. Empty text hides the block.

### Gép-Coopilot tables

Created by `Gép-Coopilot/supabase_copilot_tables.sql`:

- `copilot_tasks`: structured issue data, status and optional admin note;
- `copilot_conversations`: one conversation linked to a submitted task;
- `copilot_messages`: original user/assistant messages in chronological order.

Task statuses are `open`, `in_progress` and `resolved`. Problem types are
`missing_price`, `wrong_price`, `missing_stock`, `error_message`, `slow_search`
and `other`.

If Supabase is unavailable, the Copilot temporarily falls back to
`Gép-Coopilot/copilot_local_store.json`. This keeps issue intake usable, but it
is a local runtime fallback rather than the production source of truth.

## Authentication and Authorisation

- `POST /login` validates `app_users` and returns a random bearer token.
- Tokens are held only in memory and are not JWTs.
- `GET /me` refreshes username and role information.
- `POST /batch/access` validates the Batch access password for an already
  authenticated Price Agent user and issues a 12-hour in-memory Batch ticket.
- `GET /batch/access/validate` validates the bearer token and Batch ticket
  together for the Batch backend.
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
- `GET /copilot/config` - public, read-only feature flag used to show the widget

### Authenticated user

- `POST /login`
- `GET /me`
- `POST /batch/access`
- `GET /batch/access/validate` - internal validation used by the Batch service
- `GET /homepage-info`
- `GET /query/lookup?internal_part_no=`
- `GET /query/stream?internal_part_no=&suppliers=`
- `POST /query/feedback`
- `GET /parts`
- `GET /parts/search?q=`
- `POST /supplier/open`
- `GET /supplier/login-info`
- `POST /reyher/open`
- `POST /copilot/chat`
- `POST /copilot/tasks`

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
- `GET /admin/argip-price`
- `POST /admin/argip-price/upload`
- `GET /admin/gepcoop-stock`
- `POST /admin/gepcoop-stock/upload`
- `GET /admin/suppliers`
- `POST /admin/supplier-info`
- `POST /admin/update-supplier`
- `GET /admin/users`
- `GET /admin/app-user`
- `POST /admin/update-app-user`
- `POST /admin/users`
- `POST /admin/update-user-password`
- `POST /admin/users/{username}/admin`
- `DELETE /admin/users/{username}`
- `POST /supplier/update-password`
- `GET /copilot/admin/tasks`
- `GET /copilot/admin/tasks/{task_id}`
- `POST /copilot/admin/tasks/{task_id}/status`

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

## Gép-Coopilot

The Gép-Coopilot is a focused Hungarian issue-intake assistant embedded in the
bottom-right corner of the Price Agent. It is not a general-purpose chatbot.

Runtime loading:

- `main.py` locates `*-Coopilot/copilot_module.py` with a filesystem glob and
  includes its router dynamically. Do not replace this with an accented literal
  path: macOS and Linux may store the `é` character with different Unicode
  normalisation.
- `COPILOT_ENABLED=true` controls backend feature availability. The buyer chat
  and admin ticket menu are currently additionally hidden by
  `COPILOT_CHAT_VISIBLE=false` and
  `COPILOT_ADMIN_TICKETS_VISIBLE=false` in `ui/index.html`.
- `/copilot/config` intentionally does not require authentication, because the
  frontend needs the feature flag while bootstrapping. Chat, submit and admin
  endpoints remain authenticated.

Conversation flow:

1. The buyer describes the concrete system problem in free text.
2. `/copilot/chat` uses OpenAI (`OPENAI_MODEL`, currently `gpt-4o-mini`) to
   determine whether this is a meaningful Price Agent issue and to extract
   problem type, product number and webshop names.
3. Greetings, repeated words and vague text such as `szia szia szia` do not
   advance the issue flow.
4. If product number and webshop are already present in the first description,
   the UI skips redundant questions and immediately prepares the summary.
5. Otherwise the UI requests the missing product number, then displays a
   multi-select list of supported webshops.
6. The buyer reviews the summary and can use `Módosítás` exactly once. The
   additional text is appended to the description and a new summary is shown.
7. `Véglegesítés` creates the task and stores the conversation. No task exists
   before explicit confirmation.
8. After successful submission the UI briefly confirms that the issue was
   recorded, then clears the conversation and returns to the opening prompt.

The launcher button also acts as a toggle: clicking it while the panel is open
collapses the panel without clearing the buyer's current local draft. Draft
state exists only in that browser tab's JavaScript memory, so another logged-in
buyer cannot see unfinished text.

Failure behaviour:

- OpenAI/API/parsing failures return the fixed buyer-facing fallback:
  `Még egy chatbot is megérdemel egy kis pihenést...`
- The deterministic fallback classifier can still recognise common concrete
  issue phrases, but it does not turn greetings into tickets.
- Off-topic requests are rejected with a short scope message.

Admin behaviour:

- `Gép-Coopilot Hibapult` lists the latest 200 tasks.
- The list can be filtered by status.
- Detail view shows structured fields, summary, original conversation, status
  and internal admin note.
- Admins can set `Nyitott`, `Folyamatban` or `Megoldva`.
- Current product state: the launcher/panel and Hibapult navigation are hidden;
  direct attempts to switch to the hidden admin tab fall back to `users`.
  Existing task/conversation/message data and backend routes are retained.

## Scraper Contract

Every `browser/supplier_<id>.py` exposes **two** entrypoints:

```python
# Single lookup — used by price agent
async def fetch_price(supplier_part_no: str, on_progress=None) -> dict:
    ...

# Batch lookup — used by batch agent (one browser session, many searches)
async def fetch_prices(part_nos: list[str], on_progress=None, on_item=None) -> list[dict]:
    ...
```

Both share two internal helpers that must never be duplicated:
- `_login_or_restore(pw, emit)` — launches Chromium, restores session or logs in fresh.
- `_search_and_parse(page, supplier_part_no, emit)` — searches one part, returns result dict.

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

`product_url` is optional and currently captured by most scrapers.
The frontend prefers it over a generated search URL.

`stock` may be integer quantity, location dictionary, human-readable string, or `None`.

Scraper responsibilities:

1. Restore a saved session when fresh.
2. Verify that the restored session is still authenticated.
3. Invalidate and recreate stale/invalid sessions.
4. Log in using `.env` credentials.
5. Search for an exact supplier part number.
6. Distinguish product-not-found from product-found-without-price.
7. Return raw price and its quantity basis, never a pre-normalised guess.
8. Close Chromium in a `finally` path.

Most sessions use a 20-hour freshness window. Reyher uses 23 hours.

Supplier-specific reliability rules added in August 2026:

- **Wasishop** proves authentication with the logout control, not the URL or
  search input; parses only the exact `.shipping_card_pos`; and retries one
  clean login if authentication disappears during search.
- **KingB2B** treats an empty response from an unverified restored session as a
  stale-session signal, retries one clean login, synchronises searches with the
  matching RD3 `eseguiRicerca` response, bypasses transient portal overlays via
  the result's native click handler, and reopens the family once if the SPA
  replaces a matched article row before price injection.

### Login stability notes (batch context)

Under high parallel load (`BATCH_SUPPLIER_LIMIT=8`) two suppliers showed false
login failures. Fixes applied:

- **Fabory**: login check changed from `wait_for_url("…/hu")` to
  `wait_for_function("!pathname.includes('/login')")`, timeout 15 s → 30 s.
- **Reyher**: `_is_logged_in` Quickinput selector timeout 4 000 ms → 10 000 ms.
- `BATCH_SUPPLIER_LIMIT` reduced to **4** for stability.

## Canonical Buyer Feedback

`browser/messages.py` defines:

```python
MSG_NOT_FOUND = "jelenleg nem találom a webshopban a terméket"
MSG_NOT_PRICED = "a webshopban elérhető a termék, de nincs beárazva"
```

Use `MSG_NOT_FOUND` only when the mapped supplier part cannot be found after
login/search. Use `MSG_NOT_PRICED` when the exact product is present but no
numeric price is available.

## Price Normalisation and Recommendation

Scrapers return the supplier's raw price and price basis:

```python
price_per_db = price_raw / price_unit_qty
```

For non-HUF currencies, only EUR is currently supported:

```python
price_per_db_huf = price_per_db * EUR_TO_HUF_RATE
```

The default exchange rate is 400 HUF/EUR if the environment value is absent,
invalid or non-positive. Admin changes update `.env` and `os.environ` immediately.

`compute_recommendation()`:

- excludes errors and results without usable prices;
- ranks HUF prices directly and EUR prices through `price_per_db_huf`;
- returns the cheapest supplier;
- reports the difference and saving percentage against second place;
- adds a stock warning when the runner-up's numeric stock is more than double
  the winner's stock.

## Frontend Behaviour

`ui/index.html` is a single-file application with embedded CSS and JavaScript.
There is no frontend build step.

### Pages / states

| Page element | ID | Visible when |
|---|---|---|
| Login page | `#login-page` | No bearer token in `sessionStorage`. **Starts hidden** — boot JS shows it only if no token, preventing a login flash when switching apps. |
| App selector | `#app-select-page` | After login, or when returning from batch agent with `?select=1`. Same background as login (photo + mesh + vignette + spotlight). |
| Main app | `#app-page` | After entering the Price Agent from the selector. |

### Boot logic

```javascript
// login-page starts with class="hidden" in HTML
if (authToken) {
  bootstrapSession();       // validate token, then show app or app-selector
} else {
  document.getElementById('login-page').classList.remove('hidden');
}
```

### App selector

- Two tiles: Price Agent and Batch Price Agent.
- Clicking Price Agent tile → `showApp()`.
- Clicking Batch tile → opens a password modal → on success redirects to port 8001.
- Background: identical to login page (full-screen photograph, mesh grid, vignette,
  mouse-following spotlight). Header is `position:absolute` so tiles are centered
  in the full viewport on all screen sizes.
- Spotlight effect generalised to work on both login and app-selector pages.

### Main buyer workflow

- session bootstrap from `sessionStorage`;
- optional homepage announcement;
- autocomplete and mapping preview;
- supplier filtering;
- per-supplier progress pipeline;
- recommendation card and feedback;
- supplier result cards with ordering information;
- product/deep-link opening;
- webshop login helper.
- bottom-right Gép-Coopilot issue intake when both backend and frontend
  visibility switches are enabled (currently hidden).

### Admin tabs

- Admin account
- Users
- Webshop login (homepage info, supplier credentials, supplier ordering info)
- Part-number mapping
- Argip price list
- Gép-Coop stock
- Run log
- Exchange rate
- Gép-Coopilot Hibapult with status filtering (implemented, currently hidden)

Escape dynamic user/database text with `escHtml()` or `escAttr()` before adding
it to template strings.

## Running Locally

Direct Python development:

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Local URL: `http://localhost:8000/`

Docker:

```bash
docker compose up -d --build
```

Docker URL: `http://localhost:8080/`

The Docker image is based on the Playwright Python `v1.60.0-noble` image.
`requirements.txt` must pin `playwright==1.60.0` to match — a mismatch causes
`BrowserType.launch: Executable doesn't exist` at runtime.

`docker-compose.yml` mounts both `assets/` and `.env`, allocates 256 MB shared
memory, uses `restart: unless-stopped`, and sets `init: true` on both services.
The Docker init (Tini) must remain enabled because the long-running Uvicorn
process otherwise becomes PID 1 and accumulates orphaned Chromium zombies after
Playwright lookups.

The same root `.env` is loaded by both containers. If the Copilot UI is restored,
production requires:

```text
COPILOT_ENABLED=true
OPENAI_API_KEY=<secret>
OPENAI_MODEL=gpt-4o-mini
```

## Deployment

Hetzner deployment uses Docker Compose under `/opt/price_agent` and the
`price-agent.service` systemd unit.

Server: Hetzner CPX42 (8 vCPU AMD, 16 GB RAM, 320 GB SSD) — ~€22.59/month.
IP: `178.104.208.200`
Production access goes through one Nginx HTTPS endpoint using a public
Let's Encrypt IP-address certificate; no purchased domain is required:

- Price Agent: `https://178.104.208.200/`
- Batch Agent: `https://178.104.208.200/batch-agent/`
- Container ports `8080` and `8001` bind only to `127.0.0.1` on the host.
- `deploy/enable-https.sh` provisions a six-day Let's Encrypt IP certificate,
  enables automatic renewal, and closes direct app ports.
- Nginx accepts public traffic on ports `80` and `443`; port `80` redirects to
  HTTPS. UFW allows SSH, HTTP and HTTPS and explicitly denies `8080/8001`.
- The active certificate was first issued on **2026-06-25**, is renewed by
  `snap.certbot.renew.timer`, and reloads Nginx through a deploy hook.

Useful server commands:

```bash
systemctl status price-agent
systemctl restart price-agent
journalctl -u price-agent -f
docker compose logs price-agent --tail=50
docker compose logs batch-price-agent --tail=50
certbot certificates
systemctl status snap.certbot.renew.timer
ufw status numbered
ss -lntp | grep -E ':(80|443|8080|8001) '
```

Update workflow:

```bash
cd /opt/price_agent
git pull
docker compose up -d --build
```

SSE reverse proxies must disable buffering (`proxy_buffering off`).

Post-deploy checks:

```bash
curl -sS https://178.104.208.200/health
curl -sS -o /dev/null -w '%{http_code}\n' https://178.104.208.200/batch-agent/
certbot renew --cert-name 178.104.208.200 --dry-run
```

## Known Deployment Pitfalls

| Problem | Cause | Fix |
|---|---|---|
| `BrowserType.launch: Executable doesn't exist` | `playwright==X` in `requirements.txt` doesn't match the Docker base image version | Pin `playwright` to match the base image (currently `v1.60.0`) |
| `invalid api key` from Supabase | `supabase-py < 2.15.0` doesn't support the `sb_secret_*` key format | Use `supabase>=2.15.0` in `requirements.txt` |
| Login page flashes on app switch | `#login-page` without `class="hidden"` in HTML | Always start login page hidden; boot JS removes `.hidden` only if no token |
| Docker build uses cache, old packages installed | `docker compose build` reuses pip layer | Run `docker compose build --no-cache` when `requirements.txt` changes |
| git pull fails with auth error on server | GitHub token expired or not set in remote URL | `git remote set-url origin https://<user>:<token>@github.com/...` |
| Copilot button remains hidden although `COPILOT_ENABLED=true` | Copilot router did not load, commonly due to an accented folder-name normalisation mismatch | Keep the `*-Coopilot/copilot_module.py` glob loader and verify `/copilot/config` returns `{"enabled":true}` |
| `/copilot/config` returns 401 during UI bootstrap | Feature discovery was incorrectly tied to bearer auth | Keep this endpoint public; protect only chat/task/admin operations |
| Copilot and Hibapult remain hidden with a healthy router | Product-level frontend visibility switches are false | Change `COPILOT_CHAT_VISIBLE` and/or `COPILOT_ADMIN_TICKETS_VISIBLE` only as an intentional release decision |
| KingB2B reports unstable/empty results from a fresh-looking session | ASP.NET/RD3 server state expired before the saved browser-state age | Keep one clean-session retry and RD3 response synchronisation; invalidate only the KingB2B session file when diagnosing |
| KingB2B reports `MSG_NOT_PRICED` although the family has a numeric price | SPA replaced a transient old row with the new family view | Keep the one-time family reopen in `_extract_row` and exact row/price wait |
| Wasishop accepts a restored page but returns unrelated variant data | Search box/URL was mistaken for authentication or parsing was page-global | Require the logout marker and scope parsing to the exact article card |
| Chromium zombie count grows after scraper runs | Uvicorn is PID 1 and does not reap orphaned browser children | Keep `init: true` on both Compose services and recreate the containers |

## Database Migrations

Run SQL files through the Supabase SQL Editor when provisioning or upgrading:

```text
deploy/supabase_app_users.sql
deploy/supabase_query_runs_username.sql
deploy/supabase_query_runs_feedback.sql
deploy/supabase_supplier_info_items.sql
deploy/supabase_article_mapping_new_suppliers.sql
deploy/supabase_argip_price_list.sql
deploy/supabase_gepcoop_stock.sql
Gép-Coopilot/supabase_copilot_tables.sql
```

Batch agent schema and incremental migration:

```text
batch-price-agent/deploy/supabase_batch_tables.sql
batch-price-agent/deploy/supabase_batch_runner_migration.sql
```

## Adding a Supplier

1. Add `{supplier_id}_part_no` to `article_mapping` and the mapping upload column list.
2. Add supplier metadata and environment prefix to `SUPPLIER_META` in `main.py`.
3. Add its default URL to `_SUPPLIER_URLS` in `agent/tools.py`.
4. Create `browser/supplier_<id>.py` with both `fetch_price()` and `fetch_prices()`.
   Use the shared `_login_or_restore` + `_search_and_parse` pattern.
5. Add the ID to `_IMPLEMENTED_SUPPLIERS`.
6. Add dispatch logic in `fetch_supplier_price()`.
7. Add search and login URLs in `main.py`.
8. Add the supplier to `batch-price-agent/shared/supplier_registry.py`.
9. Add the supplier to frontend filters/labels in both UIs.
10. Verify raw unit quantity, currency, stock parsing, canonical errors, session
    reuse, product-link behaviour, and batch performance.

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
GET /copilot/config reflects the backend flag; buyer/admin Copilot UI remains
hidden while its two frontend visibility switches are false
login → app-selector appears (no login flash)
enter price agent → mapping preview works
selected-supplier query streams correctly
switch to batch agent (tile click)
when intentionally re-enabled: Copilot rejects vague text, recognises a
complete issue and submits a task; admin status filter and update work
admin save/reload
```

Do not commit:

- `.env`;
- runtime session JSON;
- `assets/homepage_info.json`;
- files under `WORK/`;
- unrelated user changes already present in the worktree.
