# Concept UI/UX — compilare cerințe + mecanism de garanție (LIVING DOC)

**Status:** în construcție (pornit 22-06-2026 din feedback-ul fondatorului). Sursa canonică
pentru reproiectarea modului în care fabrica generează UI/UX. Se completează cu research-ul
(§6) + deciziile fondatorului. Acoperă cererea fondatorului #10 (compilarea tuturor
schimbărilor de UX + cum le garantăm la nivel de execuție).

## 0. Problema (de ce facem asta)
UI iese funcțional dar „fără gust", inconsistent între ecrane; culori/spațiere alese prost.
Cauze de concept:
- verificarea e pe COMPORTAMENT (teste), NU pe ASPECT — niciun ochi uman nu vede ecranul
  înainte să fie declarat „gata";
- spec-ul descrie CE face ecranul, nu CUM arată — agentul ghicește aspectul;
- generare dintr-o singură trecere, fără fondatorul în buclă.

## 1. Cerințe & schimbări dorite (feedback fondator 22-06)
### A. Date & gestionare entități (blochează testarea)
- Lipsește gestionarea datelor de bază în aplicație: contragenți, contracte, marfă au MODELE
  (modulul `parties` etc.) dar NU au ecran/API de adăugare-editare-ștergere; doar panoul
  tehnic Django. Presupunere de plan: datele vin din migrarea 1C (neconstruită). [#1, #2 + întrebarea]
- Oriunde se selectează o entitate existentă → trebuie și buton „adaugă nouă" pe loc. [#3] → principiu global (§3).
- Populare cu date demo pentru testare. [#11] → în lucru (seed demo).

### B. Calitatea & procesul de generare UI
- Culori (font/fundal) de refăcut cu gust. [#4]
- UI generat acum „fără gust" → research pe instrumente AI + metodologii, ce putem adapta. [#7] → research în lucru (§6).
- Chestionar UX obligatoriu înainte de a construi un modul. [#9] → §2.
- Compilarea tuturor schimbărilor + garanție la nivel de execuție. [#10] → acest document + §4.

### C. Arhitectura & modularitatea UI
- Organizarea meniului/navigării să fie modulară, ușor de regândit ulterior (meniu/submeniu,
  taburi, meniu orizontal, butoane, view-uri diferite per flux operațional). [#5]
- Separare backend/frontend pe ETAPE DIFERITE (nu „felii verticale" cu ambele într-o etapă);
  frontul să fie modificabil cu efort și risc minim. [#6] → regulă (§3).
- UI parametrizabil/ușor de schimbat — CE EXISTĂ deja: strat de ~16 componente proprii (singurul
  loc care atinge biblioteca de bază — schimbare aspect ecran-cu-ecran), tokens centralizați
  (culori/spațiere/tipografie într-un fișier), preferințe de view per utilizator. CE LIPSEȘTE:
  paletă cu gust + parametrizare pe layout/meniu. [#8]

## 2. Chestionar UX obligatoriu — poarta de spec (#9)
Agentul care scrie spec-ul unui modul de UI NU trece la construcție până nu răspunde EXPLICIT
la toate întrebările (poartă mecanică, nu „atenție"):
- a) Ce eveniment operațional de business rezolvă modulul?
- b) Care sunt scenariile de lucru în acest eveniment?
- c) De ce operațiuni / informații / filtre poate avea nevoie utilizatorul în proces?
- d) Cum se organizează toate elementele vizuale ca să fie comod unui om?
- e) Ce riscuri de a face modulul incomod există?
- f) Ce reguli/convenții globale de UI există deja pe proiect (vezi §3)?
- g) **Ce tipuri de elemente de interfață sunt potrivite pentru fiecare input/output** — se
  potrivește controlul cu datele și se minimizează efortul (numărul de clickuri). Ex.: alegere
  dintre 2 valori → checkbox/comutator (1 click), NU dropdown (2 clickuri). [adăugat 22-06]
- h) … de suplimentat din research (bune practici design/arhitectură UI + creare UX).

## 3. Principii/reguli globale de UI (seed — se completează din research)
- „Adaugă nou pe loc" din orice selector de entitate. [#3]
- O etapă cu UI = backend într-o etapă, frontend în etapă DEDICATĂ, separate. [#6]
- O singură sursă pentru culori/spațiere/tipografie (tokens). [#4 / #8]

## 4. Mecanism de garanție la nivel de execuție (#10) — DE DEFINIT după research
Candidați (de validat/adaptat din research):
- poarta cu chestionarul §2 (obligatorie înainte de build);
- poartă de revizie VIZUALĂ — preview pe care îl vede fondatorul înainte de semnătură;
- auto-verificare a agentului cu CAPTURĂ DE ECRAN (agentul „vede" ce a făcut);
- pas de referință/mockup în spec;
- separare back/front pe etape.

## 5. Decizii deschise (fondator)
1. Gestionare date de bază: ECRANE în aplicație (recomandat) vs doar import 1C + admin tehnic.
2. Separare back/front ca regulă dură: adoptăm? (recomandat: da)
3. Pornire research + acest document: DA (în curs).
4. Paletă culori: fix rapid acum vs după research.

## 6. Intrări (surse)
- Research AI-assisted UI/UX + metodologii: `docs/research/ui-ux-ai-assisted-research-22-06-2026.md` (în lucru).
- Fundația UI existentă: contractul F6 (componente proprii „antd-fenced", tokens, legea UX-first).
