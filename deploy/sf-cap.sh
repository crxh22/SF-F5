#!/usr/bin/env bash
# ============================================================================
# sf-cap.sh — run a command inside a MEMORY-CAPPED transient cgroup scope.
#
# Why: 19-06-2026 OOM incident. A full ERP test run (frontend vitest, ~29 GB)
# launched in a long-lived SSH session exhausted all 31 GB RAM; the kernel global
# OOM killer then killed the user-session manager (systemd --user), and systemd
# tore down the whole user@1000.service cgroup — every architect tmux session +
# the orchestrator died at once. This wrapper bounds any heavy run (tests, builds)
# to a fixed RAM quota. On breach ONLY this scope's processes are OOM-killed — the
# host, the architect sessions and the factory keep running.
#
# Pairs with the "Scut": the user manager + architect + tmux carry
# OOMScoreAdjust=-1000 so they are NEVER the kernel's victim.
#
# Two non-obvious correctness points (both learned by VERIFYING, 19-06-2026):
#  1) Cap swap too (MemorySwapMax). MemoryMax alone limits RAM only; with swap
#     free the process dodges the cap by swapping (slow, not killed). Default 0.
#  2) Reset the payload's oom_score_adj to 0. A run launched from the (protected,
#     -1000) architect session inherits -1000 and would be UNKILLABLE — the cap
#     could not enforce (process stalls instead of dying). We force it killable.
#     (Raising oom_score_adj toward 0 is always allowed unprivileged.)
#  3) Allow a SMALL swap quota, NOT zero. With swap=0 a breach FREEZES the whole
#     cgroup in endless reclaim (verified 19-06: a swap=0 scope hung 2 min, no
#     kill). A small swap quota gives the kernel reclaim headroom so the memcg OOM
#     killer fires CLEANLY (exit 137) the moment RAM+swap is exhausted. The HARD
#     ceiling is therefore MemoryMax + MemorySwapMax.
#
# Usage:   deploy/sf-cap.sh <command> [args...]
#          SF_CAP_MAX=22G deploy/sf-cap.sh bash scripts/test.sh
# Defaults: MemoryMax=22G + MemorySwapMax=2G  => ~24 GB hard ceiling (host has
#           31 GB; ~7 GB stays reserved for the architect session + OS). Founder-set
#           24 GB factory budget (19-06). Override: SF_CAP_MAX, SF_CAP_SWAP.
#
# VERIFY the cap actually enforces (mechanical guarantee, not trust):
#   SF_CAP_MAX=200M SF_CAP_SWAP=64M deploy/sf-cap.sh \
#     python3 -c 'c=[];
# while 1: import os; b=bytearray(10*1024*1024); b[:]=b"x"*len(b); c.append(b)'
#   -> expected: exit 137 (OOM-killed in scope) at ~RAM+swap, host unaffected.
# ============================================================================
set -euo pipefail
MEM_MAX="${SF_CAP_MAX:-22G}"
MEM_SWAP="${SF_CAP_SWAP:-2G}"
if [ "$#" -eq 0 ]; then
  echo "sf-cap: no command given. Usage: sf-cap.sh <command> [args...]" >&2
  exit 2
fi
# --collect: auto-GC the transient scope on exit. The inner bash makes the payload
# killable (point 2) then exec-replaces itself with the real command.
exec systemd-run --user --scope --quiet --collect \
  -p MemoryMax="$MEM_MAX" -p MemorySwapMax="$MEM_SWAP" \
  -- bash -c 'echo 0 > /proc/self/oom_score_adj 2>/dev/null || true; exec "$@"' sf-cap "$@"
