# Webshop Bejelentkezési és Keresési Útmutató

Ez a dokumentum leírja, hogyan működik az automatikus scraper minden beszállítónál.

Utolsó frissítés: **2026-08-14**.

Biztonsági szabály: ez a dokumentum nem tartalmaz valódi belépési adatot. A
felhasználónevek és jelszavak a gitignored `.env` megfelelő
`SUPPLIER_<betű>_USERNAME/PASSWORD` változóiban vannak.
A rendszer a háttérben **Playwright** böngészőt használ (headless Chromium).

---

## A) csavarda.hu

| | |
|---|---|
| **URL** | https://csavarda.hu/bejelentkezes |
| **Felhasználónév** | `SUPPLIER_A_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_A_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Megnyílik a `/bejelentkezes` oldal
2. Kitölti az `#email` és `#password` mezőket
3. Beküldi a formot
4. A rendszer a Budapest (`/pest`) telephelyet választja automatikusan

**Keresés:**
- URL: `https://csavarda.hu/pest/kereso?search={cikkszám}`
- Az első találatra kattint → megnyílik egy oldalsó fiók (drawer)
- Kiolvassa: **Nettó egységár** (Ft/db), **Készlet** (Budapest + Vecsés)

**Pénznem:** HUF

---

## B) irontrade.hu

| | |
|---|---|
| **URL** | https://irontrade.hu/bejelentkezes |
| **Felhasználónév** | `SUPPLIER_B_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_B_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Megnyílik a `/bejelentkezes` oldal
2. Kitölti a `#LoginEmail` és `#LoginPassword` mezőket
3. Beküldi a formot
4. ⚠️ Ha a Livewire CSRF hibát dob ("This page has expired") → automatikusan újra próbálkozik
5. A `wait_until="load"` és a gomb disabled attribútumának eltávolítása szükséges (Livewire késleltetés miatt)

**Keresés:**
- URL: `https://irontrade.hu/kereso?name={cikkszám}`
- Az első termékre kattint → termékoldal
- Kiolvassa: **Nettó ár** (Ft), **Készlet**

**Pénznem:** HUF

---

## C) webshop.koelner.hu

| | |
|---|---|
| **URL** | https://webshop.koelner.hu/belepes/ |
| **Felhasználónév** | `SUPPLIER_C_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_C_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Megpróbálja visszaállítani a mentett munkamenetet (`assets/.koelner_session.json`)
2. Ha érvényes a session → bejelentkezést kihagyja
3. Ha lejárt → kitölti a `#login_username` és `#login_password` mezőket → `#loginbutton`
4. Mentés: sikeres bejelentkezés után a cookie-kat elmenti

**Keresés:**
- URL: `https://webshop.koelner.hu/termekek/?keres={cikkszám}`
- Végigmegy a termékcsoportokon → megkeresi a `tr.gy_item.item-selected` sort
- Kiolvassa: ár (`td.NETTO`), készlet (`td.KESZLET`)

**Pénznem:** HUF

---

## D) eshop.mekrs.cz

| | |
|---|---|
| **URL** | https://eshop.mekrs.cz/en |
| **Felhasználónév** | `SUPPLIER_D_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_D_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Megnyílik az `/en` oldal
2. Kitölti az `input[name='username']` és `input[name='password']` mezőket
3. Kattint a `[data-testid='login-button']` gombra
4. A bejelentkezési form eltűnése jelzi a sikert

**Keresés:**
- A főkeresőbe beírja a cikkszámot
- Megvárja az autocomplete-et → "Show all results" kattintás
- Kiolvassa: ár (`span.text-primaryRed`), egységmennyiség, készlet

**Pénznem:** CZK (korona) → az összehasonlításhoz HUF-ra konvertálva jelenik meg

---

## E) fabory.com

| | |
|---|---|
| **URL** | https://www.fabory.com/hu/login |
| **Felhasználónév** | `SUPPLIER_E_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_E_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Visszaállítja a 20 óránál frissebb sessiont, majd a főoldalon a tényleges
   account DOM-jelzőkkel (`/logout` link + logged-in marker) igazolja a belépést
2. Érvénytelen sessionnél megnyitja a `/hu/login` oldalt
3. Cookie banner elfogadása ("Összes elfogadása")
4. Kitölti az "Email cím" és jelszó mezőket → "Belépés"
5. A belépést ugyanazokkal az account DOM-jelzőkkel ellenőrzi, majd sessiont ment

**Keresés:**
- A főoldal látható `#search` mezőjébe írja a cikkszámot, majd Entert nyom
- A kereső-URL közvetlen megnyitása nem használható: a Fabory ezt időnként a
  `/hu` főoldalra irányítja vissza, ami korábban hamis „nincs beárazva” hibát adott
- Ha az űrlapkeresés sem jut valódi `/search` vagy `/p/` oldalra, egyszer újrapróbálja;
  tartósan bizonytalan állapotot nem jelent termék- vagy árhibának
