#!/usr/bin/env bash
# sf-architect-resume.sh — D-0059 (founder option A, 19-06-2026): external watchdog that
# WAKES the Main-Architect session when it is STUCK with work to do — the D-0056 failure
# class (the architect's OWN claude calls hit the usage limit, the session froze, and it did
# NOT auto-resume after the reset; the founder had to wake it by hand).
#
# It runs OUTSIDE any architect session (a systemd timer, like the orchestrator watchdog), so
# it survives a frozen/dead architect. INERT by default: a bare run is DRY-RUN — it logs every
# signal + the decision and takes NO action. `--act` performs the wake. The timer runs `--act`.
#
# WAKE TRIGGER — all three true:
#   1. the orchestrator is RUNNING (PID-ANCHORED: liveness mtime fresh + pidfile pid alive with
#      an sf_factory cmdline — NOT pgrep, which false-matches a prompt containing the path). When
#      the factory is paused (tests-only / offline surgery) the architect is FOUNDER-driven and a
#      stale session is correct — this gate makes the whole mechanism a no-op then.
#   2. the architect transcript (.jsonl of the session_id in ~/.claude/sf-architect-session) is
#      STALE: untouched > STALE_MIN min (it updates every few seconds while the architect works;
#      staleness ⇒ idle or frozen).
#   3. there is architect-actionable WORK: an OPEN escalation targeted at the architect
#      (phase_architect | main_architect). Founder-targeted escalations + pending decisions are
#      the FOUNDER's to act on — the architect waiting on those is correct, no wake.
# Then: send-keys a resume nudge to the architect's tmux session (full context preserved); if
# that session is GONE (architect process died, not just froze), page the founder (v1 — the
# riskier auto-RELAUNCH of a dead session into a context-less prompt is deferred, D-0059). A
# COOLDOWN latch caps it at one action / SF_ARCH_COOLDOWN_MIN so a wake that does not take never
# storms. Any query failure ⇒ fail-explicit no-op (never wake on guessed state, Doctrine §7).
set -uo pipefail

ACT=0; [ "${1:-}" = "--act" ] && ACT=1
STALE_MIN="${SF_ARCH_STALE_MIN:-20}"
COOLDOWN_MIN="${SF_ARCH_COOLDOWN_MIN:-30}"
# Hardcoded (NOT $HOME): a systemd system service does not inherit the login env.
H=/home/artur/.claude
FACT=/home/artur/projects/SF-F5/.factory
# Explicit tmux socket: a systemd service has no $TMUX and must reach the user's server by
# path. Default socket is /tmp/tmux-<uid>/default; a missing socket ⇒ has-session fails ⇒
# tmux treated as gone ⇒ founder paged (never a wrong send-keys).
TMUX_SOCKET="${SF_TMUX_SOCKET:-/tmp/tmux-$(id -u)/default}"
tmuxc(){ tmux -S "$TMUX_SOCKET" "$@"; }
DB="$FACT/factory.db"
PIDFILE="${SF_ORCH_PIDFILE:-$FACT/orchestrator.pid}"
LIVENESS="${SF_ORCH_LIVENESS:-$FACT/liveness}"
STALENESS_S="${SF_ORCH_STALENESS_S:-300}"
SID_FILE="$H/sf-architect-session"
TMUX_FILE="$H/sf-architect-tmux"
PROJ_DIR="$H/projects/-home-artur-projects-SF-F5"
STATE_FILE="$H/sf-architect-resume.state"
NTFY="https://ntfy.sh/claude-artur-md-hello"
now=$(date +%s)
log(){ echo "[arch-resume] $(date -u +%FT%TZ) $*"; }

# signal 1 — orchestrator alive, PID-ANCHORED (watchdog parity), NOT pgrep. `pgrep -f
# '.venv/bin/sf-factory run'` false-matches any process whose cmdline merely CONTAINS that path
# — a parallel claude session whose succession PROMPT embeds it (the D-0058 morning-resume
# command does exactly that) would read as "alive" and make this script wake the architect
# during an intentional pause. Liveness mtime (touched every loop_tick_s) + the pidfile process
# being alive with an sf_factory cmdline is unforgeable.
orch=0; lage=-1
if [ -f "$LIVENESS" ]; then
  lage=$(( now - $(stat -c %Y "$LIVENESS" 2>/dev/null || echo 0) ))
  [ "$lage" -lt "$STALENESS_S" ] && orch=1
