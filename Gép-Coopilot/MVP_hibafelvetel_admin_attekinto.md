# Gép-Coopilot MVP - Hibafelvétel és admin áttekintő

> **Aktuális termékstátusz — 2026-08-14:** az MVP implementációja és a korábbi
> hibajegyek megmaradtak, de a vásárlói chat és az admin
> **Gép-Coopilot Hibapult** menü jelenleg rejtett. A vezérlés a
> `ui/index.html` `COPILOT_CHAT_VISIBLE` és
> `COPILOT_ADMIN_TICKETS_VISIBLE` kapcsolóival történik. A rejtett adminfül
> közvetlen megnyitása az `users` fülre irányít. A visszakapcsolás külön
> termékdöntés és teljes chat/admin regressziós teszt után történhet.

## 1. Cél

A Gép-Coopilot célja, hogy a Price Agent felületén belül a felhasználó gyorsan és egyszerűen tudjon hibát rögzíteni.

Ebben az első verzióban a Gép-Coopilot nem javít hibát automatikusan, nem küld értesítést, nem hoz létre GitHub issue-t, és nem indít fejlesztői folyamatot.

Az első verzió célja kizárólag:

- a felhasználói probléma rövid begyűjtése,
- a szükséges alapadatok strukturált rögzítése,
- a hiba feladatként történő mentése,
- admin panelen áttekinthető lista biztosítása.

Egyszerű folyamat:

```text
Felhasználó hibát jelez
↓
Gép-Coopilot kérdez néhány rövidet
↓
Összefoglalja a hibát
↓
Felhasználó jóváhagyja
↓
Feladat létrejön
↓
Admin panelen látható
```

## 2. Név

A funkció neve:

```text
Gép-Coopilot
```

A felületen például így jelenjen meg:

```text
💬 Gép-Coopilot
```

Később lehet hozzá egy kis logó is, például egy egyszerű robot/asszisztens ikon, amely illeszkedik a Price Agent dizájnjához.

## 3. Helye a felületen

A Price Agent oldal jobb alsó sarkában jelenjen meg egy kis gomb:

```text
💬 Gép-Coopilot
```

Kattintásra jobb alul nyíljon meg egy kisebb support chat jellegű ablak.

Javasolt méret:

```text
Szélesség: 360-420 px
Magasság: 500-600 px
Pozíció: jobb alsó sarok
```

## 4. Első üzenet

Amikor a chat megnyílik, a Gép-Coopilot magyarul köszönti a felhasználót:

```text
Szia! Van valami probléma? Segítek röviden rögzíteni.
```

Fontos: a bot ne legyen túl beszédes. Röviden kérdezzen, röviden válaszoljon.

## 5. Chatbot viselkedési elv

A Gép-Coopilot nem általános beszélgetőtárs.

Csak ebben segít:

```text
A Price Agent / Gép-Coop árlekérdező rendszer működésével kapcsolatos problémák felvétele.
```

Nem válaszoljon más témákra, például:

- időjárás,
- politika,
- általános kérdések,
- programozási tanácsadás,
- magánbeszélgetés,
- befektetés,
- viccek,
- más rendszerek hibái.

Nem ide tartozó témánál rövid válasz:

```text
Ebben sajnos nem tudok segíteni. A költségek csökkentése érdekében csak a rendszer működésével kapcsolatos hibák felvételében tudok segíteni.
```

Ezután ne folytassa a beszélgetést más témában.

## 6. Begyűjtendő adatok

Kötelező adatok:

```text
1. Probléma típusa
2. Érintett webshop
3. Cikkszám / SKU / EAN
4. Mi történt?
```

Opcionális adat:

```text
5. Mit várt volna a felhasználó?
```

A bot maximum 4-5 kérdésből próbálja összegyűjteni a hibát.

## 7. Javasolt beszélgetési flow

### 7.1. Indítás

Bot:

```text
Szia! Van valami probléma? Segítek röviden rögzíteni.
```

Felhasználó:

```text
Igen, nem talál árat.
```

### 7.2. Probléma típusa

Ha a felhasználó már leírta, hogy "nem talál árat", akkor a bot ne kérdezze meg újra.

Ha nem egyértelmű:

```text
Milyen probléma történt?
```

Gyors válaszlehetőségek:

```text
- Nem talál árat
- Rossz árat mutat
- Nem talál készletet
- Hibaüzenet jelent meg
- Lassú volt a keresés
- Más probléma
```

### 7.3. Webshop

```text
Melyik webshopnál jelentkezett?
```

