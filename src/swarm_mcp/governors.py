"""LLM-governed pipeline governors.

This module implements the control-flow governor layer for the pipeline interpreter.
Governors are registered in natural language and evaluated by a Claude model at
pipeline trigger points (on_fail, on_success) to return a continuation decision.

The continuation algebra is fixed:

    next              — proceed to the next step normally
    jump(target)      — jump to a named step
    halt              — stop the pipeline cleanly (no error)
    broken(reason)    — stop and advertise the pipeline as broken
    patch_pipeline    — deep-merge patch the pipeline definition, then continue

The ``context`` dict in each continuation accumulates across the pipeline.
It is written to /shared/governor-context.json so steps can read it.

Governor specs are stored as JSON files in ~/.claude/governors/.

Usage in a pipeline step::

    {"id": "train", ..., "on_fail": {"governor": "Failure"}}
    {"id": "judge", ..., "on_success": {"governor": "Validation"}}
"""

import concurrent.futures
import copy
import json
import logging
import os
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

GOVERNOR_REGISTRY_DIR = os.path.expanduser("~/.claude/governors")


# ── Registry ──────────────────────────────────────────────────────


@dataclass
class GovernorSpec:
    """A registered LLM-governed governor."""
    name: str
    spec: str           # Natural language: what should this governor decide?
    model: str = "claude-haiku-4-5-20251001"
    description: str = ""
    beam_width: int = 1  # N>1 → self-consistency sample N continuations and aggregate


def save_governor(spec: GovernorSpec) -> None:
    """Persist a governor spec to the registry directory."""
    os.makedirs(GOVERNOR_REGISTRY_DIR, exist_ok=True)
    path = os.path.join(GOVERNOR_REGISTRY_DIR, f"{spec.name}.json")
    with open(path, "w") as f:
        json.dump({
            "name": spec.name,
            "spec": spec.spec,
            "model": spec.model,
            "description": spec.description,
            "beam_width": spec.beam_width,
        }, f, indent=2)


def load_governor(name: str) -> "GovernorSpec | None":
    """Load a governor spec by name, or None if not registered."""
    path = os.path.join(GOVERNOR_REGISTRY_DIR, f"{name}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return GovernorSpec(
        name=data["name"],
        spec=data["spec"],
        model=data.get("model", "claude-haiku-4-5-20251001"),
        description=data.get("description", ""),
        beam_width=data.get("beam_width", 1),
    )


def list_governors() -> list:
    """Return all registered governor specs, sorted by name."""
    if not os.path.exists(GOVERNOR_REGISTRY_DIR):
        return []
    result = []
    for fname in sorted(os.listdir(GOVERNOR_REGISTRY_DIR)):
        if fname.endswith(".json"):
            spec = load_governor(fname[:-5])
            if spec:
                result.append(spec)
    return result


# ── Continuation algebra ──────────────────────────────────────────


@dataclass
class GovernorContinuation:
    """Fixed continuation returned by any LLM governor.

    action values:
        next            — proceed normally
        jump            — jump to ``target`` step
        halt            — stop cleanly
        broken          — stop and write broken status (see pipeline_status tool)
        patch_pipeline  — apply ``pipeline_patch`` as a deep-merge and continue

    ``confidence`` is a self-reported score in [0.0, 1.0]. When a governor runs
    with ``beam_width > 1`` the harness aggregates N samples by
    confidence-weighted majority vote (see ``evaluate_governor_beam``); the
    losers are retained on ``alternatives`` so later phases can do actual
    step-lookahead beam search.
    """
    action: str
    target: str | None = None
    reason: str | None = None
    context: dict = field(default_factory=dict)
    pipeline_patch: dict | None = None
    confidence: float = 1.0
    alternatives: list = field(default_factory=list)


# ── Utilities ─────────────────────────────────────────────────────


def deep_merge(base: dict, patch: dict) -> dict:
    """JSON Merge Patch (RFC 7396): null deletes, dicts recurse, scalars/arrays replace."""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ── Evaluation ────────────────────────────────────────────────────


def evaluate_governor(
    spec: "GovernorSpec",
    pipeline_def: dict,
    current_step: dict,
    results: list,
    governor_context: dict,
) -> "GovernorContinuation":
    """Evaluate an LLM governor and return a continuation.

    Shells out to ``claude -p --model <spec.model>`` so authentication follows
    the same OAuth-via-keychain path the rest of the MCP server uses. The
    governor spec is passed via ``--append-system-prompt``; pipeline state is
    the user prompt. A single lightweight decision call, no container.
    """
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
        "governor_context": governor_context,
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
        "Respond with JSON only (no markdown fences, no other text):\n"
        '{"action": "next|jump|halt|broken|patch_pipeline", '
        '"target": "<step_id or null>", '
        '"reason": "<explanation>", '
        '"context": {}, '
        '"pipeline_patch": null, '
        '"confidence": <float in [0.0, 1.0] — how sure you are this is the right call>}'
    )

    system_prompt = (
        spec.spec
        + "\n\nYou must respond with valid JSON only. "
        "No markdown fences, no explanation outside the JSON object. "
        "The ``confidence`` field is required: 1.0 means certain, 0.5 means a toss-up, "
        "0.0 means you're guessing. Be honest — the harness uses it for beam search."
    )

    claude_bin = shutil.which("claude") or "claude"
    try:
        proc = subprocess.run(
            [
                claude_bin, "-p",
                "--model", spec.model,
                "--append-system-prompt", system_prompt,
                user_msg,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("governor subprocess timed out after 120s") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout)[:400]}"
        )

    text = (proc.stdout or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json\n"):
            text = text[5:]
        text = text.strip()
    if not text:
        raise RuntimeError("governor claude -p returned empty output")
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        text = text[first_brace:last_brace + 1]

    data = json.loads(text)
    raw_conf = data.get("confidence")
    try:
        confidence = float(raw_conf) if raw_conf is not None else 1.0
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))

    return GovernorContinuation(
        action=data.get("action", "next"),
        target=data.get("target"),
        reason=data.get("reason"),
        context=data.get("context") or {},
        pipeline_patch=data.get("pipeline_patch"),
        confidence=confidence,
    )


