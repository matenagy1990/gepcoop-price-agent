# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Automates supplier price and stock lookups for Gép-Coop procurement staff. A user enters an internal part number; the system translates it to a supplier part number, scrapes the supplier's website via Playwright, and returns the current price, unit, and stock level.

## Architecture

The system uses an **AI agent + tool calling** pattern:

```
[Streamlit UI] → POST /query → [FastAPI] → [Claude Agent]
                                                ├── lookup_mapping()   → reads assets/article_ID.csv
                                                └── fetch_supplier_price() → Playwright → supplier site
```

- The Claude agent orchestrates the workflow but never accesses websites directly
- `lookup_mapping` translates Gép-Coop internal part numbers to supplier part numbers
- `fetch_supplier_price` dispatches to a per-supplier Playwright script based on `supplier_id`
- The agent never guesses prices or stock — if a tool fails, it returns a human-readable error

## Planned Project Structure

```
price_agent/
├── .env                        # API keys and supplier credentials (not committed)
├── assets/
│   ├── article_ID.csv          # Internal → supplier part number mapping
│   └── user.csv                # Supplier URLs and login usernames (reference only)
├── main.py                     # FastAPI entry point, POST /query endpoint
├── agent/
│   ├── agent.py                # Claude agent with tool calling loop
│   └── tools.py                # lookup_mapping and fetch_supplier_price definitions
├── browser/
│   └── supplier_csavarda.py    # Playwright script for csavarda.hu (pilot supplier)
├── ui/
│   └── app.py                  # Streamlit frontend
└── requirements.txt
```

## Key Data Files

### `assets/article_ID.csv`
Maps Gép-Coop internal part numbers to Csavarda part numbers:
```
Gép-Coop cikkszám,Csavarda cikkszám
934128ZN,934012000000801000
```

### `assets/user.csv`
Reference list of supplier websites and usernames. Passwords are stored in `.env`, not here.

## Environment Variables (`.env`)

```
ANTHROPIC_API_KEY=...
SUPPLIER_A_URL=https://csavarda.hu/
SUPPLIER_A_USERNAME=...
SUPPLIER_A_PASSWORD=...
```

The pilot supplier is `csavarda.hu` (`supplier_id = "supplier_a"`). Adding a new supplier means adding new `SUPPLIER_X_*` vars here and a new Playwright script in `browser/`.

## Running the Project

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Start the backend
uvicorn main:app --reload

# Start the UI (separate terminal)
streamlit run ui/app.py
```

## Browser Tool Pattern

Each supplier gets its own script in `browser/`. The script must:
1. Read credentials from `.env` via `python-dotenv`
2. Log in to the supplier site
3. Search for the given `supplier_part_no`
4. Return a structured dict: `{supplier_part_no, price, currency, unit, stock, queried_at}`
5. Raise a descriptive exception on failure (login failed, product not found, selector mismatch)

CSS selectors are hardcoded per supplier script. When the supplier site changes layout, only that script needs updating.

## Price Normalisation (critical business rule)

Each supplier quotes prices in different units. All prices **must be normalised to price per 1 db (piece)** before being returned, so results from different suppliers are directly comparable.

| Supplier | Raw price example | Unit | Normalisation |
|---|---|---|---|
| csavarda.hu | `6,76 Ft / db` | 1 db | ÷ 1 |
| irontrade.hu | `249,60 Ft / 1.000 db` | 1000 db | ÷ 1000 |

The browser script returns the **raw** price and quantity unit as-is from the page. Normalisation happens in `tools.py` (`fetch_supplier_price`) before the result is handed back to the agent:

```python
price_per_db = raw_price / price_quantity   # e.g. 249.60 / 1000 = 0.2496
```

The final result always includes:
- `price_per_db` — normalised, comparable price per 1 piece
- `price_raw` — original price as shown on the supplier site (e.g. `249.60`)
- `price_unit_qty` — the quantity the raw price applies to (e.g. `1000`)
- `unit` — the unit name, always `"db"`

The UI displays both: the normalised price (`0.25 Ft/db`) and the original (`249.60 Ft / 1.000 db`).

## Tool Return Contract

`lookup_mapping` output:
```json
{"supplier_id": "supplier_a", "supplier_part_no": "Y", "supplier_url": "https://csavarda.hu/"}
```

`fetch_supplier_price` output:
```json
{
  "supplier_part_no": "Y",
  "price_per_db": 0.2496,
  "price_raw": 249.60,
  "price_unit_qty": 1000,
  "currency": "HUF",
  "unit": "db",
  "stock": 114000,
  "queried_at": "2026-02-24T11:22:00"
}
```

## Adding a New Supplier

1. Add rows to `assets/article_ID.csv` with the new supplier's part numbers and a new `supplier_id`
2. Add `SUPPLIER_X_URL`, `SUPPLIER_X_USERNAME`, `SUPPLIER_X_PASSWORD` to `.env`
3. Create `browser/supplier_x.py` following the same interface as `supplier_csavarda.py`
4. Register the new `supplier_id` → script mapping in `agent/tools.py`
