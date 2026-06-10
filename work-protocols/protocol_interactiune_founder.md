# protocol_interactiune_founder.md — how agents communicate with the founder

**Purpose:** canonical protocol for the AI ↔ founder communication mode. Governs *how* you communicate, not *what* you decide.

## 1. When to read + what it governs

- **When:** any interaction with the founder.
- **What it governs:** the choice of communication mode. Does NOT replace the substantive criterion (who decides what).

## 2. The META principle (core)

**Before communicating with the founder, identify the nature of the task and choose the mode accordingly:**

| Mode | Nature | How you communicate |
|---|---|---|
| (1) Founder's decision | Touches business / money / data / security / legal / strategy / priority / hard-to-reverse risk | Prepared options, in his terms, with a recommendation |
| (2) Exploration / analysis | Open problem, root-causing, design, restructuring — no choice among fixed options | Free dialogue: synthesis + hypotheses, NOT rigid options |
| (3) Autoresolve | Technical + local + reversible + testable + does NOT touch business/money/data/security/legal + not an architecture question | Resolve, report concisely, do NOT ask |

**Modes can flow into one another:** you explore in (2); when a high-risk decision point appears (irreversible, business/money/data/security/legal), you switch to (1) and prepare options with a recommendation. The nature of the task at the current moment decides the mode — not the one you started in.

## 3. Mode (1) — founder's decision

- **Prepared options with a recommendation**, NOT an open question: "A does X, B does Y, I recommend A because Z" + concrete context. NOT "what do you want me to do?".
- **Format: proposal + 1-2 concrete comparative examples**, not an abstract description. The founder decides fast on examples, not on theory.
- **Terms he understands:** cost / speed / risk / impact. NOT technical jargon.
- **If the options are technical and the founder isn't versed in them** (e.g. which library, which language): reframe into clear priorities (cost vs speed vs risk vs maintenance) & consequences, OR decide yourself and report. Do NOT ask a blind technical question.

## 4. Mode (2) — exploration / brainstorm / analysis / debate

- **First invest effort to understand deeply** (evidence, multiple perspectives, verification, simulations).
- **Come with synthesis + hypotheses + the patterns you see + where the decision points are.** NOT blind questions, NOT rigid options.
- **Free, iterative dialogue.** Key decisions are identified IN the dialogue, not pre-decided.
- Working mode: "investigation report + dialogue", not "questionnaire".

## 5. Mode (3) — autoresolve

- When it's technical + local + reversible + testable + non-business: **resolve, report concisely, do NOT ask needlessly.**

## 6. CONSTRAINTS
- **NO** context-stripped IDs (`K1`, `F-12`, `ADR-0021`), acronyms without a gloss (HLD, DoR, FCFS), or internal-doc cross-refs. Each → re-explain inline (`K1 = candidate-utility flow, founder-approved via chat`) or drop it — cold context (founder on phone, hours later) makes redundant clarity cheaper than an unresolvable reference.
- NEVER the `AskUserQuestion` tool.
- ONLY Romanian, plain language

## 7. Escalation

- **High-risk** (strategy / business / money / data / security / legal / irreversible operation): block and escalate to the founder as mode (1). Do NOT default-action on something irreversible.
- **Low / medium-risk reversible:** autoresolve (mode 3), report in summary.
