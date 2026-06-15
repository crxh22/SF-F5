#!/usr/bin/env bash
# ============================================================================
# claude_redesign.sh — launch a SEPARATE, PARALLEL Main-Architect-class session
# for DISCUSSION + the factory-REDESIGN task, in tmux with Remote-Control.
#
# A deliberately scoped-DOWN copy of claude_canon.sh. This session does NOT
# manage the live factory. It loads the SAME canon (doctrine + conventions +
# founder protocol + architect operations) so it reasons AS a main architect,
# but a default scoping PROMPT forbids it from touching the live factory runtime
# (the session-marker, the monitor, sf-factory mutations, the orchestrator,
# factory.db, the `factory` tmux). The founder coordinates any redesign ↔
# live-factory integration manually, through the session that DOES manage the
# factory (ETAPA-5g or its successor).
#
# Differences from claude_canon.sh (the live Main-Architect launcher):
#   - tmux session + RC name default to `redesign` / `Redesign` (distinct from
#     the live architect's, so the two never collide on the founder's phone).
#   - its own assembled-canon file (_assembled_canon_redesign.md) — zero shared
#     state with the live launcher.
#   - a default scoping PROMPT (the no-factory-management boundary) when none is
#     passed; pass your own prompt as $1 to override.
#   - it never writes ~/.claude/sf-architect-session and never starts the
#     monitor (neither does claude_canon.sh — those came from the architect's
#     prompt; here the scoping prompt ALSO explicitly forbids them).
#
# Usage:  ./claude_redesign.sh ["initial prompt"]    (no arg → scoping prompt)
#   Env overrides: SFF5_REDESIGN_TMUX (default redesign) + SFF5_REDESIGN_RC
#   (default Redesign) — DEDICATED names, deliberately NOT the live architect's
#   SFF5_TMUX_SESSION/SFF5_RC_NAME, so an inherited environment can't collide.
#   Also SFF5_MODEL (default opus), SFF5_EFFORT (default max), SFF5_NO_RC
#   (offline), SFF5_NO_TMUX (direct, non-persistent), CLAUDE_BIN.
# Fail-fast: any canon file missing/empty -> abort. tmux missing -> abort
# (SFF5_NO_TMUX=1 to bypass).
# ============================================================================
set -euo pipefail
LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
# Names come from DEDICATED redesign vars (NOT SFF5_TMUX_SESSION/SFF5_RC_NAME) so
# the live architect's exported names in the environment can NEVER be inherited and
# collide — the whole point of this parallel session is a DISTINCT identity.
TMUX_SESSION="${SFF5_REDESIGN_TMUX:-redesign}"
SFF5_MODEL="${SFF5_MODEL:-opus}"
SFF5_RC_NAME="${SFF5_REDESIGN_RC:-Redesign}"
RC_ARGS=()
if [ -z "${SFF5_NO_RC:-}" ]; then RC_ARGS=(--remote-control "$SFF5_RC_NAME"); fi

# Same canon as the live architect, so this session reasons AS a main architect.
CANON_FILES=(
  "00 - DOCTRINA.md"
  "work-protocols/conventions.md"
  "work-protocols/protocol_interactiune_founder.md"
  "work-protocols/architect-operations.md"
)

# Default scoping prompt — the no-factory-management boundary. Used only when no
# prompt arg is passed. THIS is what keeps the parallel session from interfering
# with the live factory; it overrides the architect-operations canon's
# factory-management framing for THIS session. (read -d '' returns non-zero at
# EOF, so `|| true` guards `set -e`.)
read -r -d '' DEFAULT_PROMPT <<'PROMPT' || true
Ești o sesiune Main-Architect (ai citit doctrina + regulile de operare arhitect din canon), PARALELĂ cu sesiunea care gestionează fabrica LIVE. SCOPUL TĂU: discuție cu fondatorul + o sarcină separată de REDESIGN al fabricii, coordonată manual de fondator.

NU GESTIONEZI FABRICA LIVE — o altă sesiune (ETAPA-5g sau succesoarea ei) o gestionează. Canonul de operare-arhitect descrie gestionarea fabricii (escaladări, detector stuck, redeploy), DAR acelea NU sunt sarcina ta — sunt doar context pentru redesign.

INTERDICȚII STRICTE (ca să NU interferezi cu sesiunea live):
- NU atinge ~/.claude/sf-architect-session (marker-ul de context al arhitectului live) și NU porni ~/.claude/sf-architect-monitor.sh.
- NU rula comenzi care MODIFICĂ fabrica live: `sf-factory run|resume|resolve-escalation|decide|seed-phases`, repornirea/oprirea orchestratorului, scrieri în .factory/factory.db, watchdog-ul, sau tmux-ul `factory`.
- NU porni sesiuni succesoare și NU scrie handoff de arhitect — ciclul tău de viață îl gestionează fondatorul.
- Inspecție READ-ONLY e OK la cererea fondatorului (`sf-factory status`, loguri, cod, jurnal de decizii).
- Pentru redesign poți scrie LIBER design/prototip pe o RAMURĂ GIT SEPARATĂ; integrarea cu fabrica live o coordonează fondatorul prin sesiunea care o gestionează.

Comunici în română (protocol_interactiune_founder). Fondatorul te dirijează — așteaptă-i instrucțiunile.
PROMPT

# This launcher's OWN assembled canon (separate from the live _assembled_canon.md).
CANONFILE="$LAUNCHER_DIR/_assembled_canon_redesign.md"
{
  printf '=== SF-F5 CANON === This block is the SF-F5 canon, assembled at launch from the source files below.\n'
  for rel in "${CANON_FILES[@]}"; do
    f="$LAUNCHER_DIR/$rel"
    if [ ! -s "$f" ]; then
      echo "claude_redesign: canon file missing or empty: $rel — aborting launch." >&2
      exit 1
    fi
    printf '\n--- %s ---\n' "$rel"
    cat "$f"
  done
  printf '\n=== END SF-F5 CANON ===\n'
} > "$CANONFILE"

# No prompt passed -> use the scoping prompt; else passthrough the caller's args.
if [ "$#" -eq 0 ]; then
  set -- "$DEFAULT_PROMPT"
fi

# Direct exec when explicitly requested or when already inside tmux (no nesting).
if [ -n "${SFF5_NO_TMUX:-}" ] || [ -n "${TMUX:-}" ]; then
  exec "$CLAUDE_BIN" --append-system-prompt-file "$CANONFILE" --model "$SFF5_MODEL" --effort "${SFF5_EFFORT:-max}" "${RC_ARGS[@]}" "$@"
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "claude_redesign: tmux not found — install tmux, or rerun with SFF5_NO_TMUX=1 for a direct (non-persistent) launch." >&2
  exit 1
fi

CMD="$(printf '%q ' "$CLAUDE_BIN" --append-system-prompt-file "$CANONFILE" --model "$SFF5_MODEL" --effort "${SFF5_EFFORT:-max}" "${RC_ARGS[@]}" "$@")"
echo "claude_redesign: tmux session '$TMUX_SESSION' (RC '$SFF5_RC_NAME') — detach: Ctrl-b d, re-attach: rerun this script" >&2
exec tmux new-session -A -s "$TMUX_SESSION" -c "$LAUNCHER_DIR" "$CMD"
