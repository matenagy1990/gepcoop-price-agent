# Batch Price Agent — technikai leírás

A **Tömeges árlekérdező** (batch agent) műszaki útmutatója.
Utoljára ellenőrizve a kódbázishoz és az éles szerverhez:
**2026-06-25** (`main`: `8f8883f`).

> Ez a fájl **csak a batch agentet** írja le. A Price Agent (egyedi árlekérdező)
> teljes dokumentációja a gyökérben: `CLAUDE.md`. A két alkalmazás közös scrapereket
> és közös Supabase adatbázist használ.

---

## 1. Mi ez és mire való?

A beszerző **sok** Gép-Coop cikkszámot ad meg egyszerre (max. 400), kiválasztja a
webshopokat, és az alkalmazás mindet lekérdezi, majd egy összehasonlító mátrixban
+ Excel exportban adja vissza az árakat és készleteket.

| | Price Agent (egyedi) | Batch Agent (tömeges) |
|---|---|---|
| Cikkszám / futás | 1 | max. 400 |
| Webshop / futás | 1+ (párhuzamos) | 1–14 (párhuzamos) |
| Böngésző / webshop | 1 (nyit–zár) | 1 (nyit–sok cikk–zár) |
| Eredmény | valós idejű cards | mátrix + Excel |
| Belső port | 8080 | 8001 |
| Éles útvonal | `/` | `/batch-agent/` |

---

## 2. Hogyan kapcsolódik a Price Agenthez?

A batch agent **nem másolja le** a scrapereket — a price agent kódját importálja
futásidőben. Egy scrapert elég egy helyen javítani; a javítás mindkét appra hat.

```
batch-price-agent/                     GepcoopPriceAgent/  (price agent gyökere)
  main.py  ──── importál ────────────►  agent/tools.py       (fetch_supplier_price, árfolyam)
  batch/runner.py ── importál ────────►  browser/supplier_*.py (14 scraper)
                                         browser/vipa_otp.py   (Vipa OTP belépés)
```

Az import útvonalát a **`PRICE_AGENT_PATH`** env változó adja meg
(lokálisan `..`, Dockerben `/app/GepcoopPriceAgent`). A `batch/runner.py`
ezt teszi a Python keresési útvonalára (`sys.path`).

---

## 3. Architektúra (fájlonkénti felelősség)

```
batch-price-agent/
├── main.py                     FastAPI app: HTTP + SSE endpointok, mentés Supabase-be,
│                               ütemező háttérfolyamat, Vipa-kezelés
├── batch/
│   ├── planner.py              Mapping előellenőrzés (egyetlen kötegelt Supabase-lekérdezés)
│   ├── runner.py               Scraperek futtatása, SSE esemény-stream
│   ├── aggregator.py           Eredményekből összehasonlító mátrix
│   └── export_excel.py         Excel (.xlsx) generálás (csak Mátrix lap)
├── shared/
│   ├── supabase_client.py      Cache-elt Supabase kliens
│   ├── supplier_registry.py    Választható webshopok listája + keresési URL-ek
│   └── price_utils.py          Ár-segédfüggvények
├── ui/index.html               Teljes felhasználói felület (egy fájl, Inter/Oswald dizájn)
├── Dockerfile                  Playwright Python v1.60.0-noble alapkép
├── requirements.txt            Python csomagok (playwright==1.60.0, supabase>=2.15.0)
└── .env / .env.example         Titkok (a .env NEM kerül gitbe)
```

---

## 4. A feldolgozás lépésről lépésre

1. **Cikkszámok megadása + webshopok kiválasztása** (UI).
   - A bemenet normalizált, egyedi cikkszámai számítanak a limiteknél.
   - 1-50 cikkszám esetén csak azonnali futás választható.
   - 51-400 cikkszám esetén csak ütemezett futás választható.
   - A korlátokat a backend is ellenőrzi.
