"""Sandbox specifications — reusable, composable agent execution environments.

A :class:`SandboxSpec` is the *Reader monad's environment*: it describes
everything an agent needs to run (model, tools, MCPs, resource limits, mounts,
…) without being tied to any specific prompt or task.  Specs are immutable
data; :meth:`SandboxSpec.merge` produces a new spec with overrides applied.

Named specs are stored as JSON files and discovered through the registry search
paths in priority order:

1. Project directory registered via :func:`wrap_project`.
2. ``SWARM_PROJECT_DIR`` environment variable (if set).
3. ``~/.claude/sandboxes/`` (global fallback).
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field

from . import registry

logger = logging.getLogger(__name__)


@dataclass
class SandboxSpec:
    """Complete specification for a Claude agent's Docker execution environment.

    All fields have sensible defaults so a bare ``SandboxSpec()`` is valid and
    produces a minimal agent container.  Named specs loaded from JSON files
    override only the fields present in the file; the rest keep their defaults.

    Claude configuration
    --------------------
    model:
        Claude model alias — ``"haiku"``, ``"sonnet"`` (default), or
        ``"opus"``.
    tools:
        Allowed Claude tool names.  Defaults to the five read/write tools.
    mcps:
        MCP server names to attach (looked up in the host ``.claude.json``).
    system_prompt:
        Optional system prompt passed via ``--system-prompt``.
    claude_md:
        Optional project instructions written to ``CLAUDE.md`` in the
        container workspace.
    output_schema:
        JSON Schema dict for structured output (``--json-schema`` flag).
    effort:
        Effort level passed to the model — ``"low"``, ``"medium"``,
        ``"high"``, or ``"max"``.
    max_budget:
        Explicit USD budget cap for the agent run.

    Type system
    -----------
    input_type:
        Natural-language description (or ``[ref]`` syntax) of what the agent
        receives.  Injected into the prompt by the agent runner.
    output_type:
        Natural-language description of what the agent must produce.

    Filesystem
    ----------
    mounts:
        List of host-to-container bind-mount dicts, each with
        ``host_path``, ``container_path``, and optional ``readonly`` (bool,
        default ``True``).
    workdir:
        Working directory inside the container (default: ``"/workspace"``).
    input_files:
        Dict mapping container paths to file content strings.  Files are
        written to the workspace before the agent starts.

    Network
    -------
    network:
        ``True`` (default) enables host networking so the agent can reach the
        Anthropic API.  ``False`` isolates the container completely.
    network_mode:
        Explicit Docker ``--network`` value (e.g. ``"my-restricted-net"``).
        When set, overrides the ``network`` bool.  Use this to reference a
        pre-configured Docker network with custom routing rules (e.g. one that
        only allows outbound to ``api.anthropic.com``).

    Resources
    ---------
    memory:
        Docker memory limit string (e.g. ``"2g"``).
    cpus:
        Docker CPU limit (e.g. ``2.0``).
    gpu:
        Pass ``--gpus all`` to Docker and acquire the ``"gpu"`` resource pool.
    resources:
        Named resource pool names to acquire before execution (e.g.
        ``["gpu", "database"]``).

    Runtime
    -------
    timeout:
        Maximum wall-clock execution time in seconds (default: 1800 = 30 min).
    env_vars:
        Extra environment variables to pass into the container.
    """

    # Claude configuration
    model: str = "sonnet"
    tools: list[str] = field(default_factory=lambda: ["Read", "Write", "Glob", "Grep", "Bash"])
    mcps: list[str] = field(default_factory=list)
    system_prompt: str | None = None
    claude_md: str | None = None
    output_schema: dict | None = None
    effort: str | None = None
    max_budget: float | None = None

    # Types (natural language contracts)
    input_type: str | None = None
    output_type: str | None = None

    # Filesystem
    mounts: list[dict] = field(default_factory=list)
    workdir: str = "/workspace"
    input_files: dict = field(default_factory=dict)  # {container_path: content}

    # Network
    network: bool = True  # True = host network (needed for API), False = none
    network_mode: str | None = None  # Explicit --network value; overrides network bool when set

    # Resources
    memory: str | None = None  # e.g. "2g"
    cpus: float | None = None  # e.g. 2.0
    gpu: bool = False  # Pass --gpus all to Docker
    resources: list[str] = field(default_factory=list)  # Named resource pools to acquire

    # Runtime
    timeout: int = 1800  # 30 minutes default
    env_vars: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialise the spec to a JSON-compatible dict, omitting default/empty values.

        Returns:
            A dict containing only fields that are not ``None``, not an empty
            list, and not an empty dict — suitable for writing to a JSON file.
        """
        return {k: v for k, v in asdict(self).items() if v is not None and v != [] and v != {}}

    def merge(self, overrides: dict) -> "SandboxSpec":
        """Return a new ``SandboxSpec`` with selected fields replaced.

        This spec is not modified.  Only keys that exist in the dataclass and
        have a non-``None`` override value are replaced.

        Args:
            overrides: Mapping of field names to new values.  Unknown keys and
                ``None`` values are silently ignored.

        Returns:
            A new :class:`SandboxSpec` instance with the overrides applied.
        """
        data = asdict(self)
        for k, v in overrides.items():
            if v is not None and k in data:
                data[k] = v
        return SandboxSpec(**data)


