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

import json
import logging
import re

from . import registry

logger = logging.getLogger(__name__)

MAX_RESOLVE_DEPTH = 3

_VERDICT_SCORE_DEFAULTS = {"VALID": 1.0, "PARTIAL": 0.5, "INVALID": 0.0, "UNKNOWN": 0.0}


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

    The resulting prompt asks the validator to emit a single JSON object with
    a continuous ``score`` in [0, 1], a ``verdict`` string, and a list of
    actionable ``issues``. The continuous score is the key signal for
    iterative refinement loops: it tells a calling pipeline whether successive
    attempts are converging, not just whether they crossed the pass threshold.

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

    return f"""You are a type validator. Your job is to check whether an artifact matches its declared type and report, in machine-readable form, both how well it matches and what would need to change to match better.

# Declared Type
{resolved_type}

# Artifact to Validate
{artifact_description}

# Instructions
1. Check each verification criterion listed in the type definition.
2. Compute a continuous ``score`` in [0.0, 1.0]:
   - 1.0 — every criterion is fully satisfied; nothing to improve
   - 0.5 — partial; the artifact has the right shape but missing or wrong pieces
   - 0.0 — the artifact fundamentally isn't this type
   Intermediate values are fine and encouraged — a caller is using this score
   to judge whether successive attempts are converging.
3. List ``issues``: short, actionable strings saying what would need to change
   to push the score higher. Empty list when score == 1.0.
4. Set ``verdict``: "VALID" if score >= 0.95, "PARTIAL" if 0 < score < 0.95,
   "INVALID" if score == 0.

Respond with ONE JSON object only — no markdown fences, no prose before or
after. Schema:

{{"verdict": "VALID" | "PARTIAL" | "INVALID",
  "score": <float in [0.0, 1.0]>,
  "issues": ["<actionable issue 1>", "<actionable issue 2>"]}}"""


def parse_validation_response(text: str) -> dict:
    """Parse a validator's output into ``{verdict, score, issues, raw}``.

    Tries JSON first (the modern format from :func:`build_validation_prompt`),
    then falls back to line-scanning for a ``VALID`` / ``PARTIAL`` / ``INVALID``
    verdict token so old-format validators and noisy outputs still produce a
    usable result. When falling back, ``score`` is derived from the verdict
    via :data:`_VERDICT_SCORE_DEFAULTS` and ``issues`` is an empty list.

    The ``raw`` field preserves the original validator text for audit.

    Args:
        text: Raw text output from a validator agent. May contain markdown
            fences, thinking tokens, or free prose around the JSON.

    Returns:
        A dict with keys:
            - ``verdict`` (str): one of ``VALID``, ``PARTIAL``, ``INVALID``, ``UNKNOWN``.
            - ``score`` (float): continuous score in [0.0, 1.0].
            - ``issues`` (list[str]): actionable issues, possibly empty.
            - ``raw`` (str): original validator text.
    """
    if not text:
        return {"verdict": "UNKNOWN", "score": 0.0, "issues": [], "raw": ""}

    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) > 1:
            cleaned = parts[1]
            if cleaned.startswith("json\n"):
                cleaned = cleaned[5:]
            cleaned = cleaned.strip()

    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidate = cleaned[first_brace : last_brace + 1]
        try:
            data = json.loads(candidate)
            verdict = str(data.get("verdict", "UNKNOWN")).upper()
            if verdict not in _VERDICT_SCORE_DEFAULTS:
                verdict = "UNKNOWN"
            try:
                score = float(data.get("score", _VERDICT_SCORE_DEFAULTS[verdict]))
            except (TypeError, ValueError):
                score = _VERDICT_SCORE_DEFAULTS[verdict]
            score = max(0.0, min(1.0, score))
            raw_issues = data.get("issues") or []
            if isinstance(raw_issues, str):
                raw_issues = [raw_issues]
            issues = [str(i) for i in raw_issues if i]
            return {"verdict": verdict, "score": score, "issues": issues, "raw": text}
        except (json.JSONDecodeError, AttributeError):
            pass

    verdict = "UNKNOWN"
    for line in text.split("\n"):
        line = line.strip()
        if line in ("VALID", "PARTIAL", "INVALID"):
            verdict = line
            break
        if line.startswith(("VALID", "PARTIAL", "INVALID")):
            verdict = line.split()[0]
            break
    return {
        "verdict": verdict,
        "score": _VERDICT_SCORE_DEFAULTS[verdict],
        "issues": [],
        "raw": text,
    }
