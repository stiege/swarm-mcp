# Refs & The Monad Stack

A **ref** is the fundamental unit of data exchange in swarm-mcp. Instead of passing raw text between combinators, every agent execution produces a ref — a lightweight pointer to results stored on disk, enriched with metadata that travels alongside the data.

---

## Why Refs Instead of Raw Text?

**Lazy evaluation.** Agent output — which can be thousands of tokens — stays on disk until something explicitly needs it. Combinators like `chain`, `reduce`, and `filter` can inspect metadata, route, validate, and transform without deserializing large payloads through the MCP protocol.

**Composability.** A ref returned by `run` can be handed directly to `guard`, `filter`, `reduce`, or used as context in a `chain` stage. The downstream combinator decides when (and whether) to unwrap. This keeps pipeline definitions concise and avoids redundant data copies.

**Safety.** Every ref carries the metadata needed to reason about the result: who produced it, what it cost, whether it passed type validation, what security classification it carries, and whether it was encrypted. That metadata is never separated from the result because it is part of the ref.

---

## Ref Anatomy

A ref is a flat Python dict (serialized to JSON at the MCP boundary). The core fields produced by every `run` call are:

```json
{
  "agent_id": "agent-3f7a",
  "ref": "run-9b2c/agent-3f7a",
  "exit_code": 0,
  "duration_seconds": 14.3,
  "cost_usd": 0.0041,
  "model": "claude-opus-4-5",
  "output_dir": "/tmp/swarm-mcp/run-9b2c/agent-3f7a",
  "error": null
}
```

| Field | Type | Description |
|---|---|---|
| `agent_id` | string | Unique identifier for this agent execution |
| `ref` | string | Canonical address: `run_id/agent_id` — used to resolve content from disk |
| `exit_code` | int | 0 = success, non-zero = failure |
| `duration_seconds` | float | Wall-clock time for the agent run |
| `cost_usd` | float | Token cost for this execution |
| `model` | string | Claude model variant used |
| `output_dir` | string | Absolute path to the agent's output directory |
| `error` | string or null | Error message if `exit_code != 0` |

The `ref` field (`run_id/agent_id`) is the stable address used by `_resolve_ref()` to load the agent's text output from disk without carrying that text in the ref dict itself.

---

## What Lives on Disk

Each agent execution creates a directory at `/tmp/swarm-mcp/{run_id}/{agent_id}/` containing five files:

### `result.json`

The full serialized result from the Claude SDK, including the complete message history, tool calls, and metadata. This is the authoritative source of truth. `_resolve_ref()` reads this file when a combinator needs the agent's text output.

### `stream.jsonl`

A newline-delimited JSON log of every event emitted by the Claude streaming API during the run. Useful for debugging token-by-token what the agent did and when.

### `artifacts.jsonl`

Logs written by the `PostToolUse` hook. Every tool call the agent made (bash, computer, file read/write, MCP calls) is recorded here with its inputs and outputs. Use this to audit exactly what side effects the agent produced.

### `output.md`

The agent's final text response, unwrapped from the SDK result structure and written as plain Markdown. Human-readable; useful for quick inspection without parsing JSON.

### `prompt.txt`

The original prompt sent to the agent. Stored so that you can reconstruct exactly what the agent was asked, independent of the calling context.

!!! tip "Inspecting a run"
    After a `run` or `par` call, you can browse `/tmp/swarm-mcp/{run_id}/` to inspect every agent's artifacts, stream, and output without any additional tool calls.

---

## The Monad Stack

After a raw ref is produced by an agent execution, `enrich_ref()` applies a stack of **monad-style stamps** — each one adds a new layer of metadata without mutating the fields already present. You can think of each stamp as wrapping the ref in a new context that downstream combinators can inspect and enforce.

The stamps are applied in this order:

### 1. Provenance

```python
stamp_provenance(ref, parent_refs, content_hash, timestamp)
```

Stamps where this result came from and what it contains.

| Field added | Description |
|---|---|
| `parent_refs` | List of ref addresses that fed into this result (e.g., in a `chain` stage) |
| `content_hash` | SHA-256 of the output text — enables deduplication and integrity checks |
| `timestamp` | ISO-8601 wall-clock time the result was produced |

### 2. Cost

```python
stamp_cost(ref, step_cost, budget_tracker)
```

Stamps financial accounting for the run and its context.

| Field added | Description |
|---|---|
| `step_cost` | Cost of this specific agent execution in USD |
| `spent_so_far` | Cumulative cost across the current run or pipeline |
| `remaining` | Budget remaining (`limit - spent_so_far`) |
| `limit` | The `max_budget` ceiling set by the caller |

