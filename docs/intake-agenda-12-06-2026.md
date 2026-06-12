# Agenda interviului de pornire ERP — pregătită 12-06-2026

Durată estimată: **60–90 minute**, într-o sesiune ca asta (chat). Tot ce e mai jos are materialul pregătit dinainte — tu doar decizi. Documentele complete sunt în `docs/projects/erp/` (planul proiectului, harta fazelor, cele 4 contracte dintre faze), derivate integral din documentația ta finalizată din ERP-start.

La final, deciziile tale se consemnează ca prima intrare în jurnalul de decizii al proiectului ERP (`docs/projects/erp/decision-log.md`) și fabrica pornește pe ele.

---

## Decizia 1 — Harta fazelor: câte linii de lucru în paralel după Fundație?

Construcția începe cu o fază **Fundație** (schema de bază + motoarele comune: documente, registre, postări, drepturi de acces). După ea, restul domeniilor pot merge în paralel, fiecare pe ramura ei, cu contracte înghețate între ele.

- **Varianta A (recomandată):** după Fundație pornesc, pe rând controlat, **3 faze paralele** — Stoc/Aprovizionare, Comenzi-service/Producție, Bani/Plăți — apoi Salarizare, apoi Rapoarte, la final Migrarea din 1C. Cost: mai mulți agenți activi simultan în vârf. Viteză: cea mai bună. Risc: ținut în frâu de contractele dintre faze + porțile de integrare (mecanismul care a prins deja conflictul „semănat" la testul de acum două zile).
- **Varianta B:** doar 2 paralele (Stoc + Comenzi-service), Banii intră după. Viteză mai mică, risc marginal mai mic.

**Recomandare: A** — cu plasa de siguranță de la Decizia 5 (pornire eșalonată).

## Decizia 2 — Conținutul fazei Fundație: confirmi lista?

Pe scurt, Fundația construiește: scheletul aplicației (Django + PostgreSQL + React, cum ai decis în ADR-0002), entitățile de bază (contrageți, contracte, persoane juridice, vehicule, utilizatori + drepturi + dispozitive înregistrate, nomenclatoare, tichete), motorul de documente (versiuni, audit, storno, cascada de dependențe, straturi de statusuri), motorul de postări + toate registrele, mecanica numerelor fiscale (diapazoane + e-factura), plus trei subsisteme transversale pe care recenzia le-a mutat aici pentru că toate fazele au nevoie de ele: **formulare printabile**, **stocarea fotografiilor** și **cadrul de notificări**.

Întrebarea reală: e ceva în lista de mai sus pe care îl vrei SCOS din prima fază (livrat mai târziu), sau ceva absent pe care îl vrei DIN PRIMA?

**Recomandare:** lista așa cum e — e exact „nucleul comun" pe care l-am stabilit în documentație, nimic decorativ.

## Decizia 3 — Comanda de teste a proiectului ERP

Fiecare integrare de cod trece printr-o poartă care rulează automat toată suita de teste a ERP-ului. Propunerea: comanda fixă `bash scripts/test.sh` în repo-ul ERP — scriptul crește odată cu proiectul (azi pytest pe backend, mai târziu + frontend), iar configurația fabricii nu se mai atinge.

**Recomandare: da** (decizie tehnică; o confirmi și o închidem — intră în consemnarea finală).

## Decizia 4 — Plasarea fluxurilor confidențiale de TVA (Metoda 1 / Metoda 2)

Așa cum e schițat: documentele Metodei 1 (achiziție/vânzare de servicii + plăți) stau în faza **Bani/Plăți**, contul de tip „revânzare TVA" (Metoda 2) în faza **Comenzi-service**, iar monitorizarea pe contract (cât s-a facturat / plătit / întors) în faza **Rapoarte**. Scopurile de acces rămân separate (Metoda 1 ≠ Metoda 2), cum ai decis pe 10-06.

**Recomandare:** confirmă split-ul — respectă structura documentelor și ține confidențialitatea pe scope-uri separate.

## Decizia 5 — Pornirea eșalonată după Fundație („teren de probă")

Planul complet se înregistrează în fabrică de la început (toate cele 7 faze, cu harta dependențelor), DAR după Fundație se dă drumul întâi DOAR fazei Stoc/Aprovizionare — prima fază completă pe teren de probă, cum am stabilit în specificația fabricii. Abia după ce ea trece, se eliberează automat restul paralelelor. Mecanismul e deja construit și testat azi (reținerea e configurabilă în `factory.config.yaml`).

**Recomandare:** păstrează reținerea pe `[fundație, stoc-aprovizionare]` — prima desfășurare în 3 paralele să se întâmple după ce conveierul și-a dovedit treaba pe o fază reală întreagă.

## Decizia 6 — Punct de control la primul plan de fază

Prima dată când Arhitectul de Fază (agent) produce planul de stagii pentru Fundație, opresc orchestratorul, citesc planul cu ochii mei, și abia apoi îi dau drumul la execuție. O singură dată — la prima utilizare reală a mecanismului de planificare; după aceea planurile merg automat.

**Recomandare: da** — verificare ieftină a unui mecanism nou, exact o dată.

## Decizia 7 — Semnalul de start

Dacă deciziile 1–6 sunt date, imediat după interviu: consemnez decizia (prima intrare în jurnalul ERP), fac bootstrap-ul repo-ului de lucru (`erp-workspace`), înregistrez fazele în orchestrator cu noua comandă, pornesc fabrica și înarmez watchdog-ul. De aici încep să cadă, pe muncă reală, criteriile rămase din definiția de done a fabricii (stagiu cap-coadă, restart fără pierderi, audit încrucișat, decizie de pe telefon, onestitate la eșec, merge paralel real).

**Recomandare:** start imediat după interviu; primele decizii de tip „poartă umană" îți vor veni pe telefon prin ntfy + dashboard, ca la demonstrația de ieri.

---

## Ce am rezolvat singur (tehnic, local, reversibil — doar raportez)

- **Comanda de înregistrare a fazelor** (`seed-phases`) — proiectată, recenzată advers de 2 revieweri, construită, verificată de un agent separat: validare strictă a planului, totul-sau-nimic în baza de date, refuz mecanic dacă orchestratorul rulează.
- **Repo unic** pentru ERP (backend + frontend împreună) — modelul de lucru al fabricii cere un singur repo de integrare.
- **Permisiunile agenților claude** în mod neinteractiv — fără asta primul builder real s-ar fi blocat la prima scriere (defect descoperit la pregătire, frate geamăn cu cel de la codex prins la testul de ieri); compensat cu un **detector mecanic** care alarmează dacă vreun agent scrie în afara terenului lui.
- **Contextul de proiect pentru Arhitectul de Fază** — agentul primește acum în prompt unde e documentația ta de business, planul proiectului și contractele (până azi primea doar numele fazei).
- 4 recenzii adversariale pe tot pachetul: 39 de constatări, toate aplicate, zero respinse.

## Puncte informative (nu cer nimic acum)

- **Exportul din 1C** rămâne la tine — blochează doar faza de migrare (ultima), nimic altceva.
- O nuanță minoră de aliniat cândva în ERP-start: registrul facturilor fiscale furnizor (15A) e definit pe „documente de achiziție", dar Metoda 1 cere să lege și achiziții de servicii — am consemnat-o în contractul dintre faze, o rezolvăm la înghețarea contractelor, fără impact acum.
- Documentația ta rămâne sursa unică de adevăr — planul proiectului doar arată spre ea (pin pe commit-ul `51e32b0` din 11-06).
