#!/usr/bin/env bash
# ============================================================================
# claude_canon.sh - launch `claude` with the SF-F5 canon in the system prompt,
# inside tmux so the session survives SSH disconnects.
# Same pattern as the ERP-start launcher: concatenates THIS repo's own canon
# files below into _assembled_canon.md at every launch (gitignored,
# regenerated), then runs claude with it appended to the system prompt.
# Block format: === SF-F5 CANON === / --- file ---.
#
# tmux behavior:
#   - Outside tmux -> create-or-attach session $SFF5_TMUX_SESSION (default:
#     sf-f5) running claude. If that tmux session already EXISTS, you are
#     just re-attached to it and any extra args are IGNORED (tmux -A semantics).
#   - Already inside tmux -> exec claude directly (no nesting).
#   - Detach (claude keeps running on the server): Ctrl-b d.
#     Re-attach later: run this script again.
#   - SFF5_NO_TMUX=1 -> skip tmux, direct launch (dies with the SSH hangup).
#
# Behavior: claude --append-system-prompt-file <CANONFILE> <all passthrough args>
#   CANONFILE default = _assembled_canon.md, assembled from CANON_FILES below.
#   Override: SFF5_CANON_FILE=<path>   inject a specific file instead.
#   Override binary: CLAUDE_BIN=<path>   (default: claude on PATH).
#   Override tmux session name: SFF5_TMUX_SESSION=<name>.
# Fail-fast: any canon file missing/empty -> abort launch (a partial canon in
# the system prompt is worse than a noisy stop). tmux missing -> abort with a
# hint (persistence is this script's contract; SFF5_NO_TMUX=1 to bypass).
#
# Usage:  ./claude_canon.sh [any claude args...]
# ============================================================================
set -euo pipefail
LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
TMUX_SESSION="${SFF5_TMUX_SESSION:-sf-f5}"

# Canon list (order = order in the injected block). Edit here to add/remove.
CANON_FILES=(
  "00 - DOCTRINA.md"
  "work-protocols/conventions.md"
  "work-protocols/protocol_interactiune_founder.md"
)

if [ -n "${SFF5_CANON_FILE:-}" ]; then
  CANONFILE="$SFF5_CANON_FILE"
else
  CANONFILE="$LAUNCHER_DIR/_assembled_canon.md"
  {
    printf '=== SF-F5 CANON === This block is the SF-F5 canon, assembled at launch from the source files below.\n'
    for rel in "${CANON_FILES[@]}"; do
      f="$LAUNCHER_DIR/$rel"
      if [ ! -s "$f" ]; then
        echo "claude_canon: canon file missing or empty: $rel — aborting launch." >&2
        exit 1
      fi
      printf '\n--- %s ---\n' "$rel"
      cat "$f"
    done
    printf '\n=== END SF-F5 CANON ===\n'
  } > "$CANONFILE"
fi

# Direct exec when explicitly requested or when already inside tmux (no nesting).
if [ -n "${SFF5_NO_TMUX:-}" ] || [ -n "${TMUX:-}" ]; then
  exec "$CLAUDE_BIN" --append-system-prompt-file "$CANONFILE" "$@"
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "claude_canon: tmux not found — install tmux, or rerun with SFF5_NO_TMUX=1 for a direct (non-persistent) launch." >&2
  exit 1
fi

CMD="$(printf '%q ' "$CLAUDE_BIN" --append-system-prompt-file "$CANONFILE" "$@")"
echo "claude_canon: tmux session '$TMUX_SESSION' — detach: Ctrl-b d, re-attach: rerun this script" >&2
exec tmux new-session -A -s "$TMUX_SESSION" -c "$LAUNCHER_DIR" "$CMD"
