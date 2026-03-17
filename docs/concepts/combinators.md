# Combinators

Combinators are the building blocks of swarm-mcp pipelines. Each one wraps one or more agent executions with a specific coordination pattern — fan-out, sequencing, synthesis, filtering, racing, retrying, or guarding — and returns a structured result containing [refs](refs.md) that carry full metadata lineage.

---

## Combinator Reference

| Combinator | Pattern | Returns | Use when… |
|---|---|---|---|
| `run` | Single agent | ref dict | You need one agent to do one thing |
| `par` | Parallel, heterogeneous | refs array | Tasks differ in prompt or config |
| `map` | Parallel, template-driven | refs array | Same prompt shape, many inputs |
| `chain` | Sequential pipeline | staged refs | Each stage depends on the prior output |
| `reduce` | N-to-1 synthesis | single ref | Merge many results into one |
| `map_reduce` | Fan-out + synthesis | single ref | Process many inputs, then summarize |
| `filter` | Type-gate validation | valid refs only | Remove bad outputs before downstream use |
| `race` | Speculative execution | first winner | Latency matters more than cost |
| `retry` | Auto-retry with context | ref or error | Flaky tasks or strict output types |
| `guard` | Enforce monad conditions | ref or error | Policy enforcement at pipeline boundaries |

---

## run

**Single agent execution.** Runs one Claude agent in an isolated Docker container and returns a ref.

### Signature

```
run(
  prompt,
  sandbox?,        # "docker" | "none"
  network?,        # "none" | "bridge" | ...
  tools?,          # list of allowed tool names
  mounts?,         # host paths to bind-mount
  model?,          # claude model string
  timeout?,        # seconds
  system_prompt?,
  claude_md?,      # content for CLAUDE.md
  output_schema?,  # JSON schema for structured output
  mcps?,           # MCP servers to attach
  effort?,         # "low" | "medium" | "high"
  max_budget?,     # USD ceiling
  env_vars?,
  input_files?,
  memory?,         # container memory limit
  cpus?,
  gpu?,
  resources?,
  input_type?,
  output_type?
) -> ref dict (JSON)
```

### Example

```json
{
  "tool": "run",
  "arguments": {
    "prompt": "Read /data/sales.csv and return the top 5 products by revenue as JSON.",
    "model": "claude-opus-4-5",
    "output_schema": "{\"type\":\"array\",\"items\":{\"type\":\"object\"}}",
    "mounts": ["/data:/data:ro"],
    "timeout": 60
  }
}
```

**Returns:**

```json
{
  "agent_id": "agent-3f7a",
  "ref": "run-9b2c/agent-3f7a",
  "exit_code": 0,
  "duration_seconds": 11.4,
  "cost_usd": 0.0038,
  "model": "claude-opus-4-5",
  "output_dir": "/tmp/swarm-mcp/run-9b2c/agent-3f7a",
  "error": null
}
```

!!! tip "When to use"
    Use `run` for any task that stands alone. For tasks that need to be repeated across many inputs, use `map`. For tasks that must execute in sequence, use `chain`.

---

## par

**Parallel execution with per-task configuration.** Runs an array of independent tasks concurrently and returns all refs.

### Signature

```
par(
  tasks,            # JSON array of task objects
  max_concurrency?  # default 5
) -> {run_id, total, succeeded, failed, results: [refs]}
```

Each task object can specify its own `prompt`, `model`, `tools`, `timeout`, `system_prompt`, `sandbox`, `network`, `mounts`, `mcps`, `output_schema`, and `effort`.

### Example

```json
{
  "tool": "par",
  "arguments": {
    "tasks": [
      {
        "prompt": "Summarize Q1 earnings report.",
        "model": "claude-haiku-4-5",
        "timeout": 30
      },
      {
        "prompt": "Summarize Q2 earnings report.",
        "model": "claude-haiku-4-5",
        "timeout": 30
      },
      {
        "prompt": "Identify YoY trends across all quarters.",
        "model": "claude-opus-4-5",
        "timeout": 90
      }
    ],
    "max_concurrency": 3
  }
}
```

