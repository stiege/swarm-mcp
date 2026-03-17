# Pipelines

A **pipeline** is a sequence of agent steps that executes as a single, resumable, cost-tracked unit. Pipelines model multi-stage workflows — test/fix loops, summarise-then-verify chains, ETL transformations — without requiring you to write any glue code.

Under the hood the pipeline interpreter is a **free monad**: the JSON definition is pure data that describes a computation, and the interpreter decides at runtime how to sequence, branch, retry, and resume each step. This separation means you can store, version, and load pipeline definitions from files, pass them as strings, or generate them programmatically.

---

## The `pipeline` Tool

```
pipeline(definition: str, resume?: str) → PipelineResult
```

`definition` is either:

- A **JSON string** containing a pipeline object (inline), or
- A **pipeline name** that is looked up in the registry (`pipelines/<name>.json` in the project directory or `~/.claude/pipelines/`).

`resume` is optional — see [Resume Support](#resume-support).

---

## Pipeline Object

```json
{
  "name": "optional-name",
  "sandbox": { ... },
  "budget": 2.50,
  "deadline_seconds": 300,
  "classification": "internal",
  "steps": [ ... ]
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | — | Human-readable label, also used as registry key |
| `sandbox` | object | — | Base [SandboxSpec](sandboxes.md) applied to every step (step-level fields override) |
| `budget` | number | unlimited | Maximum USD to spend across all steps; stops pipeline when reached |
| `deadline_seconds` | number | unlimited | Wall-clock time limit for the whole pipeline |
| `classification` | string | — | Informational tag passed through to results |
| `steps` | array | required | Ordered list of step objects |

---

## Step Fields

Each step is a JSON object. `id` and `prompt` are always required; all other fields are optional.

### Identity & Prompt

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | string | required | Unique identifier within the pipeline; used by `on_fail`, `next`, `retry_if` |
| `prompt` | string | required | Instruction sent to the agent for this step |

### Control Flow

| Field | Type | Default | Description |
|---|---|---|---|
| `on_fail` | string | stop | Step `id` to jump to when this step fails (non-zero exit or error text) |
| `next` | string | next in array | Step `id` to jump to after this step succeeds |
| `condition` | string | — | `"prev.error"` — only execute this step if the **previous** step failed |
| `max_retries` | integer | 3 | How many times to retry this step before calling it a failure |
| `retry_if` | object | — | `{"target_step": "keyword"}` — retry `target_step` if `keyword` found in this step's output |

### Sandbox Overrides

Any field from [SandboxSpec](sandboxes.md) can be placed directly on a step and will override the pipeline-level sandbox for that step only. Common examples:

| Field | Type | Description |
|---|---|---|
| `model` | string | Claude model to use (e.g. `"opus"`, `"sonnet"`) |
| `tools` | array | MCP/built-in tools available to the agent |
| `system_prompt` | string | Additional system context |
| `timeout` | integer | Per-step timeout in seconds |
| `memory` | string | Docker memory limit (e.g. `"4g"`) |
| `gpu` | boolean | Whether to acquire a GPU resource slot |

---

## Context Passing Between Steps

Every step automatically receives:

1. The **previous step's output text** as additional context in its prompt.
2. If the previous step failed, an **error context block** is injected so the current step knows what went wrong.

This means steps naturally chain: a summariser can read the analyst's output, a reviewer can read the writer's draft, and a fixer can read the tester's error output — all without writing any data-passing code.

### The `/shared/` Directory

For binary data, large files, or structured artifacts that shouldn't be embedded in prompts, steps share a filesystem directory.

- Host path: `/tmp/swarm-mcp/<run_id>/shared/`
- Container path: `/shared/` (mounted into every step container)

A step that produces a file writes it to `/shared/output.json`; the next step reads it from the same path. The directory persists for the life of the pipeline run and is reused when [resuming](#resume-support).

```json
{
  "id": "generate",
  "prompt": "Analyse the dataset and write results to /shared/results.json"
},
{
  "id": "visualise",
  "prompt": "Read /shared/results.json and produce a summary table"
}
```

---

## Control Flow

### `on_fail` — Error Branching

```json
{
  "id": "run-tests",
  "prompt": "Run the test suite and report failures",
  "on_fail": "fix-code"
},
{
  "id": "fix-code",
  "prompt": "Fix the test failures reported above",
  "condition": "prev.error"
}
```

When `run-tests` fails, the interpreter jumps to `fix-code` instead of stopping. Without `on_fail`, any failure halts the pipeline immediately.

### `next` — Unconditional Jump

```json
{
  "id": "fast-check",
  "prompt": "Run lint only",
  "next": "deploy",
  "on_fail": "full-test"
}
```

On success, `fast-check` skips directly to `deploy`, bypassing any steps in between.

### `condition: "prev.error"` — Conditional Execution

A step with `condition: "prev.error"` is **skipped** unless the immediately preceding step produced an error. This lets you place remediation steps inline without them running on the happy path.

### `max_retries` and `retry_if`

```json
{
  "id": "call-api",
  "prompt": "Fetch the report from the external API and save to /shared/report.json",
  "max_retries": 5
},
{
  "id": "validate-report",
  "prompt": "Check /shared/report.json for completeness",
  "retry_if": {"call-api": "incomplete"}
}
```

- `max_retries` caps how many times the interpreter retries a step that fails (default: 3).
- `retry_if`: if `validate-report`'s output contains the word `"incomplete"`, the interpreter goes back and reruns `call-api` (subject to that step's own `max_retries`).

---

## Budget Tracking

When `budget` is set, the interpreter accumulates the USD cost of every step. Before launching each step it checks:

```
spent_so_far >= budget_limit  →  stop pipeline, mark budget_exceeded
```

The final result includes `total_cost_usd` and a `budget` object showing limit, spent, and whether the limit was hit.

!!! warning "Budget is a guardrail, not a hard cap"
    The check happens *before* each step starts. A single expensive step can still exceed the budget if it runs to completion after passing the pre-step check.

---

## Deadline Tracking

When `deadline_seconds` is set:

- Before each step the interpreter checks elapsed time. If the deadline has passed, the pipeline stops with `deadline_met: false`.
- Each step's `timeout` is also **clamped** to the remaining wall-clock time, so a step cannot outlive the pipeline deadline even if its own timeout is longer.

The final result always includes `deadline_met` (boolean) and `total_duration_seconds`.

---

## Resume Support

Pipelines can be resumed after interruption or failure.

```
pipeline(definition="my-pipeline", resume="<run_id>")
```

This reuses the `/shared/` directory from the original run, so files produced by completed steps are still available.

To skip directly to a specific step:

```
pipeline(definition="my-pipeline", resume="<run_id>/<step_id>")
```

The interpreter fast-forwards to `step_id` and continues from there. Steps before that point are not re-executed, but their shared-directory outputs remain accessible.

!!! tip "Finding run IDs"
    The `run_id` is returned in every pipeline result object under the `run_id` key. Store it if you anticipate needing to resume.

---

## Storing Pipelines in the Registry

Rather than passing large JSON strings to every call, save pipeline definitions to files:

```
~/.claude/pipelines/my-pipeline.json
<project>/pipelines/my-pipeline.json
```

Then reference by name:

```
pipeline(definition="my-pipeline")
```

The registry searches the project directory first, then `~/.claude/pipelines/`, so project-specific pipelines can shadow global ones.

---

## Annotated Example: Test-Fix Loop

This five-step pipeline runs tests, attempts an automatic fix on failure, re-runs the tests to verify the fix, and finally generates a coverage report. It demonstrates `on_fail`, `condition`, `next`, `retry_if`, and the shared directory.

```json
{
  "name": "test-fix-loop",
  "budget": 3.00,
  "deadline_seconds": 600,
  "sandbox": {
    "model": "sonnet",
    "tools": ["Read", "Write", "Bash"],
    "workdir": "/workspace"
  },
  "steps": [
    {
      "id": "run-tests",
      "prompt": "Run `pytest -x` and write the full output to /shared/test-output.txt. Exit with an error if any tests fail.",
      "on_fail": "auto-fix",
      "max_retries": 1
    },
    {
      "id": "auto-fix",
      "prompt": "Read /shared/test-output.txt. Identify the root cause and apply the minimal code change to fix the failing test. Write a brief explanation to /shared/fix-summary.txt.",
      "condition": "prev.error",
      "on_fail": "report-failure",
      "next": "verify-fix"
    },
    {
      "id": "verify-fix",
      "prompt": "Run `pytest -x` again and write output to /shared/verify-output.txt. The fix from the previous step should make the suite pass.",
      "on_fail": "report-failure",
      "retry_if": {"auto-fix": "still failing"},
      "next": "coverage"
    },
    {
      "id": "coverage",
      "prompt": "Run `pytest --cov` and write the coverage summary to /shared/coverage.txt. Return the overall coverage percentage.",
      "model": "haiku"
    },
    {
      "id": "report-failure",
      "prompt": "Read /shared/test-output.txt and /shared/fix-summary.txt if it exists. Write a concise failure report explaining what was attempted and what still fails.",
      "condition": "prev.error"
    }
  ]
}
```

### Walk-through

1. **`run-tests`** — runs the suite. If it passes, execution falls through to `auto-fix`... but wait, `auto-fix` has `condition: "prev.error"`, so it is **skipped**, and the interpreter continues to `verify-fix`.

    !!! note
        When `on_fail` is set and the step *succeeds*, the normal `next` logic applies. The step after `run-tests` in array order is `auto-fix`, which is skipped due to its `condition`, so control reaches `verify-fix`.

2. **`auto-fix`** — only runs if `run-tests` failed (via `on_fail` jump + `condition: "prev.error"`). On success it jumps to `verify-fix` via `next`.

3. **`verify-fix`** — re-runs the suite. If the fix output contains `"still failing"`, `retry_if` sends execution back to `auto-fix` for another attempt. On success, jumps to `coverage`.

4. **`coverage`** — uses the cheaper `haiku` model since this is a read-and-summarise task. Overrides the pipeline-level `model` for this step only.

5. **`report-failure`** — only executes if `verify-fix` failed (or any earlier step routed here via `on_fail`). Produces a human-readable summary.

---

## Return Value

```json
{
  "run_id": "a3f9c2d1",
  "steps_executed": 4,
  "total_steps": 5,
  "final": "Coverage: 87%",
  "all_results": { "run-tests": {...}, "coverage": {...} },
  "total_cost_usd": 0.42,
  "total_duration_seconds": 118,
  "budget": {"limit": 3.00, "spent": 0.42, "exceeded": false},
  "deadline_met": true
}
```

| Field | Description |
|---|---|
| `run_id` | Unique identifier for this run; use for resume |
| `steps_executed` | Number of steps that actually ran |
| `total_steps` | Total steps in the definition |
| `final` | Text output of the last step that executed |
| `all_results` | Map of step id → full result object |
| `total_cost_usd` | Aggregate cost across all steps |
| `total_duration_seconds` | Wall-clock time for the whole pipeline |
| `budget` | Limit, amount spent, and whether the limit was exceeded |
| `deadline_met` | `false` if the pipeline was stopped by the deadline |

---

## Related Pages

- [Sandboxes](sandboxes.md) — configure the Docker environment for each step
- [Resources](resources.md) — GPU and named resource pools within pipeline steps
- [Combinators](combinators.md) — `par` and `map` for fan-out within a step
- [Types](types.md) — validate step outputs against type definitions