### 3. Time

```python
stamp_deadline(ref, deadline)
```

Stamps time-awareness so downstream combinators can abort work that has already missed its window.

| Field added | Description |
|---|---|
| `deadline` | Unix timestamp after which this result (or dependent work) is stale |
| `time_remaining` | Seconds between stamp time and the deadline |

### 4. Validated

```python
stamp_validated(ref, validated_as, verdict, validation_ref)
```

Stamps the result of type-gate validation (used by `filter` and `retry`).

| Field added | Description |
|---|---|
| `validated_as` | The declared output type that was checked (e.g., `"json"`, `"python"`) |
| `validation_verdict` | `"VALID"` or `"INVALID"` |
| `validation_ref` | Ref address of the validator agent's own result, for auditability |

### 5. Retried

```python
stamp_retry(ref, attempt, max_retries, prior_errors)
```

Stamps retry state so the agent receiving a re-run knows its history.

| Field added | Description |
|---|---|
| `attempt` | Current attempt number (1-indexed) |
| `max_retries` | Maximum attempts allowed |
| `prior_errors` | List of error messages from previous failed attempts |

### 6. Classified

```python
stamp_classification(ref, level, allowed_mcps, denied_mcps)
```

Stamps security classification so `guard` can enforce access control policies.

| Field added | Description |
|---|---|
| `level` | Human-readable classification label (e.g., `"confidential"`) |
| `level_numeric` | Numeric equivalent for comparison operators |
| `allowed_mcps` | List of MCP server names this result may flow to |
| `denied_mcps` | List of MCP server names this result must not reach |

### 7. Encrypted

```python
stamp_encrypted(ref, key_id, algorithm)
```

Stamps encryption provenance so consumers know whether they need to decrypt before use.

| Field added | Description |
|---|---|
| `key_id` | Identifier of the key used to encrypt the payload |
| `algorithm` | Encryption algorithm (e.g., `"AES-256-GCM"`) |

---

## A Fully Enriched Ref

After the full monad stack has been applied, a ref might look like this:

```json
{
  "agent_id": "agent-3f7a",
  "ref": "run-9b2c/agent-3f7a",
  "exit_code": 0,
  "duration_seconds": 14.3,
  "cost_usd": 0.0041,
  "model": "claude-opus-4-5",
  "output_dir": "/tmp/swarm-mcp/run-9b2c/agent-3f7a",
  "error": null,

  "parent_refs": ["run-8a1b/agent-0c9d"],
  "content_hash": "e3b0c44298fc1c149afb4c8996fb924...",
  "timestamp": "2026-03-17T14:22:05Z",

  "step_cost": 0.0041,
  "spent_so_far": 0.0312,
  "remaining": 0.9688,
  "limit": 1.00,

  "deadline": 1742221200,
  "time_remaining": 3595.2,

  "validated_as": "json",
  "validation_verdict": "VALID",
  "validation_ref": "run-9b2c/validator-agent-7e2f",

  "attempt": 2,
  "max_retries": 3,
  "prior_errors": ["JSONDecodeError: Expecting value at line 1"],

  "level": "internal",
  "level_numeric": 2,
  "allowed_mcps": ["filesystem", "github"],
  "denied_mcps": ["web-search"],

  "key_id": "key-prod-2026-03",
  "algorithm": "AES-256-GCM"
}
```

Not every ref will have all layers — stamps are applied selectively based on what the calling combinator configured.

---

## Unwrap vs Pass

Most of the time you should **pass refs between combinators** and let the pipeline stay lazy:

```json
// chain: pass the ref from stage 1 as context into stage 2
// stage 2 receives the text automatically — you never unwrap manually
{
  "stages": [
    { "prompt": "Analyze this dataset and identify anomalies." },
    { "prompt": "Write a remediation plan for the anomalies above." }
  ]
}
```

**Unwrap** (call `_resolve_ref()` / read `output.md`) when:

- You need to display final output to a user
- You are passing text into a non-swarm-mcp system that does not understand refs
- You are building a custom combinator that must operate on the actual content

!!! warning "Don't unwrap prematurely"
    Unwrapping inside a pipeline defeats lazy evaluation. If you find yourself extracting text from a ref only to pass it into another `run` call, use `chain` or `reduce` instead — they handle the unwrap internally and maintain full metadata lineage.

!!! note "Mixed inputs"
    `_extract_texts()` handles mixed input arrays that contain plain strings, dicts with a `.text` field, and refs with a `.ref` field. `reduce` and `map_reduce` use this automatically, so you can freely mix literal text and refs in the same input list.
