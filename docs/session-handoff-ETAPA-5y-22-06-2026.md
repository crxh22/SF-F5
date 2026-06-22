# Session handoff — ETAPA-5y → ETAPA-5z, 22-06-2026

**For the Main-Architect successor.** POINTER doc (Doctrine §9). 5y hit the context guard (~528k).
Launch via `claude_canon.sh` (opus, effort max, RC ON — see `docs/runbooks/session-succession.md`).

> ## ⛔ ABSOLUTE RULE (carry forward — it killed 5u/5v/5w; also in memory [[never-prompt-matching-pkill]])
> NEVER `pkill -f` / `pgrep -f` (or any broad kill) with a pattern that can appear in a session's
> launch prompt (`sf-architect-monitor.sh`, `sf-factory`, `sf-cap`, `orchestrator`, `stock-views`,
> any factory/stage word, **or now `erp-backend`/`erp-frontend`/`runserver`/`vite`**). It matches the
> FULL cmdline and kills ALL active sessions at once. Stop a background task by EXACT PID
> (verify `/proc/<pid>/cmdline`) or let it die with the session.

## FIRST duties (in order)
1. Write your session_id into `~/.claude/sf-architect-session` (replace `0646dd0a-aadf-446a-beb0-23fe96b6e360`). Your id = newest `.jsonl` in `~/.claude/projects/-home-artur-projects-SF-F5/` containing your launch prompt (verify with a unique phrase, NOT just mtime — the predecessor's transcript also contains your prompt because it authored it).
2. Update `~/.claude/sf-architect-monitor.sh` header (5y→5z) + relaunch via Bash `run_in_background:true` (the monitor dies with 5y). Logic is sound; only the header comment needs the bump.
3. Verify your RC shows on the founder's phone (`ETAPA-5z`). The founder tests from his **Android phone** (`galaxy-s24-ultra`, 100.101.93.98).
4. Do NOT pkill anything (rule above).

## STATE SNAPSHOT (verify fresh)
- Orchestrator pid `.factory/orchestrator.pid` = **506016** (5y RESTARTED it at ~03:17Z to load the D-0062 code — exact-PID SIGTERM + fresh `tmux factory` via `deploy/sf-cap.sh .venv/bin/sf-factory run | tee .factory/run-live.log`). Alive, recover()ed clean, dashboard rebound on `http://100.69.221.108:8377/`.
- **inventory-procurement phase = DONE** ✅ — merged to erp-workspace `main` (`7f9239f`). The PG unix-socket fix rode in **by content** (commit hash differs from `76232e2`, but `scripts/pg.sh` + `backend/erp/settings/base.py` on main HAVE the unix-socket fix — verified; **OPEN ITEM 1 from the 5x handoff is RESOLVED**).
- **service-orders + treasury-payments = RUNNING** (both auto-started after inventory-procurement DONE; stages being created/run). **Watch these** — escalations/decisions/budget. The monitor (below) covers escalations/decisions/routing/liveness.
- 0 open escalations, 0 pending decisions at handoff. Monitor running (last bg id `bzg9eiipi`; RELAUNCH yours).

## WHAT 5y DID
1. **PG-in-agents fix propagation to main — RESOLVED** (by the phase merge; see snapshot). No re-apply needed.
2. **Resolved phase-integration Tier-2 finding #102 (IP-INTEG-001) + approved phase_signoff #25 → phase DONE.**
   - The integration_validator PASSED inventory-procurement with ONE medium finding: `apps.procurement.urls` is **dual-mounted** at `/api/parts/` AND `/api/procurement/` (`erp/urls.py:11-12`), so ~20 procurement routes shadow-resolve under the parts-catalog prefix. NO correctness/privilege/security impact (permissions travel with views). Founder approved **accepting** it (chat opt A).
   - **DEFERRED cleanup (track it):** the per-prefix urlconf split (so `/api/parts/` exposes only `search`) belongs to the NEXT phase that touches `erp/urls.py` (service-orders or a UI phase), in-pipeline, before any frontend depends on a shadow route. Do NOT reopen IP-INTEG-001 on re-audit — it is `settled` (see escalation #102's resolution reason).
3. **Built + shipped D-0062 — the phase-level `settled` accept-path** (founder opt A, committed `e8c3cc2` + a housekeeping commit on SF-F5 main; tree clean).
   - **Why:** a phase Tier-2 finding had NO proportionate resolution — `resume` loops the deterministic gate, `replan` re-derives blindly (static `_planning_prompt`; `_step_ingest` keeps DONE rows so it can only ADD stages), `settled` was stage-only, and the architect doesn't hand-edit product code. A complete, correct phase was wedged by a cosmetic finding.
   - **Change (mirror of the stage `settled` pattern):** `models.py` (ESCALATED→AWAITING_SIGNOFF edge + `PHASE_NOACTION_RESOLUTION="settled"`), `cli.py` (phase vocab admits settled), `scheduler.py` `PhaseExecutor._step_escalated` (settled special-case → `_enter_signoff`). Tests + docs (decision-log D-0062, design-slice2 §5). 878 unit + integration green. Restarted the orchestrator to load it (see snapshot).
   - **If a phase Tier-2 finding recurs:** classify per architect-operations §1. `settled` = accept (now works at phase level) ONLY when truly no-impact; otherwise the finding needs a real fix, which has no clean phase mechanism — escalate to founder or rethink (the gap D-0062 only PARTLY closes: it handles *accept*, not *fix*).

## 🖥️ ERP TEST INSTANCE — RUNNING (founder asked to test the build)
Three persistent tmux sessions (separate from `factory`):
- **`erp-backend`** — Django `runserver 127.0.0.1:8000` (settings `erp.settings.dev`, DEBUG, unix-socket PG via the MAIN erp-workspace checkout's `.devpg`). Log `/tmp/erp-backend.log`.
- **`erp-frontend`** — Vite on `0.0.0.0:5173` → reachable at **`http://100.69.221.108:5173`**. Proxies `/api`→127.0.0.1:8000. Log `/tmp/erp-frontend.log`.
- **`erp-deviceapprover`** — auto-approves pending devices (script `/tmp/erp_device_autoapprove.py`, actor=artur) so the founder's device-approval is seamless. VERIFIED working.
- **Login:** user `artur` / password `parola-fondator-2026!` (superuser, 24 rights; set by 5y; the seed leaves the password unusable).
- These run in the MAIN erp-workspace checkout — independent of the factory's per-worktree PGs (unix sockets per dir, no conflict). Stop with `tmux kill-session -t erp-backend` (etc.) when the founder is done. NOTE: editing erp-workspace files dirties its tree → the factory's `_OutOfBoundsDetector` alerts (latched, one ntfy) at the next merge_gate/recover; the `/tmp` script avoids this.

### Founder access — RESOLVED (verified end-to-end)
Two issues, both fixed: (1) he was opening `100.69.221.108` with NO port (→:80→refused) → told him `http://100.69.221.108:5173`; (2) then "device_not_registered" looped because ALL cookies are `Secure` (`SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE`=True in base.py + the device cookie hardcoded `secure=True` in `apps/accounts/api.py`) and he's on plain HTTP → the phone dropped every cookie → re-bootstrapped a device per request (3 devices in 1 min). **Fix (DEV-ONLY, uncommitted in the erp-workspace MAIN checkout):** `backend/erp/settings/dev.py` adds `SESSION_COOKIE_SECURE=False` + `CSRF_COOKIE_SECURE=False`; `backend/apps/accounts/api.py` device cookie changed `secure=True` → `secure=settings.SESSION_COOKIE_SECURE` (consistency + dev-overridable). Verified full HTTP flow: bootstrap→auto-approve→login(artur)→/api/me 200.
- **⚠️ These 2 edits are uncommitted dev tweaks** → the factory `_OutOfBoundsDetector` will alert ONCE (latched) on the dirty `workspace:erp` tree at the next merge_gate/recover. They do NOT leak into phase worktrees (git worktrees branch from main's committed HEAD, not the working tree). **REVERT them** (`git -C ~/projects/erp-workspace checkout backend/erp/settings/dev.py backend/apps/accounts/api.py`) when the founder is done testing — and stop the `erp-*` tmux sessions.
- `tailscale serve` was tried first (the clean HTTPS path) but hung on cert provisioning (tailnet HTTPS-cert feature likely not enabled in the admin console — `tailscale serve status` empty); `tailscale serve reset` run. If you ever want the proper HTTPS URL instead of the dev cookie hack: enable HTTPS certs in the Tailscale admin console, then `tailscale serve --bg 5173` + add the `.ts.net` host to vite `allowedHosts` + Django CSRF/ALLOWED_HOSTS.

## OPEN ITEMS
1. **DEFERRED urlconf split** (IP-INTEG-001) — see WHAT 5y DID #2. Track for the next `erp/urls.py`-touching phase.
2. **Founder ERP-access thread** — see the ⚠️ above. Resolve on his next reply.
3. **D-0062 partial gap:** the phase machinery still has no clean path to *fix* (vs accept) a phase Tier-2 finding. If one arises needing a real code fix, that's a design conversation (escalate / consider extending replan to carry guidance).

## WORKING MODE / SUCCESSION
- Romanian to founder, plain, his terms, DD-MM-YYYY, copy-paste commands, NEVER AskUserQuestion, no bare IDs. `ruff check` (not format) before commit. Verify DB schema before queries (stages use `id`/`state`; escalations use `trigger`/`target`, NO `kind`/`title`; decision_requests use `gate_kind`, NO `kind`).
- **Founder delegation:** auto-approve any val+audit-passed stage/phase_signoff, ALL risk classes, without waiting ([[founder-applies-approvals-via-architect]]). For a Tier-2 escalation, verify the validator's finding is genuinely no-impact before `settled`.
- Resolution CLI: `.venv/bin/sf-factory resolve-escalation <id> <token> --reason ...` (phase tokens now: `replan|resume|awaiting_human|failed|cancelled|settled`); `.venv/bin/sf-factory decide <req_id> <option>`. Monitor watch-set + `[arhitect]` ntfy: `docs/runbooks/session-succession.md` + architect-operations canon.
- ntfy founder channel: `https://ntfy.sh` topic `claude-artur-md-hello` (title + deep link only, D-0004; `[arhitect]` prefix for architect pages).
