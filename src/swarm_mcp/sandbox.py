"""Sandbox specifications — reusable, composable agent environments.

A sandbox spec is the Reader monad's environment: it describes everything
an agent needs to run without being tied to a specific prompt or task.

Specs are stored as JSON files in ~/.claude/sandboxes/ and referenced by name.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

SANDBOX_DIR = os.path.expanduser("~/.claude/sandboxes")


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
    """Load a named sandbox spec from ~/.claude/sandboxes/<name>.json."""
    path = os.path.join(SANDBOX_DIR, f"{name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Sandbox spec not found: {path}")
    with open(path) as f:
        data = json.load(f)
    # Handle tools as comma-separated string or list
    if isinstance(data.get("tools"), str):
        data["tools"] = [t.strip() for t in data["tools"].split(",") if t.strip()]
    return SandboxSpec(**{k: v for k, v in data.items() if k in SandboxSpec.__dataclass_fields__})


def save_sandbox(name: str, spec: SandboxSpec) -> str:
    """Save a sandbox spec to ~/.claude/sandboxes/<name>.json."""
    os.makedirs(SANDBOX_DIR, exist_ok=True)
    path = os.path.join(SANDBOX_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(spec.to_dict(), f, indent=2)
    return path


def list_sandboxes() -> list[dict]:
    """List all saved sandbox specs."""
    if not os.path.isdir(SANDBOX_DIR):
        return []
    result = []
    for entry in sorted(os.scandir(SANDBOX_DIR), key=lambda e: e.name):
        if entry.name.endswith(".json"):
            name = entry.name[:-5]
            try:
                spec = load_sandbox(name)
                result.append({"name": name, "model": spec.model, "tools": spec.tools, "mcps": spec.mcps})
            except Exception:
                result.append({"name": name, "error": "failed to load"})
    return result


def resolve_sandbox(sandbox: str | None = None, **overrides) -> SandboxSpec:
    """Resolve a sandbox spec from a name or inline overrides.

    If sandbox is a name, load it and apply overrides.
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

    # Apply overrides
    if overrides:
        override_data = {}
        for k, v in overrides.items():
            if v is not None and k in SandboxSpec.__dataclass_fields__:
                override_data[k] = v
        if override_data:
            spec = spec.merge(override_data)

    return spec
