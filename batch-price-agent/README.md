# Batch Price Agent — technikai leírás

A **Tömeges árlekérdező** (batch agent) műszaki útmutatója.
Utoljára ellenőrizve a kódbázishoz: **2026-06-20**.

> Ez a fájl **csak a batch agentet** írja le. A beszerzési (egyedi) árlekérdező
> leírása a gyökérben lévő `CLAUDE.md`. A két alkalmazás közös scrapereket
> használ (lásd lent), de külön kódbázis és külön dokumentáció.

---

## 1. Mi ez és mire való?

A beszerző **sok** Gép‑Coop cikkszámot ad meg egyszerre (max. 50), kiválasztja a
webshopokat, és az alkalmazás mindet lekérdezi, majd egy összehasonlító
mátrixban + Excel exportban adja vissza az árakat és készleteket.

A különbség az egyedi árlekérdezőhöz képest: ott **1** cikket kérdezünk
interaktívan; itt **sok** cikket × **sok** webshopot, kötegelve.

---

## 2. Hogyan kapcsolódik a price agenthez?

A batch agent **nem másolja le** a scrapereket — a price agent kódját
importálja futásidőben. Ez azt jelenti: ha egy scrapert (pl. csavarda) javítunk,
a javítás **automatikusan** mindkét alkalmazásra hat. Egy helyen kell karbantartani.

```
batch-price-agent/                     GepcoopPriceAgent/  (a price agent gyökere)
  main.py  ─────── importál ────────►   agent/tools.py        (fetch_supplier_price, árfolyam)
  batch/runner.py ─ importál ──────►    browser/supplier_*.py (a 14 scraper)
                                        browser/vipa_otp.py   (Vipa OTP belépés)
```

Az import útvonalát a **`PRICE_AGENT_PATH`** környezeti változó adja meg
(lokálisan a `..`, Dockerben `/app/GepcoopPriceAgent`). A `batch/runner.py`
ezt teszi a Python keresési útvonalára (`sys.path`), így a `browser.supplier_*`
és `agent.tools` modulok importálhatóvá válnak.

> 🎓 **Fogalom – import:** egy másik fájl kódjának „behívása", hogy itt is
> használhassuk. A `from agent.tools import fetch_supplier_price` annyit tesz:
> „használd a price agentben definiált `fetch_supplier_price` függvényt".

---

## 3. Architektúra (mely fájl miért felel)

```
batch-price-agent/
├── main.py                     FastAPI app: HTTP/SSE endpointok, mentés Supabase-be
├── batch/
│   ├── planner.py              Mapping előellenőrzés (article_mapping lekérdezés)
│   ├── runner.py               A scraperek futtatása, esemény-stream (SSE)
│   ├── aggregator.py           Eredményekből összehasonlító mátrix építése
│   └── export_excel.py         Excel (.xlsx) generálás
├── shared/
│   ├── supabase_client.py      Supabase kapcsolat (cache-elt kliens)
│   ├── supplier_registry.py    A választható webshopok listája + keresési URL-ek
│   └── price_utils.py          Ár-segédfüggvények
├── ui/index.html               A teljes felhasználói felület (egy fájl)
├── Dockerfile                  Konténer-recept a szerverhez
├── docker-compose.yml          (a gyökérben lévő közös compose hivatkozik rá)
└── .env / .env.example         Titkok és beállítások (a .env NINCS gitben)
```

> 🎓 **Fogalom – FastAPI:** egy Python keretrendszer webes API-k építéséhez.
> Az `@app.get("/suppliers")` azt mondja: „ha valaki a `/suppliers` címet kéri
> le, futtasd az alatta lévő függvényt, és add vissza, amit visszaad".