2. **Mapping előellenőrzés** (`POST /batch/preview` → `batch/planner.py`):
   - Egyetlen kötegelt Supabase-lekérdezéssel (`.in_()`) nézzük meg, melyik
     Gép-Coop cikkszámhoz melyik webshopban van társított cikkszám.
   - A felület mutatja: hány keresés lehetséges, hol hiányzik a mapping.
3. **Futás indítása** (`POST /batch/run` → `batch/runner.py`):
   - Webshoponként csoportosítjuk a feladatokat.
   - Legfeljebb `BATCH_SUPPLIER_LIMIT` (alapértelmezés: **4**) webshopot
     futtatunk párhuzamosan.
   - Minden webshopon belül **egy böngészőben**, egy bejelentkezéssel kérdezzük
     le az összes cikket (`fetch_prices`).
   - Az eredményeket SSE-n streameljük és Supabase-be mentjük.
4. **Eredmény** (`aggregator.py` + `export_excel.py`):
   - Összehasonlító mátrix (cikk × webshop, a legolcsóbb kiemelve zölddel).
   - Letölthető Excel (csak Mátrix lap, külön legjobb-ajánlat összesítővel).

---

## 5. A scraperek: `fetch_price` vs `fetch_prices`

Minden webshop-scraper (`browser/supplier_*.py`) **kétféle belépőt** kínál:

| Függvény | Ki használja | Viselkedés |
|---|---|---|
| `fetch_price(part)` | Price agent (egyedi) | 1 böngésző, belép, 1 cikk, zár |
| `fetch_prices(parts, …)` | Batch agent (tömeges) | 1 böngésző, belép **egyszer**, **N** cikket keres, zár |

A közös logika két belső segédben van:

- `_login_or_restore(pw, emit)` — böngésző indítás + session visszaállítás vagy friss login
- `_search_and_parse(page, part, emit)` — egy cikk keresése + ár/készlet leolvasása

`fetch_prices` szerződése:
```python
async def fetch_prices(part_nos, on_progress=None, on_item=None) -> list[dict]:
    # Visszaad egy listát a part_nos sorrendjéhez igazítva.
    # Minden elem VAGY siker-dict, VAGY {"supplier_part_no": ..., "error": ...}.
    # Egy cikk hibája SOHA nem szakítja meg a többit.
    # on_item(index, total, part_no, result|None, error|None) — élő haladás.
```

### Dispatch és fallback

A `batch/runner.py` minden webshopnál ellenőrzi: van-e `fetch_prices`?
- **Van** → egy böngésző, egy bejelentkezés (gyors, kötegelt út).
- **Nincs** → cikkenkénti `fetch_price` hívás (régi út — biztonság hálóként marad).

Jelenleg **mind a 14 scraper** kínál `fetch_prices`-t.

---

## 6. Cikkszám-mapping (egyetlen kötegelt lekérdezés)

`batch/planner.py` a Supabase `article_mapping` táblából nézi ki a mappingokat.

Egyetlen `.in_()` lekérdezés fut kis-/nagybetű-független párosítással (eredeti
+ csupa nagy + csupa kicsi alak). 100 cikk: régi N+1 módszer ~6,9 s → jelenleg
~0,1 s.

---

## 7. Vipa OTP belépés

A Vipa egyszer használatos e-mail tokennel lép be (OTP, statikus jelszó nincs).

- Session fájl: `…/GepcoopPriceAgent/assets/sessions/vipa_session.json`
  (megosztott — ha a price agentben belép valaki, a batch agent is használja).
- Endpointok: `GET /vipa/status`, `POST /vipa/initiate-login`, `POST /vipa/complete-login`.
- Motor: `browser/vipa_otp.py` (a price agent gyökerében, mindkét app importálja).
- A token-popup **csak akkor** jelenik meg, ha (a) Vipa ki van választva **és**
  (b) nincs friss session. Élő session esetén nincs popup.
