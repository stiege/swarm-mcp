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

import copy
import json
import logging
import os
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
        }, f, indent=2)


def load_governor(name: str) -> "GovernorSpec | None":
    """Load a governor spec by name, or None if not registered."""
    path = os.path.join(GOVERNOR_REGISTRY_DIR, f"{name}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return GovernorSpec(**data)


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
    """
    action: str
    target: str | None = None
    reason: str | None = None
    context: dict = field(default_factory=dict)
    pipeline_patch: dict | None = None


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

    Calls the Claude API directly — lightweight decision call, no container.
    The governor spec is the system prompt; pipeline state is the user message.
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
        '"pipeline_patch": null}'
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=spec.model,
        max_tokens=1024,
        system=(
            spec.spec
            + "\n\nYou must respond with valid JSON only. "
            "No markdown fences, no explanation outside the JSON object."
        ),
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
    return GovernorContinuation(
        action=data.get("action", "next"),
        target=data.get("target"),
        reason=data.get("reason"),
        context=data.get("context") or {},
        pipeline_patch=data.get("pipeline_patch"),
    )
