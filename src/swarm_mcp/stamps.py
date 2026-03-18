"""Monadic wrappers for the swarm ref system.

Each monad layer adds optional fields to a ref dict rather than wrapping it in
a nested data structure.  This keeps refs flat and JSON-serialisable while
still composable — any combinator can inspect or add any layer.

Monad stack (outermost first)::

    Encrypted(Classified(Costed(Timed(Provenanced(Validated(Ref a))))))

In practice each layer is a ``stamp_*`` function that mutates the ref dict
in-place and returns it, and a corresponding ``check_*`` / ``is_*`` predicate
that reads the added fields.

Layers
------
- **Provenance** — content hash, parent refs, timestamp (``stamp_provenance``).
- **Costed** — step cost, running total, budget remaining (``stamp_cost``).
- **Timed** — deadline tracking, time-remaining computation (``stamp_deadline``).
- **Validated** — type verdict: VALID / PARTIAL / INVALID (``stamp_validated``).
- **Retried** — attempt counter and prior error list (``stamp_retry``).
- **Classified** — data-sensitivity level, MCP allowlist / denylist
  (``stamp_classification``).
- **Encrypted** — Fernet-encrypted payload with key reference
  (``stamp_encrypted``).
"""

import hashlib
import json
import logging
import os
import time

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


# ── Provenance ────────────────────────────────────────────────────