- Env: `SUPPLIER_VIPA_URL`, `SUPPLIER_VIPA_USERNAME` (nincs jelszó-változó).

---

## 8. Futás-gating (csak kereshető webshopok)

Indításkor a UI kiszámolja, mely kiválasztott webshopokban van **mapping utáni
kereshető cikkszám**. Ahol egy cikknek sincs mappingje, az a webshop nem kerül
be a futásba — nincs üres oszlop a mátrixban, és Vipa esetén OTP sem kérünk
feleslegesen.

---

## 9. Ütemezett futások

A mapping után a cikkszámok számától függően pontosan egy futtatási mód érhető el:

- **1-50 egyedi cikkszám:** csak azonnali futás (`POST /batch/run`).
- **51-400 egyedi cikkszám:** csak ütemezett futás (`POST /batch/schedule`).

A felület elrejti a nem használható opciót, de a backend is visszautasítja a
szabályt megkerülő közvetlen API-kérést.

### Környezet-mód

A `_is_server_mode()` függvény (`main.py`) felismeri, hol fut az app:

| Környezet | Ütemezési mód | UI viselkedés |
|---|---|---|
| Lokál (direct uvicorn) | **manuális** | Pontos dátum+perc választó (bármely jövőbeli perc) |
| Szerver (Docker/Hetzner) | **automatikus** | Gomb → backend kiosztja a következő szabad félórás sávot 23:30-tól |

Érzékelés: `/.dockerenv` fájl létezése (Docker-specifikus) + `SCHEDULE_MODE` env
változó (felülírható). A `docker-compose.yml`-ben `SCHEDULE_MODE: auto`.

Automatikus sávok: 23:30 → 00:00 → 00:30 → … → 04:30
(a foglalt sávokat kihagyja).

### Háttér-ütemező (`_scheduler_loop`)

Indításkor elindul egy háttérfolyamat (`asyncio.create_task`), amely **60 másodpercenként**:
1. Lekérdezi a DB-ből az esedékes futásokat (`status='scheduled' AND scheduled_at <= now`).
2. Atomikusan (`scheduled` → `running`) elindítja a `_execute_batch` közös végrehajtón.

Ha egy ütemezést töröltél, az ütemező nem találja a DB-ben → soha nem indul el.
Egyetlen igazságforrás = az adatbázis.

### Korábbi futások UI-ban

- „⏰ ütemezve \<időpont\>" jelölés az ütemezett sorokon.
- **„Új"** badge a még meg nem nyitott eredményeken (localStorage alapján).
- **🗑 Törlés** minden sorhoz (ütemezett és lefutott egyaránt); éppen futó nem törölhető.
- A projekt neve után külön oszlopban látható a futtató neve.
- A lista részleges egyezéssel szűrhető projekt- és futtatónévre.
- A backend legfeljebb 500 sort ad vissza, a UI szűréskor query paramétereket küld.

### Futtató azonosítása és kijelentkezés

A Batch csempe csak érvényes Price Agent bejelentkezés után érhető el. A
felhasználó ezután megadja a `BATCH_ACCESS_PASSWORD` második hozzáférési kódot.
A Price Agent ezt szerveroldalon ellenőrzi, majd egy 12 órás, véletlen Batch
hozzáférési jegyet ad.

A bearer token és a Batch-jegy URL hashben kerül át a Batch alkalmazásnak
(lokálisan `:8001`, szerveren `/batch-agent/`),
amely azonnal eltávolítja őket a címsorból és saját `sessionStorage`-ában
tárolja. Minden Batch API-kérés elküldi:

```text
Authorization: Bearer <price-agent-token>
X-Batch-Access-Token: <batch-ticket>
```

A Batch backend a Price Agent `/batch/access/validate` endpointján ellenőrzi
mindkét értéket. A futtató nevét a hitelesített válaszból veszi, ezért azt a
böngésző nem tudja más felhasználó nevére átírni.

