#!/usr/bin/env python
"""Dev helper (ARH-04 structure-authoring mandate): validate a hand-authored
phase-plan.json against the SAME factory code the orchestrator runs at ingest —
artifacts.read_phase_plan (schema + id grammar + acyclic + contract-first
reachability) and artifacts.evaluate_stage_sizes (the small-stage gate).

Usage:  python scripts/validate_phase_plan.py <phase-plan.json> [...]

Exit 0 = read_phase_plan accepts it (the factory would ingest it). Size-gate
findings are REPORTED (the live gate is warn-mode) — 'over'/'under' are real
sizing issues to fix; 'skipped' means a nullable axis was omitted.

Limits + risk_classes mirror factory.config.yaml (planning.stage_size_limits =
7/6/6/1/1; risk_classes = routine/structural/critical). Kept in sync by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sf_factory.artifacts import (  # noqa: E402
    ArtifactContractError,
    StageSizeLimits,
    evaluate_stage_sizes,
    read_phase_plan,
)

RISK_CLASSES = {"routine", "structural", "critical"}
LIMITS = StageSizeLimits(
    max_acceptance_criteria=7,
    max_touched=6,
    max_dependency_degree=6,
    min_acceptance_criteria=1,
    min_touched=1,
)


def validate(path: Path) -> bool:
    print(f"\n=== {path} ===")
    try:
        plan = read_phase_plan(path, RISK_CLASSES)
    except ArtifactContractError as exc:
        print(f"  REJECTED by read_phase_plan:\n    {exc}")
        return False
    print(f"  read_phase_plan: OK — {len(plan.stages)} stages, {len(plan.dag_edges)} edges")

    # Per-stage degree + kind/role/risk summary
    deg_in: dict[str, int] = {s.id: 0 for s in plan.stages}
    deg_out: dict[str, int] = {s.id: 0 for s in plan.stages}
    for a, b in plan.dag_edges:
        deg_out[a] += 1
        deg_in[b] += 1
    contracts = [s.id for s in plan.stages if s.role == "contract"]
    print(f"  contract stages: {contracts or 'NONE (reachability check skipped)'}")
    for s in plan.stages:
        ac = len(s.acceptance_criteria) if s.acceptance_criteria is not None else None
        to = len(s.touched) if s.touched is not None else None
        deg = deg_in[s.id] + deg_out[s.id]
        flags = []
        if ac is not None and ac > LIMITS.max_acceptance_criteria:
            flags.append(f"AC={ac}>7")
        if to is not None and to > LIMITS.max_touched:
            flags.append(f"touched={to}>6")
        if deg > LIMITS.max_dependency_degree:
            flags.append(f"degree={deg}>6")
        if ac is None:
            flags.append("AC=None")
        if to is None:
            flags.append("touched=None")
        tag = "  ".join(flags)
        print(
            f"    {s.id:<28} {str(s.kind):<8} {str(s.role):<8} {s.risk_class:<10}"
            f" AC={ac} touched={to} degree={deg}   {tag}"
        )

    violations = evaluate_stage_sizes(plan, LIMITS)
    real = [v for v in violations if v.kind != "skipped"]
    if real:
        print(f"  SIZE-GATE findings ({len(real)} non-skipped):")
        for v in real:
            print(f"    {v.stage_id}: {v.axis} {v.kind} value={v.value} limit={v.limit}")
    else:
        print("  SIZE-GATE: clean (no over/under findings)")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ok = True
    for arg in sys.argv[1:]:
        ok = validate(Path(arg)) and ok
    print("\nALL VALID" if ok else "\nSOME REJECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
