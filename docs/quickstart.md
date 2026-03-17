# Quickstart

This guide gets you from zero to a working multi-agent pipeline in about ten minutes. All examples assume swarm-mcp is installed and connected to Claude Code.

---

## Your First Agent

`run()` launches a single agent in a Docker container and returns a ref. The ref is a small metadata dict — it does not contain the output text yet.

```
run(
  prompt="Summarise the CAP theorem in exactly two sentences.",
  sandbox={"model": "sonnet"}
)
```

**Example response from Claude Code:**

```json
{
  "run_id": "r-abc123",
  "agent_id": "a-001",
  "status": "complete"
}
```

That ref is the handle you pass to other combinators or to `unwrap()`.

!!! tip "Models"
    The `model` field in `sandbox` defaults to `"sonnet"`. Use `"opus"` for harder reasoning tasks or `"haiku"` for fast, cheap classification work.

---

## Reading Results with unwrap()

`unwrap()` takes a ref (or a list of refs) and returns the agent's text output.

```
unwrap(ref={"run_id": "r-abc123", "agent_id": "a-001"})
```

**Returned text:**

```
The CAP theorem states that a distributed system can guarantee at most two of the
following three properties: consistency, availability, and partition tolerance.
In practice, network partitions are unavoidable, so system designers must choose
between consistency and availability during a partition event.
```

!!! tip "Batch unwrap"
    Pass a list of refs to `unwrap()` to materialise multiple results in one call. The order of returned texts matches the order of the input refs.

---

## Debugging with inspect()

`inspect()` shows the full metadata for a ref: status, cost, timing, and a preview of the output without fetching the full text.

```
inspect(ref={"run_id": "r-abc123", "agent_id": "a-001"})
```

**Example response:**

```json
{
  "run_id": "r-abc123",
  "agent_id": "a-001",
  "status": "complete",
  "elapsed_seconds": 4.2,
  "cost_usd": 0.0031,
  "output_preview": "The CAP theorem states that..."
}
```

Use `inspect()` before `unwrap()` when you want to confirm an agent finished successfully or to check costs before materialising a large batch.

---

## Your First Parallel Job

`par()` launches multiple agents simultaneously and returns a list of refs, one per task. The agents run in parallel inside separate containers.

```
par(tasks=[
  {
    "prompt": "List three advantages of PostgreSQL over MySQL.",
    "sandbox": {"model": "haiku"}
  },
  {
    "prompt": "List three advantages of MySQL over PostgreSQL.",
    "sandbox": {"model": "haiku"}
  },
  {
    "prompt": "When would you choose SQLite over either?",
    "sandbox": {"model": "haiku"}
  }
])
```

**Returned refs:**

```json
[
  {"run_id": "r-def456", "agent_id": "a-001"},
  {"run_id": "r-def456", "agent_id": "a-002"},
  {"run_id": "r-def456", "agent_id": "a-003"}
]
```

Unwrap all three at once:

```
unwrap(refs=[
  {"run_id": "r-def456", "agent_id": "a-001"},
  {"run_id": "r-def456", "agent_id": "a-002"},
  {"run_id": "r-def456", "agent_id": "a-003"}
])
```

!!! tip "Fan-out over a list"
    Use `map()` instead of `par()` when you have a list of inputs and one prompt template. `map()` applies the prompt to each item and returns refs in the same order as the input list.

---

## Your First Pipeline with chain()

`chain()` runs agents sequentially, feeding the output of each step into the next. It returns the ref from the final step.

This example drafts a blog post and then edits it for clarity in two separate agent calls:

```
chain(steps=[
  {
    "prompt": "Write a 200-word blog post introduction about the benefits of event sourcing.",
    "sandbox": {"model": "sonnet"}
  },
  {
    "prompt": "You will receive a draft blog post introduction. Edit it to improve clarity and remove any jargon. Return only the revised text.",
    "sandbox": {"model": "sonnet"}
  }
])
```

**What happens internally:**

1. Agent 1 runs with the first prompt. Its output text is captured.
2. Agent 2 runs with the second prompt, and the output of Agent 1 is appended as input context.
3. `chain()` returns the ref for Agent 2.

Unwrap the final ref to get the edited introduction:

```
unwrap(ref={"run_id": "r-ghi789", "agent_id": "a-002"})
```

!!! tip "Longer chains"
    `chain()` accepts any number of steps. Each step receives the previous step's output automatically. For branching or merging, combine `chain()` with `par()` or use a named pipeline definition.

---

## Next Steps

You now know the three fundamental patterns:

| Pattern | Combinator | Use when |
|---|---|---|
| Single agent | `run()` | One task, one result |
| Parallel agents | `par()` / `map()` | Independent tasks that can run simultaneously |
| Sequential agents | `chain()` | Each step depends on the previous output |

From here:

- [Refs](concepts/refs.md) — understand the monadic ref architecture in depth.
- [Combinators](concepts/combinators.md) — full reference for `reduce()`, `map_reduce()`, `filter()`, `race()`, `retry()`, and `guard()`.
- [Pipelines](concepts/pipelines.md) — define reusable, versioned pipeline specs as data.
- [Sandboxes](concepts/sandboxes.md) — configure model, tools, memory, CPU, GPU, mounts, and more per agent.