A Batch fejlécében külön `Kijelentkezés` gomb van. Ez törli a batch
tokeneket, majd a Price Agent alkalmazásválasztójához
irányít, ahol a közös Price Agent munkamenet is törlődik.

Közvetlen Batch-megnyitáskor, hiányzó/lejárt tokennél vagy lejárt Batch
jegynél a felület visszairányít a Price Agent alkalmazásválasztójához.

### Aktív futás megszakítása

Az azonnali futás külön `asyncio.Task` objektumban fut, amelyet a backend
`run_id` alapján nyilvántart. A `POST /batch/run/{run_id}/cancel` meghívása:

1. megszakítja az aktív taskot;
2. `cancelled` státuszt ment a `batch_runs` táblába;
3. `cancelled` SSE eseményt küld;
4. a frontend törli az aktív futás állapotát és visszanavigál az
   `Új batch árlekérdezés` oldalra.

A Korábbi futások táblából a `running` sor szintén megszakítható. Már befejezett,
hibás vagy törölt futás nem szakítható meg utólag.

### Adatbázis-követelmény

```sql
-- Egyszeri migráció, már fut a Supabase-en:
alter table batch_runs add column if not exists scheduled_at timestamptz;
```

A futtató nevének mentéséhez és a projekt/futtató szerinti gyors szűréshez
futtasd a Supabase SQL Editorban:

```sql
alter table public.batch_runs
    add column if not exists runner_name text;

alter table public.batch_runs
    add column if not exists scheduled_at timestamptz;

create extension if not exists pg_trgm;

create index if not exists batch_runs_project_name_trgm_idx
    on public.batch_runs using gin (project_name gin_trgm_ops);

create index if not exists batch_runs_runner_name_trgm_idx
    on public.batch_runs using gin (runner_name gin_trgm_ops);
```

A teljes migráció külön fájlban is megtalálható:
`deploy/supabase_batch_runner_migration.sql`.

`status` mező lehetséges értékei: `scheduled`, `running`, `completed`, `partial`, `failed`, `cancelled`.

---

## 10. Státuszok és megjelenítés

A magyar szövegek az UI-ban és Excelben azonosak, de a színezési szabály
szándékosan eltér:

| `scrape_status` | Megjelenített szöveg | UI mátrix |
|---|---|---|
| *(nincs mapping)* | Hiányzó mapping | szürke |
| `not_found` | Termék nincs a webshopban | piros |
| `not_priced` | Termék nincs beárazva | piros |
| `timeout` | Technikai hiba | piros |
| `login_failed` | Technikai hiba | piros |
| `error` | Technikai hiba | piros |
| `ok` | ár + készlet megjelenik | zöld (legjobb ár) |

Az Excelben nincs piros vagy szürke háttér. Ott kizárólag a legjobb ajánlat
egységár-cellája kap zöld kitöltést; a hibák és hiányzó mappingok csak szöveggel
jelennek meg.

---

## 11. Excel export

Az export egyetlen **Mátrix** munkalapot tartalmaz (a részletes lap el lett távolítva).

A terméknév után egy háromoszlopos **Összesítő** mutatja a legjobb ajánlat
egységárát, készletét és webshopját. Az adattáblában kizárólag a legjobb
webshop egységár-cellája kap zöld kiemelést; hibás vagy mapping nélküli
eredményhez nem tartozik piros vagy szürke háttérszín.

- Fájlnév: `batch_<projektnév>_<run_id[:8]>.xlsx` — a projektnévből csak ASCII
  karakterek kerülnek be (ékezetes betűk `_`-ra cserélve, hogy HTTP fejlécben
  ne okozzanak `latin-1` kódolási hibát).
- Fejléc: kék (`#2F5496`), aláfejléc: (`#4472C4`), legjobb ár: halvány zöld kiemelés.
- Hiányzó mapping és hibás eredmény: magyar szöveg, háttérszínezés nélkül.
- Link oszlop: `=HYPERLINK(...)` képlettel, ha a scraper visszaadott `product_url`-t.

