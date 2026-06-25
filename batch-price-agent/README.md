# Batch Price Agent — technikai leírás

A **Tömeges árlekérdező** (batch agent) műszaki útmutatója.
Utoljára ellenőrizve a kódbázishoz: **2026-06-21**.

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
| Port | 8080 | 8001 |

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
   - Letölthető Excel (csak Mátrix lap).

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

A mapping után a beszerző kétféleképp indíthat:

- **Azonnali futás** — azonnal indul (`POST /batch/run`).
- **Ütemezett futás** — a batch agent maga indítja el a megadott időpontban
  (`POST /batch/schedule`).

### Környezet-mód

A `_is_server_mode()` függvény (`main.py`) felismeri, hol fut az app:

| Környezet | Ütemezési mód | UI viselkedés |
|---|---|---|
| Lokál (direct uvicorn) | **manuális** | Pontos dátum+perc választó (bármely jövőbeli perc) |
| Szerver (Docker/Hetzner) | **automatikus** | Gomb → backend kiosztja a következő szabad 40 perces sávot 20:00-tól |

Érzékelés: `/.dockerenv` fájl létezése (Docker-specifikus) + `SCHEDULE_MODE` env
változó (felülírható). A `docker-compose.yml`-ben `SCHEDULE_MODE: auto`.

Automatikus sávok: 20:00 → 20:40 → 21:20 → … (foglalt sávokat kihagyja).

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

## 10. Státusz-feliratok (egységesítve)

Minden felületen (UI mátrix, Excel mátrix) azonos magyar szövegek:

| `scrape_status` | Megjelenített szöveg | Háttérszín |
|---|---|---|
| *(nincs mapping)* | Hiányzó mapping | szürke |
| `not_found` | Termék nincs a webshopban | piros |
| `not_priced` | Termék nincs beárazva | piros |
| `timeout` | Technikai hiba | piros |
| `login_failed` | Technikai hiba | piros |
| `error` | Technikai hiba | piros |
| `ok` | ár + készlet megjelenik | zöld (legjobb ár) |

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
- Hiányzó mapping cella: szürke `—`, hibacellák: piros háttér, magyar szöveg (ld. fent).
- Link oszlop: `=HYPERLINK(...)` képlettel, ha a scraper visszaadott `product_url`-t.

---

## 12. API endpointok

| Metódus | Útvonal | Mit csinál |
|---|---|---|
| `GET` | `/` | UI (ui/index.html) kiszolgálása |
| `GET` | `/config` | Ütemezési mód (`auto`/`manual`) + auto-paraméterek |
| `GET` | `/suppliers` | Választható webshopok listája |
| `GET` | `/vipa/status` | Van-e friss Vipa session? `{"live": true/false}` |
| `POST` | `/vipa/initiate-login` | Vipa OTP e-mail kérése |
| `POST` | `/vipa/complete-login` | Token beküldése, session mentése |
| `POST` | `/batch/preview` | Mapping előellenőrzés (nem indít scrapert) |
| `POST` | `/batch/run` | Azonnali batch futás, visszaad `batch_run_id`-t |
| `POST` | `/batch/schedule` | Ütemezett futás (auto: kiosztott sáv; manuális: megadott perc) |
| `GET` | `/batch/run/{id}/progress` | SSE stream a futás haladásáról |
| `GET` | `/batch/run/{id}` | Egy futás eredménye (mátrix) |
| `GET` | `/batch/runs` | Korábbi + ütemezett futások listája |
| `POST` | `/batch/run/{run_id}/cancel` | Aktív futás megszakítása |
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

A price agent ilyenkor párhuzamosan fut 8000-es porton. Az app-selector (8080-on,
ha Dockerben fut) a 8001-es portra irányít át a Batch tile kattintásakor.

---

## 15. Szerver / deploy

- Hetzner CPX42 szerver (8 vCPU AMD, 16 GB RAM, 320 GB SSD): `178.104.208.200`
- Batch agent URL: `http://178.104.208.200:8001`
- A gyökér `docker-compose.yml` mindkét szolgáltatást indítja.
- A batch konténer a price agent kódját **csak olvashatóan** (`ro`) csatolja,
  de a `assets/sessions` mappát **írhatóan** — Vipa session mindkét appból menthető.
- `SCHEDULE_MODE: auto` a szerveren: 20:00-tól, 40 perces sávok.
- `restart: unless-stopped` biztosítja, hogy reboot után újraindul.

### Frissítés a szerveren

```bash
cd /opt/price_agent
git pull
docker compose up -d --build
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

A felület egyetlen `ui/index.html` fájl (beágyazott CSS + JS, nincs build lépés).

---

## 18. Hibakódok (`scrape_status`)

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
