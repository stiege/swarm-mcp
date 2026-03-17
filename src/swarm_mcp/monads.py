"""Monadic wrappers for the swarm ref system.

Each monad is a field (or set of fields) on the ref dict. No nested wrappers —
just a flat dict with optional fields that combinators and the pipeline
interpreter can check.

The monad stack:
  Encrypted (Classified (Costed (Timed (Provenanced (Validated (Ref a))))))

In practice: a ref dict with optional fields for each layer.
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


# ── Encrypted ────────────────────────────────────────────────────

KEYS_DIR = os.path.join("/tmp/swarm-mcp", ".keys")


def _generate_key() -> tuple[str, bytes]:
    """Generate a Fernet key and return (key_id, key_bytes)."""
    key = Fernet.generate_key()
    key_id = hashlib.sha256(key).hexdigest()[:12]
    return key_id, key


def _store_key(key_id: str, key: bytes) -> None:
    """Persist a key to the keys directory."""
    os.makedirs(KEYS_DIR, exist_ok=True)
    path = os.path.join(KEYS_DIR, key_id)
    with open(path, "wb") as f:
        f.write(key)
    os.chmod(path, 0o600)


def _load_key(key_id: str) -> bytes | None:
    """Load a key by ID. Returns None if not found."""
    path = os.path.join(KEYS_DIR, key_id)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def encrypt_text(text: str, key: bytes) -> bytes:
    """Encrypt text with a Fernet key."""
    return Fernet(key).encrypt(text.encode())


def decrypt_text(token: bytes, key: bytes) -> str:
    """Decrypt a Fernet token back to text."""
    return Fernet(key).decrypt(token).decode()


def stamp_encrypted(ref: dict, key_id: str) -> dict:
    """Mark a ref as encrypted. The key_id is needed to decrypt."""
    ref["encrypted"] = {
        "key_id": key_id,
        "algorithm": "fernet",
    }
    return ref


def is_encrypted(ref: dict) -> bool:
    """Check if a ref is encrypted."""
    return "encrypted" in ref


def check_encrypted(ref: dict, key_id: str | None = None) -> tuple[bool, str]:
    """Check if the caller has the right key for an encrypted ref.

    Returns (can_access, reason).
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
    """Apply all relevant monadic stamps to a ref in one call.

    If encrypt=True, generates a key, encrypts the text on disk, and stamps
    the ref. The key_id is returned in ref["encrypted"]["key_id"].
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
