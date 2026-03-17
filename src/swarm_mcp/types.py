"""Natural language type system for swarm agents.

Types are Markdown files that describe what something *is*, what it *contains*,
and how to *verify* it.  They reference each other with ``[name]`` syntax,
which is resolved by inlining the referenced type's content recursively.

Type files are discovered through the registry search paths in priority order:

1. Project directory registered via ``wrap_project()``
2. ``SWARM_PROJECT_DIR`` environment variable (if set)
3. ``~/.claude/types/`` (global fallback)

Each type file is a plain Markdown document.  The first line is used as a
one-line summary when listing types.  The rest can be free prose, bullet lists,
or structured criteria — the format is intentionally flexible because these
types are consumed by LLMs, not by a formal parser.
"""

import logging
import os
import re

from . import registry

logger = logging.getLogger(__name__)

MAX_RESOLVE_DEPTH = 3


def list_types() -> list[dict]:
    """List all registered types across all search paths.

    Returns:
        A list of dicts, each with keys:

        - ``"name"`` — type name (filename without ``.md`` extension).
        - ``"summary"`` — first line of the type file.
        - ``"source"`` — directory path where the file was found.
    """
    resources = registry.list_resources("types", ".md")
    result = []
    for r in resources:
        with open(r["path"]) as f:
            first_line = f.readline().strip()
        result.append({"name": r["name"], "summary": first_line, "source": r["source"]})
    return result


def get_type(name: str) -> str | None:
    """Get the raw content of a type definition.

    Searches registered project directories first, then ``~/.claude/types/``.

    Args:
        name: The type name without extension (e.g. ``"mcp-server"``).

    Returns:
        The full text of the type definition, stripped of leading/trailing
        whitespace, or ``None`` if no matching file was found.
    """
    path = registry.find_resource("types", name, ".md")
    if path is None:
        return None
    with open(path) as f:
        return f.read().strip()


def resolve_type(description: str, depth: int = 0, _seen: set | None = None) -> str:
    """Resolve ``[type-name]`` references in a type description.

    Recursively inlines referenced types up to ``MAX_RESOLVE_DEPTH`` levels
    deep.  Each type name is only inlined once — subsequent references to the
    same type are replaced with ``"(see above)"`` to prevent duplication.
    Unknown ``[references]`` are left unchanged so the LLM can still
    interpret them.

    Args:
        description: Type description text that may contain ``[name]`` refs.
        depth: Current recursion depth (internal; callers should use default 0).
        _seen: Set of already-inlined type names (internal; thread through
            recursive calls to prevent duplicate inlining).

    Returns:
        The description with all resolvable ``[references]`` inlined.
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
    """Build a context string describing input/output types for prompt injection.

    Resolves any ``[reference]`` syntax in each type description and formats
    the result as labelled Markdown sections suitable for prepending to an
    agent's task prompt.

    Args:
        input_type: Description or ``[ref]``-containing text for what the
            agent receives.  Pass ``None`` to omit the input-type section.
        output_type: Description or ``[ref]``-containing text for what the
            agent must produce.  Pass ``None`` to omit the output-type section.

    Returns:
        A Markdown string with ``# Input Type`` and/or ``# Output Type``
        sections, separated by a blank line.  Returns an empty string when
        both arguments are ``None``.
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
    """Build a prompt instructing an agent to validate an artifact against a type.

    The resulting prompt follows a structured format that asks the validator to
    evaluate each criterion and emit a ``VALID`` / ``PARTIAL`` / ``INVALID``
    verdict in a machine-parseable section.

    Args:
        artifact_description: The artifact content or description to validate
            (e.g. the text output from a prior agent run).
        declared_type: The resolved type definition text to validate against.
            Callers should resolve ``[references]`` first via
            :func:`resolve_type` if the raw type content may contain them.

    Returns:
        A structured prompt string ready to be sent to a validator agent.
    """
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
