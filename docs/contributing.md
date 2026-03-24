# Contributing

swarm-mcp is an open-source project and welcomes contributions. This page
covers development setup, project architecture, and how to add new features.

---

## Development Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — Python package manager
- Docker — for running integration tests
- Claude Code CLI — with OAuth configured (`claude login`)

### Clone and install

```bash
git clone https://github.com/ahodge/swarm-mcp
cd swarm-mcp

# Install in editable mode with dev dependencies
uv sync --dev
```

### Build the agent image

```bash
# Copy the claude and uv binaries into the project root
cp "$(which claude)" ./claude
cp "$(which uv)" ./uv

# Build the Docker image
docker build -t swarm-agent .
```

### Run the server locally

```bash
uv run swarm-mcp
```

Or via MCP in Claude Code, pointing at the local checkout:

```json
{
  "mcpServers": {
    "swarm": {
      "command": "uv",
      "args": ["--directory", "/path/to/swarm-mcp", "run", "swarm-mcp"]
    }
  }
}
```

---

## Running Tests

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run a specific test file
uv run pytest tests/test_monads.py

# Run tests matching a name pattern
uv run pytest -k "test_encrypt"
```

!!! info "Test directory"
    The `tests/` directory at the project root is where all tests live. Add
    new test files named `test_<module>.py` following pytest conventions.

---

## Architecture Overview

The server is split into focused modules. Understanding which layer to touch
for a given change is the key to navigating the codebase quickly.

```
src/swarm_mcp/
├── server.py      # MCP tool definitions (the public API)
├── agent.py       # Docker execution and output parsing
├── sandbox.py     # SandboxSpec dataclass and resolution
├── stamps.py      # Ref enrichment: provenance, cost, encryption, classification
├── monads.py      # LLM-governed control flow (GovernorSpec, evaluate_monad)
├── types.py       # Natural language type system (load, resolve, validate)
├── registry.py    # Search paths for sandboxes, types, pipelines
└── docker.py      # Docker command construction and image management
```

### `server.py` — tool definitions

Every MCP tool (`run`, `par`, `map`, `reduce`, `map_reduce`, `chain`,
`pipeline`, `unwrap`, `inspect`, `encrypt`, `decrypt`, `classify`, `guard`,
`filter`, `race`, `retry`, `validate`, `wrap`, `wrap_project`, `save_sandbox_spec`,
`list_sandbox_specs`, `list_type_registry`, `get_type_definition`) is
registered here using `@mcp.tool()` decorators.

Tool handlers parse arguments, call lower-level modules, and format the return
value. They do not contain business logic directly — they delegate to `agent.py`,
`stamps.py`, `monads.py`, and `types.py`.

The global concurrency semaphore (`_semaphore`) and resource pool dict
(`_resource_pools`) live at module level in `server.py`.

### `agent.py` — execution engine

`run_agent(prompt, spec, run_id, agent_id)` is the core function. It:

1. Creates the output directory at `/tmp/swarm-mcp/{run_id}/{agent_id}/`
2. Injects type context into the prompt (via `types.build_type_context`)
3. Writes `prompt.txt`
4. Calls `_setup_agent_home()` to generate a minimal HOME with claude config,
   MCP config, CLAUDE.md, and the PostToolUse hook
5. Builds the Docker command (via `docker.get_docker_run_cmd`)
6. Launches the container with stdin from `prompt.txt` and stdout to `stream.jsonl`
7. Waits for completion (or kills on timeout)
8. Parses `stream.jsonl` via `_parse_stream_output()`
9. Writes `result.json` and returns an `AgentResult`

### `sandbox.py` — sandbox specs

`SandboxSpec` is a dataclass capturing everything needed to configure an agent
container. `resolve_sandbox()` accepts a name (looked up via registry), a raw
JSON string, or `None` (defaults), and applies any inline overrides on top.

### `stamps.py` — ref enrichment

Contains all the stamps applied to refs:

- `stamp_provenance()` — SHA-256 content hash and parent ref chain
- `stamp_cost()` — budget tracking fields
- `stamp_deadline()` — deadline tracking
- `stamp_validated()` — type validation verdict
- `stamp_retry()` — retry attempt tracking
- `stamp_classification()` — sensitivity level and MCP allowlist
- `stamp_encrypted()` / `encrypt_text()` / `decrypt_text()` — Fernet encryption

`enrich_ref()` is the single call that applies all relevant stamps in one shot.

### `types.py` — type system

Loads `.md` type files from registry search paths. `resolve_type()` walks
`[type-name]` references recursively (up to depth 3). `build_validation_prompt()`
generates a structured prompt for a validator agent. `build_type_context()`
injects `input_type` / `output_type` descriptions into agent prompts.

### `registry.py` — search paths

Manages three search path lists (`pipelines`, `sandboxes`, `types`). Priority
order: explicitly registered paths → `SWARM_PROJECT_DIR` env var → `~/.claude/`.
`wrap_file()` copies host files into the ref namespace. `wrap_project()`
registers a project directory's subdirectories.

### `docker.py` — container management

`get_docker_run_cmd()` assembles the full `docker run` command from a
`SandboxSpec`. Handles GPU flags, memory/CPU limits, network mode, volume
mounts, MCP project mounts, the PostToolUse hook mount, and all Claude CLI
flags (`--model`, `--allowedTools`, `--output-format stream-json`, etc.).
`ensure_image()` auto-builds the `swarm-agent` image if it's missing.

---

## How to Add a New Combinator Tool

Here is the full checklist for adding a new MCP tool — for example, a
hypothetical `tournament()` combinator.

### 1. Define the tool in `server.py`

```python
@mcp.tool()
def tournament(
    tasks: str,
    rounds: int = 2,
    model: str = "sonnet",
    max_concurrency: int = 4,
) -> str:
    """Run tasks in elimination rounds, keeping the best result per round.

    Args:
        tasks: JSON array of prompt strings.
        rounds: Number of elimination rounds.
        model: Claude model for all agents.
        max_concurrency: Max agents running simultaneously.

    Returns:
        JSON with the winning ref and per-round results.
    """
    task_list = json.loads(tasks)
    run_id = uuid.uuid4().hex[:12]

    # ... implementation ...

    return json.dumps({"winner": winning_ref, "rounds": round_results}, indent=2)
