# Regulile de operare ale arhitectului — traducere RO a `work-protocols/architect-operations.md`

> Traducere fidelă pentru fondator (19-06-2026, ETAPA-5m). Identificatorii tehnici
> (`rework:SPEC`, `escalations.target`, numele de evenimente) rămân în engleză — sunt
> nume din cod. 👻 = referință la un mecanism PROIECTAT DAR NECONSTRUIT (vezi inventarul
> D-0059). Originalul englez e sursa canonică; ăsta e doar pentru lectura ta.

**Regim:** încărcat în prompt-ul de sistem DOAR al rolurilor de tip arhitect (main_architect,
phase_architect, spec_agent) prin stratul de arhitect al canonului — NU în canonul comun.
Aceste reguli guvernează CUM rezolvă și amendează un arhitect; nu repetă ce impune mecanic
planul de control (orchestratorul).

## 1. Rezolvarea unui „contest" — repară artefactul care generează problema, nu o amâna ca „datorie editorială"

Un audit își re-derivă constatările din SPECIFICAȚIE și din contracte la FIECARE rundă. Deci o
nepotrivire nereparată în acele texte **regenerează aceeași constatare la următorul audit** — o
buclă fără sfârșit contest→escaladare→anulare→re-ridicare. „Notează pentru mai târziu" se bazează
pe atenție și eșuează (Doctrina §20). Observat de două ori înainte să existe regula: linia
graf-de-migrare din core-entities §7 (3 runde) și clauza idle-timeout din auth-access (2 runde).

Când rezolvi un contest care a prevalat, întâi CLASIFICĂ-l, apoi acționează în ACEEAȘI rezolvare —
niciodată „anulează-și-amână":

