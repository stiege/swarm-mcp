"""Natural language type system for swarm agents.

Types are markdown files that describe what something is, what it contains,
and how to verify it. They reference each other with [name] syntax,
resolved by inlining the referenced type's content.

Types are found via the registry search paths (project dir → ~/.claude/types/).
"""

import logging
import os
import re

from . import registry

logger = logging.getLogger(__name__)

MAX_RESOLVE_DEPTH = 3


def list_types() -> list[dict]:
    """List all registered types across all search paths."""
    resources = registry.list_resources("types", ".md")
    result = []
    for r in resources:
        with open(r["path"]) as f:
            first_line = f.readline().strip()
        result.append({"name": r["name"], "summary": first_line, "source": r["source"]})
    return result


def get_type(name: str) -> str | None:
    """Get the raw content of a type definition. Searches project dir then ~/.claude/types/."""
    path = registry.find_resource("types", name, ".md")
    if path is None:
        return None
    with open(path) as f:
        return f.read().strip()


def resolve_type(description: str, depth: int = 0, _seen: set | None = None) -> str:
    """Resolve [type-name] references in a type description.

    Recursively inlines referenced types up to MAX_RESOLVE_DEPTH.
    Each type is only inlined once — subsequent references become "(see above)".
    Unknown references are left as-is (the LLM will still understand).
    """
    if depth >= MAX_RESOLVE_DEPTH:
        return description
    if _seen is None:
        _seen = set()

    def replace_ref(match):
        ref_name = match.group(1)
        if ref_name in _seen:
            return f"**{ref_name}** (see above)"
        content = get_type(ref_name)
        if content is None:
            return match.group(0)  # leave as-is
        _seen.add(ref_name)
        resolved = resolve_type(content, depth + 1, _seen)
        return f"**{ref_name}**: {resolved}"

    return re.sub(r"\[([a-zA-Z0-9_-]+)\]", replace_ref, description)


def build_type_context(input_type: str | None, output_type: str | None) -> str:
    """Build a context string describing input/output types for injection into prompts.

    Resolves [references] and formats as a clear section.
    """
    parts = []

    if input_type:
        resolved = resolve_type(input_type)
        parts.append(f"# Input Type\nYour input is: {resolved}")

    if output_type:
        resolved = resolve_type(output_type)
        parts.append(f"# Output Type\nYou must produce: {resolved}")

    return "\n\n".join(parts)


def build_validation_prompt(artifact_description: str, declared_type: str) -> str:
    """Build a prompt that asks an agent to validate an artifact against a type."""
    resolved_type = resolve_type(declared_type)

    return f"""You are a type validator. Your job is to check whether an artifact matches its declared type.

# Declared Type
{resolved_type}

# Artifact to Validate
{artifact_description}

# Instructions
1. Check each verification criterion listed in the type definition
2. For each criterion, report PASS or FAIL with a brief explanation
3. Give an overall verdict: VALID (all pass), PARTIAL (some fail), or INVALID (critical failures)

Respond in this exact format:
## Checks
- [criterion]: PASS/FAIL — explanation
- [criterion]: PASS/FAIL — explanation

## Verdict
VALID/PARTIAL/INVALID

## Issues
(list any failures that need fixing, or "None" if all passed)"""