---

## 12. API endpointok

| Metódus | Útvonal | Mit csinál |
|---|---|---|
| `GET` | `/` | UI (ui/index.html) kiszolgálása |
| `GET` | `/config` | Ütemezési mód, auto-paraméterek, `batch_max_items=400`, `immediate_max_items=50` |
| `GET` | `/suppliers` | Választható webshopok listája |
| `GET` | `/vipa/status` | Van-e friss Vipa session? `{"live": true/false}` |
| `POST` | `/vipa/initiate-login` | Vipa OTP e-mail kérése |
| `POST` | `/vipa/complete-login` | Token beküldése, session mentése |
| `POST` | `/batch/preview` | Mapping előellenőrzés (nem indít scrapert) |
| `POST` | `/batch/run` | Azonnali batch futás, visszaad `batch_run_id`-t |
| `POST` | `/batch/schedule` | Ütemezett futás (auto: kiosztott sáv; manuális: megadott perc) |
| `GET` | `/batch/run/{id}/progress` | SSE stream a futás haladásáról |
| `GET` | `/batch/run/{id}` | Egy futás eredménye (mátrix) |
| `GET` | `/batch/runs` | Korábbi + ütemezett futások; `project_name`, `runner_name`, `limit` szűrők |
| `POST` | `/batch/run/{run_id}/cancel` | Aktív futás megszakítása és `cancelled` státusz mentése |
| `DELETE` | `/batch/run/{id}` | Futás törlése (ütemezett/lefutott; futó nem) |
| `GET` | `/batch/run/{id}/export.xlsx` | Excel letöltés |

---

## 13. Környezeti változók (`.env`)

A `.env` fájl **NEM kerül gitbe**. Sablon: `.env.example`.
A szerveren a gyökér `.env`-ből táplálkozik mindkét alkalmazás
(`env_file: - .env` a `docker-compose.yml`-ben).

| Változó | Leírás |
|---|---|
| `SUPABASE_URL` | Supabase projekt URL |
| `SUPABASE_KEY` | Supabase API kulcs (`sb_secret_*` formátum, **supabase-py ≥ 2.15.0 kell**) |
| `EUR_TO_HUF_RATE` | EUR→HUF árfolyam (pl. `350`) |
| `BATCH_MAX_ITEMS` | Max. cikkszám / futás (alapértelmezés: `400`) |
| `BATCH_IMMEDIATE_MAX_ITEMS` | Azonnali futás felső határa (alapértelmezés: `50`); efelett csak ütemezés engedélyezett |
| `BATCH_SUPPLIER_LIMIT` | Párhuzamos webshopok száma. **Ajánlott: 4** — magasabb értéknél (8+) terhelés alatt hamis login-failed léphet fel |
| `SCRAPER_TIMEOUT_SECONDS` | Egy lekérdezés időkorlátja másodpercben |
| `SCRAPER_RETRY_COUNT` | Újrapróbálkozások száma hiba esetén (csak a fallback per-cikk úton) |
| `SCHEDULE_MODE` | `auto` vagy `manual`. Ha nincs megadva: `/.dockerenv` alapján dönt. Szerveren `auto` |
| `PRICE_AGENT_PATH` | A price agent gyökerének elérési útja (importhoz). Lokál: `..`, Docker: `/app/GepcoopPriceAgent` |
| `PRICE_AGENT_AUTH_URL` | A Price Agent belső címe a bearer + Batch-jegy ellenőrzéséhez. Lokál: `http://127.0.0.1:8080`, Docker: `http://price-agent:8080` |
| `BATCH_ACCESS_PASSWORD` | A bejelentkezés után kért második Batch hozzáférési kód; a gyökér `.env`-ben tárolandó |
| `SUPPLIER_*_URL/USERNAME/PASSWORD` | Webshop belépési adatok (price agent `.env`-jéből másolva) |
| `SUPPLIER_VIPA_URL/USERNAME` | Vipa (OTP-alapú, nincs jelszó) |