### 7.4. Cikkszám

```text
Melyik cikkszámmal kerestél?
```

### 7.5. Mi történt?

```text
Mi történt pontosan?
```

### 7.6. Rövid összefoglaló

```text
Rögzítem így?

Probléma: Nem talál árat
Webshop: Gép-Coop
Cikkszám: 93386088ZN
Történés: Üres találatot adott
```

Gombok:

```text
Beküldés
Módosítás
```

### 7.7. Beküldés után

```text
Köszönöm, rögzítettem a hibát.

Azonosító: #124
```

## 8. Mikor jön létre feladat?

Feladat csak akkor jöjjön létre, amikor a felhasználó jóváhagyja a beküldést.

```text
Chat indul
↓
Adatok gyűjtése
↓
Bot összefoglal
↓
Felhasználó jóváhagyja
↓
Task létrejön
```

## 9. Admin panel funkció

Az admin panelben legyen külön nézet a Gép-Coopilot által rögzített hibáknak.

Ajánlott név:

```text
Gép-Coopilot Hibapult
```

## 10. Admin panel - áttekintő lista

Javasolt táblázat:

```text
ID | User | Probléma | Webshop | Cikkszám | Státusz | Létrehozva
```

Példa:

```text
#124 | Kovács Péter | Nem talál árat | Gép-Coop | 93386088ZN | Nyitott | 2026.06.23
#125 | Nagy Anna | Rossz árat mutat | Schäfer | 370200060090 | Folyamatban | 2026.06.23
#126 | Teszt User | Hibaüzenet | Koelner | 8056689219358 | Megoldva | 2026.06.22
```

## 11. Státuszok

Csak 3 státusz legyen:

```text
Nyitott
Folyamatban
Megoldva
```

Belső értékek:

```text
open
in_progress
resolved
```

Alapértelmezett státusz új task létrehozásakor:

```text
open
```

## 12. Státuszváltás logika

Admin kézzel módosíthatja:

```text
Nyitott → Folyamatban → Megoldva
```

Egyszerűbb esetben:

```text
Nyitott → Megoldva
```

## 13. Admin panel részletes nézet

Ha az admin rákattint egy hibára, nyíljon meg a részletes nézet.

Ott jelenjen meg:

```text
Hibajegy azonosító
User
Cég / ügyfél
Probléma típusa
Webshop
Cikkszám
Mi történt?
Bot által készített összefoglaló
Eredeti beszélgetés
Státusz
Létrehozás ideje
Utolsó módosítás ideje
```

Példa:

```text
Hibajegy: #124

User: Kovács Péter
Cég: Teszt Kft.
Probléma típusa: Nem talál árat
Webshop: Gép-Coop
Cikkszám: 93386088ZN

Leírás:
A felhasználó szerint a rendszer üres találatot adott, pedig árat kellett volna megjelenítenie.

Státusz:
Nyitott
```

## 14. Admin műveletek

Az admin az első verzióban csak ezeket tudja:

```text
- státusz módosítása
- hiba részleteinek megtekintése
- opcionálisan belső megjegyzés írása
```

Nem kell az első verzióba:

```text
- email értesítés
- automatikus GitHub issue
- automatikus javítás
- automatikus deployment
```

## 15. Adatbázis javaslat Supabase-ben

### 15.1. copilot_tasks

Mezők:

```text
id
customer_id
user_id
title
problem_type
webshop
product_number
description
expected_result
summary
status
admin_note
created_at
updated_at
```

Példa rekord:

```text
id: 124
customer_id: customer_1
user_id: user_15
title: Gép-Coop árlekérdezési hiba
problem_type: missing_price
webshop: Gép-Coop
product_number: 93386088ZN
description: Üres találatot adott.
expected_result: Árat kellett volna mutatnia.
summary: A felhasználó szerint a Gép-Coop webshopnál a 93386088ZN cikkszámra nem jelent meg ár.
status: open
created_at: 2026-06-23 14:32
updated_at: 2026-06-23 14:32
```

### 15.2. copilot_conversations

Mezők:

```text
id
task_id
customer_id
user_id
created_at
```

### 15.3. copilot_messages

Mezők:

```text
id
conversation_id
sender
message
created_at
```

Példa:

```text
sender: assistant
message: Melyik webshopnál jelentkezett?

sender: user
message: Gép-Coop
```

## 16. Miért külön task és beszélgetés?

A task a lényeges, strukturált adat.

A beszélgetés csak háttérinformáció.