def load_sandbox(name: str) -> SandboxSpec:
    """Load a named sandbox spec from the registry search paths.

    Searches registered project directories first, then
    ``~/.claude/sandboxes/``.  The JSON file may use a comma-separated string
    for the ``"tools"`` field; it will be split into a list automatically.

    Args:
        name: Sandbox name without the ``.json`` extension.

    Returns:
        A :class:`SandboxSpec` populated from the JSON file.

    Raises:
        FileNotFoundError: If no spec named *name* exists in any search path.
    """
    path = registry.find_resource("sandboxes", name, ".json")
    if path is None:
        raise FileNotFoundError(f"Sandbox spec '{name}' not found in search paths: {registry._search_paths.get('sandboxes', [])}")
    with open(path) as f:
        data = json.load(f)
    if isinstance(data.get("tools"), str):
        data["tools"] = [t.strip() for t in data["tools"].split(",") if t.strip()]
    return SandboxSpec(**{k: v for k, v in data.items() if k in SandboxSpec.__dataclass_fields__})


def save_sandbox(name: str, spec: SandboxSpec) -> str:
    """Persist a sandbox spec to disk.

    Writes to the first registered search path, or to
    ``~/.claude/sandboxes/`` if no paths are registered.

    Args:
        name: Filename stem for the spec (e.g. ``"web-researcher"``).
        spec: The :class:`SandboxSpec` to serialise.

    Returns:
        Absolute path to the written JSON file.
    """
    paths = registry._search_paths.get("sandboxes", [])
    save_dir = paths[0] if paths else os.path.expanduser("~/.claude/sandboxes")
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"{name}.json")
    with open(path, "w") as f:
        json.dump(spec.to_dict(), f, indent=2)
    return path


def list_sandboxes() -> list[dict]:
    """List all sandbox specs discoverable across all registered search paths.

    Returns:
        A list of dicts.  Successful entries have keys ``"name"``,
        ``"model"``, ``"tools"``, ``"mcps"``, and ``"source"``.  Entries that
        fail to load have ``"name"``, ``"error"``, and ``"source"`` instead.
    """
    resources = registry.list_resources("sandboxes", ".json")
    result = []
    for r in resources:
        try:
            spec = load_sandbox(r["name"])
            result.append({"name": r["name"], "model": spec.model, "tools": spec.tools, "mcps": spec.mcps, "source": r["source"]})
        except Exception:
            result.append({"name": r["name"], "error": "failed to load", "source": r["source"]})
    return result


def resolve_sandbox(sandbox: str | None = None, **overrides) -> SandboxSpec:
    """Resolve a sandbox spec from a name, inline JSON, or keyword overrides.

    This is the canonical factory used by all server tool handlers.  The three
    resolution modes are:

    - **Named spec** — *sandbox* is a non-JSON string: load from registry and
      apply *overrides* on top.
    - **Inline JSON** — *sandbox* starts with ``"{"``: parse as a JSON object
      and treat it like a named spec's content, then apply *overrides*.
    - **Defaults only** — *sandbox* is ``None``: start from a bare
      :class:`SandboxSpec` and apply *overrides*.

    In all cases *overrides* are applied via :meth:`SandboxSpec.merge` so
    unknown keys and ``None`` values are ignored.

    Args:
        sandbox: Named spec, inline JSON spec, or ``None``.
        **overrides: Field values to overlay on the resolved spec.

    Returns:
        A fully resolved :class:`SandboxSpec`.
    """
    if sandbox and sandbox.startswith("{"):
        data = json.loads(sandbox)
        if isinstance(data.get("tools"), str):
            data["tools"] = [t.strip() for t in data["tools"].split(",") if t.strip()]
        spec = SandboxSpec(**{k: v for k, v in data.items() if k in SandboxSpec.__dataclass_fields__})
    elif sandbox:
        spec = load_sandbox(sandbox)
    else:
        spec = SandboxSpec()

    if overrides:
        override_data = {}
        for k, v in overrides.items():
            if v is not None and k in SandboxSpec.__dataclass_fields__:
                override_data[k] = v
        if override_data:
            spec = spec.merge(override_data)

    return spec
