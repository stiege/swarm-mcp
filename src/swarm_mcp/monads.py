"""Monadic wrappers for the swarm ref system.

Each monad is a field (or set of fields) on the ref dict. No nested wrappers —
just a flat dict with optional fields that combinators and the pipeline
interpreter can check.

The monad stack:
  Classified (Costed (Timed (Provenanced (Validated (Ref a)))))

In practice: a ref dict with optional fields for each layer.
"""

import hashlib
import json
import logging
import time

logger = logging.getLogger(__name__)


# ── Provenance ────────────────────────────────────────────────────

def stamp_provenance(ref: dict, parent_refs: list[str] | None = None, text: str = "") -> dict:
    """Add provenance fields to a ref dict."""
    content_hash = hashlib.sha256(text.encode()).hexdigest()[:16] if text else None
    ref["provenance"] = {
        "parent_refs": parent_refs or [],
        "content_hash": content_hash,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return ref


# ── Costed ────────────────────────────────────────────────────────

def stamp_cost(ref: dict, budget_limit: float | None = None, spent_so_far: float = 0) -> dict:
    """Add budget tracking fields to a ref."""
    step_cost = ref.get("cost_usd") or 0
    total_spent = spent_so_far + step_cost
    ref["budget"] = {
        "step_cost": step_cost,
        "spent_so_far": total_spent,
        "remaining": (budget_limit - total_spent) if budget_limit else None,
        "limit": budget_limit,
    }
    return ref


def check_budget(ref: dict) -> bool:
    """Returns True if budget is OK, False if exceeded."""
    budget = ref.get("budget")
    if not budget or budget.get("limit") is None:
        return True
    return (budget.get("remaining") or 0) >= 0


# ── Timed ─────────────────────────────────────────────────────────

def stamp_deadline(ref: dict, deadline: float | None = None) -> dict:
    """Add deadline tracking. deadline is a Unix timestamp."""
    if deadline:
        ref["deadline"] = deadline
        ref["time_remaining"] = max(0, deadline - time.time())
    return ref


def remaining_time(deadline: float | None) -> float | None:
    """Compute seconds remaining until deadline."""
    if deadline is None:
        return None
    return max(0, deadline - time.time())


# ── Validated ─────────────────────────────────────────────────────

def stamp_validated(ref: dict, declared_type: str, verdict: str, validation_ref: str | None = None) -> dict:
    """Mark a ref as validated against a type."""
    ref["validated_as"] = declared_type
    ref["validation_verdict"] = verdict  # VALID, PARTIAL, INVALID
    if validation_ref:
        ref["validation_ref"] = validation_ref
    return ref


def is_validated(ref: dict, required_type: str | None = None) -> bool:
    """Check if a ref has been validated, optionally against a specific type."""
    verdict = ref.get("validation_verdict")
    if verdict != "VALID":
        return False
    if required_type and ref.get("validated_as") != required_type:
        return False
    return True


# ── Retried ───────────────────────────────────────────────────────

def stamp_retry(ref: dict, attempt: int, max_retries: int, prior_errors: list[str] | None = None) -> dict:
    """Track retry attempts on a ref."""
    ref["retry"] = {
        "attempt": attempt,
        "max_retries": max_retries,
        "prior_errors": prior_errors or [],
    }
    return ref


# ── Classified ────────────────────────────────────────────────────

CLASSIFICATION_LEVELS = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


def stamp_classification(ref: dict, level: str, allowed_mcps: list[str] | None = None, denied_mcps: list[str] | None = None) -> dict:
    """Classify a ref's sensitivity level."""
    ref["classification"] = {
        "level": level,
        "level_numeric": CLASSIFICATION_LEVELS.get(level, 0),
        "allowed_mcps": allowed_mcps,
        "denied_mcps": denied_mcps,
    }
    return ref


def check_classification(ref: dict, mcps_requested: list[str]) -> tuple[bool, str]:
    """Check if requested MCPs are allowed given the ref's classification.

    Returns (allowed, reason).
    """
    classification = ref.get("classification")
    if not classification:
        return True, "no classification set"

    denied = classification.get("denied_mcps") or []
    allowed = classification.get("allowed_mcps")

    for mcp in mcps_requested:
        if mcp in denied:
            return False, f"MCP '{mcp}' denied for classification '{classification['level']}'"
        if allowed is not None and mcp not in allowed:
            return False, f"MCP '{mcp}' not in allowed list for classification '{classification['level']}'"

    return True, "ok"


# ── Ref Enrichment ────────────────────────────────────────────────

def enrich_ref(
    ref: dict,
    run_id: str,
    text: str = "",
    parent_refs: list[str] | None = None,
    budget_limit: float | None = None,
    spent_so_far: float = 0,
    deadline: float | None = None,
    classification: str | None = None,
    attempt: int | None = None,
    max_retries: int | None = None,
) -> dict:
    """Apply all relevant monadic stamps to a ref in one call."""
    stamp_provenance(ref, parent_refs, text)
    stamp_cost(ref, budget_limit, spent_so_far)

    if deadline:
        stamp_deadline(ref, deadline)
    if classification:
        stamp_classification(ref, classification)
    if attempt is not None:
        stamp_retry(ref, attempt, max_retries or 3)

    return ref
