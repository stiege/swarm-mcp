# Sandbox Configuration

Every agent run in swarm-mcp executes inside an isolated Docker container. The **sandbox spec** is the blueprint for that container: which model to use, which tools are available, what filesystem is mounted, how much memory it gets, and how long it can run.

Sandbox specs can be defined inline, saved to the registry by name, and merged with per-call overrides — giving you a single reusable baseline with easy customisation per call.

---

## SandboxSpec Fields

### Claude Configuration

These fields control how the Claude agent inside the container behaves.

| Field | Type | Default | Description |
|---|---|---|---|
| `model` | string | `"sonnet"` | Claude model identifier (`"haiku"`, `"sonnet"`, `"opus"`) |
| `tools` | array | `["Read","Write","Glob","Grep","Bash"]` | Built-in tools and MCP tool names available to the agent |
| `mcps` | array | `[]` | Additional MCP server configurations to attach |
| `system_prompt` | string | — | Extra system-level instruction prepended to the agent's context |
| `claude_md` | string | — | Contents of a `CLAUDE.md` file injected into the container |
| `output_schema` | object | — | JSON Schema that the agent's final output must conform to |
| `effort` | string | — | Thinking effort hint: `"low"`, `"medium"`, or `"high"` |
| `max_budget` | number | — | Per-agent USD spending cap |
| `input_type` | string | — | Type name from the [Type System](types.md) describing expected input |
| `output_type` | string | — | Type name from the [Type System](types.md) describing expected output |

### Filesystem

| Field | Type | Default | Description |
|---|---|---|---|
| `mounts` | array | `[]` | Additional Docker volume mounts: `[{"host": "/data", "container": "/data", "readonly": true}]` |
| `workdir` | string | `"/workspace"` | Working directory inside the container |
| `input_files` | object | `{}` | Map of container path → content string; files written into the container before the agent starts |

### Network

| Field | Type | Default | Description |
|---|---|---|---|
| `network` | boolean | `true` | Whether the container has outbound internet access |

### Resources

| Field | Type | Default | Description |
|---|---|---|---|
| `memory` | string | — | Docker memory limit (e.g. `"2g"`, `"512m"`) |
| `cpus` | number | — | CPU quota as a float (e.g. `2.0` = two cores) |
| `gpu` | boolean | `false` | Whether to request a GPU; automatically adds `"gpu"` to the named resource list (see [Resources](resources.md)) |
| `resources` | array | `[]` | Additional named resource pools to acquire before running (see [Resources](resources.md)) |

### Runtime

| Field | Type | Default | Description |
|---|---|---|---|
| `timeout` | integer | `1800` | Maximum execution time in seconds (30 minutes) |
| `env_vars` | object | `{}` | Environment variables injected into the container: `{"MY_VAR": "value"}` |

---

## Named Sandbox Specs

Repeated configurations can be saved to the registry and referenced by name, keeping call sites clean.

### Saving a Spec

```
save_sandbox_spec(name="gpu-researcher", spec_json='{...}')
```

The spec is saved to the first writeable search path, or to `~/.claude/sandboxes/<name>.json` if no project path is configured.

### Listing Saved Specs

```
list_sandbox_specs()
```

Returns all named specs across all search paths, with the path each was loaded from. Project-level specs shadow global specs with the same name.

### Using a Named Spec

Pass the name as the `sandbox` argument to any run tool:

```json
{
  "sandbox": "gpu-researcher",
  "prompt": "Analyse the satellite imagery in /data/images/"
}
```

---

## Inline Specs

Pass a full JSON object directly when you do not want to use the registry:

```json
{
  "sandbox": {
    "model": "opus",
    "tools": ["Read", "Write", "Bash"],
    "memory": "8g",
    "timeout": 3600
  },
  "prompt": "Run the full benchmark suite"
}
```

---

## Merging and Overrides

Sandbox specs are **immutable value objects** — merging always produces a new spec rather than mutating the original. When you reference a named spec and supply extra fields, the extras are merged on top:

```json
{
  "sandbox": "base-researcher",
  "model": "opus",
  "timeout": 7200
}
```

This loads `base-researcher` from the registry and returns a new spec with `model` and `timeout` overridden. All other fields from `base-researcher` are preserved.

The same merge behaviour applies inside [Pipelines](pipelines.md): the pipeline-level `sandbox` object is the base, and each step's fields are merged on top of it.

!!! tip "Override only what changes"
    Define a project-wide base spec with your standard tools, memory limits, and system prompt. Override only the fields that genuinely differ per call — model for expensive steps, timeout for long-running steps, gpu for compute-intensive ones.

---

## Complete Example Spec

```json
{
  "model": "sonnet",
  "tools": ["Read", "Write", "Glob", "Grep", "Bash"],
  "mcps": [
    {
      "name": "internal-db",
      "command": "mcp-postgres",
      "args": ["--connection-string", "postgres://..."]
    }
  ],
  "system_prompt": "You are a data analyst. Always cite your sources.",
  "output_type": "AnalysisReport",
  "mounts": [
    {"host": "/data/warehouse", "container": "/data", "readonly": true}
  ],
  "workdir": "/workspace",
  "input_files": {
    "/workspace/config.json": "{\"threshold\": 0.95}"
  },
  "network": false,
  "memory": "4g",
  "cpus": 2.0,
  "gpu": false,
  "resources": ["db-pool"],
  "timeout": 900,
  "env_vars": {
    "ANALYSIS_MODE": "strict",
    "LOG_LEVEL": "info"
  }
}
```

---

## Using Sandboxes in `run`, `par`, and `map`

Every combinator accepts a `sandbox` argument that follows the same inline/named/merged resolution.

### `run` — single agent

```json
{
  "sandbox": "my-spec",
  "prompt": "Summarise the Q3 earnings report"
}
```

### `par` — parallel agents with a shared spec

```json
{
  "sandbox": {"model": "haiku", "timeout": 300},
  "prompts": [
    "Summarise document A",
    "Summarise document B",
    "Summarise document C"
  ]
}
```

### `map` — per-item spec overrides

Each item in the map call can supply its own overrides on top of the base sandbox:

```json
{
  "sandbox": "base-analyst",
  "items": [
    {"prompt": "Analyse region North", "env_vars": {"REGION": "north"}},
    {"prompt": "Analyse region South", "env_vars": {"REGION": "south"}, "memory": "8g"}
  ]
}
```

See [Combinators](combinators.md) for full `par` and `map` documentation.

---

## Security Considerations

!!! warning "Network access"
    `network: true` (the default) allows the container to make arbitrary outbound connections. Set `network: false` for agents that should only process local data.

!!! warning "Mounts"
    Be cautious about mounting host directories with write access. Prefer `"readonly": true` for input data and use `input_files` for small configuration payloads.

See the [Security](../security.md) page for a full discussion of isolation boundaries.

---

## Related Pages

- [Resources](resources.md) — GPU and named resource pool configuration (`gpu`, `resources`)
- [Types](types.md) — `input_type` and `output_type` fields
- [Pipelines](pipelines.md) — pipeline-level sandbox as a base spec for all steps
- [Combinators](combinators.md) — `run`, `par`, `map` call signatures
