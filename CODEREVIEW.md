# Code Review: swarm-mcp

This document records genuine code-quality observations found during the documentation
sweep.  None of these were changed in the sweep (they are logic, not style) but they
are worth addressing in future work.

---

## Genuine Code-Quality Issues

### 1. `docker.py` — misplaced `import json` at module bottom

`docker.py` contains:

```python
# Needed for json.dumps in get_docker_run_cmd
import json  # noqa: E402
```

This import is at the very bottom of the file (after all function definitions) with a
`noqa: E402` suppressor.  Standard Python style requires all imports at the top.
The comment explains it was placed there to avoid an import loop, but `json` is a
standard-library module with no dependencies — moving it to the top will not cause a
circular import.  The `# noqa` directive is masking a real linting violation.

**Fix:** Move `import json` to the top import block and remove the noqa comment.

---

### 2. `server.py` — `inspect` tool uses `dir()` instead of local variables

```python
return json.dumps({
    "ref": ref,
    "file": inspect_path,
    "tool_calls": tool_calls if 'tool_calls' in dir() else [],
    "has_partial_output": bool(text) if 'text' in dir() else False,
})
```

Using `dir()` to check whether a local variable was assigned is fragile and hard to
read.  `tool_calls` and `text` are only defined inside the `if os.path.exists(stream_file)`
and `if os.path.exists(result_file)` branches respectively.  The correct pattern is to
initialise them to empty/falsy defaults before the conditional blocks.

**Fix:** Add `tool_calls: list[str] = []` and `text: str = ""` before the relevant
`if` blocks.

---

### 3. `server.py` — `filter` tool has a redundant loop variable

```python
for r, ref_obj, result in zip(ref_list, ref_list, results):
```

`ref_list` is zipped with itself, so `r` and `ref_obj` are always the same object.
Only `ref_obj` is used in the body; `r` is unused.  This works but is confusing.

**Fix:** Replace with `for ref_obj, result in zip(ref_list, results):`.

---

### 4. `monads.py` — `/tmp/swarm-mcp` hardcoded in two modules

The temporary work directory `/tmp/swarm-mcp` appears as a bare string in:
- `monads.py` (KEYS_DIR)
- `agent.py` (output_dir construction)
- `server.py` (_resolve_ref, unwrap, inspect, encrypt, decrypt, …)
- `tools.py` (TRUNCATE_DIR)

There is a `TRUNCATE_DIR` constant in `tools.py` and a `KEYS_DIR` constant in
`monads.py`, but neither is imported by `server.py` or `agent.py`.  Each module
repeats the string independently.

**Fix:** Define a single `SWARM_TMP_DIR` constant in `registry.py` (or a new
`constants.py`) and import it everywhere.

---

### 5. `server.py` — `retry` tool has an unguarded `result` reference

```python
data = {
    ...
    "last_ref": result.to_ref_dict(run_id) if result else None,
}
```

`result` is assigned inside the `for` loop but the `except` block at the very bottom
could theoretically be reached before any iteration (if `_generate_run_id()` itself
raised, for example).  In practice this cannot happen because `_generate_run_id` is
trivial, but static analysers will flag the bare `result` reference as potentially
unbound.

**Fix:** Initialise `result: AgentResult | None = None` before the loop.

---

### 6. `sandbox.py` — private `__dataclass_fields__` accessed externally

```python
SandboxSpec(**{k: v for k, v in data.items() if k in SandboxSpec.__dataclass_fields__})
```

`__dataclass_fields__` is a private CPython implementation detail.  The public
alternative is `dataclasses.fields(SandboxSpec)` which returns a tuple of `Field`
objects; using `{f.name for f in dataclasses.fields(SandboxSpec)}` is more robust.

---

### 7. `agent.py` — `_parse_stream_output` accumulates content blocks then discards them

```python
if msg_type == "result":
    text_parts = [obj.get("result", "")]  # replaces accumulated content
    cost = obj.get("cost_usd") or obj.get("total_cost_usd")
    break
```

When a `"result"` line is found the previously accumulated `text_parts` list is
replaced.  This is intentional (the `"result"` line contains the canonical final
text), but the earlier accumulation of `"assistant"` content chunks and
`"content_block_delta"` deltas becomes wasted work.  A comment would help; consider
reorganising so the early-accumulation path is only taken as a fallback (when no
`"result"` line is present — e.g. on timeout).

---

### 8. `server.py` — verdict parsing duplicated three times

The pattern of scanning `result.text` line-by-line for a `VALID`/`PARTIAL`/`INVALID`
token appears identically in:
- `validate` tool
- `retry` tool
- `filter` tool

This should be extracted into a small private helper `_parse_verdict(text: str) -> str`
to avoid drift if the format ever changes.

