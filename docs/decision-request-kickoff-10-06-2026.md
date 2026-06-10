# Cerere de decizii — pornirea construcției fabricii (10-06-2026)

**Stare:** în așteptarea răspunsurilor fondatorului.
**Context:** auditul mediului e finalizat (`docs/environment-audit-10-06-2026.md`). Documentul-cadru `_FRAMEWORK_MVP_DoD.md` definește ce înseamnă MVP-ul livrat. Mai jos: deciziile necesare ca să pornesc construcția în regim autonom, planul de livrare și procedura pentru pana de curent de azi.

**Cum răspunzi:** scurt, în chat. Exemplu: „1 da, 2: <o frază>, 3–5: fă cum recomanzi, orașul: X".

---

## Decizia 1 — drepturi depline pe server (sudo fără parolă) — recomand: DA

- Ce înseamnă: pot administra serverul fără să-ți cer parola (instalări, servicii de sistem, programarea opririi).
- Riscul real: orice proces de pe server poate face orice pe el. Acceptabil aici: server de test, fără date valoroase, izolat în Tailscale, ușor de refăcut — cum ai spus chiar tu.
- Ce faci tu (o singură dată; comanda îți cere parola):
  ```
  echo 'artur ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/99-artur-nopasswd && sudo chmod 440 /etc/sudoers.d/99-artur-nopasswd
  ```
  În sesiunea Claude Code o rulezi punând `!` înainte de comandă.
- Imediat după: programez eu oprirea curată de azi, instalez utilitarele lipsă, setez ora locală.

## Decizia 2 — istoria SF → SF-F5 (o frază de la tine)

Am găsit în `~/projects/SF` fabrica anterioară: orchestrator funcțional, ~343 de teste trecute, activă până pe 08-06-2026. Documentul-cadru nou spune explicit că versiunea actuală înlocuiește tot ce a fost.

- Întrebare: ce te-a făcut să resetezi? O frază e suficientă — ca să nu repet aceeași problemă și să știu cât pot refolosi de acolo.
- Recomandarea mea implicită (dacă nu spui altceva): construiesc pe arhitectura nouă din documentul-cadru; din SF refolosesc doar mecanică punctuală verificată (gestiunea worktree-urilor git, parsarea fluxurilor NDJSON, scheletul mașinii de stări), citită ca referință și rescrisă pe schema nouă — nu copiată orbește.

## Decizia 3 — auditorul „din altă familie de modele" = Codex (ChatGPT) — recomand: DA

Documentul cere ca lucrările riscante să fie auditate și de un model din altă familie decât constructorul. `codex` e deja instalat și autentificat pe abonamentul tău ChatGPT → cost suplimentar zero, zero configurare.

## Decizia 4 — notificările către tine = ntfy.sh cu topic secret — recomand: DA

- ntfy = serviciu gratuit de notificări push pe telefon; „topic secret" = un nume lung, aleator, știut doar de noi — cine nu-l știe, nu vede nimic.
- Alternativa (server ntfy propriu, doar în Tailscale): mai privată, dar încă o piesă de întreținut; o facem doar dacă apar motive.
- Acțiunea ta (2 minute, azi sau mâine): instalezi aplicația „ntfy" pe telefon; îți spun apoi exact ce să apeși.

## Decizia 5 — confirmarea propunerilor din documentul-cadru, secțiunea 15 — recomand: DA la toate