**Returns:**

```json
{
  "run_id": "run-9b2c",
  "total": 3,
  "succeeded": 3,
  "failed": 0,
  "results": [ ...refs... ]
}
```

!!! tip "When to use"
    Use `par` when tasks differ from each other in prompt or configuration. When tasks share the same prompt template but differ only in input data, `map` is more concise.

---

## map

**Template-driven fan-out.** Applies a prompt template containing `{input}` to every item in an inputs array, running all instantiations in parallel.

### Signature

```
map(
  prompt_template,   # string with {input} placeholder
  inputs,            # JSON array of input values
  sandbox?,
  network?,
  tools?,
  model?,
  timeout?,
  max_concurrency?,  # default 5
  system_prompt?,
  claude_md?,
  output_schema?,
  mcps?,
  effort?
) -> {run_id, total, succeeded, failed, results: [refs]}
```

### Example

```json
{
  "tool": "map",
  "arguments": {
    "prompt_template": "Classify the following customer review as POSITIVE, NEUTRAL, or NEGATIVE. Reply with one word only.\n\nReview: {input}",
    "inputs": [
      "Arrived two days late and the packaging was damaged.",
      "Exactly what I ordered. Fast shipping.",
      "Works fine but the manual is hard to follow."
    ],
    "model": "claude-haiku-4-5",
    "output_schema": "{\"type\":\"string\",\"enum\":[\"POSITIVE\",\"NEUTRAL\",\"NEGATIVE\"]}",
    "max_concurrency": 10
  }
}
```

!!! tip "When to use"
    Use `map` when the task is uniform across inputs — classification, extraction, translation, summarization at scale. Follow with `reduce` (or use `map_reduce`) when you need to synthesize the results.

---

## chain

**Sequential pipeline.** Executes stages one after another. Each stage receives the text output of the previous stage injected into its prompt context automatically.

### Signature

```
chain(
  stages   # JSON array of stage objects
) -> {run_id, completed_stages, total_stages, final: ref, intermediates: [refs]}
```

Each stage object supports: `prompt`, `model`, `tools`, `timeout`, `system_prompt`, `sandbox`, `network`, `mounts`, `mcps`, `output_schema`, `effort`.

### Example

```json
{
  "tool": "chain",
  "arguments": {
    "stages": [
      {
        "prompt": "Extract all action items from this meeting transcript:\n\n{transcript}",
        "model": "claude-haiku-4-5"
      },
      {
        "prompt": "For each action item, assign a priority (HIGH/MEDIUM/LOW) and an estimated effort in hours.",
        "model": "claude-opus-4-5"
      },
      {
        "prompt": "Format the prioritized action items as a Markdown table sorted by priority.",
        "model": "claude-haiku-4-5"
      }
    ]
  }
}
```

**Returns:**

```json
{
  "run_id": "run-4d8e",
  "completed_stages": 3,
  "total_stages": 3,
  "final": { ...ref for stage 3... },
  "intermediates": [ ...refs for stages 1 and 2... ]
}
```

!!! tip "When to use"
    Use `chain` when each step requires reasoning about the previous step's output. The `intermediates` array gives you full auditability of every stage, even if you only need `final`.

!!! warning "Not for independent tasks"
    If stages do not depend on each other's output, use `par` instead — it runs them in parallel and returns faster.

---

## reduce

**Synthesize N results into one.** Takes an array of strings or refs (or a mix of both), unwraps their text, and passes everything to a single synthesis agent.

### Signature

```
reduce(
  results,           # JSON array of strings or ref dicts
  synthesis_prompt,  # instruction for the synthesis agent
  sandbox?,
  network?,
  tools?,
  model?,
  timeout?,
  system_prompt?,
  mcps?
) -> {run_id, input_count, result: ref}
```

### Example

```json
{
  "tool": "reduce",
  "arguments": {
    "results": [
      { "ref": "run-9b2c/agent-3f7a" },
      { "ref": "run-9b2c/agent-4a8b" },
      "Additional context: the product launches in Q3."
    ],
    "synthesis_prompt": "You have received analysis from multiple agents. Synthesize their findings into a single executive summary of no more than 200 words.",
    "model": "claude-opus-4-5"
  }
}
```

