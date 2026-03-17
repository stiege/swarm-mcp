# Observability & Debugging

Every agent run produces a structured output directory. Two dedicated tools —
`unwrap()` and `inspect()` — give you access to text output and full debug
reports without ever needing to read raw files manually.

---

## Output Directory Structure

Each agent writes its outputs to:

```
/tmp/swarm-mcp/{run_id}/{agent_id}/
```

| File | Contents |
|---|---|
| `result.json` | Serialized `AgentResult`: exit code, text, cost, duration, model, error |
| `stream.jsonl` | Raw Claude stream-json output — one JSON object per line |
| `artifacts.jsonl` | Log of MCP tool calls and `Write` operations (PostToolUse hook) |
| `output.md` | Extracted text, written by `unwrap()` |
| `prompt.txt` | The full prompt sent to the agent (including injected type context) |

!!! info "Refs are metadata pointers"
    The ref returned by any combinator contains metadata (cost, duration, exit
    code, model) but not the agent's text. The text lives in `result.json`.
    Call `unwrap()` when you need it.

---

## `unwrap()` — Extract Text

`unwrap()` reads `result.json`, extracts the agent's text output, and writes it
to `output.md` in the same directory. Returns the file path and size.

```python
unwrap(ref="a1b2c3/agent-0")
```

Returns:

```json
{
  "ref": "a1b2c3/agent-0",
  "file": "/tmp/swarm-mcp/a1b2c3/agent-0/output.md",
  "size": 4821
}
```

Then read it in Claude Code:

```python
Read("/tmp/swarm-mcp/a1b2c3/agent-0/output.md")
```

!!! tip "Unwrap before Read"
    The `output.md` file only exists after you call `unwrap()`. The underlying
    text is in `result.json`; `unwrap()` is the extraction step.

---

## `inspect()` — Debug Report

`inspect()` generates a comprehensive debug report for any ref. It writes the
report to `inspect.md` in the agent's output directory.

```python
inspect(ref="a1b2c3/agent-0")
```

Returns:

```json
{
  "ref": "a1b2c3/agent-0",
  "report": "/tmp/swarm-mcp/a1b2c3/agent-0/inspect.md"
}
```

### What the report contains

```markdown
# Inspect Report: a1b2c3/agent-0

## Result Metadata
- exit_code: 0
- duration_seconds: 34.2
- cost_usd: 0.078
- model: sonnet
- error: null

## Output Preview (first 2000 chars)
The authentication module contains three critical issues...

## Stream Log Summary
Total lines: 847
- assistant messages: 12
- tool_use blocks: 8
- tool_result blocks: 8
- result (final): 1

Tool calls made:
  Read × 5
  Grep × 2
  Write × 1

## Artifacts (from PostToolUse hook)
3 entries in artifacts.jsonl

  [0] 2026-03-17T14:22:11Z  Write  toolu_abc123
      → /workspace/src/review.md

  [1] 2026-03-17T14:22:14Z  mcp__filesystem__read_file  toolu_def456
      → /workspace/src/auth.py

  [2] 2026-03-17T14:22:18Z  Write  toolu_ghi789
      → /workspace/src/review.md

## Files in Output Directory
  result.json      (12,341 bytes)
  stream.jsonl    (84,201 bytes)
  artifacts.jsonl  (2,105 bytes)
  prompt.txt       (1,044 bytes)
  output.md        (4,821 bytes)
  inspect.md       (this file)
```

### When to use `inspect()`

- An agent returned `exit_code != 0` and you want the full error context
- An agent ran but its output looks incomplete or wrong
- You want to audit what tools an agent called and in what order
- You're debugging a pipeline step that silently produced bad output
- Cost is higher than expected and you want to see what the agent was doing

---

## PostToolUse Artifact Hook

Every agent container runs `hooks/log-artifacts.sh` as a PostToolUse hook.
It fires after every MCP tool call and every `Write` operation and appends a
record to `/output/artifacts.jsonl`.

### What it captures

The hook matches on `mcp__*` (all MCP tool calls) and `Write` (file writes).
Other built-in tools like `Read`, `Grep`, `Glob`, and `Bash` are not captured
because they are read-only or terminal operations; `Write` and MCP calls are
the operations that change external state.

### JSON structure

Each line in `artifacts.jsonl` is one JSON object:

```json
{
  "timestamp": "2026-03-17T14:22:11Z",
  "tool": "Write",
  "tool_use_id": "toolu_abc123",
  "input": {
    "file_path": "/workspace/src/review.md",
    "content": "## Code Review\n\nFound 3 issues..."
  },
  "response": {
    "success": true
  }
}
```