---

## 14. Lokális futtatás

A batch agent a price agent virtuális környezetét (`.venv`) és kódját használja.

```bash
cd batch-price-agent
PRICE_AGENT_PATH=/abszolut/ut/a/GepcoopPriceAgent \
  ../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

Ezután: <http://127.0.0.1:8001>

A Price Agent helyi integrált tesztelésnél a `8080`-as porton fut, a Batch
Agent pedig a `8001`-esen. Az alkalmazásválasztó helyben a `8001`-es portra,
éles környezetben a `/batch-agent/` útvonalra irányít.

Helyi tesztelésnél a Batch alkalmazást a Price Agent alkalmazásválasztójából
nyisd meg. Így megkapja az érvényes bearer tokent és a szerveroldalon ellenőrzött
Batch-jegyet. A `:8001` cím közvetlen megnyitása hozzáférés nélkül visszairányít.

---

## 15. Szerver / deploy

- Hetzner CPX42 szerver (8 vCPU AMD, 16 GB RAM, 320 GB SSD): `178.104.208.200`
- Price Agent: `https://178.104.208.200/`
- Batch Agent: `https://178.104.208.200/batch-agent/`
- A `8080` és `8001` Docker-portok csak `127.0.0.1` címen érhetők el.
- Kívülről az Nginx `443`-as HTTPS végpontja fogadja a forgalmat.
- A `80`-as HTTP-port minden kérést HTTPS-re irányít.
- A nyilvánosan megbízható Let's Encrypt IP-tanúsítvány hatnapos, de a
  `snap.certbot.renew.timer` automatikusan megújítja, majd újratölti az Nginxet.
- Az UFW csak az SSH (`22`), HTTP (`80`) és HTTPS (`443`) portokat engedi be;
  a `8080` és `8001` kívülről tiltott.
- A gyökér `docker-compose.yml` mindkét szolgáltatást indítja.
- A batch konténer a price agent kódját **csak olvashatóan** (`ro`) csatolja,
  de a `assets/sessions` mappát **írhatóan** — Vipa session mindkét appból menthető.
- `SCHEDULE_MODE: auto` a szerveren: 23:30-tól, 30 perces sávok.
- `restart: unless-stopped` biztosítja, hogy reboot után újraindul.

### Frissítés a szerveren

```bash
cd /opt/price_agent
git pull
docker compose up -d --build
```

### Szerverellenőrzés

```bash
curl -sS https://178.104.208.200/health
curl -sS -o /dev/null -w '%{http_code}\n' https://178.104.208.200/batch-agent/
certbot certificates
systemctl status snap.certbot.renew.timer
ufw status numbered
```

Ha csak a batch agentet kell frissíteni:
```bash
docker compose up -d --build batch-price-agent
```

---

## 16. Ismert deployment-csapdák

| Tünet | Ok | Megoldás |
|---|---|---|
| `BrowserType.launch: Executable doesn't exist` | `playwright` verzió a `requirements.txt`-ben nem egyezik a Docker base image verziójával | Pin `playwright==1.60.0` (egyezik a `playwright/python:v1.60.0-noble` base image-dzsel) |
| `invalid api key` (Supabase) | `supabase-py < 2.15.0` nem ismeri az `sb_secret_*` kulcsformátumot | `supabase>=2.15.0` a `requirements.txt`-ben |
| Docker build gyors (0.3 s), de a csomag verziója nem változik | Régi pip-réteg cache-elve | `docker compose build --no-cache batch-price-agent` |
| `git pull` auth hiba a szerveren | GitHub token lejárt | `git remote set-url origin https://<user>:<token>@github.com/matenagy1990/gepcoop-price-agent.git` |
| git pull blokkol: helyi módosítás | Manuálisan szerkesztett fájl ütközik | `git checkout <fájl> && git pull` |