- **Artefactul e genuin greșit** — SPECIFICAȚIA/contractul afirmă ceva ce codul corect NU face
  (textul minte despre cod). → Amendează textul acum (`rework:SPEC`). TREBUIE să se schimbe. Dacă
  amendamentul e pur documentar (nu schimbă cod), **trimite-l pe calea documentară ca să nu forțeze
  o reconstrucție inutilă.** 👻 *(„calea documentară" = mecanismul neconstruit nr. 1)*

- **Constatarea e corectă dar nu cere acțiune** — și codul și specificația sunt bune; observația e
  adevărată dar comportamentul e acceptat (ex. o margine mai restrictivă, auto-vindecătoare; o idee
  de apărare-în-adâncime amânată). → Dă-i **dispoziția „fără-acțiune"** (corectă · recunoscută ·
  închisă permanent), NU un contest și NU un rework de spec. Asta o închide la pasul de audit și o
  înregistrează ca „settled", ca auditurile ulterioare să n-o re-ridice — evitând și bucla de
  regenerare și o reconstrucție inutilă. *(Acest mecanism — „dispoziția fără-acțiune" — E construit.)*

**Flag-ul mecanic de recurență de pe dashboard e plasa de siguranță:** dacă o constatare pe care ai
închis-o sau anulat-o reapare, ăsta e semnalul că rădăcina n-a fost reparată cu adevărat —
întoarce-te la artefactul generator, nu anula din nou. 👻 *(„flag-ul de recurență" = mecanismul
neconstruit nr. 2)*

## 2. Cară DE CE-ul în rolul re-intrat

Fiecare re-intrare de rework pe care o autorizezi (rezolvarea unei escaladări, re-specificare,
reconstrucție) trebuie să-ți care raționamentul în `--reason`-ul rezolvării: ajunge în prompt-ul
agentului re-intrat. Un agent Spec/Build cu context proaspăt nu poate repara ce nu vede — numește
exact artefactul, linia și contradicția.

## 3. `rework:MERGE_GATE` — doar pentru o eșuare LA poarta de merge, niciodată ca să sari peste porțile dinainte

`rework:MERGE_GATE` re-intră DOAR în poarta de merge (Tier-1 rebase+suită + Tier-2
integration_validator) — fără re-validare, fără re-audit, fără poarta umană §9. E rezolvarea corectă
și ieftină pentru o etapă care a eșuat LA poarta de merge cu `agent_run_failed` (ex.
integration_validator și-a depășit fereastra de context): validarea structurală și dublul audit au
trecut deja și nu trebuie re-rulate, iar re-validarea re-cheltuie inutil bugetul (deja mare) al
etapei — exact ce a forțat existența acestui token (D-0041, document-engine la 107M față de plafonul
structural de 120M).

NICIODATĂ aplicat la:
- o escaladare **`unresolved_contest`** — poarta închide doar constatările `open` ale
  integration_validator, deci constatările structurale contestate ar rămâne `contested` pentru
  totdeauna și etapa ar putea ajunge DONE cu un contest atârnat, niciodată-închis. Folosește
  `rework:VALIDATE` / `rework:BUILD`.
- o etapă care **n-a trecut încă de AUDIT** (escaladată din SPEC/BUILD) — ar sări la poartă cu zero
  validare structurală și, pe o etapă critică, ar ocoli poarta umană §9 a fondatorului. Re-intră în
  pasul care chiar a eșuat.

Deliberat **nu există gardă-mașină** (Doctrina §8 — niciun mecanism preventiv fără un incident);
regula asta E garda. O aplicare greșită e incidentul care ar justifica o precondiție în cod.

## 4. Scara de rutare a escaladărilor + detectorul de escaladări blocate al orchestratorului (robustness UNIT 2, D-0042)

Orchestratorul consumă acum `escalations.target` ca **semnal de rutare viu** — înlocuitorul durabil,
în cod, al monitorului bash legat-de-sesiune care era înainte singura cale de notificare a
arhitectului. E **doar un strat mecanic**: citește, paginează și re-etichetează `target`; NICIODATĂ
nu rezolvă o escaladare, nu tranziționează o unitate, nu pornește un agent (mandatul fondatorului:
„niciun agent-rezolvitor"). Rezolvarea rămâne judecata ta — răspunzi tot prin `cli resolve-escalation`
/ cardul de pe dashboard.

**Scara de rutare** (sursa unică din cod, consumată de scheduler + glosată de dashboard):

```
phase_architect  →  main_architect  →  founder
```

Locurile de creare scriu primele două după natura escaladării (conveior-de-etapă →
`phase_architect`, transversal → `main_architect`); detectorul URCĂ spre `founder` (autoritatea de
produs supremă) și se oprește acolo (nicio treaptă peste fondator). Un „bump" e doar o schimbare de
etichetă + destinatar-pagină — bump la `founder` NU ridică un card de decizie și nu tranziționează
unitatea.

**Detectorul** (rulează la fiecare tick al orchestratorului) emite trei evenimente distincte,
grep-abile mecanic, și paginează printr-un prefix de titlu ntfy DISTINCT **`[arhitect]`** pe UNICUL
topic comun (titlul lasă fondatorul să releze corect și un observator de telefon să distingă).
Fiecare se declanșează O DATĂ per episod/treaptă (cu zăvor — fără oboseală de alarmă):

| eveniment | când | acțiune |
|---|---|---|
| `escalation_opened_notice` | o escaladare țintită-pe-arhitect e văzută `open` și nenotificată — **vârstă 0, la primul tick, înainte de orice prag** | o pagină `[arhitect]` → face „arhitectul află ≤5 min" o lege în COD care supraviețuiește unui monitor de sesiune mort. Escaladările țintite-pe-`founder` NU sunt prim-notificate aici (sunt domeniul fondatorului). |
| `escalation_bumped` | una `open` a cărei vârstă spune că aparține unei trepte MAI ÎNALTE decât `target`-ul curent (prag implicit 30 min) | urcă `target` direct la treapta derivată-din-vârstă + paginează acea treaptă. `target`-ul persistat E zăvorul, deci urcă cel mult o treaptă per interval-de-prag și se oprește la `founder`. |
| `escalation_stuck_resolved` | una `resolved` de mai mult decât pragul DAR unitatea e ÎNCĂ `ESCALATED` (rezolvarea n-a fost preluată) | paginează `target`-ul curent. Rândul e deja rezolvat; TĂCEREA e bug-ul. Detectorul NU re-rezolvă / re-creează / tranziționează — e doar plasa de siguranță zgomotoasă. |

O eșuare de livrare a unei pagini NU dărâmă niciodată bucla: loghează un eveniment `alert_delivery_failed`
și reîncearcă la următorul tick.

**Monitorul tău de sesiune TREBUIE să facă grep pe aceste trei tipuri de evenimente** (și să recunoască
titlurile ntfy `[arhitect]`) ca o sesiune succesoare să afle de escaladări într-un poll de la trecerea
pragului. Push-ul ntfy `[arhitect]` e plasa de siguranță umană dacă monitorul e căzut. *(Acest grep — P4
în D-0059 — tocmai l-am reparat în monitor.)*