- Az első `/p/` linkre kattint (termékoldal)
- Kiolvassa: **Nettó ár** (`Ft / ár / {mennyiség}`), **Készlet** ("Raktáron" / "Nincs készleten")

**Pénznem:** HUF
⚠️ *Készlet csak raktáron/nem raktáron értékkel jelenik meg, pontos darabszám nem elérhető.*

---

## F) rio.reyher.de

| | |
|---|---|
| **URL** | https://rio.reyher.de/hu/customer/account/login |
| **Ügyfélszám** | 901187 |
| **Felhasználónév** | `SUPPLIER_F_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_F_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Megpróbálja visszaállítani a mentett session cookie-kat (`assets/sessions/reyher_session.json`)
2. Ha érvényes → bejelentkezést kihagyja, egyenesen a főoldalra navigál
3. Ha lejárt → kitölti az "Ügyfélszám", "Felhasználónév", "Jelszó" mezőket → "Bejelentkezés"

**Keresés:**
- A "Cikkszám" keresőbe beírja a számot → Enter
- Megvárja a DOM-ban az EUR árat
- Kiolvassa: ár (EUR), csomagolási egység

**Pénznem:** EUR
⚠️ *Készlet nem elérhető (Reyher nem publikálja).*

---

## G) hopefix.cz

| | |
|---|---|
| **URL** | https://www.hopefix.cz/en/login |
| **E-mail** | csoknyaibalazs@gepcoop.hu |
| **Jelszó** | `SUPPLIER_G_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Megnyílik az `/en/login` oldal
2. Cookie banner elfogadása (cseh: "Vše přijmout" / angol: "Accept all")
3. Kitölti az "E-mail" és "Password" mezőket → "Login"

**Keresés:**
1. A `#search_input` keresőbe beírja a cikkszámot
2. Megvárja a jQuery UI autocomplete dropdownt (`#ui-id-1`)
3. A megfelelő találatra kattint → termékoldal (`/en/products/{slug}#{cikkszám}`)
4. Megvárja a `networkidle` állapotot (AJAX betöltés)
5. Megkeresi a `.toggle-expander` gombot a sorban → kattint rá az expander nyitásához
6. Az expander `<select>` első opciójából olvassa ki a `data-price` és `data-qty` attribútumokat

**Pénznem:** EUR
⚠️ *Ha a terméknek nincs ára (data-price="0") → "no pricing available" hibaüzenet.*

---

## H) fbonline.fastbolt.com

| | |
|---|---|
| **URL** | https://fbonline.fastbolt.com/login |
| **Shortname** | GEP001 |
| **Felhasználónév** | `SUPPLIER_H_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_H_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Megnyílik a `/login` oldal
2. Kitölti a Shortname, Loginname, Password mezőket → "Sign in"
3. Átirányítás a dashboardra

**Keresés:**
- URL: `https://fbonline.fastbolt.com/search?q={cikkszám}`
- Kiolvassa: ár (EUR), egységmennyiség, készlet

**Pénznem:** EUR

---

## I) shop.schaefer-peters.com

| | |
|---|---|
| **URL** | https://shop.schaefer-peters.com/b2b/en/ |
| **Felhasználónév** | `SUPPLIER_I_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_I_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Megnyílik a `/sp/en/login/` oldal
2. Kitölti az `input[name='input_login']` és `input[name='input_password']` mezőket → "Log in"
3. Átirányítás: `/b2b/en/?action=shop_login`

**Keresés:**
- Az `input[type='search']` mezőbe beírja a cikkszámot → Enter
- Termékoldal: `/b2b/en/art-{slug}-p{id}/`
- Ha keresési listán landol → az első `/b2b/en/art-` linkre kattint
- Kiolvassa: ár (`span[itemprop='price']`), egység (`.priceLabel`), készlet (`.inventory p`)

**Pénznem:** EUR
⚠️ *Havonta változhat a jelszó! Ha a bejelentkezés sikertelen, a rendszer jelszóváltoztatási kérelmet küld.*

---

## J) kingb2b.it

| | |
|---|---|
| **URL** | https://kingb2b.it/PORTAL/ |
| **Felhasználónév** | `SUPPLIER_J_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_J_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Megnyílik a `/PORTAL/` oldal (SPA, megvárja a `#header-search` megjelenését)
2. Ellenőrzi a bejelentkezési állapotot (`div.button-text-doc` láthatósága)
3. Ha nincs bejelentkezve: `div.header-button.account` kattintás → modal nyílik
4. Kitölti a Username és Password mezőket → LOGIN gomb
5. Bezárja a promóciós popupot és csak az árva Bootstrap backdropot távolítja el
6. A frissnek látszó, de használhatatlan mentett sessiont egyszer érvényteleníti,
   tisztán újrabejelentkezik, majd megismétli a keresést

**Keresés:**
1. A `#header-search` mezőbe beírja a cikkszámot és a keresési ikon handlerét indítja
2. Megvárja a saját `eseguiRicerca` RD3 hálózati választ, így az előző keresés
   DOM-ja nem számít új találatnak
