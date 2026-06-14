# Runbook — Main-Architect session succession (D-0037, founder directive 12-06-2026)

**Purpose:** controlled handoff instead of auto-compaction for the Main-Architect session.
A `UserPromptSubmit` hook (`~/.claude/hooks/sf-architect-context-guard.sh`, registered in
`~/.claude/settings.json`) estimates the session's context from its transcript size
(bytes/5, calibrated 12-06-2026) and, past `SF_HANDOFF_THRESHOLD_TOKENS` (default 500k),
injects a succession note into the architect session ONLY — every other session/agent is
a silent no-op via the marker-file guard (`~/.claude/sf-architect-session` must equal the
firing session's id).

## The protocol (executed by the SITTING architect session when the note appears)

1. **Finish the current work unit** — never hand off mid-slice (a successor inheriting a
   half-built slice re-derives context expensively and errs). The note repeats on every
   founder prompt until succession; no urgency spike — 500k of a 1M window.
2. **Write the handoff** to `docs/session-handoff-<ETAPA-name>-DD-MM-YYYY.md` — POINTER
   document per Doctrine §9 (history = decision log; the handoff carries: where everything
   lives, live snapshot disclaimer, immediate work items in order, working-mode learnings).
   Archive pattern per existing handoffs. Commit it.
3. **Launch the successor** (incremented letter: ETAPA-5a → 5b → 5c …):
   ```bash
   SFF5_TMUX_SESSION=etapa-5<next> SFF5_RC_NAME=ETAPA-5<next> /home/artur/projects/SF-F5/claude_canon.sh \
     "Ești ETAPA-5<next>, succesoarea sesiunii Main-Architect. Citește docs/session-handoff-<...>.md și continuă. Scrie session_id-ul tău în ~/.claude/sf-architect-session (înlocuiește conținutul) ca să preiei garda de context."
   ```
   The launcher carries the canon + effort + **Remote Control** identically (claude_canon.sh
   contract): it passes `--model opus --effort max --remote-control ETAPA-5<next>`, so RC is
   ON and the session is phone-named at launch (D-0041) — no manual taps. `SFF5_RC_NAME`
   sets the phone-visible label (defaults to the tmux session name otherwise).
4. **Hand over the marker:** the successor's FIRST duty (in its launch prompt) is writing
   its own session id into `~/.claude/sf-architect-session` — the context guard follows
   the marker, never the name. (The predecessor can pre-clear the marker if paranoid;
   a missing marker = guard inert, never wrong-target.)
5. **Founder: zero taps needed** (D-0041 automated `/rc` + naming). The successor already
   appears on the phone as `ETAPA-5<next>` with Remote Control live — the founder just OPENS
   it. **VERIFY before the predecessor goes silent:** confirm the successor shows up on the
   phone (claude.ai/code, green dot) — if RC silently failed, the founder is still reachable
   on the predecessor's live RC, so DO NOT go silent until the successor's RC is confirmed.
   The old session stays attached in its tmux window, idle — founder reviews history via
   remote control and exits it manually when done.
6. The predecessor announces the succession to the founder (one line, where the successor
   lives) and goes silent. It must NOT keep working after the successor takes the marker
   (two architects = two writers — same reason the factory has a sole-writer rule).

## Threshold & calibration

- Default 500k tokens ≈ 2.5MB transcript (estimator bytes/5; calibrated against ETAPA-5a:
  2.6MB ≈ ~520k real context). Override per-launch: `SF_HANDOFF_THRESHOLD_TOKENS=<n>`.
- Re-calibrate when the estimate drifts >20% from the founder-visible context meter; the
  divisor lives in the hook script with its calibration comment.

## Scoping guarantee (the founder's danger, mechanically closed)

The hook fires for every Claude Code session on this machine but ACTS only when
`session_id == cat ~/.claude/sf-architect-session`. Factory pipeline agents (claude -p in
workspace worktrees), Main-Architect subagents (builders/verifiers — they reach 300k+
legitimately), and any ad-hoc session: silent exit 0. Tested 12-06-2026 (4 paths: match
over threshold → note; non-match → silent; match under threshold → silent; corrupt input
→ silent).
