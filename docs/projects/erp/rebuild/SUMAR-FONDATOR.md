# Sumar pentru fondator — structura nouă a etapelor ERP (ARH-04, 23-06-2026)

*Limbaj simplu. Deciziile tale sunt la final. Detaliul tehnic e în `STRUCTURE.md`; nu trebuie să-l citești
ca să aprobi — te bazezi pe auditul dublu pentru detalii.*

## Ce am făcut cât ai fost plecat

Am re-derivat TOT lucrul neterminat din ERP ca **10 straturi** dependente, fiecare testabil de tine pe
rând (nu pe vechile 7 faze — alea n-aveau valoare). Fiecare strat = mai multe **etape mici** (40 în total),
fiecare verificată mecanic cu codul real al fabricii (nimic „pe ochi"). Am pus etapele să fie pe rând:
întâi backend, apoi un ecran frontend separat — și o etapă-„contract" care îngheață cusăturile comune
înainte ca restul să se ramifice.

Structura a trecut prin **audit dublu, două familii de AI independente (opus + codex)**, plus o verificare
încrucișată. Au găsit lucruri reale; le-am reparat pe toate (vezi mai jos). E gata de aprobarea ta.

## Ce acoperă (cele 10 straturi)

Pe scurt, în ordine: (0) meniu/navigare, (1) nomenclatoare de bază + cadrul de ecrane CRUD, (2) firma ta
+ case/conturi + curs valutar, (3) contrapărți + contracte, (4) catalog piese + producție, (5) vehicul +
re-verificarea motorului + acțiunile pe documente (anulare/stornare/istoric), (6) operațiuni stoc/aprovizionare,
(7) cont de plată + ZN, (8) plăți/încasări + alocare, (9) config/utilizatori/drepturi.

Asta = exact găurile care fac ERP-ul **inutilizabil azi** (nu poți adăuga/edita contrapărți, contracte, piese,
nomenclatoare nicăieri în aplicație). După aceste 10 straturi, ERP-ul devine **folosibil cap-coadă**.

## ⚠ Ce NU acoperă — și trebuie să accepți conștient (nu „pe furiș")

Ambele audituri au insistat să-ți spun clar: structura asta livrează ERP-ul **de bază utilizabil**, NU tot
ERP-ul. Am **amânat deliberat** pentru o rundă viitoare (motivul: se construiesc pe un nucleu solid +
verificat de tine întâi):

- **Salarizarea** (algoritmi salariu angajat, document de calcul, plăți, cabinet) — domeniu separat.
- **Rapoartele/proiecțiile** (R01–R21, sinecost, WIP) + **închiderea de perioadă** — vin după ce ai date
  operaționale reale.
- **Migrarea din 1C** (import date, **solduri de deschidere**, documente deschise, reconciliere) — se face
  DUPĂ ce sistemul e construit + verificat.
- **Fluxul complet de comenzi-service dincolo de cont+ZN**: **defectarea** (documentul #1), **primirea/predarea
  vehiculului** (act primire/predare, custodie), **producția**, documentele de vânzare/livrare incompletă.
  Stratul 7 livrează coloana comercială (cont de plată + ZN); restul fluxului se construiește peste ea
  runda viitoare. (Între timp: la stratul 5 ai un formular simplu de adăugare vehicul.)
- **Căutarea globală** (nice-to-have).

Dacă vrei vreunul din astea ÎN runda asta, spune-mi — îl adaug. Altfel, aprobi runda asta = straturile 0–9.

## Ce am reparat în fabrică (nu trebuie să faci nimic)

Am găsit o **eroare reală**: etapele backend „grele" (structural/critical) mergeau pe modelul greșit — pe
opus (Claude) în loc de codex. Asta anula intenția ta din 22-06 („backend → codex" pentru capacitate). Am
reparat-o chirurgical: acum **backend-ul merge pe codex (economisește limita Claude), DAR tot primește
audit dublu opus+codex (calitatea rămâne)**. Reparat, testat (173+71 teste verzi), comis pe main.

(Problema veche cu bucla treasury la merge-gate e deja rezolvată din alte sesiuni — socket scurt + cap pe
buclă. Nu mai e un risc.)

## 🟢 DECIZIILE TALE (răspunde scurt în chat, le aplic eu)

**1. Cum „aterizează" structura în fabrică?** Azi fabrica își GENEREAZĂ singură etapele la rulare; structura
mea dublu-auditată n-are cale mecanică să intre. Două variante:
- **(A) Garanție mecanică — RECOMAND.** Adaug un pic de cod: fabrica ADOPTĂ exact etapele mele aprobate
  (verificate că sunt identice), iar generatorul rămâne doar pe contracte/seams. Ce ai aprobat = exact ce
  rulează. Îl construiesc eu DUPĂ ce aprobi (e parte din re-seed).
- **(B) Fără cod nou, pe încredere.** Spunem generatorului „adoptă planul ăsta cuvânt cu cuvânt". Mai puțin
  de lucru, dar nu e garanție mecanică (poate devia).
→ **Recomand A** (tu ceri mereu garanții mecanice, nu „sunt atent"). Tu zici: **A** sau **B**.

**2. Stratul 9 (config/utilizatori/drepturi): ecrane în aplicație SAU rămâne pe Django admin în v1?** Decizia
ta veche (ADR-0002) zicea Django admin pentru v1. Am pregătit ecranele oricum, dar pot fi amânate. Recomand:
**Django admin pentru utilizatori/drepturi în v1** (ieftin, merge deja) + eventual doar ecranul de parametri
în aplicație (se editează des). Tu zici: **toate ecranele**, **doar parametri**, sau **rămâne pe admin**.

**3. (implicit, decid eu dacă nu zici altceva)** Verificarea ta per-strat: eu opresc fabrica între straturi
ca să testezi fiecare strat livrat, apoi pornesc următorul. Dacă vrei 100% mecanic, seedăm incremental. Merg
pe varianta mea (eu gestionez) dacă nu spui altceva.

## După ce aprobi

Aprobarea ta → re-seed (câteva comenzi copy-paste pentru tine: oprire/pornire fabrică) → build strat cu
strat, fiecare verificat de TINE pe ERP-ul de test înainte de următorul. **M-am OPRIT înainte de build**
(cum ai cerut — build-ul e rularea lungă scumpă, are nevoie de aprobarea ta).

## Stare acum
Fabrica OPRITĂ. Tot lucrul salvat pe main (2 commit-uri: structura + reparația de routing). Cele 2 ramuri
parcate (cont-quote + treasury) păstrate — se altoiesc la straturile 7 și 8. Capacitate săptămânală ~13%
rămasă (suficient). Sesiunea ARH-03 rămâne pornită/idle pe dashboard (n-am omorât-o).

---

## RĂSPUNS FONDATOR (23-06) + ce urmează

**Deciziile tale, aplicate:**
1. **A** — wiring-ul mecanic e CONSTRUIT și testat (commit `90fb3dd`): fabrica adoptă exact etapele
   aprobate (verificate sha256, byte cu byte) și generatorul rămâne doar pe contracte. Dacă generatorul
   atinge planul → se oprește singur (garanție mecanică). Inert pentru orice alt proiect (doar `erp` îl are).
2. **Ecrane în aplicație** pentru stratul 9 (NU Django admin) — cele 3 etape rămân build-ready ca atare;
   `users-rights-fe` + `config-rights-rest` sunt critice (le aprobi tu la build).

**⚠ Onestitate brutală — build-ul NU poate porni acum.** Săptămânalul Claude e la ~13%. O rulare de 40 de
etape l-ar epuiza în prima oră și ar rămâne pe jumătate. Resetul săptămânal = **25-06, ~06:00 (ora ta)**.
Recomandare:
- **Varianta bună:** facem **re-seed-ul acum** (e ieftin — arhivăm baza veche, semănăm cele 10 straturi,
  altoim cele 2 ramuri parcate, config), apoi **pornim build-ul după resetul de 25-06** (capacitate plină).
- **Alternativă:** probăm DOAR stratul L0 acum (2 etape, ieftin) ca să validăm că tot mecanismul nou
  (adopția planului + contracte + build) merge cap-coadă, apoi restul după reset.

Re-seed-ul arhivează baza de date curentă (recuperabilă din arhivă, dar e un pas mare) — de aceea aștept
**„da"-ul tău** pe el și pe varianta de timing, NU îl pornesc singur. Restul e gata.