!!! tip "When to use"
    Use `reduce` after a `par` or `map` call when you need a single cohesive output. It handles mixed input arrays (refs and plain strings) transparently via `_extract_texts()`.

---

## map_reduce

**Fan-out and synthesize in one call.** Equivalent to `map` followed by `reduce`, but expressed as a single combinator. All intermediate refs are produced and then synthesized without a manual intermediate step.

### Signature

```
map_reduce(
  prompt_template,       # string with {input} placeholder
  inputs,                # JSON array of input values
  synthesis_prompt,
  sandbox?,
  network?,
  tools?,
  model?,
  timeout?,
  system_prompt?,
  claude_md?,
  output_schema?,
  mcps?,
  effort?,
  reduce_model?,         # separate model for the synthesis step
  reduce_system_prompt?
) -> {run_id, input_count, result: ref}
```

### Example

```json
{
  "tool": "map_reduce",
  "arguments": {
    "prompt_template": "Identify the main security vulnerabilities in this code snippet:\n\n{input}",
    "inputs": [
      "def login(user, pwd):\n    return db.execute(f'SELECT * FROM users WHERE user={user}')",
      "const token = req.cookies.session_token;",
      "subprocess.run(user_input, shell=True)"
    ],
    "synthesis_prompt": "Consolidate the vulnerability findings into a prioritized remediation checklist.",
    "model": "claude-haiku-4-5",
    "reduce_model": "claude-opus-4-5"
  }
}
```

!!! tip "When to use"
    Use `map_reduce` when the map and reduce steps belong together conceptually and you don't need to inspect intermediate refs. Use separate `map` + `reduce` calls when you want to inspect, filter, or transform intermediate results between steps.

---

## filter

**Type-gate validation.** Validates each ref in an input array against a declared output type, in parallel, and returns only the refs that pass. Failed refs are collected in `rejected`.

### Signature

```
filter(
  refs,           # JSON array of ref dicts
  declared_type,  # expected output type string
  model?,
  timeout?
) -> {run_id, total, valid, invalid, results: [valid refs], rejected: [invalid refs]}
```

### Example

```json
{
  "tool": "filter",
  "arguments": {
    "refs": [ ...array of refs from a prior map call... ],
    "declared_type": "json",
    "model": "claude-haiku-4-5",
    "timeout": 20
  }
}
```

**Returns:**

```json
{
  "run_id": "run-7c3a",
  "total": 10,
  "valid": 8,
  "invalid": 2,
  "results": [ ...8 refs with validation_verdict: "VALID"... ],
  "rejected": [ ...2 refs with validation_verdict: "INVALID"... ]
}
```

Each ref in `results` and `rejected` will have `validated_as`, `validation_verdict`, and `validation_ref` stamped by the Validated monad layer.

!!! tip "When to use"
    Use `filter` between `map` (or `par`) and `reduce` when the map step produces structured output that must conform to a schema before synthesis. It prevents malformed results from corrupting a downstream `reduce`.

---

## race

**Speculative execution.** Runs all tasks in parallel and returns the first one that succeeds, cancelling the rest. Useful when latency is the primary concern and redundant work is acceptable.

### Signature

```
race(
  tasks,           # JSON array of task objects (same as par)
  max_concurrency? # default 5
) -> {run_id, winner: ref, attempted, failed}
```

### Example

```json
{
  "tool": "race",
  "arguments": {
    "tasks": [
      {
        "prompt": "Generate a one-paragraph product description for item SKU-881.",
        "model": "claude-haiku-4-5"
      },
      {
        "prompt": "Generate a one-paragraph product description for item SKU-881.",
        "model": "claude-haiku-4-5"
      },
      {
        "prompt": "Generate a one-paragraph product description for item SKU-881.",
        "model": "claude-opus-4-5"
      }
    ]
  }
}
```

**Returns:**