---

## 17. UI dizájn

A batch agent UI a price agent vizuális stílusát követi:

- **Betűtípusok:** Inter (szöveg), Oswald (fejlécek, gombok), JetBrains Mono (kódok)
- **Paletta:** `--navy: #161B63`, `--navy2: #1F4977`, `--blue: #009fe6`
- **Gombok:** gradiens háttér, 16px border-radius, hover-emelkedés
- **Kártyák:** fehér háttér, enyhe árnyék, 16px radius
- **Fő nézetek:** Új batch árlekérdezés, futási folyamat, eredménymátrix,
  korábbi futások
- **Korábbi futások:** projekt- és futtatónév-szűrés, státusz, időpont,
  megnyitás/törlés/megszakítás műveletek

A felület egyetlen `ui/index.html` fájl (beágyazott CSS + JS, nincs build lépés).

---

## 18. Supabase adatmodell

### `batch_runs`

Egy futás fejléce:

```text
id
project_name
runner_name
created_at
scheduled_at
status
selected_suppliers[]
total_input_count
unique_part_count
searchable_count
missing_mapping_count
success_count
error_count
completed_at
duration_ms
```

### `batch_run_items`

Az egyedi bemeneti Gép-Coop cikkszámok eredeti sorrendben. Tartalmazza a
terméknevet, sorindexet és az előellenőrzési státuszt.

### `batch_run_supplier_results`

Egy sor egy cikkszám és egy webshop eredménye. Tárolja a mapping- és
scrape-státuszt, nyers és HUF-ra normalizált árat, készletet, TOP/alternatíva
jelölést, hibát és időzítési adatokat.

### `batch_run_events`

Opcionális audit tábla. A jelenlegi élő UI elsődleges eseményforrása az
in-memory SSE queue; ez a tábla nincs használva a normál eredménybetöltéshez.

Az aktuális létrehozó SQL:
`deploy/supabase_batch_tables.sql`. A már létező környezetekhez a futtatónév
és index migráció: `deploy/supabase_batch_runner_migration.sql`.

---

## 19. Ellenőrzési lista módosítás után

Backend:

```bash
../.venv/bin/python -m py_compile main.py batch/*.py shared/*.py
```

Kézi folyamatok:

1. 50 cikkszámnál csak az azonnali futás látszik és indul.
2. 51 cikkszámnál csak az ütemezés látszik és indul.
3. 400 cikkszám elfogadott, 401 elutasított.
4. Aktív futás megszakítható, státusza `cancelled`, a UI az új futás oldalára tér vissza.
5. A Korábbi futások projekt- és futtatónév alapján szűrhetők.
6. A futtató neve megjelenik a projekt mellett.
7. Az Excel összesítő a legjobb árat, készletet és webshopot mutatja.
8. Az Excelben csak a legjobb ár zöld; nincs piros vagy szürke kitöltés.
9. A kijelentkezés visszavisz a Price Agenthez és megszünteti a közös sessiont.

---

## 20. Hibakódok (`scrape_status`)

| Státusz | Jelentés | Jelleg |
|---|---|---|
| `ok` | Sikeres ár + készlet | — |
| `not_found` | Webshop nem találta a terméket | üzleti válasz |
| `not_priced` | Termék létezik, de nincs ára | üzleti válasz |
| `timeout` | Időtúllépés | technikai hiba |
| `login_failed` | Bejelentkezés nem sikerült | technikai hiba |
| `error` | Egyéb (hálózati) hiba | technikai hiba |
| `no_mapping` / `skipped` | Nincs mapping, nem próbálta | nincs scrape |

**Tipp:** `not_found` / `not_priced` üzleti válasz (nem programhiba). `timeout` /
`login_failed` / `error` jellemzően technikai ok — újrafuttatással sokszor megoldódik.
