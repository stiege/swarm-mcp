# Parallel Code Review

Review multiple source files simultaneously — each agent focuses on one file —
then reduce the individual reviews into a consolidated summary report.

---

## Overview

The pattern: fan out one review agent per file, then synthesize findings.

```
[auth.py, db.py, api.py, utils.py]
         │
         ▼
map(review_template × 4 files)  ──► 4 refs  ──► reduce(summary_prompt) ──► 1 ref
                                                                                │
                                                                          unwrap()
                                                                                │
                                                                         review.md
```

Each agent receives its file path and performs a focused review in isolation.
Because the agents run concurrently, a four-file review takes no longer than a
single-file review.

---

## Step 1 — Fan Out with `map()`

Mount your source tree read-only so agents can `Read` and `Grep` without
mutating anything:

```python
map(
    prompt_template="You are a senior software engineer performing a thorough code review. Review the file at {input}.\n\nFor each issue found, report:\n- **Severity**: critical / warning / nit\n- **Location**: file name and line number(s)\n- **Problem**: concise description of the issue\n- **Fix**: specific suggested change\n\nCheck for:\n1. Bugs and logic errors (off-by-one, null dereferences, race conditions)\n2. Security vulnerabilities (injection, improper auth checks, exposed secrets, insecure defaults)\n3. Performance issues (N+1 queries, unnecessary allocations, blocking I/O)\n4. Style and readability (unclear naming, missing docstrings, dead code)\n5. Test coverage gaps\n\nEnd your review with a one-paragraph summary and an overall grade: A / B / C / D / F.",
    inputs='["/workspace/src/auth.py", "/workspace/src/db.py", "/workspace/src/api/routes.py", "/workspace/src/utils.py"]',
    model="sonnet",
    tools="Read,Glob,Grep",
    mounts='[{"host_path": "/home/me/myproject", "container_path": "/workspace", "readonly": true}]',
    max_concurrency=4
)
```

This returns an array of four refs — one per file:

```json
{
  "refs": [
    { "ref": "run01/agent-0", "exit_code": 0, "cost_usd": 0.08, "duration_seconds": 28.1 },
    { "ref": "run01/agent-1", "exit_code": 0, "cost_usd": 0.11, "duration_seconds": 34.5 },
    { "ref": "run01/agent-2", "exit_code": 0, "cost_usd": 0.09, "duration_seconds": 31.2 },
    { "ref": "run01/agent-3", "exit_code": 0, "cost_usd": 0.06, "duration_seconds": 22.8 }
  ],
  "succeeded": 4,
  "failed": 0
}
```

---

## Step 2 — Synthesize with `reduce()`

Pass the array of refs from the map step to `reduce()`. The synthesis agent
receives all four individual reviews as context and writes a consolidated report:

```python
reduce(
    refs='["run01/agent-0", "run01/agent-1", "run01/agent-2", "run01/agent-3"]',
    synthesis_prompt="You have received individual code reviews for four files in the same codebase. Produce a consolidated code review report.\n\nStructure the report as follows:\n\n## Overall Assessment\n- Composite grade across all files\n- 2-3 sentence summary of the codebase's health\n\n## Critical Issues (must fix before merge)\nList all critical-severity issues from any file. Group by theme (security, correctness, etc.).\n\n## Warnings (should fix soon)\nList warning-severity issues, grouped by theme.\n\n## Patterns and Recurring Problems\nIdentify issues that appear in multiple files — these indicate systemic problems that need broader attention.\n\n## Positive Observations\nNote what the code does well.\n\n## Recommended Fix Order\nPrioritized list of the top 5 things to address first.",
    model="sonnet"
)
```

!!! tip "Passing refs from map to reduce"
    The refs array returned by `map()` can be passed directly to `reduce()`.
    Copy the `refs` array from the map result and paste it as the `refs`
    argument to `reduce()`.

---

## Step 3 — Read the Report

```python
unwrap(ref="run01/agent-reduce-0")
# → { "file": "/tmp/swarm-mcp/run01/agent-reduce-0/output.md", "size": 5832 }

Read("/tmp/swarm-mcp/run01/agent-reduce-0/output.md")
```

To read a single file's individual review:

```python
unwrap(ref="run01/agent-1")   # db.py review only
```

---

## Full Working Code

Here is the complete two-step call sequence with realistic parameters:

```python
# Step 1: fan out — one agent per file
map_result = map(
    prompt_template="Perform a thorough code review of {input}. Report each issue with severity (critical/warning/nit), location (file:line), problem description, and suggested fix. Grade the file A–F at the end.",
    inputs='["/workspace/src/auth.py", "/workspace/src/db.py", "/workspace/src/api/routes.py", "/workspace/src/utils.py"]',
    model="sonnet",
    tools="Read,Glob,Grep",
    mounts='[{"host_path": "/home/me/myproject", "container_path": "/workspace", "readonly": true}]',
    max_concurrency=4,
    timeout=300
)

# Step 2: synthesize — collect into a report
# (use the refs from map_result.refs)
reduce_result = reduce(
    refs='["run01/agent-0", "run01/agent-1", "run01/agent-2", "run01/agent-3"]',
    synthesis_prompt="Consolidate these four code reviews into a single report. Group critical issues by theme. Identify patterns across files. Give an overall grade and a prioritized fix list.",
    model="sonnet"
)

# Step 3: extract the text
unwrap(ref="run01/agent-reduce-0")
```

---

## Mount Configuration

The `mounts` field accepts a JSON array of volume mount objects:

```json
[
  {
    "host_path": "/home/me/myproject",
    "container_path": "/workspace",
    "readonly": true
  }
]
```

| Field | Description |
|---|---|
| `host_path` | Absolute path on the host machine |
| `container_path` | Where it appears inside the container |
| `readonly` | `true` prevents agents from modifying your source tree |

!!! warning "Absolute paths inside the container"
    The `inputs` array must use container paths (e.g. `/workspace/src/auth.py`),
    not host paths. The agents run inside Docker and only see the container
    filesystem.

---

## Variations

### Review only changed files

Use `Bash` to get a git diff list, then pass it to `map()`:

```python
map(
    prompt_template="Review {input} for issues introduced in recent changes. Focus on the diff context, not the entire file history.",
    inputs='["/workspace/src/auth.py", "/workspace/src/payments.py"]',
    model="sonnet",
    tools="Read,Glob,Grep,Bash",
    mounts='[{"host_path": "/home/me/myproject", "container_path": "/workspace", "readonly": true}]'
)
```

### Filter out passing reviews

Use `filter()` after `map()` to keep only reviews that found real issues,
then reduce only those:

```python
# Only synthesize files that had actual problems
filter(
    refs='["run01/agent-0", "run01/agent-1", "run01/agent-2", "run01/agent-3"]',
    declared_type="code-review-with-issues"
)
```

Define `types/code-review-with-issues.md` to require at least one critical or
warning finding. See [Types](../concepts/types.md) for how to write type files.

### Single-step with `map_reduce()`

If you don't need to inspect individual reviews before synthesis, use
`map_reduce()` to do both in one call:

```python
map_reduce(
    prompt_template="Review {input} for bugs, security issues, and performance problems. Severity, location, and fix for each finding.",
    inputs='["/workspace/src/auth.py", "/workspace/src/db.py", "/workspace/src/api/routes.py", "/workspace/src/utils.py"]',
    synthesis_prompt="Consolidate these reviews. Critical issues first, then patterns, then grade.",
    model="sonnet",
    tools="Read,Glob,Grep",
    mounts='[{"host_path": "/home/me/myproject", "container_path": "/workspace", "readonly": true}]',
    max_concurrency=4
)
```

---

## Debugging a Failed Agent

If one agent returns `exit_code != 0`, use `inspect()` to see what happened:

```python
inspect(ref="run01/agent-1")
```

This writes a debug report to `inspect.md` with the error message, any partial
output, the tool calls the agent made, and the files it accessed. See
[Observability](../observability.md) for details.

---

!!! note "See also"
    - [Research Fan-Out](research.md) — same pattern applied to topics instead of files
    - [map() reference](../concepts/combinators.md#map)
    - [reduce() reference](../concepts/combinators.md#reduce)
    - [Observability & Debugging](../observability.md)