3. Megvárja a termékcsalád megjelenését (`div.singola-famiglia`)
4. Kattint a termékcsaládra; ha a portál loading overlaye takarja, a natív
   JavaScript click handlert használja
5. Megkeresi a `tr.articoli-row[id="{cikkszám}"]` sort
6. Ha a SPA az ár betöltése előtt visszacseréli a sort a családkártyára, egyszer
   újranyitja a családot
7. Kiolvassa: `td[data-cell="PREZZO"]` (ár), `td[data-cell="STOCK"]` (készlet)
   - `%` jelzés → 100 db-onkénti ár
   - `N` jelzés → darabonkénti ár

Egy tiszta sessionben üres eredmény `MSG_NOT_FOUND`; egy még nem ellenőrzött,
visszaállított session ugyanezzel az állapottal előbb kötelezően újrabelép.

**Pénznem:** EUR

---

## K) wasishop.de

| | |
|---|---|
| **URL** | https://www.wasishop.de/login_form.php |
| **Felhasználónév** | `SUPPLIER_K_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_K_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Visszaállítja a 20 óránál frissebb sessiont, de csak a látható
   kijelentkezési linket fogadja el hiteles belépési bizonyítékként
2. Érvénytelen sessionnél megnyitja a `/login_form.php` oldalt
3. Cookie banner elutasítása
4. Kitölti a "Name" és "Passwort" mezőket → "Anmelden"
5. Belépés után ismét ellenőrzi a kijelentkezési markert és menti a sessiont
6. Ha keresés közben lejár a session, egyszer tisztán újrabelép és újrapróbálja

**Keresés:**
- `input[name='search']` mezőbe beírja a cikkszámot → Enter → `Artikelliste.php`
- Megvárja az adott cikkszám pontos `.shipping_card_pos` kártyájának árát
- Árat és készletet kizárólag ebből a pontos kártyából olvas, így a hasonló
  variánsok adatai nem keverednek
- Kiolvassa: ár (EUR/100 db), készlet
  - Ha sávos árazás van ("Staffelpreis") → a középső sáv árát veszi
  - Ha egyáras → `div.price.discount`

**Pénznem:** EUR

---

## L) inoxmare.com

| | |
|---|---|
| **URL** | https://www.inoxmare.com/en |
| **Felhasználónév** | `SUPPLIER_L_USERNAME` (`.env`) |
| **Jelszó** | `SUPPLIER_L_PASSWORD` (`.env`) |

**Bejelentkezés lépései:**
1. Visszaállítja a 20 óránál frissebb sessiont
2. A `Sign Out`/`Welcome,` account állapot és a `#item-input` exact kereső együtt
   igazolja a belépést
3. Lejárt sessionnél tiszta kontextusban újrabelép és új sessiont ment

**Keresés:**
- A `#item-input` exact cikkszámkeresőbe írja a beszállítói cikkszámot → Enter
- A biztos találat feltétele egyszerre a `?art={cikkszám}` URL és a pontos
  `tr[id="{cikkszám}"]` terméksor
- Ismeretlen cikknél a webshop a `/catalogsearch/result/` általános keresőoldalra
  vált; csak két egymást követő ilyen állapot után ad „nem található” eredményt
- Időszakos/meg nem erősített állapotnál egyszer visszatér a főoldalra és
  újrapróbálja. Ha ezután sem stabilizálódik, külön workflow-hibát ad, nem hamis
  termékhiányt
- Kiolvassa: ár (EUR/100 db), pontos készlet és termékleírás

**Pénznem:** EUR

---

## Összefoglaló táblázat

| ID | Webshop | Pénznem | Készlet? | Megjegyzés |
|----|---------|---------|----------|------------|
| A | csavarda.hu | HUF | ✅ darabszám | Budapest + Vecsés |
| B | irontrade.hu | HUF | ✅ darabszám | Livewire CSRF kezelés |
| C | koelner.hu | HUF | ✅ darabszám | Session mentés |
| D | mekrs.cz | CZK | ✅ darabszám | EUR-hoz konvertálva |
| E | fabory.com | HUF | ⚠️ Raktáron/Nem | Pontos szám nem elérhető |
| F | reyher.de | EUR | ❌ Nincs adat | Nem publikus |
| G | hopefix.cz | EUR | ✅ darabszám | ×100 (100-as csomagok) |
| H | fastbolt.com | EUR | ✅ darabszám | |
| I | schaefer-peters.com | EUR | ✅ darabszám | Havi jelszóváltás! |
| J | kingb2b.it | EUR | ✅ darabszám | RD3-szinkron, session- és SPA-race recovery |
| K | wasishop.de | EUR | ✅ darabszám | Szigorú auth + pontos termékkártya; sávos ár lehetséges |
| L | inoxmare.com | EUR | ✅ darabszám | Exact URL+sor ellenőrzés, egyszeri tranziens retry |
