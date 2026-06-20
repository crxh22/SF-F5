# Pe scurt — 20-06-2026 (sesiunea arhitect ETAPA-5r)

Am deblocat cele 3 etape care stăteau în coadă de azi-dimineață. Două așteaptă
acum o decizie de la tine (apar ca 2 carduri în dashboard). În plus am găsit
problema de fond care le-a blocat.

## Ce am nevoie de la tine (3 lucruri)

1. **Memorie / viteză.** Testele de backend rămân fără memorie când rulează mai
   mulți agenți deodată. Levier imediat (1 clic, reversibil): scade „max agenți
   simultan" din tab-ul ⚙ Configurare de la **4 la 2**. Reparația durabilă e un
   mic deploy separat. (Nu l-am tras eu — e decizia ta. Detalii la pct. 2.)
2. **Cardul „retururi furnizor" (decizie fiscală).** Ce total punem pe factura
   de retur când returul are ȘI marfă cu TVA, ȘI fără TVA. Recomand: totalul =
   toată marfa returnată. Detalii la pct. 3.
3. **Cardul „stock-views" (buget).** Etapa a consumat mult și degeaba (din cauza
   memoriei). Reia cu buget mărit, sau o ținem pe loc. Detalii la pct. 4.

---

## 1. Cele 3 etape blocate — rezolvate

- **Inventariere (stocktaking).** Auditorul a semnalat că „ce vede omul în
  previzualizare nu e exact ce se postează". Observația e corectă la nivel de cod,
  DAR codul e corect — face exact ce am decis tot azi (re-evaluarea costului la
  momentul salvării e intenționată; previzualizarea e doar o estimare). Greșeala
  era în TEXTUL designului, care promitea prea mult. Am trimis o corectură **doar
  de text (fără cod)**, care trece prin verificare + gate-ul tău normal.
- **Retururi furnizor.** Te așteaptă în card — vezi pct. 3 (decizie fiscală).
- **Stock-views.** Te așteaptă în card — vezi pct. 4 (buget).

## 2. Problema de fond — testele backend rămân fără memorie

**Ce se întâmplă:** agenții care construiesc backend-ul rulează testele Python.
Când rulează mai mulți agenți deodată, suma depășește limita de 22 GB și sistemul
omoară testele. Agentul reîncearcă la nesfârșit → muncă și bani irosiți.

**Dovada (nu presupunere):** la o singură rulare pe stock-views (azi, 12:40),
agentul a pornit testele de **~292 de ori**, cu **4 omoruri din lipsă de memorie**
(„exit 137"). Acea etapă a ars **~44 milioane** de unități de lucru — de ~7× cât o
etapă sănătoasă — aproape tot degeaba.

*Cinstit:* chiar acum nu se vede niciun omor „live", pentru că fabrica e repornită
și inactivă (etapele așteaptă deciziile). Dovada e din rulările de dinainte de
repornire.

**Două leviere (alegi tu — e cost vs. viteză):**
- **Imediat, 0 cod, reversibil:** „max agenți simultan" 4 → 2 din ⚙ Configurare.
  Mai puțini agenți deodată = vârf de memorie mai mic. Cost: fabrica merge ceva
  mai încet.
- **Durabil:** o limită de memorie pe testele backend — exact ca cea pe care am
  pus-o deja pe testele de frontend (a mers: 0 omoruri). E un deploy separat în
  repo-ul ERP. Cost: o etapă de lucru. Beneficiu: rezolvă cauza, nu simptomul.

**Recomandare:** pe termen scurt levierul imediat; în paralel pregătim reparația
durabilă.

## 3. Decizia fiscală — cardul „retururi furnizor"

**Situație:** un retur la furnizor care conține ȘI marfă cu TVA, ȘI marfă fără TVA,
pe același retur. Sistemul emite o factură fiscală (15B) pentru retur.

**Întrebarea:** ce TOTAL trece pe acea factură?
- **Varianta A:** totalul = TOATĂ marfa returnată (cu + fără TVA).
- **Varianta B:** totalul = doar marfa cu TVA.

Designul scris se contrazice singur: o secțiune zice A, pseudocodul detaliat (pe
care l-a urmat codul) zice B. Azi nu se vede diferența — toate cazurile testate au
doar marfă cu TVA, deci diferă DOAR la un retur mixt. TVA-ul în sine e calculat
corect în ambele variante (doar pe partea cu TVA).

**Recomandarea mea: Varianta A** (totalul = toată marfa; linia de TVA rămâne doar
pe partea cu TVA — asta deja e corectă în cod). Așa funcționează normal o factură:
totalul reflectă tot ce e pe document. Dar e o cifră pe un document fiscal —
**confirmă tu**, că tu știi cerința fiscală exactă. Dacă alegi A, urmează o mică
corectură (separăm cele două cifre), reversibilă, fără schimbare de structură.

## 4. Decizia buget — cardul „stock-views"

Etapa „stock-views" (paginile de vizualizare stocuri) a depășit plafonul de buget
(44 vs. 30 milioane), iar cea mai mare parte s-a irosit pe problema de memorie de
la pct. 2. A epuizat și singura reîncercare permisă.

**Opțiuni:**
- **A (recomand):** întâi reducem memoria (pct. 2), apoi reluăm etapa cu buget
  mărit. Dacă memoria e rezolvată, ar trebui să termine repede (o etapă sănătoasă
  ≈ 6 milioane).
- **B:** o ținem pe loc până facem reparația durabilă, ca să nu mai irosim.

Am pus-o pe **pauză** (nu mai consumă) până decizi.