> 🎓 **Fogalom – SSE (Server-Sent Events):** a szerver „élőben" küldi a böngészőnek
> az eseményeket, ahogy haladnak a lekérdezések (pl. „csavarda 3/10 kész").
> Így a felhasználó valós időben látja a folyamatot, nem egy néma várakozást.

---

## 4. A feldolgozás lépésről lépésre

1. **Cikkszámok megadása + webshopok kiválasztása** (UI).
2. **Mapping előellenőrzés** (`/batch/preview` → `batch/planner.py`):
   - Egyetlen kötegelt Supabase lekérdezéssel megnézzük, melyik Gép‑Coop
     cikkszámhoz melyik webshopban van társított cikkszám.
   - A felület megmutatja: hány keresés lehetséges, hol hiányzik a mapping.
3. **Futás indítása** (`/batch/run` → `batch/runner.py`):
   - Webshoponként csoportosítjuk a feladatokat.
   - Párhuzamosan, de korlátozott számban futtatjuk a webshopokat
     (`BATCH_SUPPLIER_LIMIT`).
   - Minden webshopon belül **egy böngészőben** kérdezzük le az összes cikket
     (lásd 5. pont).
   - Az eredményeket élőben streameljük (SSE) és Supabase-be mentjük.
4. **Eredmény** (`aggregator.py` + `export_excel.py`):
   - Összehasonlító mátrix (cikk × webshop, a legolcsóbb kiemelve).
   - Letölthető Excel.

---

## 5. A scraperek: `fetch_price` vs `fetch_prices` (a 2026‑06‑19-i átállás)

Ez a rész a legfontosabb a teljesítmény szempontjából.

Minden webshop-scraper (`browser/supplier_*.py`) **kétféle belépőt** kínál:

| Függvény | Ki használja | Mit csinál |
|---|---|---|
| `fetch_price(part)` | **price agent** (egyedi) | 1 böngészőt indít, belép, **1** cikket keres, zár |
| `fetch_prices(parts, …)` | **batch agent** (tömeges) | 1 böngészőt indít, belép **egyszer**, **az összes** cikket azon a session‑ön keresi, majd zár |

A közös logika két belső segédbe van kiemelve, hogy **egy helyen** legyen javítható:

- `_login_or_restore(...)` — böngésző indítás + bejelentkezés/session‑visszaállítás
- `_search_and_parse(page, part, ...)` — egy cikk keresése + ár/készlet leolvasása

> 🎓 **Miért gyorsabb a `fetch_prices`?** A régi módszer cikkenként új böngészőt
> indított és újra bejelentkezett. Az új módszer **egyszer** lép be, és sok
> keresést végez ugyanazon a munkameneten. Bolt-analógia: egyszer mész be és
> kérdezel meg 10 árat, nem 10-szer lépsz be újra. Mérve: ~4,2 mp/cikk →
> ~1,75 mp/cikk, és webshoponként 1 böngészőindítás (a korábbi N helyett) →
> kevesebb hiba is.

### A runner „okos" választása (dispatch + fallback)

A `batch/runner.py` minden webshopnál megnézi: van‑e a scrapernek `fetch_prices`‑e?
- **Van** → a gyors, kötegelt utat használja (egy böngésző).
- **Nincs** → visszaesik a régi, cikkenkénti `fetch_price` hívásra (változatlan viselkedés).

> 🎓 **Fogalom – fallback (visszaesés):** „B terv", ha az új megoldás nem
> elérhető. Ez tette **biztonságossá** a lépésenkénti átállást: amíg nem volt
> mind a 14 scraper kész, a kész nélküliek a régi úton futottak tovább.

Jelenleg **mind a 14 scraper** kínál `fetch_prices`-t, tehát mindegyik a gyors
úton fut. A fallback a jövőbeli új webshopok átállásáig marad biztonsági hálóként.

### A `fetch_prices` szerződése (mit ad vissza)

```python
async def fetch_prices(part_nos, on_progress=None, on_item=None) -> list[dict]:
    # Visszaad egy listát a part_nos sorrendjéhez igazítva.
    # Minden elem VAGY siker (ugyanaz, mint a fetch_price), VAGY {"supplier_part_no", "error"}.
    # Egy cikk hibája SOHA nem szakítja meg a többit.
    # on_item(index, total, part_no, result|None, error|None) — élő haladás cikkenként.
```

A `runner.py` ebből építi az eredménysorokat és normalizálja az árat
(`price_per_db`, EUR→HUF) — ugyanazzal a logikával, mint a price agent.

---

## 6. Cikkszám-mapping (egyetlen kötegelt lekérdezés)

A `batch/planner.py` a Supabase `article_mapping` táblából nézi ki, melyik
Gép‑Coop cikkhez melyik webshopban van társított cikkszám.

Korábban **cikkszámonként külön** lekérdezés futott (N+1 minta) — 100 cikknél
ez ~7 másodperc volt. Most **egyetlen** kötegelt `.in_()` lekérdezés fut
(~0,1 mp), kis‑/nagybetű‑független párosítással (eredeti + csupa nagy + csupa
kicsi alak), Python‑oldalon nagybetűs kulcs alapján.

> 🎓 **Fogalom – N+1 lekérdezés:** gyakori teljesítmény-hiba: ahelyett hogy
> egyszer kérnél le mindent, minden elemre külön kérdezel. Mint amikor 100
> levelet 100 külön borítékban adsz fel egy helyett. A megoldás: kötegelés.

---

## 7. Vipa OTP belépés

A Vipa egyszer használatos kóddal (OTP, az e‑mailbe érkező token) lép be.

- A session a **megosztott** fájlban tárolódik:
  `…/GepcoopPriceAgent/assets/sessions/vipa_session.json` — ugyanaz, amit a
  price agent ír. Tehát ha az egyik appban belépsz, a másik is használja.
- Endpointok: `/vipa/status` (van‑e friss session), `/vipa/initiate-login`
  (OTP e‑mail kérése), `/vipa/complete-login` (token beküldése).
- A motorja a megosztott `browser/vipa_otp.py`.

A felületen a token‑popup **csak akkor** jelenik meg, ha (a) a Vipa a kiválasztott
webshopok között van, **és** (b) nincs friss session. Élő session esetén nincs
popup, a futás egyből indul.

---

## 8. Futás-gating (csak a kereshető webshopok)

A futtatás indításakor a UI kiszámolja, mely kiválasztott webshopokban van
**mapping utáni kereshető cikkszám**. Amelyikben egy cikknek sincs mappingje,
azt a webshopot **nem vonjuk be**:
- nem jelenik meg a progress táblában,
- nem kerül a futásba (nincs üres oszlop a mátrixban),
- Vipa esetén OTP‑kérés sem indul feleslegesen.

---

## 9. Ütemezett futások

A mapping után a beszerző kétféleképp indíthat: **azonnali** futás (mint eddig),
vagy **ütemezett** futás (későbbi időpontra). Ez nagyobb keresésszámnál hasznos.

### Környezet-mód: lokál = manuális, szerver = automatikus

A batch agent felismeri, hol fut (`_is_server_mode()` a `main.py`-ban):
- **Lokál** (közvetlen uvicorn) → **manuális** mód: a beszerző egy **pontos
  dátum+idő (percre)** választóban adja meg az időpontot.
- **Szerver** (Hetzner, Docker) → **automatikus** mód: a beszerző csak az
  „Ütemezett futás"-ra kattint, a backend kiosztja a következő **szabad 40 perces
  sávot 20:00-tól** (20:00 → 20:40 → 21:20 …; a foglaltakat kihagyja).

> 🎓 **Hogyan érzékeli a környezetet?** A Docker konténer létrehozza a
> `/.dockerenv` fájlt — ez csak a szerveren létezik. A `SCHEDULE_MODE` env
> változóval felül is írható (`auto`/`manual`). A `docker-compose.yml`-ben
> `SCHEDULE_MODE: auto` van beállítva.

### Háttér-ütemező

A `main.py` indításakor egy háttérfolyamat (`_scheduler_loop`) **percenként**
megnézi a DB-ben az esedékes (`status='scheduled' AND scheduled_at <= most`)
futásokat, és **atomikusan** (`scheduled`→`running`) elindítja. A közös végrehajtó
(`_execute_batch`) az azonnali és az ütemezett futást is kezeli.

> 🎓 **Miért nem indul el törölt ütemezés?** Mert az ütemező **minden percben
> frissen a DB-ből** dolgozik — nincs memóriában „elfelejtett" másolat. Ha törölted
> a sort, az ütemező nem találja → soha nem indul el. Egyetlen igazságforrás = a DB.

### Korábbi futások — jelölések és törlés

- Az ütemezett futás „⏰ ütemezve <időpont>" jelöléssel látszik, „Indításra vár…"
  felirattal.
- A lefutott, de **még meg nem nyitott** eredmény **„Új"** címkét kap (a böngésző
  `localStorage`-ában tárolt „olvasott" állapot alapján; megnyitáskor eltűnik).
- **🗑 Törlés** minden sorhoz: ütemezett → nem fog elindulni; lefutott → az
  eredményével együtt törlődik; **éppen futó nem törölhető**.

### Adatbázis

Az ütemezéshez a `batch_runs` táblában kell egy `scheduled_at timestamptz` oszlop
(egyszeri Supabase migráció, már felvéve). A lokál és a szerver **ugyanazt a
Supabase-t** használja. A `status` mező új értéke: `scheduled`.

```sql
alter table batch_runs add column if not exists scheduled_at timestamptz;
```

---

## 10. API endpointok

| Metódus + útvonal | Mit csinál |
|---|---|
| `GET /` | A felület (ui/index.html) kiszolgálása |
| `GET /config` | Ütemezési mód (`auto`/`manual`) + auto-paraméterek a UI-nak |
| `GET /suppliers` | A választható webshopok listája |
| `GET /vipa/status` | Van‑e friss (használható) Vipa session? `{ "live": true/false }` |
| `POST /vipa/initiate-login` | Vipa OTP e‑mail kérése (headless belépés indítása) |
| `POST /vipa/complete-login` | A kapott token beküldése, session mentése |
| `POST /batch/preview` | Mapping előellenőrzés (nem indít scrapert) |
| `POST /batch/run` | Azonnali batch futás indítása, visszaad egy `batch_run_id`-t |
| `POST /batch/schedule` | Ütemezett futás létrehozása (auto: kiosztott sáv; manuális: megadott idő) |
| `GET /batch/run/{id}/progress` | SSE esemény-stream a futás haladásáról |
| `GET /batch/run/{id}` | Egy futás eredménye (mátrix) |
| `GET /batch/runs` | Korábbi futások listája (ütemezettek is, `scheduled_at`-tel) |
| `DELETE /batch/run/{id}` | Futás törlése (ütemezett vagy lefutott; `running` nem) |
| `GET /batch/run/{id}/export.xlsx` | Az eredmény letöltése Excelben |

> 🎓 **Fogalom – GET vs POST vs DELETE:** a `GET` adatot **kér**, a `POST` adatot
> **küld/művelet**, a `DELETE` pedig **töröl** egy erőforrást.

---

## 11. Környezeti változók (`.env`)

> 🎓 **Fogalom – .env:** a titkokat és beállításokat tartalmazó fájl. **Nincs
> a gitben** (a `.gitignore` kizárja), hogy jelszavak ne kerüljenek fel a
> GitHubra. A `.env.example` a sablon: ugyanazok a kulcsok, értékek nélkül.
> A `docker-compose.yml` egyes értékeket felülír a szerveren.

| Változó | Jelentés |
|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY` | Adatbázis kapcsolat |
| `EUR_TO_HUF_RATE` | EUR→HUF átváltási árfolyam |
| `BATCH_MAX_ITEMS` | Max. cikkszám / futás (alap: 50) |
| `BATCH_SUPPLIER_LIMIT` | Egyszerre párhuzamosan futó webshopok száma. **Ajánlott: 4** — magasabb értéknél (pl. 8) a sok egyidejű belépés terhelés alatt hamis „login failed"-et okozhat |
| `SCRAPER_TIMEOUT_SECONDS` | Egy lekérdezés időkorlátja |
| `SCRAPER_RETRY_COUNT` | Hányszor próbáljuk újra hiba esetén (per‑cikk úton) |
| `SCHEDULE_MODE` | Ütemezési mód felülírása: `auto` vagy `manual`. Ha nincs megadva, a `/.dockerenv` dönt (Docker → auto). A szerveren `auto` |
| `SUPPLIER_*_URL`, `SUPPLIER_*_USERNAME`, `SUPPLIER_*_PASSWORD` | Webshop belépési adatok (a price agent .env‑jéből másolva) |
| `SUPPLIER_VIPA_URL`, `SUPPLIER_VIPA_USERNAME` | Vipa (OTP‑alapú, nincs jelszó) |
| `PRICE_AGENT_PATH` | A price agent gyökerének elérési útja (importhoz) |

---

## 12. Lokális futtatás

A batch agent a price agent virtuális környezetét (`.venv`) és kódját használja.

```bash
cd batch-price-agent
PRICE_AGENT_PATH=/abszolut/ut/a/GepcoopPriceAgent \
  ../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

Ezután: <http://127.0.0.1:8001>

> 🎓 **Fogalom – uvicorn:** a „kiszolgáló", ami futtatja a FastAPI appot és
> fogadja a böngésző kéréseit. A `--port 8001` a cím vége (`:8001`).

> 🎓 **Fogalom – .venv (virtuális környezet):** egy elkülönített Python‑mappa a
> projekt csomagjaival, hogy ne keveredjenek más projektek függőségeivel.

---

## 13. Szerver / deploy

- A gyökérben lévő `docker-compose.yml` mindkét szolgáltatást indítja
  (`price-agent:8080`, `batch-price-agent:8001`).
- A batch konténer a price agent kódját **csak olvashatóan** csatolja
  (`:ro`), de a `assets/sessions` mappát **írhatóan** — hogy a Vipa session a
  batch UI‑ból is menthető legyen, miközben a price agent kódját nem módosíthatja.
- `SCHEDULE_MODE: auto` → a szerveren az ütemezés automatikus (20:00-tól, 40 perc).
- Az ütemezett futások csak akkor indulnak el, ha a **szerver fut** az adott
  időpontban; a Docker `restart: unless-stopped` ezt biztosítja. A szerver állása
  alatt elmaradt futások a következő körben automatikusan elindulnak.
- Indítás/frissítés a szerveren: `docker compose up -d --build`.

> 🎓 **Fogalom – Docker / konténer:** a programot és minden függőségét egy
> „dobozba" csomagolja, ami bárhol ugyanúgy fut. A `compose` több ilyen dobozt
> kezel együtt. A `:ro` = read‑only (csak olvasható) csatolás.

---

## 14. Hibakódok (`scrape_status`)

A futás minden (cikk, webshop) párhoz egy státuszt rendel:

| Státusz | Jelentés |
|---|---|
| `ok` | Sikeres ár + készlet |
| `not_found` | A webshop nem találta a terméket |
| `not_priced` | A termék létezik, de nincs ára |
| `timeout` | Időtúllépés |
| `login_failed` | Bejelentkezés nem sikerült |
| `error` | Egyéb (pl. hálózati) hiba |

> 🎓 **Tipp a hibák olvasásához:** a `not_found` / `not_priced` **üzleti**
> válaszok (nem programhiba). A `timeout` / `error` / `login_failed` jellemzően
> **technikai** ok (hálózat, lassú oldal, lejárt belépés) — ezek gyakran egy
> újrafuttatással megoldódnak.