```json
{
  "timestamp": "2026-03-17T14:22:14Z",
  "tool": "mcp__filesystem__read_file",
  "tool_use_id": "toolu_def456",
  "input": {
    "path": "/workspace/src/auth.py"
  },
  "response": {
    "content": "import os\nimport hashlib..."
  }
}
```

### Reading artifacts manually

```bash
# Count total tool calls
wc -l /tmp/swarm-mcp/a1b2c3/agent-0/artifacts.jsonl

# Show all Write operations
grep '"tool":"Write"' /tmp/swarm-mcp/a1b2c3/agent-0/artifacts.jsonl | jq .

# Show files written
jq 'select(.tool == "Write") | .input.file_path' \
    /tmp/swarm-mcp/a1b2c3/agent-0/artifacts.jsonl
```

---

## Reading `stream.jsonl` Manually

`stream.jsonl` contains the raw output from `claude --output-format stream-json`.
Each line is a self-contained JSON object. The format has three message types:

### `assistant` — content chunks

```json
{
  "type": "assistant",
  "message": {
    "id": "msg_abc",
    "role": "assistant",
    "content": [
      { "type": "text", "text": "Looking at the auth module..." }
    ]
  }
}
```

### `result` — final output with cost

The last line of a successful run. Contains the complete text and billing
information:

```json
{
  "type": "result",
  "subtype": "success",
  "result": "The authentication module has three critical issues...",
  "cost_usd": 0.078,
  "duration_ms": 34241,
  "session_id": "sess_xyz"
}
```

### `content_block_delta` — streaming increments

For long responses, intermediate deltas may appear before the final `result`:

```json
{
  "type": "content_block_delta",
  "index": 0,
  "delta": {
    "type": "text_delta",
    "text": " additional analysis..."
  }
}
```

### Parsing stream.jsonl

```bash
# Show only the final result line
grep '"type":"result"' /tmp/swarm-mcp/a1b2c3/agent-0/stream.jsonl | jq .

# Extract cost from result
grep '"type":"result"' /tmp/swarm-mcp/a1b2c3/agent-0/stream.jsonl \
  | jq '.cost_usd'

# Count assistant message chunks
grep '"type":"assistant"' /tmp/swarm-mcp/a1b2c3/agent-0/stream.jsonl | wc -l
```

---

## Partial Output on Timeout

When an agent times out, swarm-mcp kills the container and parses whatever
was written to `stream.jsonl` before termination. The resulting ref has:

```json
{
  "ref": "a1b2c3/agent-0",
  "exit_code": -1,
  "error": "Timed out after 1800s (partial output captured: 12430 chars)",
  "cost_usd": 0.31,
  "duration_seconds": 1800.0
}
```

The partial text is preserved in `result.json` and available via `unwrap()`.
This means you never lose work-in-progress output just because an agent ran
long.

!!! warning "Cost on timeout"
    The `cost_usd` on a timed-out ref reflects tokens already billed by the
    Anthropic API. The agent was running and consuming tokens right up until the
    kill signal.

To investigate a timed-out agent:

```python
# Get whatever output was captured before timeout
unwrap(ref="a1b2c3/agent-0")

# Full debug report including stream log line count and tool calls
inspect(ref="a1b2c3/agent-0")
```

If the partial output is useful, you can continue the work with a new agent
that reads the partial result as context.

---

## Common Debugging Workflows

### Agent failed — what happened?

```python
inspect(ref="run01/agent-2")
# Read the report to see error message, last tool call, and stream log summary
Read("/tmp/swarm-mcp/run01/agent-2/inspect.md")
```

### Agent succeeded but output looks wrong

```python
unwrap(ref="run01/agent-2")
Read("/tmp/swarm-mcp/run01/agent-2/output.md")
# If output is malformed, check what the agent actually wrote vs what it should have
```

### High cost — what was the agent doing?

```python
inspect(ref="run01/agent-2")
# The report shows tool call counts and stream log line totals
# Then check artifacts for the specific MCP calls that may have triggered re-reads
```

### Pipeline step passed wrong data to next step

```python
# Read the shared directory contents from the failing step
unwrap(ref="pipeline-run01/step-2")
# Check what it wrote — does it match what step-3 was expecting?
```

---

!!! note "See also"
    - [Security](security.md) — `encrypt()` and `classify()` also affect what `unwrap()` can access
    - [Examples: Research Fan-Out](examples/research.md) — unwrap used in a complete workflow
    - [Concepts: Refs](concepts/refs.md) — full ref metadata schema