```

Tools must:
- Accept all arguments as strings or primitives (the MCP protocol is text-based)
- Parse JSON arguments using `json.loads()`
- Return a JSON string
- Have a docstring — it becomes the tool description shown to Claude

### 2. Acquire resources before spawning agents

Use the global semaphore and resource pools (see existing combinators for the
pattern):

```python
# Inside your tool, before launching containers:
if not _semaphore.acquire(timeout=RESOURCE_QUEUE_TIMEOUT):
    raise TimeoutError("Could not acquire execution slot")
try:
    result = run_agent(prompt, spec, run_id, agent_id)
finally:
    _semaphore.release()
```

### 3. Use `ThreadPoolExecutor` for concurrent execution

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
    futures = {executor.submit(run_one, task): task for task in task_list}
    results = []
    for future in as_completed(futures):
        results.append(future.result())
```

### 4. Enrich refs with stamps

After each agent run, call `enrich_ref()` to attach provenance, cost tracking,
and any other stamps:

```python
ref = result.to_ref_dict(run_id)
enrich_ref(
    ref,
    run_id,
    text=result.text,
    parent_refs=parent_ref_ids,
    budget_limit=budget,
    spent_so_far=total_cost,
)
```

### 5. Write tests

Add a test file at `tests/test_tournament.py`. Test the core logic in
isolation (mock `run_agent` so tests don't require Docker):

```python
from unittest.mock import patch, MagicMock
from swarm_mcp.agent import AgentResult

def make_result(text="output", exit_code=0):
    return AgentResult(
        agent_id="agent-0",
        text=text,
        exit_code=exit_code,
        duration_seconds=1.0,
        cost_usd=0.01,
        model="sonnet",
        output_dir="/tmp/test",
    )

@patch("swarm_mcp.server.run_agent", return_value=make_result())
def test_tournament_returns_winner(mock_run):
    from swarm_mcp.server import tournament
    result = tournament(tasks='["task A", "task B"]', rounds=1)
    data = json.loads(result)
    assert "winner" in data
```

### 6. Update the nav (if it's a top-level page)

If your addition warrants a new documentation page, add it to `mkdocs.yml`:

```yaml
nav:
  - Examples:
      - Tournament: examples/tournament.md
```

---

## Code Style

- Python 3.12+. Use type annotations on all public functions.
- Use `dataclasses` for structured data (see `SandboxSpec`, `AgentResult`).
- Keep `server.py` thin — business logic belongs in the module it's about.
- Log with `logging.getLogger(__name__)`. Use `logger.info` for normal
  operations, `logger.warning` for recoverable errors, `logger.exception`
  for unexpected failures.
- Format with `ruff format` and lint with `ruff check` (not yet enforced in
  CI, but preferred).

---

## Submitting Changes

1. Fork the repository on GitHub.
2. Create a branch: `git checkout -b feature/my-combinator`.
3. Make your changes and add tests.
4. Run `uv run pytest` to verify nothing is broken.
5. Open a pull request with a description of what the change does and why.

For large changes, open an issue first to discuss the design before writing code.

---

!!! note "See also"
    - [Concepts: Refs](concepts/refs.md) — understand the ref and stamp system before modifying `stamps.py`
    - [Concepts: Combinators](concepts/combinators.md) — the full combinator reference
    - [Observability](observability.md) — how to debug agent runs during development