```json
{
  "run_id": "run-2e5f",
  "winner": { ...ref of the first successful agent... },
  "attempted": 3,
  "failed": 0
}
```

!!! tip "When to use"
    Use `race` for user-facing tasks where tail latency is painful. Running 2-3 identical agents and taking the first response cuts p99 latency significantly. Also useful for tasks with non-deterministic success rates.

!!! warning "Cost implications"
    `race` runs all tasks regardless of which wins. You pay for every agent that starts before a winner is declared. Budget accordingly.

---

## retry

**Auto-retry with error context injection.** Re-runs a task up to `max_attempts` times on failure. Each retry receives the errors from all prior attempts injected as context, giving the agent information it can use to self-correct.

### Signature

```
retry(
  prompt,
  max_attempts?,   # default 3
  sandbox?,
  model?,
  timeout?,
  declared_type?,  # if set, also retries on type validation failure
  mcps?
) -> ref (on success) | error dict (if all attempts fail)
```

### Example

```json
{
  "tool": "retry",
  "arguments": {
    "prompt": "Call the payments API and return the transaction status for order #TXN-4492 as JSON with fields: order_id, status, amount_usd.",
    "max_attempts": 4,
    "declared_type": "json",
    "model": "claude-opus-4-5",
    "timeout": 45
  }
}
```

On attempt 2, the agent's prompt will include:

```
Prior attempt 1 failed with error: JSONDecodeError — the response contained markdown fences around the JSON. Please return raw JSON only, with no surrounding text.
```

Each ref produced by a retry attempt will have `attempt`, `max_retries`, and `prior_errors` stamped by the Retried monad layer.

!!! tip "When to use"
    Use `retry` for any task that calls external APIs, performs file I/O, or must produce structured output. Combining `retry` with `declared_type` is the most reliable way to get well-formed JSON or code from an agent.

---

## guard

**Enforce monadic conditions.** Inspects a ref's monad-layer metadata and either passes the ref through unchanged or returns an error. Use at pipeline boundaries to enforce policies before passing results to downstream systems.

### Signature

```
guard(
  ref,     # a ref dict
  check,   # "validated" | "budget" | "classification" | "encrypted" | "exists"
  value?   # check-specific comparison value
) -> ref (if guard passes) | error dict
```

### Checks

| Check | What it enforces | `value` meaning |
|---|---|---|
| `validated` | `validation_verdict == "VALID"` | Optional: expected `validated_as` type |
| `budget` | `remaining >= 0` (not over budget) | Optional: minimum required remaining USD |
| `classification` | `level_numeric <= threshold` | Required: maximum allowed `level_numeric` |
| `encrypted` | `key_id` and `algorithm` are present | Optional: required `key_id` |
| `exists` | `output_dir` is on disk and non-empty | — |

### Example: Validate before delivery

```json
{
  "tool": "guard",
  "arguments": {
    "ref": { "ref": "run-9b2c/agent-3f7a", "validated_as": "json", "validation_verdict": "VALID" },
    "check": "validated",
    "value": "json"
  }
}
```

### Example: Budget check before an expensive stage

```json
{
  "tool": "guard",
  "arguments": {
    "ref": { "ref": "run-9b2c/agent-3f7a", "remaining": 0.42, "limit": 1.00 },
    "check": "budget",
    "value": "0.25"
  }
}
```

### Example: Enforce classification ceiling

```json
{
  "tool": "guard",
  "arguments": {
    "ref": { "ref": "run-9b2c/agent-3f7a", "level": "internal", "level_numeric": 2 },
    "check": "classification",
    "value": "3"
  }
}
```

!!! tip "When to use"
    Insert `guard` calls at decision points in multi-stage pipelines: before passing results to an external system, before an expensive synthesis step, or wherever a policy violation should halt execution rather than produce a silent bad result.

!!! note "Composing guards"
    `guard` returns the ref unchanged on success, so you can chain multiple guards in sequence — `guard` for `validated`, then `guard` for `budget`, then `guard` for `encrypted` — before passing a ref to a final delivery step.