---

## Naming and Pattern Inconsistencies

| Location | Issue |
|---|---|
| `server.py` — `run` default | `tools` defaults to `"Read,Write,Glob,Grep,Bash"` as a comma-separated string (not a list). Other parameters like `mounts` default to `"[]"` (JSON string). The mixing of comma-separated and JSON-array conventions for list parameters is inconsistent. |
| `server.py` vs `sandbox.py` | `SandboxSpec.timeout` defaults to `1800` s; the `run` tool docstring says the default is 120 s. These are inconsistent (the docstring is wrong). |
| `monads.py` — `stamp_encrypted` | The key in the ref dict is `"encrypted"` (a dict), but `result.json` on disk uses both `"encrypted": True` (a bool flag) and `"encryption": {...}` (the metadata dict). The dual use of `"encrypted"` for different types is confusing. |
| `registry.py` — `_wrapped` dict | The `_wrapped` mapping (`ref_id → host path`) is populated by `wrap_file` but never consulted anywhere. It appears to be dead code from an earlier design. |

---

## Security Considerations

### Temporary directory world-readable

`/tmp/swarm-mcp` is created without explicit permissions (`os.makedirs(..., exist_ok=True)`).
On most Linux systems this means it is world-readable (mode 0o755 or umask-dependent).
Agent result files — including potentially sensitive text outputs — are written there.

**Recommendation:** Create the directory with mode `0o700` so only the process owner
can read it:
```python
os.makedirs(SWARM_TMP_DIR, exist_ok=True)
os.chmod(SWARM_TMP_DIR, 0o700)
```

### Fernet keys stored in `/tmp`

`KEYS_DIR = /tmp/swarm-mcp/.keys` stores encryption keys.  While individual key files
are `chmod 0o600`, the parent directory (and `/tmp` itself) may be accessible to other
users.  Keys in `/tmp` are also lost on reboot, making encrypted refs permanently
unreadable after a restart.

**Recommendation:** Use a persistent, permission-restricted directory (e.g.
`~/.local/share/swarm-mcp/keys`) for long-lived deployments.

### OAuth credentials copied into every container home directory

`_setup_agent_home` copies `~/.claude/.credentials.json` into a temporary staging
directory.  If the output directory is world-readable (see above) these credentials
are exposed to other local users.

**Recommendation:** Fix the `/tmp` permissions issue above and ensure the staging
directory (`output_dir/home/`) is created with mode `0o700`.

### `--permission-mode bypassPermissions`

Every container is launched with `--permission-mode bypassPermissions`, giving the
Claude agent full filesystem access inside the container.  This is by design (agents
need to read/write files), but it means a prompt-injection attack could cause the
agent to exfiltrate data through files visible to the container.  The network is also
enabled by default.

**Recommendation:** Document this threat model explicitly.  Consider disabling network
for agents that only perform local computation (set `spec.network = False`).

### `spec.mcps` mounts `~/projects/mcp` into containers

If the `mcps` list is non-empty, the entire `~/projects/mcp` directory is mounted
read-write into the container.  A malicious or compromised agent prompt could
potentially modify MCP server source code on the host.

**Recommendation:** Mount `~/projects/mcp` read-only (add `:ro` to the bind-mount
flag).

---

## Suggestions for Future Improvements

1. **Extract `SWARM_TMP_DIR` as a single constant** shared across all modules (see
   issue #4 above).

2. **Add a test suite** — the `dev` dependency group lists `pytest` and
   `pytest-asyncio` but no tests exist.  At minimum, unit tests for
   `_parse_stream_output`, `resolve_type`, `check_classification`, and
   `resolve_sandbox` would catch regressions cheaply.

3. **Type-check with mypy/pyright** — several functions use bare `dict` return types.
   Introducing typed dicts or dataclasses for ref objects would make the codebase
   easier to refactor safely.

4. **`race` tool does not actually cancel losers** — the docstring says "remaining
   tasks are abandoned (their containers are killed)" but the implementation simply
   waits for all tasks to complete via `_run_par_internal` before selecting the
   winner.  True speculative execution would require a different approach (e.g.
   `concurrent.futures.as_completed` with early cancellation).

5. **Pipeline `retry_if` control-flow complexity** — the nested `for/else` in the
   `pipeline` interpreter is difficult to follow.  Extracting it into a dedicated
   `_next_step_index(step, steps, result, retry_counts)` function would improve
   readability without changing behaviour.

6. **`wrap_project` is not persistent** — search paths registered via `add_search_path`
   live only in the current process's memory.  If the MCP server restarts, users must
   call `wrap_project` again.  Supporting `SWARM_PROJECT_DIR` at startup (which is
   already implemented) or a config file would help.
