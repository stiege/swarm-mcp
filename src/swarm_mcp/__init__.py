"""swarm-mcp — MCP server for orchestrating parallel Claude agent workloads.

This package exposes a FastMCP server (``swarm_mcp.server``) that provides
higher-order combinators (run, par, map, chain, reduce, map_reduce, pipeline,
…) for launching Claude agents inside isolated Docker containers and composing
their results in a monadic ref system.

Public API
----------
The primary entry point is the server's ``main()`` function, invoked via the
``swarm-mcp`` console script.  For programmatic / library use the key types
and helpers are re-exported here:

Types
~~~~~
- :class:`AgentResult` — dataclass returned by every agent execution.
- :class:`SandboxSpec` — dataclass describing an agent's execution environment.

Sandbox helpers
~~~~~~~~~~~~~~~
- :func:`resolve_sandbox` — build a ``SandboxSpec`` from a name or inline JSON.
- :func:`load_sandbox` — load a named spec from the search paths.
- :func:`save_sandbox` — persist a spec to ``~/.claude/sandboxes/``.
- :func:`list_sandboxes` — enumerate all registered specs.

Registry helpers
~~~~~~~~~~~~~~~~
- :func:`wrap_file` — bring a host file/directory into the ref system.
- :func:`wrap_project` — register a project's pipelines/sandboxes/types dirs.
- :func:`add_search_path` — add a custom resource search path at runtime.
"""

from .agent import AgentResult
from .sandbox import SandboxSpec, list_sandboxes, load_sandbox, resolve_sandbox, save_sandbox
from .registry import add_search_path, wrap_file, wrap_project

__version__ = "0.1.0"

__all__ = [
    # Core types
    "AgentResult",
    "SandboxSpec",
    # Sandbox helpers
    "load_sandbox",
    "save_sandbox",
    "list_sandboxes",
    "resolve_sandbox",
    # Registry helpers
    "wrap_file",
    "wrap_project",
    "add_search_path",
]
