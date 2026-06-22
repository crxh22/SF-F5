# Revizia pipeline-ului fabricii SF-F5 — 22-06-2026

**Pentru fondator.** Răspuns la cererea: *"cum e actual pipeline-ul, ce agenți cu ce roluri
clare există, care există doar în teorie"* — în formă grafică + datele care o susțin.

- **Schema grafică:** `pipeline-map-22-06-2026.png` (sursa: `pipeline-map-22-06-2026.dot`).
- **Sursa de adevăr:** `src/sf_factory/scheduler.py` (conveyorul), `factory.config.yaml`
  (rolurile + clasele de risc), plus **dovada empirică** — tabela `process_registry` din
  `.factory/factory.db` (ce s-a pornit cu adevărat în viața fabricii).

---

## 1. Cum funcționează, pe scurt

Fabrica are **două conveyoare** conduse de **Orchestrator** (cod Python deterministic —
NU un LLM; `scheduler.py`, ticăie la 5s):

- **Conveyor FAZĂ** (un strat = o fază): `PLANNING` (phase_architect descompune faza în etape
  + îngheață contractele) → `RUNNING` (rulează etapele) → `INTEGRATING` (integration_validator
  verifică etapele împreună) → `AWAITING_SIGNOFF` (fondatorul semnează faza) → `DONE`.
- **Conveyor ETAPĂ** (fiecare etapă din fază): `SPEC` (spec_agent) → `BUILD` (builder) →
  `VALIDATE` (validator) → `AUDIT` (2 auditori — doar structural/critical) → `AWAITING_HUMAN`
  (fondator §9 — doar critical) → `MERGE_GATE` (testele + integration_validator) → `DONE`.

Deasupra ambelor: **fondatorul** (PO + arbitru) și **main_architect** (sesiunea interactivă =
arhitectul; om-în-buclă, NU e pornit de fabrică).

---

## 2. Rosterul agenților — cu dovada empirică (câte rulări reale)

| Rol (cheie cod) | Model / CLI | Când pornește | Rulări reale | Status |
|---|---|---|---|---|
| `phase_architect` | opus, print | la planificarea fiecărei faze | **4** | **nucleu** |
| `spec_agent` | opus, print | la SPEC, fiecare etapă | **42** | **nucleu** |
| `builder_routine` | sonnet, print | la BUILD, etape *routine* | **18** | **nucleu** |
| `builder_heavy` | opus, print | la BUILD, etape *structural/critical* | **274** | **nucleu** |
| `validator` | sonnet, print | la VALIDATE, etape *routine* | **15** | **nucleu** |
| `validator_structural` | opus, print | la VALIDATE, *structural/critical* | **179** | **nucleu** |
| `auditor_same_model` | opus, print | la AUDIT, *structural/critical* | **144** | **nucleu** |
| `auditor_cross_model` | **codex** gpt-5.5 | la AUDIT, *structural/critical* | **144** | **nucleu** |
| `integration_validator` | opus, print | MERGE_GATE etapă + INTEGRATING fază | **46** | **nucleu** |
| `cp1_triage` (CP-1) | haiku, print | la VALIDATE, **doar dacă pragurile nu decid** | **5** (ultima 14-06) | **condiționat — aproape dormant** |
| `capacity_probe` | haiku, print | **doar la atingerea limitei Claude** (canar) | **493** | **condiționat** (nu e agent de build) |
| `decision_session` | opus, print | sesiune de decizie fondator, prin dashboard | **2** (20-06) | **rar** |
| `main_architect` | opus, **interactiv** | sesiunea arhitectului (om) | **0** print | **om, nu agent autonom** |

**Nu sunt agenți LLM** (apar în config dar sunt altceva): `test_suite` (rulează `pytest` — cod;
82 rulări), `notify` (notificări ntfy — infrastructură), `intake` (interviu uman de început, o
singură dată; nici măcar nu are model definit).

---

## 3. Verdictul onest: "care există doar în teorie"

**Niciun agent autonom definit nu e pură teorie — toți cei 12 au rulat cel puțin de 2 ori.**
Diferența reală nu e "există / nu există", ci **cât de mult contează în pipeline-ul viu**:

- **Nucleul (9 roluri)** — rulează la fiecare etapă/fază, sutele de rulări o confirmă. Aici e
  fabrica adevărată: spec → build → validate → (audit dublu pe ce e serios) → merge-gate.
- **Condiționate / marginale (3):**
  - `cp1_triage` (CP-1) — gândit ca arbitru când pragurile automate nu pot decide. **5 rulări,
    ultima pe 14-06.** Practic dormant: pragurile deterministe decid aproape mereu singure.
  - `decision_session` — sesiune de dialog cu fondatorul pe un compromis. **2 rulări.** De
    obicei deciziile se iau cu un buton pe dashboard, nu cu o sesiune live.
  - `capacity_probe` — multe rulări (493) dar nu e agent de build: e un canar ieftin care
    testează dacă limita de uzaj Claude s-a ridicat, ca fabrica să-și reia lucrul singură.
- **Roluri care nu-s agenți autonomi:** `main_architect` (omul), `intake` (interviu uman),
  `notify`/`test_suite` (infrastructură/cod).

---

## 4. Observații pentru o eventuală revizie a "modului de lucru" al pipeline-ului

Brutal honest, nu validare — puncte unde mecanismul ar putea fi simplificat sau întărit:

1. **CP-1 e aproape mort.** Conceput ca triaj inteligent, dar pragurile automate decid singure
   în ~toate cazurile. De decis: îl ținem (ca plasă de siguranță rară) sau îl scoatem (un rol
   mai puțin de întreținut)?
2. **`decision_session` se suprapune cu butonul de pe dashboard.** Două căi pentru același lucru
   (decizia fondatorului pe un compromis). Merită păstrate ambele?
3. **Doar 5 etape *routine* în toată fabrica** (din 58): 24 critical + 29 structural domină.
   Aproape totul trece prin auditul dublu + (la critical) poarta umană §9. Asta e scump și lent
   — întrebarea de structură: chiar e totul "serios", sau decupăm mai multe etape mici *routine*?
4. **`auditor_cross_model` e singurul agent pe codex** (gpt-5.5) — restul e Claude. Diversitatea
   de model la audit e intenționată (prinde ce nu prinde același model), dar e și singura
   dependență de un al doilea furnizor.
