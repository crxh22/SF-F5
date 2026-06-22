# ui-ux-laws.md — legile UI/UX injectate de fabrică (LEGE, nu sugestie)

**Regim:** acesta este fișierul de reguli UI/UX injectat MECANIC în system-prompt-ul fiecărui
agent de etapă-frontend (builder, validator, auditor — claude ȘI codex), și DOAR pe etapele de
frontend. Sursa canonică unică pentru aspectul + procesul de generare UI; conținutul a fost MUTAT
aici din `docs/design/ui-ux-concept.md` (§2, §3, §4, §7) — acolo rămân doar pointere. Citește-l
ÎNAINTE de a scrie spec sau cod de frontend.

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
> + vezi **§7** — legile UX-ERP + aplicațiile-referință (intrarea fondatorului 22-06); intră în fișierul de reguli UI (§4.1) ca lege de fabrică.
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
4. **Bucla vizuală**: build-agentul randează + captură (**2 lățimi: desktop + telefon; fără tabletă**,
   decizie fondator 22-06) + autoverificare (rupt/suprapus/gol/overflow) + autocorecție 2-3×. Prinde
   „rupt", NU „frumos" (~10-18% îmbunătățire — cercetare).
5. **Poarta de revizie vizuală a fondatorului**: fondatorul parcurge ecranele randate (pe ERP-ul de
   test) înainte de semnarea fazei de UI. Singurul judecător de „gust". **Doar la primele câteva
   iterații de UI, apoi reevaluăm** (decizie fondator 22-06). Pe ecranele importante.
+ Structural: separare front/back pe etape (#6); o referință vizuală (1-2 aplicații-model).
**De evitat acum (nu se potrivesc):** generatoare gen v0/Lovable/bolt, unelte Figma (nu avem
design-uri), servicii plătite de testare vizuală. [cercetare]

## 7. Referință vizuală + filosofia UX — INTRAREA FONDATORULUI (22-06; decizia §5.1 REZOLVATĂ)
Cea mai valoroasă intrare (era pe drumul critic, bloca primul strat de frontend). Primită de la fondator.

### 7.1 Aplicații-referință (3)
**Oracle NetSuite · Microsoft Dynamics 365 · Odoo ERP.**
Cum le citim: pentru **STRUCTURĂ, DENSITATE și MODELUL PE ROLURI** (ERP-uri serioase, dense, optimizate
pentru operator) — executate însă CURAT și modern (Odoo e cel mai apropiat ca aspect curat). NU copiem
aglomerarea/datatul lui NetSuite/Dynamics; luăm rigoarea + execuție cu gust. „Referință" = ton + densitate
+ model de rol, NU clonă.

### 7.2 Principiile fondatorului (3)
- **Design pe ROLURI:** fiecare angajat (contabil, șef producție, mecanic) vede DOAR informațiile +
  butoanele de care are nevoie. (rolul → ce ecran/permisiuni; modelul Right/RightsTemplate există deja.)
- **Flexibilitate / customizare FĂRĂ programare:** utilizatorul poate rearanja coloanele, schimba
  culorile, adăuga scurtături — fără cod.
- **Responsive ADAPTAT LA ROL (nu „mobil universal"):** contabilul = desktop-primar (nu prea are nevoie de
  telefon; contează cum arată pe desktop); mecanicul = telefon-primar (interfața de bază pe smartphone). →
  fiecare ecran are un device-țintă derivat din rolul care-l folosește.

### 7.3 Filosofia UX a ERP-ului (documentul fondatorului — LEGE, nu sugestie)
**Model mental:** un ERP se optimizează pentru a **500-a** utilizare a aceleiași sarcini de către operator,
NU pentru prima interacțiune. Instinctele de „web modern" sunt INVERSATE aici (sistemul înlocuiește fluxuri
cu memorie musculară). **Busola fiecărei decizii: accelerează sau încetinește bucla de bază a operatorului
experimentat?**

**4 legi inviolabile:**
1. **Bucla de bază e SACRĂ:** identifică cele 4-5 acțiuni repetate (sute/zi); fă-le aproape instantanee +
   complet operabile de la TASTATURĂ. Niciodată pierdere de date — auto-save de draft mereu.
2. **Feedback INSTANT:** confirmare instantă + update optimist la fiecare acțiune; întârzieri de 2s după
   salvare = inacceptabile.
3. **Schimb de valoare VIZIBIL:** introducerea datelor trebuie să-l ajute pe cel care le introduce (nu doar
   pe șef) — fiecare câmp obligatoriu îl ajută să-și amintească / vadă instrucțiuni / evite întrebări.
4. **Structură centrată pe PROCES (nu pe baza de date):** urmează cum lucrează oamenii, nu arhitectura DB.
   Un arc narativ prin ciclul de viață: sosire → evaluare → aprobare → execuție → control → finalizare →
   livrare.

**Decizii de design:**
- **Formulare:** densitate mare cu ierarhie tipografică > spațiu gol. Progressive disclosure ascunde cele
  20% cazuri-excepție. Validare la momentul potrivit (nu agresivă pe câmp gol, nu întârziată).
- **Liste:** „scoate excepțiile, nu liste" — arată cele 7 lucruri care cer atenție ACUM, nu toate cele 300.
  Claritate vizuală a stării: poziția în proces, responsabil, blocaje, urgență.
- **Protecție:** „fă erorile greu de produs — structural." Blochează tranzițiile invalide; rigid pe căile
  critice, flexibil pe excepții cu motivare urmărită.
- **Rapoarte:** răspund la decizii concrete (unde pierdem timp/bani, cine e supraîncărcat), nu dashboard-uri
  decorative.

**Checklist de auto-verificare (7 teste înainte de livrare; numite în doc):** operabilitate de la tastatură;
zero pierdere de date la închiderea tab-ului; statusuri explicite; prevenire structurală a erorilor; valoare
livrată operatorului (+ feedback instant + densitate/ierarhie).

### 7.4 Cum aterizează în plan (sinteză arhitect)
- **Aceste legi intră în fișierul de reguli UI (§4.1)** → lege de fabrică pentru fiecare etapă de frontend
  (via `work-protocols/architect-operations.md`; vezi `erp-rebuild-reseed-playbook.md` §4).
- **Meniul/navigarea (Stratul 0):** centrat pe **PROCES** pentru operațiuni (arcul sosire→…→livrare),
  centrat pe **catalog** pentru nomenclatoare (nomenclatoarele SUNT date de referință — corect
  entity-centric; operațiunile urmează procesul).
- **Poarta vizuală a fondatorului (mecanismul §4.5):** se calibrează pe „**e rapid la a 500-a folosire?**",
  NU pe „e drăguț la prima vedere". Tastatură + viteză + densitate, nu doar estetică.
- **Roluri + device:** intră ca cerință în chestionarul §2 (cine folosește ecranul → ce vede → pe ce device).
- **Tensiune de rezolvat (notă):** „densitate > spațiu gol, optimizat pentru operator" TEMPEREAZĂ instinctul
  de „modern airy/curat" din cercetarea inițială (§6). NU e contradicție: execuție curată + tipografică, dar
  DENSĂ și RAPIDĂ, nu aerisită-și-lentă.
