# Runbook — Main-Architect session LAUNCH protocol (the mechanical contract)

Companion to `session-succession.md` (the WHY/WHEN). This file is the HOW — the exact, verified
mechanics. **The founder is phone-only (no terminal): a wrong launch leaves the project with NO architect
and no way for him to fix it.** The sitting architect MUST auto-launch its successor itself — never ask the
founder to run a command. Follow verbatim. Refs: D-0041 (auto-RC), D-0051 (headless fix),
memory [[never-prompt-matching-pkill]]; failure evidence in PART 2 of the ARH-02 launch research (22-06).

## A. Session-START algorithm (the successor's FIRST actions, in order)
1. **Claim the marker.** Your session id = the newest `.jsonl` in
   `~/.claude/projects/-home-artur-projects-SF-F5/` that contains a UNIQUE phrase from YOUR launch prompt
   (NOT by mtime — the predecessor's transcript also contains the prompt; the LIVE one keeps GROWING).
   Write it into `~/.claude/sf-architect-session`, REPLACING the predecessor's id. The context guard
   follows this marker, not the session name.
2. **Verify RC.** Confirm you appear on the founder's phone (claude.ai/code, green dot) as `ARH - NN`. Do
   NOT go silent until the founder confirms — if RC silently failed he is still reachable on the
   PREDECESSOR's live RC.
3. **Do NOT start the session monitor while the factory is STOPPED** — it loops on exit-12 (orchestrator
   absent). Start it (and bump its header `ARH-NN`) ONLY when you restart the factory at re-seed. The
   monitor is `~/.claude/sf-architect-monitor.sh`; launch it via the Bash tool with
   `run_in_background: true` (never `nohup … &`).
4. Read the predecessor's handoff `docs/session-handoff-ARH-<prev>-*.md` IN FULL.

## B. FORBIDDEN / DANGEROUS actions (these killed real sessions)
- **NEVER `pkill -f` / `pgrep -f` (or any broad kill) with a pattern that can appear in a session's launch
  prompt** — `sf-architect-monitor.sh`, `sf-factory`, `orchestrator`, `erp-backend`, any factory/stage
  word. The architect prompts EMBED these strings; `pkill -f` matches the full cmdline and kills the
  `claude` process of EVERY active architect at once (the 5u/5v/5w + 5r/5s/5t triple-deaths).
- **Stop a task ONLY by EXACT PID** (verify `/proc/<pid>/cmdline` first, then `kill <pid>`) or **EXACT
  tmux session name** (`tmux kill-session -t <name>`). Before any kill, eyeball `pgrep -af <pat> | grep -v claude`.
  This is for stopping an ERRANT FACTORY task — **NEVER for retiring the predecessor architect session** (see the ⛔ below).
- Monitors/watchers are CHILDREN of your session — they die with it. You do NOT need to kill them before
  succession; just let them exit.
- **Never set `SFF5_NO_TMUX=1`** (headless, dies on SSH hangup, RC never registers) and **never
  `SFF5_NO_RC=1`** (no phone control) for a succession launch.
- After the successor takes the marker, the predecessor GOES SILENT — never two architects writing at once.
- **⛔ NEVER kill or exit the PREDECESSOR architect session** — no `tmux kill-session`, no `kill <pid>`, no
  `/exit`, nothing. Sole-writer is enforced by the MARKER reassignment, NOT by killing — so killing is never
  needed. The predecessor stays attached + idle so its history remains READABLE on the founder's
  claude.ai/code dashboard. An architect-initiated kill DROPS that dashboard view (founder-observed
  22-06-2026: ARH-03 `tmux kill-session`'d ARH-02 and its dashboard history vanished — the on-disk
  `.jsonl` transcript survives, but the founder reads history on the dashboard). **The FOUNDER retires the
  old session himself**, his own way, when done reading it (`session-succession.md` steps 5–6). Do NOT
  "declutter" his phone by killing old sessions — that is his call, not the architect's.

## C. FINALIZATION — the successor AUTO-LAUNCH command (run from YOUR Bash)
Your Bash is non-interactive but runs inside your tmux (`$TMUX` is set). This is the exact case
`claude_canon.sh` handles (D-0051): it sees no TTY → unsets the inherited `$TMUX` → creates a DETACHED,
pty-backed, RC-registered tmux session. So just run the launcher with the two env vars — **do NOT wrap it
in `env -u TMUX tmux new-session`** (that double-nests and mislabels RC; the launcher already does the
unset + detached `new-session -d` internally). `NN` = predecessor's number + 1, zero-padded; the tmux slug
uses a dash, the RC label uses spaces.

    SFF5_TMUX_SESSION=arh-NN SFF5_RC_NAME="ARH - NN" /home/artur/projects/SF-F5/claude_canon.sh \
      "Ești ARH - NN, succesoarea sesiunii Main-Architect. Citește docs/session-handoff-ARH-<prev>-DD-MM-YYYY.md ÎNTREG + docs/runbooks/session-launch-protocol.md, apoi continuă. PRIMA acțiune: scrie session_id-ul tău în ~/.claude/sf-architect-session (înlocuiește conținutul). REGULĂ ABSOLUTĂ: niciodată pkill -f / pgrep -f cu tipar din prompt; oprește task-uri DOAR prin PID exact sau nume exact de sesiune tmux. NU porni monitorul (fabrica e OPRITĂ). Verifică RC pe telefon (ARH - NN) și nu tăcea până confirmă fondatorul. <restul contextului>"

**VERIFY before going silent:** `tmux has-session -t arh-NN` succeeds AND a `claude … --remote-control "ARH - NN"`
process is running (`ps -eww -o pid,args | grep -F -- '--remote-control ARH - NN' | grep -v grep`), AND the
founder confirms it on his phone. Always pick a FRESH, never-used `NN` — branch D refuses (exit 1) on a name
collision rather than silently re-attaching with args ignored, so a reused name = a failed launch.
