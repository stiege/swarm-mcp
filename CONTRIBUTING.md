# Contributing to swarm-mcp

Thank you for your interest in contributing! This guide covers everything you need to get started.

## Development Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (for building and running agent containers)

### Clone and install

```bash
git clone https://github.com/[owner]/swarm-mcp.git
cd swarm-mcp
uv sync
```

### Build the agent image

Every agent task runs inside a Docker container. Build the image once before running any tools:

```bash
docker build -t swarm-agent .
```

---

## Project Structure

All source lives under `src/swarm_mcp/`:

| File | Purpose |
|------|---------|
| `server.py` | FastMCP entry point. Registers every tool handler, wires subsystems together, manages the global concurrency semaphore and named resource pools. |
| `agent.py` | Executes a single agent run: writes task files into a temp directory, calls `docker.py` to spin up the container, collects `result.json`, and returns an `AgentResult`. |
| `docker.py` | Thin wrapper around the Docker SDK. Translates a `SandboxSpec` into `docker run` arguments (mounts, env vars, resource limits, network policy, etc.). |
| `sandbox.py` | Defines `SandboxSpec` (the typed configuration for a container run) and helpers to save/load/resolve sandbox presets from disk. |
| `registry.py` | Implements `wrap`, `wrap_project`, `save_sandbox_spec`, and `list_sandbox_specs` — resource and preset management tools. |
| `monads.py` | Monadic ref-manipulation tools: `unwrap`, `inspect`, `guard`, `classify`, `encrypt`, `decrypt`. |
| `tools.py` | Response-building utilities shared across tool handlers. |
| `types.py` | Natural-language type-checking: `list_type_registry`, `get_type_definition`, `validate`. |

---

## Running Your First Test

Start the server locally:

```bash
uv run swarm-mcp
```

Then, from a second terminal (or from an MCP client), call `run`:

```json
{
  "tool": "run",
  "arguments": {
    "task": "print('hello from swarm')",
    "sandbox": {}
  }
}
```

You should receive an `AgentResult` whose `result.json` contains the agent's output. Inspect the result:

```json
{
  "tool": "inspect",
  "arguments": {
    "ref": "<ref-id-from-run>"
  }
}
```

---

## Code Style

- **Linter / formatter**: [ruff](https://docs.astral.sh/ruff/)

  ```bash
  uv run ruff check src/
  uv run ruff format src/
  ```

- **Type hints**: required on all public functions and methods. Use `from __future__ import annotations` for forward references.

- **Docstrings**: [Google style](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings).

  ```python
  def run_agent(spec: SandboxSpec, task: str) -> AgentResult:
      """Run a single agent task in a Docker container.

      Args:
          spec: Container configuration (image, mounts, env, limits).
          task: The task string passed to the agent entrypoint.

      Returns:
          An AgentResult containing the parsed result.json and metadata.

      Raises:
          DockerError: If the container fails to start or exits non-zero.
      """
  ```

- **No bare `except`**: always catch specific exceptions.

- **Imports**: stdlib → third-party → local, each group separated by a blank line (ruff enforces this).

---

## Pull Request Process

1. **Fork** the repository and create a feature branch from `main`:

   ```bash
   git checkout -b feat/my-new-combinator
   ```

2. **Implement** your change, including type hints and docstrings.

3. **Lint and format**:

   ```bash
   uv run ruff check src/ && uv run ruff format src/
   ```

4. **Open a PR** with a clear description: what changed, why, and how to test it. For new tools or sandbox fields, include an example tool call JSON in the PR body.

5. A maintainer will review and may request changes before merging.

---

## Reporting Issues

When filing a bug, please include:

- **Tool call JSON**: the exact MCP tool call that triggered the bug (see the bug report template).
- **`result.json`**: the raw output returned by the agent, if available.
- **`inspect` output**: the result of calling the `inspect` tool on the ref.
- **Environment**: OS, Docker version, Python version, swarm-mcp version.

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.yml) — it will prompt you for all of the above.

---

## Adding a New Combinator

Combinators are tools that launch one or more agents and compose their results (e.g. `par`, `map`, `chain`).

**Pattern to follow:**

1. **Register the tool in `server.py`** using the `@mcp.tool()` decorator, following the style of existing combinators:

   ```python
   @mcp.tool()
   def my_combinator(tasks: list[str], sandbox: dict | None = None) -> list[AgentResult]:
       """One-sentence summary.

       Args:
           tasks: ...
           sandbox: ...

       Returns:
           ...
       """
       resolved = resolve_sandbox(sandbox)
       with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
           return list(pool.map(lambda t: run_agent(resolved, t), tasks))
   ```

2. **Acquire the global semaphore** (and any named resource pool) before calling `run_agent`, exactly as existing combinators do. This ensures the `MAX_CONCURRENT` limit is respected.

3. **Return `AgentResult` objects** (or lists/dicts thereof) so callers can pass refs to monadic tools.

4. **Add a docstring** with an example tool call in the `Examples:` section.

---

## Adding a New Sandbox Field

Sandbox fields configure the Docker container environment (resource limits, mounts, env vars, etc.).

The change must be threaded through three files:

1. **`sandbox.py` — `SandboxSpec`**: Add the field to the dataclass/TypedDict with a type annotation and default value.

   ```python
   @dataclass
   class SandboxSpec:
       ...
       my_new_field: str | None = None
   ```

2. **`docker.py`**: Read the field from the `SandboxSpec` and translate it into the appropriate `docker run` argument.

   ```python
   def build_run_kwargs(spec: SandboxSpec) -> dict:
       kwargs = { ... }
       if spec.my_new_field is not None:
           kwargs["some_docker_param"] = spec.my_new_field
       return kwargs
   ```

3. **`agent.py`**: Pass the field through to `docker.py` if any pre-processing is needed before the Docker call.

Always update docstrings in all three files to document the new field, and include an example in your PR.