fi
if [ "$orch" -eq 1 ]; then
  pid=$(head -1 "$PIDFILE" 2>/dev/null | tr -dc '0-9' || true)
  if [ -z "$pid" ] || ! grep -qaE 'sf_factory|sf-factory' "/proc/$pid/cmdline" 2>/dev/null; then orch=0; fi
fi

# signal 2 — architect transcript staleness (mtime of the current session's .jsonl)
sid=$(cat "$SID_FILE" 2>/dev/null || true)
tj="$PROJ_DIR/$sid.jsonl"
if [ -n "$sid" ] && [ -f "$tj" ]; then
  mt=$(stat -c %Y "$tj" 2>/dev/null || echo 0)
  age=$(( now - mt )); stale=$(( age > STALE_MIN*60 ? 1 : 0 ))
else
  age=-1; stale=0   # no transcript ⇒ cannot judge ⇒ fail-safe: not stale, no wake
fi

# signal 3 — architect-actionable open escalations
work=$(sqlite3 -readonly "$DB" "SELECT COUNT(*) FROM escalations WHERE status='open' AND target IN ('phase_architect','main_architect');" 2>/dev/null || echo "")
[ -z "$work" ] && work=-1   # query failed ⇒ unknown ⇒ fail-explicit (no wake)

arch_tmux=$(cat "$TMUX_FILE" 2>/dev/null || true)
if [ -n "$arch_tmux" ] && tmuxc has-session -t "$arch_tmux" 2>/dev/null; then tmux_alive=1; else tmux_alive=0; fi

log "signals: orchestrator=$orch (liveness_age=${lage}s thr=${STALENESS_S}s) architect_stale=$stale (age=${age}s thr=$((STALE_MIN*60))s sid=${sid:0:8}) work=$work tmux=[$arch_tmux] alive=$tmux_alive act=$ACT"

# decision — short-circuit on each gate with an explicit reason
if [ "$orch" -ne 1 ]; then log "no-op: orchestrator not running (factory paused — architect is founder-driven)"; exit 0; fi
if [ "$stale" -ne 1 ]; then log "no-op: architect transcript fresh (age=${age}s) — working or recently active"; exit 0; fi
if [ "$work" -lt 1 ]; then log "no-op: no architect-targeted open escalation (work=$work) — idle is correct"; exit 0; fi

# STUCK: orchestrator up + architect stale + architect-targeted work pending. Cooldown latch.
last=$(cat "$STATE_FILE" 2>/dev/null || echo 0); [[ "$last" =~ ^[0-9]+$ ]] || last=0
if [ $(( now - last )) -lt $(( COOLDOWN_MIN*60 )) ]; then
  log "STUCK but in cooldown ($(( (now-last)/60 ))min < ${COOLDOWN_MIN}min) — skip"; exit 0
fi

nudge="Reia lucrul: limita de utilizare probabil s-a resetat sau ai fost blocat. Verifica dashboard-ul, escaladarile deschise (cli list-escalations) si ultima intrare din docs/decision-log.md, apoi continua de unde ai ramas."
if [ "$tmux_alive" -eq 1 ]; then
  if [ "$ACT" -eq 1 ]; then
    if tmuxc send-keys -t "$arch_tmux" "$nudge" Enter 2>/dev/null; then
      echo "$now" > "$STATE_FILE"; log "ACTED: send-keys wake -> tmux '$arch_tmux' (cooldown armed ${COOLDOWN_MIN}min)"
    else
      log "FAILED: send-keys to tmux '$arch_tmux' errored — paging founder"
      curl -s -H "Title: Arhitect blocat — trezirea a esuat" -H "Priority: max" -d "send-keys a esuat catre tmux $arch_tmux; verifica manual" "$NTFY" >/dev/null 2>&1 || true
    fi
  else
    log "WOULD send-keys wake -> tmux '$arch_tmux' (dry-run; pass --act to perform)"
  fi
else
  if [ "$ACT" -eq 1 ]; then
    curl -s -H "Title: [arhitect] Arhitect MORT pe limita — reporneste-l" -H "Priority: max" -d "Sesiunea tmux a arhitectului a disparut iar fabrica are escaladari deschise pe arhitect. Reporneste prin claude_canon.sh (vezi runbook succesiune)." "$NTFY" >/dev/null 2>&1 && { echo "$now" > "$STATE_FILE"; log "ACTED: architect tmux GONE — paged founder (cooldown armed)"; } || log "FAILED: founder page curl errored"
  else
    log "WOULD page founder: architect tmux '[$arch_tmux]' GONE (dry-run)"
  fi
fi
