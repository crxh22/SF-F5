"""Consultation-point framework (design §1/§4): registry from config,
schema-validated closed verdict sets, deterministic fallback, full call
logging, breach detection. CP-1 is the only registered point in MVP (DoD §4).

A consultation is a pure-function LLM call (DoD §2.1): bounded input assembled
strictly from the registry's declared input keys, one spawn through the runner
(tagged ``kind='consultation'`` + ``cp_id`` — the §2 creep scan's
precondition), strict parse of EXACTLY ONE JSON object against a pydantic
model whose ``verdict`` is ``Literal[verdicts]``. Invalid or ambiguous output
(≠1 object, unknown verdict, empty rationale) executes the registry's
deterministic fallback with ``fallback_used=True`` — a garbage reply can only
ever produce a *registered* verdict, recorded, never guessed (Doctrine §7).
Any call shaped outside the registry (unknown cp_id, input keys diverging from
the declared set, input beyond ``max_input_bytes``) is a governance breach:
``cp_breach_attempt`` event + ``ConsultationBreachError`` (§6), never executed.
Every executed call lands in the ``consultations`` table with input digest,
verdict, model, latency, cost and the raw stream path (DoD §3.4).

May import: models, config, db, runner (design §1).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from sf_factory import db
from sf_factory.config import ConsultationPointCfg, FactoryConfig
from sf_factory.db import Database
from sf_factory.models import ConsultationBreachError, ProcessError, new_id
from sf_factory.runner import AgentRunner

# ----------------------------------------------------------------------- verdict


@dataclass(frozen=True)
class Verdict:
    """cp_id, value: str (∈ closed set), rationale: str, fallback_used: bool,
    consultation_id: int."""

    cp_id: str
    value: str
    rationale: str
    fallback_used: bool
    consultation_id: int


# ----------------------------------------------------------------- parse helpers

_JSON_DECODER = json.JSONDecoder()


def _extract_json_objects(text: str) -> list[dict]:
    """Every top-level JSON OBJECT in ``text``, left to right. A decoded
    object's whole span is skipped, so objects nested inside it (or inside its
    string values) never count separately. Surrounding prose or a markdown
    fence is tolerated for LOCATING the object — what §4 makes binding is the
    exactly-one count, which the caller enforces. Non-object JSON values are
    never counted: the verdict contract is an object."""
    objects: list[dict] = []
    pos = 0
    while (start := text.find("{", pos)) != -1:
        try:
            value, end = _JSON_DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        objects.append(value)  # raw_decode starting at '{' yields a dict
        pos = end
    return objects


def _canonical_payload(inputs: Mapping[str, str]) -> bytes:
    """Canonical input payload — the §2 ``input_digest`` preimage and the
    ``max_input_bytes`` measure: key-sorted compact JSON, UTF-8. Key order of
    the caller's mapping never changes the digest.

    Also imported by scheduler's ``_fit_consultation_inputs`` so CP callers
    bound their assembled inputs with EXACTLY this measure (a drifting copy
    would re-open the false-breach path the §6 backstop is meant to catch)."""
    return json.dumps(
        dict(inputs), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _verdict_schema(cp: ConsultationPointCfg) -> type[BaseModel]:
    """Pydantic model for one registry point: ``verdict`` is
    ``Literal[verdicts]`` (the closed set, §4), ``rationale`` a string;
    ``strict`` + ``extra='forbid'`` — any unregistered shape is invalid input
    for the fallback path, never coerced into a verdict."""
    name = re.sub(r"\W", "_", f"VerdictSchema_{cp.id}")
    return create_model(
        name,
        __config__=ConfigDict(extra="forbid", strict=True),
        verdict=(Literal[tuple(cp.verdicts)], ...),  # type: ignore[valid-type]
        rationale=(str, ...),
    )


# --------------------------------------------------------------------- consultor


class Consultor:
    """CP framework: registry from config, schema-validated closed verdict
    sets, deterministic fallback, full call logging, breach detection."""

    def __init__(self, cfg: FactoryConfig, db: Database, runner: AgentRunner) -> None:
        """Registry = cfg.consultation_points; CP-1 is the only registered point in MVP.

        The ``db`` parameter (frozen §4 name) shadows the module import only
        within this scope — same convention as AgentRunner. Verdict schemas are
        built once here: the registry is fixed for the process lifetime."""
        self._cfg = cfg
        self._db = db
        self._runner = runner
        self._registry: dict[str, ConsultationPointCfg] = {
            cp.id: cp for cp in cfg.consultation_points
        }
        self._schemas: dict[str, type[BaseModel]] = {
            cp.id: _verdict_schema(cp) for cp in cfg.consultation_points
        }

    # ------------------------------------------------------------------ public

    async def consult(
        self, cp_id: str, *, unit_level: str, unit_id: str, inputs: Mapping[str, str]
    ) -> Verdict:
        """Pure-function consultation (DoD §2.1): unknown cp_id -> log
        'cp_breach_attempt' event then raise ConsultationBreachError; assemble
        bounded input (≤ max_input_bytes, input keys must equal the registry's
        declared inputs); call runner with the registry role; strict-parse
        exactly one JSON object against a pydantic model with Literal[verdicts];
        invalid/ambiguous (≠1 object, unknown verdict, empty rationale) ->
        deterministic fallback verdict with fallback_used=True; always log
        consultations row + raw stream."""
        cp = self._registry.get(cp_id)
        if cp is None:
            self._breach(
                unit_level=unit_level,
                unit_id=unit_id,
                cp_id=cp_id,
                reason="unknown cp_id: not in the config consultation registry",
                detail={"registered": sorted(self._registry)},
            )

        declared, provided = set(cp.inputs), set(inputs)
        if provided != declared:
            self._breach(
                unit_level=unit_level,
                unit_id=unit_id,
                cp_id=cp_id,
                reason="input keys must equal the registry's declared inputs",
                detail={
                    "declared": sorted(declared),
                    "missing": sorted(declared - provided),
                    "unexpected": sorted(provided - declared),
                },
            )
        non_str = sorted(str(k) for k, v in inputs.items() if not isinstance(v, str))
        if non_str:
            self._breach(
                unit_level=unit_level,
                unit_id=unit_id,
                cp_id=cp_id,
                reason="consultation input values must be strings",
                detail={"non_string_keys": non_str},
            )
        payload = _canonical_payload(inputs)
        if len(payload) > cp.max_input_bytes:
            # The CALLER must bound its inputs (e.g. diff_digest max_bytes);
            # truncating here would guess at salience (Doctrine §7).
            self._breach(
                unit_level=unit_level,
                unit_id=unit_id,
                cp_id=cp_id,
                reason="assembled input exceeds the registry's max_input_bytes bound",
                detail={
                    "payload_bytes": len(payload),
                    "max_input_bytes": cp.max_input_bytes,
                },
            )
        input_digest = hashlib.sha256(payload).hexdigest()

        result = await self._runner.run_agent(
            cp.role,
            self._build_prompt(cp, inputs),
            unit_level=unit_level,
            unit_id=unit_id,
            cwd=self._scratch_cwd(),
            kind="consultation",
            cp_id=cp.id,
        )

        value, rationale, schema_valid = self._parse_verdict(cp, result.result_text)
        with self._db.transaction() as conn:
            consultation_id = db.insert_consultation(
                conn,
                {
                    "cp_id": cp.id,
                    "unit_level": unit_level,
                    "unit_id": unit_id,
                    "input_digest": input_digest,
                    "schema_valid": int(schema_valid),
                    "fallback_used": int(not schema_valid),
                    "verdict": value,
                    "rationale": rationale,
                    "model": self._cfg.models[cp.role].model,
                    "latency_ms": result.duration_ms,
                    "cost_usd": result.cost_usd,
                    "tokens_in": result.tokens_in,
                    "tokens_out": result.tokens_out,
                    "raw_log_path": result.ndjson_log_path,
                },
            )
        return Verdict(
            cp_id=cp.id,
            value=value,
            rationale=rationale,
            fallback_used=not schema_valid,
            consultation_id=consultation_id,
        )

    # ----------------------------------------------------------------- internals

    def _breach(
        self, *, unit_level: str, unit_id: str, cp_id: str, reason: str, detail: dict
    ) -> NoReturn:
        """§6 ConsultationBreachError handling: persist the 'cp_breach_attempt'
        event — the DoD §13 governance scan reads breaches mechanically from
        logs, never from anyone's attention (Doctrine §20) — then raise. A
        breach is a caller bug: it never spawns, never logs a consultations
        row, never produces a verdict."""
        with self._db.transaction() as conn:
            db.insert_event(
                conn,
                unit_level=unit_level,
                unit_id=unit_id,
                event_type="cp_breach_attempt",
                actor="control_plane",
                payload={"cp_id": cp_id, "reason": reason, **detail},
            )
        raise ConsultationBreachError(f"consultation breach for cp_id {cp_id!r}: {reason}")

    def _build_prompt(self, cp: ConsultationPointCfg, inputs: Mapping[str, str]) -> str:
        """Deterministic prompt assembled ONLY from registry fields plus the
        bounded inputs, in registry-declared input order: purpose, the closed
        verdict set, the one-JSON-object output contract, the fallback
        consequence, then one labeled block per input."""
        closed_set = " | ".join(cp.verdicts)
        parts = [
            f"You are consultation point {cp.id} of the SF-F5 factory control plane.",
            f"Purpose: {cp.purpose}",
            "",
            f"Decide exactly ONE verdict from this closed set: {closed_set}.",
            "Respond with EXACTLY ONE JSON object — no other JSON object anywhere in"
            " your reply — of the shape:",
            f'{{"verdict": "<{closed_set}>",'
            ' "rationale": "<non-empty; cite the decisive evidence from the inputs>"}',
            "Any other output is discarded and the deterministic fallback"
            f" {cp.fallback!r} is executed.",
        ]
        for key in cp.inputs:
            parts += ["", f"--- input: {key} ---", inputs[key]]
        return "\n".join(parts)

    def _scratch_cwd(self) -> Path:
        """Fresh per-call scratch cwd under ``process.ndjson_log_dir``
        (operational space, gitignored): consultations are pure functions and
        own no workspace, but the runner requires an existing cwd — and a codex
        route with a flipped-on consultation canon would materialize AGENTS.md
        there (D-0009), which must never land in a real worktree (it would
        pollute the stage diff). Per-call freshness also makes the adapter's
        divergent-AGENTS.md refusal unreachable after a canon change."""
        log_dir = self._cfg.process.ndjson_log_dir
        if not log_dir.is_absolute():
            log_dir = self._cfg.factory.home / log_dir
        scratch = log_dir / f"{new_id('cp')}-cwd"
        try:
            scratch.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise ProcessError(
                f"cannot create consultation scratch cwd {scratch}: {exc}"
            ) from exc
        return scratch

    def _parse_verdict(
        self, cp: ConsultationPointCfg, text: str
    ) -> tuple[str, str, bool]:
        """(executed verdict, rationale, schema_valid). Strict parse per §4:
        the output must contain EXACTLY ONE JSON object, schema-valid with a
        closed-set verdict and a non-empty rationale; anything else maps to the
        registry fallback with the reason recorded — never guessed."""
        objects = _extract_json_objects(text)
        if len(objects) != 1:
            return self._fallback(
                cp, f"expected exactly 1 JSON object in the output, found {len(objects)}"
            )
        try:
            verdict_obj = self._schemas[cp.id].model_validate(objects[0])
        except ValidationError as exc:
            issues = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors()
            )
            return self._fallback(cp, f"schema-invalid verdict object: {issues}")
        rationale: str = verdict_obj.rationale  # type: ignore[attr-defined]
        if not rationale.strip():
            return self._fallback(cp, "empty rationale")
        return verdict_obj.verdict, rationale, True  # type: ignore[attr-defined]

    @staticmethod
    def _fallback(cp: ConsultationPointCfg, reason: str) -> tuple[str, str, bool]:
        """Deterministic fallback tuple; the rationale records WHY (§6: every
        failure path persists its facts)."""
        return cp.fallback, f"deterministic fallback {cp.fallback!r}: {reason}", False
