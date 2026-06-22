#!/usr/bin/env bash
# sf-dash-limits-poller.sh — refreshes the dashboard's "Limite Claude" cache
# (founder 22-06). A SEPARATE poller (the dashboard never fetches live — it only
# READS this file, read-only + fast). Runs ~every 5 min via the systemd USER
# timer sf-dash-limits-poller.timer.
#
# It calls ~/.claude/sf-limit.sh (the live OAuth query of the 5h + weekly Claude
# usage), transforms its text output into EXACTLY the JSON shape the dashboard
# reads, and writes it ATOMICALLY to /tmp/sf-dash-limits.json (temp file + mv,
# so the dashboard never reads a half-written file).
#
# Dashboard-read shape (src/sf_factory/dashboard.py:1567, _read_limits):
#   {"checked_at": iso8601Z, "five_h_pct": int, "weekly_pct": int,
#    "five_h_reset": iso8601Z, "weekly_reset": iso8601Z}
# CRITICAL: the dashboard parses every timestamp with the STRICT format
# "%Y-%m-%dT%H:%M:%SZ" (fmt_founder_ts / _age_seconds) — a literal 'Z', NO
# microseconds, NO numeric offset. sf-limit.sh emits microseconds + "+00:00",
# so we MUST normalize to that strict form or the resets blank out and the
# „verificat acum N min” age goes missing. Percentages are cast to int.
#
# Failure policy (the dashboard has NO error-state field — a missing/unparseable
# cache simply renders the graceful „indisponibil” state): on ANY sf-limit.sh
# failure or a parse miss we do NOT clobber a good file with garbage — we leave
# the existing /tmp/sf-dash-limits.json untouched and exit non-zero. A stale (but
# valid) snapshot keeps showing with an honest „verificat acum N min” age; that
# is strictly better than blanking it.
#
# Usage:
#   sf-dash-limits-poller.sh           # one-shot: query + write the cache (timer entrypoint)
#   SF_LIMIT_OUTPUT="..."  sf-dash-limits-poller.sh   # test hook: transform the given
#                                                       sf-limit.sh text instead of querying
#                                                       (skips the live OAuth call)
# Exit: 0 = cache written; 1 = transform/parse failure (cache left intact);
#       3 = sf-limit.sh could not query (no token / network — cache left intact).
set -uo pipefail

CACHE_PATH="${SF_DASH_LIMITS_PATH:-/tmp/sf-dash-limits.json}"
LIMIT_SH="${SF_LIMIT_SH:-${HOME}/.claude/sf-limit.sh}"

# 1. Obtain the raw sf-limit.sh text. SF_LIMIT_OUTPUT lets a test inject a captured
#    sample and exercise the transform without the live OAuth query.
if [[ -n "${SF_LIMIT_OUTPUT:-}" ]]; then
  raw="${SF_LIMIT_OUTPUT}"
else
  if [[ ! -x "${LIMIT_SH}" ]]; then
    echo "sf-dash-limits-poller: ${LIMIT_SH} not found/executable" >&2
    exit 3
  fi
  # sf-limit.sh exit: 0 below threshold, 2 at/above threshold (still a GOOD read),
  # 3 = could not query. Capture stdout regardless; only a 3 (or no output) is a
  # hard failure that must not clobber the cache.
  raw="$(bash "${LIMIT_SH}" 2>/dev/null)"
  rc=$?
  if [[ ${rc} -eq 3 || -z "${raw}" ]]; then
    echo "sf-dash-limits-poller: sf-limit.sh could not query (rc=${rc}); leaving ${CACHE_PATH} intact" >&2
    exit 3
  fi
fi

# 2. Transform -> the EXACT dashboard JSON shape, written atomically. python3 is
#    already an sf-limit.sh dependency (it's the engine of sf-limit.sh), so no new
#    dependency is introduced. jq is avoided here because we also need strict
#    timestamp normalization, which python's datetime does cleanly.
tmp_path="$(mktemp "${CACHE_PATH}.XXXXXX")" || {
  echo "sf-dash-limits-poller: mktemp near ${CACHE_PATH} failed" >&2
  exit 1
}
trap 'rm -f "${tmp_path}"' EXIT

if SF_LIMIT_RAW="${raw}" python3 - "${tmp_path}" <<'PY'
import json, os, re, sys
from datetime import datetime, timezone

tmp_path = sys.argv[1]
# raw sf-limit.sh text via env (stdin is taken by this heredoc).
text = os.environ.get("SF_LIMIT_RAW", "")


def norm_ts(value):
    """Any ISO-8601 instant -> the STRICT '%Y-%m-%dT%H:%M:%SZ' the dashboard
    parses (UTC, literal 'Z', no microseconds, no offset). Returns None on a
    miss so a bad reset blanks gracefully instead of poisoning the file."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_pct(label):
    m = re.search(rf"^{label}:\s*([0-9]+(?:\.[0-9]+)?)%", text, re.MULTILINE)
    return int(round(float(m.group(1)))) if m else None


def find_reset(label):
    m = re.search(rf"^{label}:.*?resets\s+(\S+)", text, re.MULTILINE)
    return norm_ts(m.group(1)) if m else None


five_h_pct = find_pct("5h")
weekly_pct = find_pct("weekly")

# Both percentages missing => sf-limit.sh output was not what we expect; refuse to
# write so we never clobber a good cache with an empty snapshot.
if five_h_pct is None and weekly_pct is None:
    print("sf-dash-limits-poller: could not parse any utilization from sf-limit.sh output", file=sys.stderr)
    sys.exit(1)

payload = {
    "checked_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "five_h_pct": five_h_pct,
    "weekly_pct": weekly_pct,
    "five_h_reset": find_reset("5h"),
    "weekly_reset": find_reset("weekly"),
}
with open(tmp_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
    fh.write("\n")
PY
then
  # Atomic publish: rename over the live path (same filesystem -> atomic).
  mv -f "${tmp_path}" "${CACHE_PATH}"
  trap - EXIT
  echo "sf-dash-limits-poller: wrote ${CACHE_PATH}"
  exit 0
else
  echo "sf-dash-limits-poller: transform failed; leaving ${CACHE_PATH} intact" >&2
  exit 1
fi