1. Modele pe roluri: arhitecții + scriitorii de specificații = cel mai puternic model; constructorul pe lucrări de rutină = model mai ieftin și rapid; triajul CP-1 (decizia „continuă / reconstruiește / respecifică / escaladează") = model ieftin — răspunsul lui e oricum validat mecanic, cu plasă de siguranță deterministă.
2. Plafoane de consum pe stage (valori de start, recalibrabile): rutină ~300k tokeni; structural ~1M; critic ~2M.
3. Poligonul de validare: faza Foundation (schema de bază ERP), apoi inventar/achiziții ca prima fază completă.
4. Puncte de consultare LLM în orchestrator: doar CP-1, nimic în plus.

Toate intră în `factory.config.yaml` — parametri, nu beton.

## Decizia 6 (opțională) — orașul tău

Serverul e pe ora UTC; tu ești pe UTC+3 (confirmat din commit-ul tău git). Îmi spui orașul (ex. Chișinău / București) și setez ceasul serverului pe ora ta, ca orele din rapoarte să coincidă cu ale tale.

---

## Pana de curent de azi — 18:00 la tine = 15:00 pe ceasul serverului

1. **Pregătirea mea:** tot ce lucrez se scrie pe disc și se comite în git — nu se pierde nimic la oprire.
2. **Oprirea** (alege una):
   - Simplu: îmi dai sudo (Decizia 1) → programez eu oprirea curată la 17:55 ora ta și îți confirm.
   - Manual: rulezi tu, oricând înainte de pană: `sudo shutdown -h 14:55` (ora e pe ceasul serverului = 17:55 la tine). Anulare, dacă pana se amână: `sudo shutdown -c`.
3. **După revenirea curentului** (~19:00 la tine):
   1. Apeși butonul de pornire al serverului; aștepți ~2 minute.
   2. Te conectezi de pe laptop ca de obicei (Tailscale + SSH pornesc singure — verificat azi).
   3. În terminal: `cd ~/projects/SF-F5`, apoi `claude --continue`, apoi scrie „continuă".

---

## Planul de livrare (rezumat)

- **Etapa 0 — azi:** răspunsurile tale + schelet de proiect Python + `factory.config.yaml` + jurnal de decizii; dacă rămâne timp până la pană, pornesc Etapa 1.
- **Etapa 1 — planul de control** (creierul determinist al fabricii): mașina de stări, baza de date operațională (SQLite), lansatorul de agenți (`claude -p` cu flux NDJSON), încărcarea configurației; teste.
- **Etapa 2 — conveiorul unui stage:** Specificație → Construcție → Validare, cu triajul CP-1 + praguri mecanice de escaladare; demonstrat pe un stage sintetic cu defecte sădite intenționat (criteriile 7 și 9 din documentul-cadru).
- **Etapa 3 — paralelism:** lucru simultan în worktree-uri git izolate + cele două porți de merge (textuală + semantică, validator pe altă familie de modele), inclusiv scenariul-capcană obligatoriu (criteriul 8).
- **Etapa 4 — canalul tău:** notificări ntfy pe telefon + dashboard minimal (o pagină, doar prin Tailscale) + watchdog — paznicul extern care te anunță dacă fabrica moare în tăcere (criteriul 4 cere o decizie reală răspunsă de pe telefon).
- **Etapa 5 — validare pe ERP real:** sesiune de interviu cu tine (~1–2h, când poți), planul fazei Foundation pe baza documentației din `ERP-start`, apoi criteriile de acceptanță pe stage-uri reale, inclusiv un merge paralel real.

**Țintă onestă (nu promisiune):** primul stage sintetic cap-coadă — 11-06; criteriile mecanice — 12–13-06; MVP complet — ~15–16-06. Condiții: răspunsurile azi + două ferestre scurte cu tine la Etapa 5.

**Constrângere de resurse, spusă pe față:** abonamentul Claude are plafon pe ferestre de 5 ore; rulările masive de agenți îl pot atinge → lucrul se întrerupe până la resetarea ferestrei și reia singur de pe disc. Dacă devine frâna principală, îți aduc atunci opțiuni concrete (ex. plată per token).

---

## Modul de lucru (cum valorific propunerea ta)

- Eu = arhitectul principal: țin arhitectura, deciziile și dialogul cu tine; nu-mi încarc contextul cu detalii de execuție.
- Echipe de subagenți, lansate în paralel, fac implementarea modulelor; verificarea o face mereu alt agent decât executorul (doctrina §4), cu context curat.
- Tot ce contează trăiește pe disc, în git — orice sesiune nouă reia de acolo fără pierderi; de aceea pana de curent nu ne afectează.
- Corecția onestă la ideea ta: „creierul" durabil al autonomiei nu poate fi memoria mea de sesiune (volatilă — se comprimă, moare la pană sau la plafon). El e chiar fabrica pe care o construim: un program determinist care coordonează agenții. Modul „eu + subagenți" e schela — o folosim acum la maximum ca să ridicăm cât mai repede construcția care preia apoi coordonarea de rutină, iar eu rămân pe rolul de arhitect și pe deciziile cu tine.
