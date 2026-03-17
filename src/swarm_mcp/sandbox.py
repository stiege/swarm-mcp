"""Sandbox specifications — reusable, composable agent environments.

A sandbox spec is the Reader monad's environment: it describes everything
an agent needs to run without being tied to a specific prompt or task.

Specs are found via the registry search paths (project dir → ~/.claude/).
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field

from . import registry

logger = logging.getLogger(__name__)


@dataclass
class SandboxSpec:
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

    # Resources
    memory: str | None = None  # e.g. "2g"
    cpus: float | None = None  # e.g. 2.0

    # Runtime
    timeout: int = 120
    env_vars: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v != [] and v != {}}

    def merge(self, overrides: dict) -> "SandboxSpec":
        """Return a new spec with overrides applied. Immutable."""
        data = asdict(self)
        for k, v in overrides.items():
            if v is not None and k in data:
                data[k] = v
        return SandboxSpec(**data)


def load_sandbox(name: str) -> SandboxSpec:
    """Load a named sandbox spec. Searches project dir then ~/.claude/sandboxes/."""
    path = registry.find_resource("sandboxes", name, ".json")
    if path is None:
        raise FileNotFoundError(f"Sandbox spec '{name}' not found in search paths: {registry._search_paths.get('sandboxes', [])}")
    with open(path) as f:
        data = json.load(f)
    if isinstance(data.get("tools"), str):
        data["tools"] = [t.strip() for t in data["tools"].split(",") if t.strip()]
    return SandboxSpec(**{k: v for k, v in data.items() if k in SandboxSpec.__dataclass_fields__})


def save_sandbox(name: str, spec: SandboxSpec) -> str:
    """Save a sandbox spec. Writes to first writable search path, or ~/.claude/sandboxes/."""
    paths = registry._search_paths.get("sandboxes", [])
    save_dir = paths[0] if paths else os.path.expanduser("~/.claude/sandboxes")
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"{name}.json")
    with open(path, "w") as f:
        json.dump(spec.to_dict(), f, indent=2)
    return path


def list_sandboxes() -> list[dict]:
    """List all sandbox specs across all search paths."""
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
    """Resolve a sandbox spec from a name or inline overrides.

    If sandbox is a name, load it via registry and apply overrides.
    If sandbox is None, build from overrides with defaults.
    If sandbox is a JSON string, parse it.
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
