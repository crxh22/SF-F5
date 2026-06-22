# Concept UI/UX — compilare cerințe + mecanism de garanție (LIVING DOC)

**Status:** în construcție (pornit 22-06-2026 din feedback-ul fondatorului; mecanismul §4 definit
după cele două cercetări — vezi §6). Sursa canonică pentru reproiectarea modului în care fabrica
generează UI/UX. Acoperă cererea fondatorului #10 (compilarea schimbărilor de UX + garanția la
nivel de execuție).

## 0. Problema (de ce facem asta)
UI iese funcțional dar „fără gust", inconsistent între ecrane; culori/spațiere alese prost.
Diagnostic confirmat de cercetare: avem ȚINTELE (strat de componente proprii, un fișier de
tokens) dar fără (a) APLICARE mecanică, (b) spec pentru ASPECT, (c) nimic care să VADĂ rezultatul,
(d) un judecător uman de gust. „Fără gust / inconsistent" e rezultatul TIPIC și previzibil al
acestei combinații — nu o defecțiune punctuală.

## 1. Cerințe & schimbări dorite (feedback fondator 22-06)
### A. Date & gestionare entități (blochează testarea)
- Lipsește gestionarea datelor de bază în aplicație: contragenți, contracte, marfă au MODELE
  (modulul `parties` etc.) dar NU au ecran/API de adăugare-editare-ștergere; doar panoul tehnic
  Django. Presupunere de plan: datele vin din migrarea 1C (neconstruită). [#1, #2 + întrebarea]
- Oriunde se selectează o entitate existentă → buton „adaugă nouă" pe loc. [#3] → §3.
- Populare cu date demo pentru testare. [#11] → în lucru (seed demo).
### B. Calitatea & procesul de generare UI
- Culori (font/fundal) de refăcut cu gust. [#4]
- UI „fără gust" → instrumente AI + metodologii, ce adaptăm. [#7] → cercetat (§6).
- Chestionar UX obligatoriu înainte de build. [#9] → §2.
- Compilarea + garanția la execuție. [#10] → acest document + §4.
### C. Arhitectura & modularitatea UI
- Organizarea meniului/navigării modulară, ușor de regândit (submeniu/taburi/orizontal/view-uri
  per flux). [#5]
- Separare backend/frontend pe ETAPE diferite; frontul modificabil cu efort/risc minim. [#6] → §3/§4.
- UI parametrizabil — CE EXISTĂ: ~16 componente proprii (singurul loc care atinge biblioteca de
  bază), tokens centralizați, preferințe de view. CE LIPSEȘTE: paletă cu gust + aplicarea mecanică
  a regulilor + parametrizare meniu. [#8]

## 2. Chestionar UX obligatoriu — poarta de spec (#9)
Agentul care scrie spec-ul de UI NU trece la build până nu răspunde EXPLICIT la toate:
- a) Ce eveniment operațional de business rezolvă modulul?
- b) Care sunt scenariile de lucru în acest eveniment?
- c) De ce operațiuni / informații / filtre poate avea nevoie utilizatorul?
- d) Cum se organizează elementele vizuale ca să fie comod unui om?
- e) Ce riscuri de a face modulul incomod există?
- f) Ce reguli/convenții globale de UI există deja (§3)?
- g) Ce tip de element de interfață e potrivit pentru fiecare input/output, cu efort minim
  (clickuri)? Ex.: alegere din 2 → checkbox/comutator (1 click), NU dropdown (2 clickuri). [22-06]
- h) Ce referință vizuală urmărim (1-2 aplicații-model)? Fără referință, AI-ul mediază spre „banal". [cercetare]
- i) Ce STĂRI acoperă ecranul: încărcare / eroare / gol / dezactivat (nu doar „calea fericită")? [cercetare]

## 3. Principii/reguli globale de UI
- Regulile se aplică MECANIC: build-ul PICĂ la încălcare, nu depinde de „atenția" agentului. [cercetare — convergent]
- O singură sursă pentru culori/spațiere/tipografie (tokens); fără culori/stiluri hardcodate. [#4/#8]
- Doar componente din stratul aprobat (nicio componentă brută inventată). [#8]
- Alegerea controlului se potrivește cu datele + minim efort (9-g).
- „Adaugă nou pe loc" din orice selector de entitate. [#3]
- O etapă cu UI = backend o etapă, frontend etapă DEDICATĂ, separate. [#6]
- Există o referință vizuală pe proiect (1-2 aplicații-model).

## 4. Mecanismul de garanție la nivel de execuție (#10) — DEFINIT (ordine = pârghie)
1. **Fișier de reguli pentru agenți** (sursa unică citită ÎNAINTE de build): tokens + componente
   aprobate + regulile de control (9-g) + ce să NU inventeze + referința vizuală. Alimentează 2-4.
   *Pârghia cea mai mare.*
2. **Garduri mecanice** (build-ul PICĂ): fără culori/stiluri în afara tokens; doar componente din
   strat; fără defaults interzise. Transformă regulile din „rugăminți" în „legi". *Cel mai ieftin, primul.*
3. **Poarta de chestionar UX la spec** (§2): aspectul + controalele + referința decise ÎNAINTE de build.
4. **Bucla vizuală**: build-agentul randează + captură (3 lățimi) + autoverificare (rupt/suprapus/
   gol/overflow) + autocorecție 2-3×. Prinde „rupt", NU „frumos" (~10-18% îmbunătățire — cercetare).
5. **Poarta de revizie vizuală a fondatorului**: fondatorul parcurge ecranele randate (pe ERP-ul de
   test) înainte de semnarea fazei de UI. Singurul judecător de „gust". Pe ecranele importante.
+ Structural: separare front/back pe etape (#6); o referință vizuală (1-2 aplicații-model).
**De evitat acum (nu se potrivesc):** generatoare gen v0/Lovable/bolt, unelte Figma (nu avem
design-uri), servicii plătite de testare vizuală. [cercetare]

## 5. Decizii deschise (fondator)
1. **Referința vizuală:** 1-2 aplicații al căror aspect îți place. (cel mai valoros input)
2. **Gestionare date de bază:** ECRANE în aplicație (recomand) vs doar import 1C + admin.
3. **Adoptăm planul §4** (5 mecanisme) + separarea front/back ca regulă? (recomand DA)
4. **Cadența reviziei:** tu vezi ecranele importante la fiecare fază de UI. (default recomandat)
5. **Accesibilitate standard (AA) implicit:** recomand DA (ieftin acum, scump retroactiv).
6. **Paleta de culori:** după ce setăm regulile + referința (recomand) vs fix rapid acum.

## 6. Intrări (surse)
- Cercetare „cum se face UI/UX cu AI" (instrumente/metodologii/integrare): `docs/research/ui-ux-ai-assisted-research-22-06-2026.md` ✅
- Cercetare „probleme tipice AI-frontend + contramăsuri": `docs/research/ai-frontend-pitfalls-22-06-2026.md` ✅
- Fundația UI existentă: contractul F6 (componente proprii, tokens, legea UX-first).
