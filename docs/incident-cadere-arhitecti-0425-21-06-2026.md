# Incident 21-06-2026, ora 04:25 — căderea simultană a 3 sesiuni de arhitect

**Investigat de ETAPA-5u, 21-06-2026. Pentru fondator.**

---

> ## ⚠️ CORECȚIE (ETAPA-5x, 21-06-2026) — concluzia „eveniment dinspre Claude" de mai jos a fost INFIRMATĂ
> Cauza reală a fost prinsă cert la un incident IDENTIC din aceeași zi (~18:55 UTC): în timpul
> predării, sesiunea 5v a rulat `pkill -f 'sf-architect-monitor.sh'` ca să-și oprească monitorul.
> `pkill -f` caută în TOATĂ linia de comandă, iar **prompt-ul de lansare al fiecărei sesiuni de
> arhitect conține acel text** (instrucțiunea „actualizează monitorul") — așa că a omorât simultan
> procesele `claude` ale tuturor sesiunilor active (5u, 5v ȘI 5w abia pornit). **Comanda exactă
> apare în transcript** — deci mecanismul e DOVEDIT, nu presupus.
>
> Incidentul de la 04:25 are semnătura identică (mor doar sesiunile active, simultan; cele inactive
> supraviețuiesc; zero urmă de memorie) → aproape sigur aceeași cauză (o comandă de oprire cu un
> tipar care se potrivește în prompturi, rulată la o predare/curățenie), **NU un eveniment Claude /
> Remote Control**. Comanda exactă de la 04:25 nu a fost fixată (eveniment vechi), dar mecanismul e
> dovedit la incidentul geamăn.
>
> **Fixul durabil pe care l-ai cerut = REGULA MECANICĂ, deja în vigoare:** niciodată `pkill -f` /
> `pgrep -f` cu un tipar ce poate apărea în prompturile sesiunilor; oprești task-uri doar prin PID
> exact (verificat în `/proc/<pid>/cmdline`) sau le lași să moară odată cu sesiunea. E în header-ul
> monitorului de sesiune, în handoff și în memoria agenților. Auto-restart-ul propus mai jos NU mai
> e necesar ca fix principal — cel mult plasă de siguranță secundară.

---

## Concluzia principală (citește doar asta dacă te grăbești)

**NU a fost o problemă de memorie / OOM.** Handoff-ul anterior a presupus că serverul
a rămas fără RAM (ca pe 19-06) — **dovezile arată clar că nu a fost asta.**

Ce s-a întâmplat de fapt: la **04:25:14**, cele **trei sesiuni de arhitect care lucrau
în acel moment** (5r, 5s, 5t) s-au închis simultan, în aceeași zecime de secundă.
Cele **două sesiuni inactive** (5p, 5q) au supraviețuit. Fabrica, serverul tmux și
restul au rămas în picioare.

Tiparul „mor doar cele active, supraviețuiesc cele inactive, toate în aceeași clipă"
+ absența oricărei urme de memorie = **un eveniment venit dinspre Claude** (conexiunea
de control / Remote Control sau backendul Claude) a închis sesiunile care aveau o
cerere în curs. Cele inactive nu aveau nimic în curs, deci nu au fost atinse.

---

## Dovada cea mai simplă că NU a fost memoria

Sesiunea **5t avea doar ~88 de secunde de viață** când a murit:
- s-a pornit la 04:23:46,
- a preluat garda (marker-ul) la 04:25:08,
- a murit la 04:25:14 — la 6 secunde după ce a preluat.

**Un proces nou-născut, de 88 de secunde, care abia scrisese două fișiere, NU are cum
să rămână fără memorie.** Și totuși a murit în aceeași zecime de secundă cu celelalte
două. Asta singură demonstrează că un declanșator comun, din exterior, le-a închis pe
toate trei — nu memoria fiecăreia.

---

## Cronologia exactă (ora locală)

| Ora | Ce s-a întâmplat |
|---|---|
| 20-06, 18:39 | 5r pornește (sesiunea cea mai veche dintre cele moarte) |
| 21-06, 04:23:46 | 5t pornește (lansată de 5s) |
| 21-06, 04:25:08 | 5t preia garda (scrie marker-ul) |
| **21-06, 04:25:14.79–.90** | **5r + 5s + 5t mor toate, în 0.1 secunde** |
| 21-06, 04:25:15–26 | sistemul curăță cele 3 sesiuni — **„închidere normală", FĂRĂ marcaj de „out-of-memory"** |
| (mai departe) | 5p, 5q, fabrica, serverul tmux — toate vii |

---

## Dovezile (în termeni simpli)

1. **Jurnalul kernelului NU arată niciun „out of memory" pe 21-06.** Jurnalul e
   complet (merge până pe 5 iunie). Ultima oară când serverul a rămas efectiv fără
   memorie a fost **19-06 la 18:14** (incidentul cunoscut). Pe 21-06 — zero.
   *(Am putut citi singur jurnalul kernelului — sunt în grupul `adm` —, deci nu a mai
   fost nevoie de comanda cu `sudo` pe care mi-ai fi dat-o tu. Bună veste: pe viitor
   pot verifica singur astfel de incidente.)*

2. **Sistemul a marcat cele 3 închideri ca „normale".** Pe 19-06, când chiar a fost
   lipsă de memorie, sistemul a notat explicit „**Failed with result 'oom-kill'**"
   (ucis de lipsă de memorie). Pe 21-06, pentru cele 3 sesiuni, a notat doar
   „**închidere, consum CPU X**" — adică s-au oprit singure / au fost oprite curat,
   NU ucise de lipsă de memorie.

3. **Memoria serverului e amplă și sănătoasă acum:** 32 GB RAM + 8 GB swap, din care
   se folosesc ~2 GB. Fabrica, în cușca ei de memorie, a urcat la maxim 7.5 GB din
   22 GB permiși — nici pe departe la perete.

4. **Nu a fost reboot** (5p, 5q, fabrica au rămas pornite peste momentul incidentului).

5. **Nu a fost ucisă de vreun mecanism de-al nostru:** nici „paznicul" de trezire
   (`sf-architect-resume`, care oricum la 04:24 a decis „nu fac nimic"), nici sesiunea
   5q (vie), nici vreo comandă manuală (nu erai conectat — dormeai), nici deconectare
   SSH.

---

## Răspuns la întrebarea ta: „de ce n-a ținut «Scut»-ul (oom_score)?"

Două lucruri:

**(a) Pentru acest incident, «Scut»-ul e irelevant — pentru că nu a fost o problemă
de memorie.** Scutul protejează contra lipsei de memorie; aici nu a fost lipsă de
memorie.

**(b) Totuși, ai dreptate că scutul e doar pe jumătate montat** — și e o gaură reală
care ne-ar lovi într-un OOM viitor:
- Scutul din 19-06 (`oom.conf`) protejează **managerul de sesiune** al utilizatorului.
  Asta **a funcționat**: de aceea de data asta 5p, 5q și fabrica au supraviețuit
  (pe 19-06 managerul a murit și a tras după el TOT; acum nu).
- DAR **procesele de arhitect în sine nu sunt protejate** (au prioritatea normală,
  „pot fi uciși"). Comentariul din cod presupunea că arhitecții mor doar dacă moare
  managerul — fals: un arhitect care își umflă singur memoria poate fi ucis direct.
- Pe scurt: jumătatea „protejează managerul" e montată; jumătatea „protejează
  arhitecții + testele grele" nu e. **Nu a contat pe 21-06, dar trebuie închisă**
  oricum (vezi fix-ul 2 mai jos), pentru ziua în care chiar va fi un OOM.

---

## Problema durabilă REALĂ (cauza-rădăcină a faptului că te-ai trezit fără arhitect)

Indiferent ce le-a închis (eveniment Claude, o eroare, sau într-o zi chiar memoria):

> **Sesiunile de arhitect nu sunt supravegheate. Când mor, rămân moarte până le observi
> TU pe telefon.**

„Paznicul" actual (`sf-architect-resume`) face doar două lucruri:
- dacă o sesiune e **înghețată** (blocată pe limită) → îi trimite un impuls să continue;
- dacă o sesiune e **moartă** → **doar îți trimite o notificare** pe telefon. NU o
  repornește (repornirea automată a fost amânată intenționat).

De-asta „3 sesiuni s-au închis la 04:25" a devenit „fondatorul s-a trezit la o fabrică
fără dirijor". Asta e ce trebuie reparat durabil.

---

## Ce-ți propun (decizia ta — cost / risc)

**Fix 1 — repornirea automată a arhitectului când moare (RECOMANDAT, rezolvă fix
incidentul ăsta).**
- Extind „paznicul" existent: când vede sesiunea moartă + fabrica vie + are treabă
  deschisă pe arhitect → o **repornește singur** prin lansatorul nostru, cu un mesaj
  care-i spune să citească ultimul handoff și să continue. (Exact ce face succesiunea
  normală — doar că automat, fără să te trezești tu.)
- Cost: mic (o modificare la un script pe care-l avem deja). Risc: mic — repornirea
  intră pe același drum verificat ca succesiunea manuală; pun o limită („nu reporni de
  mai mult de X ori pe oră") ca să nu intre în buclă.
- De ce ție-ți cer voie: ai cerut explicit să nu am „agenți care se auto-rezolvă". Asta
  e o auto-**repornire**, nu o auto-decizie. Vreau acordul tău înainte s-o montez.

**Fix 2 — închiderea găurii din «Scut» (apărare în profunzime, pentru OOM-ul viitor).**
- Pun protecția și pe procesele de arhitect, și mă asigur că orice rulare grea de teste
  trece OBLIGATORIU prin cușca de memorie (azi se poate strecura una „goală").
- Cost: mic. Risc: mic. Nu e urgent pentru azi, dar e datoria tehnică care a cauzat 19-06.

**Recomandarea mea:** **fac Fix 1 acum** (e cel care te-a durut azi), **Fix 2 imediat
după**. Spune-mi doar „da, fă amândouă" sau ce vrei să schimbi.

---

## Ce am verificat (ca să ai încredere că e complet)

Jurnal kernel (OOM) · marcajele de închidere ale sistemului pentru fiecare sesiune ·
contoarele de memorie ale cuștii fabricii · `systemd-oomd` (nu e instalat) ·
deconectări SSH / sesiuni (logind) · paznicul de trezire (codul + ce-a făcut la 04:24) ·
toate comenzile de tip „kill" din sesiunile moarte ȘI din 5q · istoricul shell ·
evenimentele fabricii în jurul minutului 04:25 (fabrica NU a căzut în masă) ·
starea limitei 5h/săptămânale (nu era epuizată) · versiunea Claude (un update automat
a aterizat în acea noapte, ~00:09 — context, nu neapărat cauză) · istoricul tuturor
căderilor de sesiuni (singura cădere simultană fără reboot = ASTA).

**Ce NU pot stabili 100%:** declanșatorul exact dinspre Claude (Remote Control vs
backend vs un crash pe un mesaj anume) — programul Claude nu scrie pe disc motivul
închiderii (mesajele de eroare s-au dus odată cu sesiunile). Dar **clasa** e clară și
dovedită: eveniment dinspre Claude pe sesiunile active, **NU memorie**.