```text
copilot_tasks = admin panel fő lista
copilot_messages = részletes beszélgetési előzmény
```

Az admin panel listában nem kell minden chat üzenetet megjeleníteni. Ott elég a rövid összefoglaló.

## 17. OpenAI API használat

A chatbot működtetéséhez OpenAI API használható.

Modell:

```text
gpt-4o-mini
```

Biztonsági döntés:

```text
Az OpenAI API kulcs nem kerülhet dokumentációba vagy kódba.
Környezeti változóból kell olvasni: OPENAI_API_KEY.
```

Javasolt `.env` változók:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o-mini
```

Költségcsökkentési elvek:

```text
- rövid system prompt
- rövid bot válaszok
- maximum néhány kérdés
- nem általános beszélgetés
- nem küldünk túl hosszú beszélgetési előzményt
- strukturált adatokat mentünk
- ha kész a hibafelvétel, zárjuk a beszélgetést
```

## 18. Javasolt chatbot system prompt

```text
Te vagy a Gép-Coopilot, a Price Agent rendszer hibafelvételi asszisztense.

Feladatod:
- Magyarul beszélj.
- Röviden válaszolj.
- Egyszerre csak egy rövid kérdést tegyél fel.
- Csak a Price Agent / Gép-Coop rendszer működésével kapcsolatos hibák felvételében segíts.
- Ne beszélgess általános témákról.
- Ne adj technikai tanácsot.
- Ne próbáld megoldani a hibát.
- Csak gyűjtsd össze a hibabejelentéshez szükséges adatokat.

Gyűjtsd össze lehetőleg ezekből a legfontosabbakat:
1. Probléma típusa
2. Érintett webshop
3. Cikkszám / SKU / EAN
4. Mi történt pontosan?
5. Mit várt volna a felhasználó?

Kérdezési szabály:
- Maximum 4 rövid kérdést tegyél fel.
- Ha már van elég információ, ne kérdezz tovább.
- Foglald össze röviden a hibát.
- Kérj jóváhagyást a beküldés előtt.
- A feladat csak jóváhagyás után jön létre.
```

## 19. Strukturált válasz javaslat

A backend célja, hogy a beszélgetésből strukturált adatot kapjon.

Példa:

```json
{
  "problem_type": "missing_price",
  "webshop": "Gép-Coop",
  "product_number": "93386088ZN",
  "description": "Üres találatot adott.",
  "expected_result": "Árat kellett volna mutatnia.",
  "ready_to_submit": true
}
```

Ha `ready_to_submit = true`, akkor a bot összefoglal és jóváhagyást kér.

## 20. Egyszerű MVP folyamat

```text
1. Felhasználó megnyitja a Gép-Coopilotot
2. Bot röviden köszönt
3. Bot begyűjt 3-4 fontos adatot
4. Bot összefoglalja a hibát
5. Felhasználó beküldi
6. Task létrejön Supabase-ben
7. Admin látja a Gép-Coopilot Hibapultban
8. Admin státuszt állít: Nyitott / Folyamatban / Megoldva
```

## 21. Mit ne tartalmazzon az első verzió?

Az első verzióból tudatosan hagyjuk ki:

```text
- email értesítés
- Slack/Teams értesítés
- GitHub issue létrehozás
- Claude/Codex integráció
- automatikus javítás
- automatikus deployment
- fájlfeltöltés
- screenshot elemzés
- bonyolult státuszkezelés
```

## 22. Első verzió sikerkritériuma

Az MVP akkor sikeres, ha:

```text
- a felhasználó 1 percen belül tud hibát rögzíteni,
- az admin panelen látszik a hibajegy,
- a hibajegy tartalmazza a legfontosabb adatokat,
- a státusz egyszerűen kezelhető,
- a chatbot nem beszél feleslegesen,
- az API költség kontroll alatt marad.
```

## 23. Rövid összefoglaló

A Gép-Coopilot első verziója egy célzott, magyar nyelvű hibafelvételi asszisztens.

Nem általános chatbot, hanem költséghatékony hibabejelentő eszköz.

A felhasználó röviden leírja a problémát, a bot néhány kérdéssel pontosít, majd taskot hoz létre.

Az admin panelen a "Gép-Coopilot Hibapult" nézetben látható:

```text
ID
User
Probléma
Webshop
Cikkszám
Státusz
Létrehozás ideje
```

A státusz csak háromféle:

```text
Nyitott
Folyamatban
Megoldva
```

Ez egyszerű, átlátható és ideális első MVP-nek.
