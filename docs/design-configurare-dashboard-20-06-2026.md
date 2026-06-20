# Propunere design — tab „Configurare" în panou + 6 îmbunătățiri (20-06-2026)

**Pentru:** fondator — aprobare pe design ÎNAINTE de a scrie cod (așa ai cerut).
**De la:** ETAPA-5o.
**Cât durează să citești:** ~4 min. **Ce decizi:** 3 lucruri, la final („CE DECIZI TU").

Toate aceste schimbări se livrează printr-un **singur restart** al fabricii (restartul
salvează etapele în lucru și le repornește curat — am văzut-o funcționând azi la 13:33).
Deci: tu aprobi designul → eu implementez tot pachetul (a–f + fix notificări + Layer 2)
→ un restart într-o fereastră liniștită → verific.

---

## 1. De ce e nevoie de un mecanism nou (pe scurt, în termenii tăi)

Azi, **toți** parametrii fabricii se citesc o singură dată, la pornire (din fișierul
`factory.config.yaml`). Ca să-i poți edita „la viu" din panou, fără să oprești fabrica,
propun un **strat de „setări la viu"**:

- Modifici în panou → valoarea se scrie într-un tabel de setări în baza de date.
- Bucla fabricii citește acel tabel **la fiecare tact (~5 secunde)** și aplică valoarea.
- Rezultat: schimbarea se aplică **în câteva secunde, fără restart**, și
  **supraviețuiește** unui restart (rămâne salvată în baza de date — nu se pierde).
- Parametrii „de structură" (ce model AI rulează fiecare rol, prețurile, adresa panoului)
  **rămân pe restart** — n-are sens să-i schimbi la viu și ar fi riscant.

Cost/risc: mic și izolat. Un singur tabel nou + citire la fiecare tact. Nu atinge logica de
producție a etapelor.

---

## 2. Cum ar arăta tab-ul „Configurare"

```
┌─ Panou fabrică ─────────────────────────────────────────────────────────┐
│ [Acum în lucru]  [Decizii]  [Plan & istoric]  [⚙ Configurare]           │
├─────────────────────────────────────────────────────────────────────────┤
│ ⚙ CONFIGURARE                                                           │
│                                                                         │
│ ── Regim de lucru ───────────────────────────────────────────────────── │
│  Drenaj manual (e):          [ ● NORMAL   ○ DRENAJ ]   aplică: imediat   │
│       DRENAJ = nu mai pornesc agenți noi; cei în lucru termină liniștit. │
│  Autodrenaj la limită (f):   [ OFF ▢ ]                aplică: imediat    │
│       Când e ON: oprește singur agenții noi la 5h ≥ 80% sau 7 zile ≥ 90%.│
│                                                                         │
│ ── Bugete tokeni / etapă ────────────────────────────────────────────── │
│  rutină (routine):     [    30.000.000 ]   = 30.000 k                    │
│  structural:           [   250.000.000 ]   = 250.000 k                   │
│  critical:             [   364.000.000 ]   = 364.000 k                   │
│       aplică: și etapei în lucru, la următoarea verificare (~secunde).   │
│       gardă: nu poți seta sub cât a consumat deja o etapă în lucru.      │
│                                                                         │
│ ── Paralelism & timpi ───────────────────────────────────────────────── │
│  max agenți simultan:  [ 4 ]   aplică: imediat                           │
│       gardă: nu sub câți rulează acum (acum rulează: 3).                 │
│  timeout agent (sec):  [ 3600 ]   aplică: la următorul agent pornit.     │
│                                                                         │
│ ── Praguri limită API ───────────────────────────────────────────────── │
│  prag 5h (%):   [ 80 ]     prag 7 zile (%):  [ 90 ]    aplică: imediat   │
│                                                                         │
│                                   [ Salvează ]   ultima modificare: —    │
└─────────────────────────────────────────────────────────────────────────┘
```

Lângă **fiecare** câmp scrie, direct în panou: *se aplică la viu?* / *când are efect?* /
*ce gardă există* — exact cum ai cerut (punctul b).

---

## 3. Cele 6 puncte (a–f), fiecare cu „când se aplică" + gardă

| # | Ce | Editabil la viu? | Când are efect | Gardă |
|---|----|------------------|----------------|-------|
| **a** | Buget tokeni / etapă (cele 3 clase) | DA | la etapa în lucru, la următoarea verificare de buget (~secunde) | nu sub consumul deja făcut de o etapă în lucru |
| **b** | Tab „Configurare" cu toți parametrii editabili + text explicativ lângă fiecare | — | — | fiecare param își are garda lui (coloana asta) |
| **c** | Tokenii afișați în mii, fără zecimale: `12.547.709 → 12.548` | — (doar afișare) | imediat după restart | — |
| **d** | Consum **total** vs **efectiv** (efectiv = total − tokenii agenților care au picat și n-au livrat nimic) | — (doar afișare) | imediat după restart | — |
| **e** | Comutator manual DRENAJ ↔ NORMAL | DA | imediat (la următorul tact, nu mai pornesc agenți) | — (oprirea e mereu sigură) |
| **f** | „Autodrenaj la limită" — comportamentul automat de oprire la limită devine opțional (ON/OFF din panou) | DA | imediat | — |

Detalii pe punctele care merită o vorbă:

- **(a) Buget / etapă — „când se aplică":** bugetul e citit de fabrică de fiecare dată când
  verifică o etapă (la fiecare tact, cât rulează etapa). Deci dacă-l schimbi, se aplică
  **și etapei care rulează acum**, la următoarea verificare. *Răspunsul la întrebarea ta
  „doar la următorul agent, sau și la cel care rulează?" → și la cel care rulează.*
  Notă: bugetul e azi pe **clasă** (rutină / structural / critical), nu pe etapă-bucată.
  Vezi decizia 2 mai jos.

- **(d) Total vs efectiv:** vestea bună — fiecare cheltuială de tokeni e deja legată în
  baza de date de agentul (rularea) care a făcut-o. Deci „efectiv" se poate calcula
  **fără modificare de structură** în baza de date: excludem tokenii rulărilor care au
  picat / au fost omorâte / au expirat **și** n-au avansat etapa. Afișez ambele cifre,
  una lângă alta (niciodată amestecate).

- **(e) vs (f):** (e) = **mâna ta pe frână** (oprești/pornești tu). (f) = **frâna automată**
  la limita de API, care de azi devine **opțională** (o pornești doar dacă vrei). Azi
  comportamentul automat e mereu pornit; după schimbare, e oprit până îl pornești tu.

---

## 4. Notificări pe telefon — o gaură de fiabilitate (legat, dar tratez separat)

Am găsit cauza erorii „429" de azi: trimitem alertele pe serviciul **gratuit** ntfy.sh,
care ne limitează când trimitem multe într-un interval scurt (azi: alertele repetate despre
`sf-cap.sh` din timpul reparației + pagina deciziei, toate în aceeași fereastră).

**Problema reală, dincolo de azi:** paginile de **decizie** (porțile care-ți cer aprobarea,
ca `supplier-fiscal-invoice` de azi) se trimit **o singură dată**. Dacă fix atunci pică
(429), **nu se reîncearcă** — rămâi fără sunet pe telefon până la alerta de întârziere, care
vine abia după **24 de ore**. Azi ai prins-o doar pentru că erai pe panou. Alertele normale
(scrieri în afara limitelor, blocaje) se reîncearcă până ajung — doar deciziile, nu.

**Recomandarea mea:** fac paginile de decizie să se reîncerce-până-ajung, exact ca alertele
(folosesc tiparul care există deja în cod). E o schimbare mică și izolată. Așa, o limitare
trecătoare de tip 429 nu-ți mai pierde niciodată poarta cea mai importantă.
Opțional, mai târziu, dacă 429 persistă: găzduim noi serviciul de notificări (scapă complet
de limita serviciului gratuit) — dar asta e o decizie separată, cu un pic de infrastructură.

---

## 5. Ce NU expun la viu (și de ce)

- **Ce model AI rulează fiecare rol** (spec/builder/validator/auditor), **prețurile**,
  **adresa/portul panoului**, **structura claselor de risc** → sunt „de structură", se
  schimbă rar și pe restart. Le pot adăuga în tab ca **read-only** (le vezi, dar le editezi
  din fișier + restart), dacă vrei vizibilitate. Spune-mi dacă le vrei afișate.

---

## 6. Plan, risc, deploy

1. Implementez: stratul de setări la viu + tab-ul + cele 6 puncte + fix-ul de notificări +
   Layer 2 (auto-dimensionarea testelor).
2. Testez fiecare gardă **rulând-o** (ex: încerc să cobor „max agenți" sub câți rulează →
   trebuie să refuze), nu pe încredere.
3. **Un singur restart** al fabricii într-o fereastră liniștită (fără etape la merge),
   ca să le livrez pe toate odată. Etapele în lucru se salvează și repornesc curat.
4. Verific live după restart.

Risc: mic. Gros, e cod nou de panou + un tabel de setări + citiri la viu; nu schimb logica
prin care etapele trec spec→build→validare→audit.

---

## CE DECIZI TU (3 lucruri)

1. **Designul tab-ului + mecanismul „setări la viu"** de mai sus — OK așa, sau vrei altceva?
   (recomand: OK)

2. **Bugetul pe etapă** — îl ții pe **clasă** (rutină/structural/critical — 3 cifre, simplu,
   cum e azi), sau vrei și **buton de buget pe o etapă anume** (mai multă putere, mai mult
   de construit)?
   (recomand: începem cu cele 3 clase; adăugăm pe-etapă doar dacă chiar îți trebuie)

3. **Fix-ul notificărilor** (paginile de decizie să se reîncerce ca alertele) — îl fac în
   acest pachet? E o schimbare mică de cod în producție + intră la același restart.
   (recomand: DA)

Până aștepți, eu intru pe **Layer 2** (nu cere aprobare). Te anunț când e gata de restart.
