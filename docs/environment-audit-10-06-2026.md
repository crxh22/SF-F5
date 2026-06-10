# Environment Audit — server-e9 — 10-06-2026

Per `_FRAMEWORK_MVP_DoD.md` §16.1: first action before planning. Facts gathered 10-06-2026 10:11–10:20 UTC on server-e9; each line summarizes a command result from the audit session.

## System

- Ubuntu 24.04.4 LTS, kernel 6.8.0-124-generic; 16 CPU cores; 31 GiB RAM (~26 free); disk 98G total, 80G free on `/`.
- Clock: `Etc/UTC`. Founder local time = UTC+3 (evidence: founder's initial git commit authored `+0300`). Planned power outage today at 15:00 UTC (18:00 founder-local), ~1h.
- `systemctl is-system-running` → `running`; uptime since 05-06-2026.

## Runtimes & CLIs

| Tool | Status |
|---|---|
| python3 | 3.12.3 (stdlib sqlite 3.45.1 — WAL capable) |
| uv | 0.11.19 — **installed during this audit** (`~/.local/bin`) |
| node / npm | v24.16.0 / 11.13.0 |
| git | 2.43.0 |
| claude CLI | 2.1.170, authenticated (subscription) |
| codex CLI | 0.139.0, authenticated — `codex login status` → "Logged in using ChatGPT" |
| gh | 2.93.0 |
| tmux, jq, curl, wget, rg | present |
| sqlite3 CLI | MISSING (apt → needs sudo); convenience only, python stdlib suffices |
| gemini CLI | MISSING — not needed; codex covers the cross-model role |

## Verified factory mechanisms

- Headless agent spawn: `claude -p "Reply with exactly: FACTORY-OK"` → returned `FACTORY-OK`.
- NDJSON streaming: `claude -p --output-format stream-json --verbose` → emits `system/init`, `assistant`, `rate_limit_event` lines. Locked decision §2.6 (process model) viable.
- Rate-limit reality: subscription enforces a rolling **five-hour window**; overage **rejected** (org-level disabled). Heavy agent fan-out can hit the cap → long runs must checkpoint to disk and resume; this is already a design requirement (§8 `context_budget`, §6 cost ledger). Treat as an economics input, not a blocker.
- Network reach: ntfy.sh → 200; github.com → 200; pypi.org → 200; api.anthropic.com reachable.

## Access & reboot resilience

- User `artur`, member of `sudo` group; passwordless sudo: **NO** (pending kickoff decision #1).
- Tailscale: server-e9 = 100.69.221.108; founder laptop `dell-artur` (Windows) connects via SSH over the tailnet (`SSH_CONNECTION` from 100.64.73.47). No exposure outside the tailnet (founder statement; `ss` shows only :22, :53, tailscale ports).
- Reboot persistence: `tailscaled` **enabled**; `ssh.socket` **enabled** (socket-activated sshd — `ssh.service` itself shows `disabled`, the normal Ubuntu 24.04 pattern). → Founder access self-restores after power-on. Verified ahead of today's outage.
- Git identity: repo `user.*` was unset; initial commit author is `crxh22 <214634318+crxh22@users.noreply.github.com>` — set as repo-local identity for factory commits.

## Neighbor repositories

- `~/projects/ERP-start` — **canonical ERP documentation** (founder designation, 10-06-2026). Mature: ~33 content files, domain docs validated through founder sessions (03-2026), 6 ADRs, locked stack (Django + PostgreSQL + React/TS + Ant Design), explicit module breakdown: foundation registers / inventory / service orders / counterparties / accounting / operations. First factory target; framework itself stays project-agnostic (founder: first but not only project).
- `~/projects/SF` — **prior factory iteration** (97M, ~1458 .py files): working async orchestrator (state machine, aiosqlite persistence, event bus, git-worktree manager, NDJSON executor registry), 343+ tests, live until 08-06-2026. Superseded by SF-F5 per DoD v3 ("no section inherits authority"). Harvest-as-reference candidate — founder's reset rationale pending (kickoff decision #2).
- `~/projects/ERP-Codex-control-tower` — older ERP docs snapshot; superseded by ERP-start.

## Gaps / pending

1. Passwordless sudo — founder decision #1. Unblocks: apt installs (sqlite3 CLI), timezone set, scheduled clean shutdown for today's outage, systemd watchdog timer later.
2. Server timezone stays UTC until founder names his city (cosmetic; machine-parsed timestamps stay ISO UTC per conventions regardless).
3. ntfy phone app — founder action, after decision #4.

**Conclusion:** environment is implementation-ready except the sudo grant; nothing blocks Etapa 0/1 work today besides system-level installs.