# ── Beam search over governor decisions ──────────────────────────


def _continuation_key(cont: "GovernorContinuation") -> tuple:
    """Bucket key for majority vote: two continuations are 'the same decision'
    if they agree on action + target (jumps) or action + patch (patch_pipeline)."""
    if cont.action == "jump":
        return ("jump", cont.target)
    if cont.action == "patch_pipeline":
        return ("patch_pipeline", json.dumps(cont.pipeline_patch or {}, sort_keys=True))
    return (cont.action,)


def aggregate_continuations(samples: list) -> "GovernorContinuation":
    """Aggregate N governor samples into a single winning continuation.

    Strategy: confidence-weighted majority vote on ``_continuation_key``. The
    bucket with the highest summed confidence wins; inside that bucket the
    individual sample with the highest confidence is the representative. Losing
    candidates are preserved on ``alternatives`` so later phases can do actual
    step-lookahead beam search.

    If *samples* is empty, falls back to a neutral ``next`` continuation.
    """
    if not samples:
        return GovernorContinuation(action="next", reason="empty beam", confidence=0.0)

    buckets: dict = defaultdict(list)
    for s in samples:
        buckets[_continuation_key(s)].append(s)

    ranked = sorted(
        buckets.items(),
        key=lambda kv: (sum(c.confidence for c in kv[1]), len(kv[1])),
        reverse=True,
    )
    winning_bucket = ranked[0][1]
    winner = max(winning_bucket, key=lambda c: c.confidence)

    alternatives = []
    for key, bucket in ranked[1:]:
        rep = max(bucket, key=lambda c: c.confidence)
        alternatives.append({
            "action": rep.action,
            "target": rep.target,
            "reason": rep.reason,
            "confidence": rep.confidence,
            "votes": len(bucket),
            "total_confidence": sum(c.confidence for c in bucket),
        })

    winner.alternatives = alternatives
    # Aggregated confidence = vote share × mean-confidence inside the bucket
    total_conf = sum(c.confidence for c in samples)
    bucket_conf = sum(c.confidence for c in winning_bucket)
    winner.confidence = bucket_conf / total_conf if total_conf > 0 else winner.confidence
    return winner


def evaluate_governor_beam(
    spec: "GovernorSpec",
    pipeline_def: dict,
    current_step: dict,
    results: list,
    governor_context: dict,
    beam_width: int | None = None,
) -> "GovernorContinuation":
    """Evaluate a governor N times in parallel and aggregate by majority vote.

    When *beam_width* is 1 (or omitted and the spec beam_width is 1) this
    collapses to a single ``evaluate_governor`` call — zero overhead on the
    common path. With N > 1 the harness calls the governor LLM N times in
    parallel threads and aggregates via :func:`aggregate_continuations`.

    This is the phase-1a implementation of governor beam search: we sample
    multiple decisions and commit to the majority, which buys robustness
    against single-call governor noise. Phase-1b will replace the ``N × LLM``
    sampling with an actual one-step pipeline-state lookahead.
    """
    n = beam_width if beam_width is not None else spec.beam_width
    if n <= 1:
        return evaluate_governor(spec, pipeline_def, current_step, results, governor_context)

    samples: list = []
    errors: list = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as executor:
        futures = [
            executor.submit(
                evaluate_governor,
                spec,
                pipeline_def,
                current_step,
                results,
                governor_context,
            )
            for _ in range(n)
        ]
        for fut in concurrent.futures.as_completed(futures):
            try:
                samples.append(fut.result())
            except Exception as e:
                errors.append(e)
                logger.warning("Governor '%s' beam sample failed: %s", spec.name, e)

    if not samples:
        raise RuntimeError(
            f"Governor '{spec.name}' beam of width {n} produced 0 usable samples"
            + (f" (errors: {errors})" if errors else "")
        )

    winner = aggregate_continuations(samples)
    logger.info(
        "Governor '%s' beam(n=%d): action=%s target=%s confidence=%.2f alts=%d",
        spec.name, n, winner.action, winner.target, winner.confidence, len(winner.alternatives),
    )
    return winner