def stamp_provenance(ref: dict, parent_refs: list[str] | None = None, text: str = "") -> dict:
    """Add provenance fields to a ref dict.

    Computes a short SHA-256 hash of *text* (if provided) and records the
    current UTC timestamp.  Mutates *ref* in-place and returns it.

    Args:
        ref: The ref dict to annotate.
        parent_refs: List of upstream ref strings this result was derived from.
        text: The result text used to compute the content hash.  Empty string
            means no hash is stored.

    Returns:
        The same *ref* dict with a ``"provenance"`` key added.
    """
    content_hash = hashlib.sha256(text.encode()).hexdigest()[:16] if text else None
    ref["provenance"] = {
        "parent_refs": parent_refs or [],
        "content_hash": content_hash,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return ref


# ── Costed ────────────────────────────────────────────────────────

def stamp_cost(ref: dict, budget_limit: float | None = None, spent_so_far: float = 0) -> dict:
    """Add budget-tracking fields to a ref dict.

    Reads ``ref["cost_usd"]`` (set by the agent runner) and accumulates it
    against *spent_so_far*.  Mutates *ref* in-place and returns it.

    Args:
        ref: The ref dict to annotate.  Must contain ``"cost_usd"`` if the
            step cost is known (may be absent or ``None``).
        budget_limit: Total USD budget cap for the pipeline, or ``None`` for
            unlimited.
        spent_so_far: USD already spent by prior steps in the same pipeline.

    Returns:
        The same *ref* dict with a ``"budget"`` key added.
    """
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
    """Return ``True`` if the budget has not been exceeded, ``False`` otherwise.

    A ref without a ``"budget"`` key (or without a configured limit) is always
    considered within budget.

    Args:
        ref: A ref dict previously stamped by :func:`stamp_cost`.

    Returns:
        ``True`` if remaining budget is ≥ 0 or no limit is set;
        ``False`` if the limit has been exceeded.
    """
    budget = ref.get("budget")
    if not budget or budget.get("limit") is None:
        return True
    return (budget.get("remaining") or 0) >= 0


# ── Timed ─────────────────────────────────────────────────────────

def stamp_deadline(ref: dict, deadline: float | None = None) -> dict:
    """Add deadline-tracking fields to a ref dict.

    Args:
        ref: The ref dict to annotate.
        deadline: Absolute Unix timestamp after which the pipeline should stop,
            or ``None`` to skip deadline tracking.

    Returns:
        The same *ref* dict, with ``"deadline"`` and ``"time_remaining"``
        keys added when *deadline* is provided.
    """
    if deadline:
        ref["deadline"] = deadline
        ref["time_remaining"] = max(0, deadline - time.time())
    return ref


def remaining_time(deadline: float | None) -> float | None:
    """Compute the number of seconds remaining until a deadline.

    Args:
        deadline: Absolute Unix timestamp, or ``None`` for no deadline.

    Returns:
        Seconds remaining (clamped to 0 if already past), or ``None`` if
        *deadline* is ``None``.
    """
    if deadline is None:
        return None
    return max(0, deadline - time.time())


# ── Validated ─────────────────────────────────────────────────────

def stamp_validated(ref: dict, declared_type: str, verdict: str, validation_ref: str | None = None) -> dict:
    """Mark a ref as validated against a declared type.

    Args:
        ref: The ref dict to annotate.
        declared_type: The type name the artifact was checked against.
        verdict: Validation outcome — one of ``"VALID"``, ``"PARTIAL"``,
            or ``"INVALID"``.
        validation_ref: Optional ref string pointing to the validator agent's
            own result (useful for audit trails).

    Returns:
        The same *ref* dict with ``"validated_as"`` and
        ``"validation_verdict"`` keys added.
    """
    ref["validated_as"] = declared_type
    ref["validation_verdict"] = verdict  # VALID, PARTIAL, INVALID
    if validation_ref:
        ref["validation_ref"] = validation_ref
    return ref


def is_validated(ref: dict, required_type: str | None = None) -> bool:
    """Check whether a ref carries a ``VALID`` validation verdict.

    Args:
        ref: The ref dict to inspect.
        required_type: If provided, also checks that the ref was validated
            against this specific type name.

    Returns:
        ``True`` only when the verdict is ``"VALID"`` and, if *required_type*
        is given, the ``validated_as`` field matches it.
    """
    verdict = ref.get("validation_verdict")
    if verdict != "VALID":
        return False
    if required_type and ref.get("validated_as") != required_type:
        return False
    return True


# ── Retried ───────────────────────────────────────────────────────

def stamp_retry(ref: dict, attempt: int, max_retries: int, prior_errors: list[str] | None = None) -> dict:
    """Record retry-attempt metadata on a ref dict.

    Args:
        ref: The ref dict to annotate.
        attempt: 1-based index of the current attempt.
        max_retries: Maximum number of attempts allowed.
        prior_errors: Error messages from all previous failed attempts.

    Returns:
        The same *ref* dict with a ``"retry"`` key added.
    """
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
    """Set data-sensitivity classification on a ref dict.

    Args:
        ref: The ref dict to annotate.
        level: Sensitivity level string — one of ``"public"``, ``"internal"``,
            ``"confidential"``, or ``"restricted"``.
        allowed_mcps: Explicit allowlist of MCP server names that may access
            this ref.  ``None`` means no allowlist (denylist still applies).
        denied_mcps: Explicit denylist of MCP server names that must not
            access this ref.

    Returns:
        The same *ref* dict with a ``"classification"`` key added.
    """
    ref["classification"] = {
        "level": level,
        "level_numeric": CLASSIFICATION_LEVELS.get(level, 0),
        "allowed_mcps": allowed_mcps,
        "denied_mcps": denied_mcps,
    }
    return ref


def check_classification(ref: dict, mcps_requested: list[str]) -> tuple[bool, str]:
    """Check whether the requested MCP servers are permitted by the ref's classification.

    A ref without a ``"classification"`` key is always permitted.  Otherwise
    the denylist is checked first, then the allowlist (if one is set).

    Args:
        ref: The ref dict, potentially stamped by :func:`stamp_classification`.
        mcps_requested: Names of MCP servers that want to access this ref.

    Returns:
        A ``(allowed, reason)`` tuple.  *allowed* is ``True`` when all
        requested MCPs pass; *reason* is ``"ok"`` on success or a
        human-readable explanation on failure.
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


# ── Encrypted ────────────────────────────────────────────────────

KEYS_DIR = os.path.join("/tmp/swarm-mcp", ".keys")
"""Directory used to persist Fernet encryption keys on disk (mode 0o600 each)."""


def _generate_key() -> tuple[str, bytes]:
    """Generate a fresh Fernet key and derive a short identifier for it.

    Returns:
        A ``(key_id, key_bytes)`` tuple where *key_id* is the first 12 hex
        characters of the key's SHA-256 digest and *key_bytes* is the raw
        Fernet key material.
    """
    key = Fernet.generate_key()
    key_id = hashlib.sha256(key).hexdigest()[:12]
    return key_id, key


def _store_key(key_id: str, key: bytes) -> None:
    """Persist a Fernet key to ``KEYS_DIR`` with mode 0o600.

    Args:
        key_id: Identifier string used as the filename.
        key: Raw Fernet key bytes to write.
    """
    os.makedirs(KEYS_DIR, exist_ok=True)
    path = os.path.join(KEYS_DIR, key_id)
    with open(path, "wb") as f:
        f.write(key)
    os.chmod(path, 0o600)


def _load_key(key_id: str) -> bytes | None:
    """Load a Fernet key from ``KEYS_DIR`` by its identifier.

    Args:
        key_id: The key identifier (filename inside ``KEYS_DIR``).

    Returns:
        The raw key bytes, or ``None`` if the file does not exist.
    """
    path = os.path.join(KEYS_DIR, key_id)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def encrypt_text(text: str, key: bytes) -> bytes:
    """Encrypt a UTF-8 string using Fernet symmetric encryption.

    Args:
        text: Plaintext string to encrypt.
        key: Fernet key bytes (32 url-safe base64-encoded bytes).

    Returns:
        Fernet token bytes (URL-safe base64; safe to store as a JSON string).
    """
    return Fernet(key).encrypt(text.encode())


def decrypt_text(token: bytes, key: bytes) -> str:
    """Decrypt a Fernet token back to a UTF-8 string.

    Args:
        token: Fernet token bytes (as returned by :func:`encrypt_text`).
        key: Fernet key bytes used for encryption.

    Returns:
        The original plaintext string.

    Raises:
        cryptography.fernet.InvalidToken: If the token is invalid or the
            key does not match.
    """
    return Fernet(key).decrypt(token).decode()


def stamp_encrypted(ref: dict, key_id: str) -> dict:
    """Mark a ref's payload as encrypted.

    This only stamps the metadata; the actual encryption must have been
    performed separately (see :func:`encrypt_text`).

    Args:
        ref: The ref dict to annotate.
        key_id: The key identifier required to later decrypt the payload.

    Returns:
        The same *ref* dict with an ``"encrypted"`` key added.
    """
    ref["encrypted"] = {
        "key_id": key_id,
        "algorithm": "fernet",
    }
    return ref


def is_encrypted(ref: dict) -> bool:
    """Return ``True`` if the ref carries an encryption stamp.

    Args:
        ref: The ref dict to inspect.

    Returns:
        ``True`` when the ``"encrypted"`` key is present in *ref*.
    """
    return "encrypted" in ref


def check_encrypted(ref: dict, key_id: str | None = None) -> tuple[bool, str]:
    """Check whether a caller possesses the correct key to access an encrypted ref.

    Args:
        ref: The ref dict to inspect.
        key_id: The key identifier the caller claims to hold, or ``None`` if
            the caller does not have a key.

    Returns:
        A ``(can_access, reason)`` tuple.  *can_access* is ``True`` when the
        ref is unencrypted or the provided *key_id* matches; *reason* is
        ``"ok"`` or ``"not encrypted"`` on success, or a human-readable
        explanation on failure.
    """
    enc = ref.get("encrypted")
    if not enc:
        return True, "not encrypted"
    if key_id is None:
        return False, f"ref is encrypted (key_id: {enc['key_id']}), no key provided"
    if enc["key_id"] != key_id:
        return False, f"wrong key_id: expected {enc['key_id']}, got {key_id}"
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
    encrypt: bool = False,
) -> dict:
    """Apply all relevant monadic stamps to a ref in a single call.

    This is the canonical way to annotate a freshly-created ref before
    returning it from a combinator.  Only non-``None`` options are applied.

    Args:
        ref: The ref dict to enrich (mutated in-place).
        run_id: Run identifier used to locate the result file when encrypting.
        text: Agent output text, used for the provenance content hash and
            optionally for encryption.
        parent_refs: Upstream ref strings this result was derived from.
        budget_limit: Total pipeline USD budget cap (passed to
            :func:`stamp_cost`).
        spent_so_far: USD already consumed by prior steps.
        deadline: Unix timestamp deadline for the pipeline (passed to
            :func:`stamp_deadline`).
        classification: Data-sensitivity level (passed to
            :func:`stamp_classification`).
        attempt: 1-based retry attempt index (passed to :func:`stamp_retry`
            when provided).
        max_retries: Maximum retries for the step (defaults to 3 when
            *attempt* is set).
        encrypt: When ``True``, generates a Fernet key, encrypts the text
            field in the on-disk ``result.json``, and stamps the ref.  The
            ``key_id`` is stored in ``ref["encrypted"]["key_id"]``.

    Returns:
        The same *ref* dict with all requested stamps applied.
    """
    stamp_provenance(ref, parent_refs, text)
    stamp_cost(ref, budget_limit, spent_so_far)

    if deadline:
        stamp_deadline(ref, deadline)
    if classification:
        stamp_classification(ref, classification)
    if attempt is not None:
        stamp_retry(ref, attempt, max_retries or 3)
    if encrypt:
        key_id, key = _generate_key()
        _store_key(key_id, key)
        # Encrypt the text on disk
        ref_str = ref.get("ref", "")
        result_file = os.path.join("/tmp/swarm-mcp", ref_str, "result.json")
        if os.path.exists(result_file):
            with open(result_file) as f:
                result_data = json.load(f)
            if result_data.get("text"):
                ciphertext = encrypt_text(result_data["text"], key)
                result_data["text"] = ciphertext.decode()  # Fernet tokens are base64-safe
                result_data["encrypted"] = True
                with open(result_file, "w") as f:
                    json.dump(result_data, f, indent=2, default=str)
        stamp_encrypted(ref, key_id)

    return ref


# ── LLM-Governed Pipeline Monads ──────────────────────────────────

import copy
from dataclasses import dataclass, field as dataclass_field

MONAD_REGISTRY_DIR = os.path.expanduser("~/.claude/monads")


@dataclass
class MonadSpec:
    name: str
    spec: str           # Natural language: what this monad should decide
    model: str = "claude-haiku-4-5-20251001"
    description: str = ""


@dataclass
class MonadContinuation:
    action: str   # "next" | "jump" | "halt" | "broken" | "patch_pipeline"
    target: str | None = None           # step_id for "jump"
    reason: str | None = None           # for "halt"/"broken"
    context: dict = dataclass_field(default_factory=dict)  # KV additions to monad_context
    pipeline_patch: dict | None = None  # deep-merge dict for "patch_pipeline"


def save_monad(spec: MonadSpec) -> None:
    os.makedirs(MONAD_REGISTRY_DIR, exist_ok=True)
    path = os.path.join(MONAD_REGISTRY_DIR, f"{spec.name}.json")
    with open(path, "w") as f:
        json.dump({"name": spec.name, "spec": spec.spec, "model": spec.model, "description": spec.description}, f, indent=2)


def load_monad(name: str) -> "MonadSpec | None":
    path = os.path.join(MONAD_REGISTRY_DIR, f"{name}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return MonadSpec(**data)


def list_monads() -> list:
    if not os.path.exists(MONAD_REGISTRY_DIR):
        return []
    result = []
    for fname in sorted(os.listdir(MONAD_REGISTRY_DIR)):
        if fname.endswith(".json"):
            spec = load_monad(fname[:-5])
            if spec:
                result.append(spec)
    return result


def deep_merge(base: dict, patch: dict) -> dict:
    """JSON Merge Patch (RFC 7396): null values delete keys, dicts recurse, scalars/arrays replace."""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def evaluate_monad(
    spec: "MonadSpec",
    pipeline_def: dict,
    current_step: dict,
    results: list,
    monad_context: dict,
) -> "MonadContinuation":
    """Evaluate an LLM monad and return a continuation decision.

    Calls the Claude API directly — lightweight decision call, no container spin-up.
    """
    import anthropic

    state = {
        "current_step": {
            "id": current_step.get("id"),
            "exit_code": results[-1].exit_code if results else None,
            "error": results[-1].error if results else None,
            "output": (results[-1].text or "")[:1000] if results else None,
        },
        "prior_steps": [
            {"id": r.agent_id, "exit_code": r.exit_code, "error": r.error}
            for r in results[:-1]
        ],
        "monad_context": monad_context,
        "pipeline": {
            "name": pipeline_def.get("name"),
            "steps": [
                {"id": s.get("id"), "prompt_snippet": s.get("prompt", "")[:100]}
                for s in pipeline_def.get("steps", [])
            ],
        },
    }

    user_msg = (
        f"Pipeline state:\n{json.dumps(state, indent=2)}\n\n"
        f"Full pipeline definition:\n{json.dumps(pipeline_def, indent=2)}\n\n"
        'Respond with JSON only (no markdown fences):\n'
        '{"action": "next|jump|halt|broken|patch_pipeline", '
        '"target": "<step_id or null>", '
        '"reason": "<explanation>", '
        '"context": {}, '
        '"pipeline_patch": null}'
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=spec.model,
        max_tokens=1024,
        system=spec.spec + "\n\nYou must respond with valid JSON only. No markdown fences, no text outside the JSON object.",
        messages=[{"role": "user", "content": user_msg}],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json\n"):
            text = text[5:]
    text = text.strip()

    data = json.loads(text)
    return MonadContinuation(
        action=data.get("action", "next"),
        target=data.get("target"),
        reason=data.get("reason"),
        context=data.get("context") or {},
        pipeline_patch=data.get("pipeline_patch"),
    )
