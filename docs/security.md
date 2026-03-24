# Security

swarm-mcp provides three security layers: Docker isolation at the execution
boundary, encryption for sensitive output at rest, and classification labels
that control which downstream tools can access a ref's content.

---

## Docker Isolation Model

Each agent runs in its own Docker container. Isolation is enforced at the
container level — not through software sandboxing or process separation within
a shared environment.

### Non-root execution

Containers run as the `ubuntu` user (uid 1000), not root. A compromised or
misbehaving agent cannot modify system files or affect other containers.

### bypassPermissions mode

Agents run with `--dangerously-skip-permissions` set in the Claude CLI
invocation. This is intentional: inside an isolated container, the normal
interactive permission prompts are unnecessary and would block automation.
The isolation boundary is the container, not Claude's permission system.

!!! warning "bypassPermissions is not a security hole"
    `bypassPermissions` only removes the *interactive prompt*. All filesystem
    access is still limited to what the container's volume mounts allow. An
    agent cannot read your home directory unless you explicitly mount it.

### Network isolation

By default, containers use `network=host` to reach the Anthropic API. To run
an agent with no network access (air-gapped), set `network: false`:

```python
run(
    prompt="Analyze the provided data file.",
    network=False,
    input_files={"/workspace/data.json": data_content}
)
```

!!! info "network=False and the Anthropic API"
    Setting `network=False` means the agent cannot call the Anthropic API
    itself, so it cannot spawn sub-agents or use MCP servers that require
    network access. Use this only for purely local computation tasks where
    you have pre-loaded everything the agent needs via `input_files` or
    `mounts`.

### Tools allowlist

The `tools` parameter controls which Claude built-in tools the agent can use.
The default is `["Read", "Write", "Glob", "Grep", "Bash"]`. To restrict:

```python
run(
    prompt="Review this document.",
    tools="Read,Glob,Grep",   # no Write, no Bash
    mounts='[{"host_path": "/docs", "container_path": "/workspace", "readonly": true}]'
)
```

### Volume mount read-only flag

Always mount source code and data read-only unless the agent specifically needs
to write there:

```json
{"host_path": "/home/me/project", "container_path": "/workspace", "readonly": true}
```

### Data flow — refs on the wire, text on disk

The MCP protocol carries only ref metadata. The agent's text output stays on
disk at `/tmp/swarm-mcp/{run_id}/{agent_id}/result.json`. Calling `unwrap()`
extracts the text into `output.md` which you then `Read()`. The text never
travels over the MCP socket unless you explicitly paste it into your conversation.

---

## Encryption

### `encrypt(ref)` — encrypt output at rest

```python
encrypt(ref="a1b2c3/agent-0")
```

Returns:

```json
{
  "ref": "a1b2c3/agent-0",
  "key_id": "f3a9e1b2c4d5",
  "encrypted": {
    "key_id": "f3a9e1b2c4d5",
    "algorithm": "fernet"
  }
}
```

What happens:

1. A new Fernet key is generated (AES-128-CBC with HMAC-SHA256).
2. The key is stored at `/tmp/swarm-mcp/.keys/{key_id}` with mode `0600`.
3. The `text` field in `result.json` is replaced with the base64-encoded
   Fernet token.
4. The ref gains an `"encrypted"` field with the `key_id` and algorithm.

### `decrypt(ref, key_id)` — decrypt to output.md

```python
decrypt(ref="a1b2c3/agent-0", key_id="f3a9e1b2c4d5")
```

Returns:

```json
{
  "ref": "a1b2c3/agent-0",
  "file": "/tmp/swarm-mcp/a1b2c3/agent-0/output.md",
  "size": 4821
}
```

The decrypted plaintext is written to `output.md`. The encrypted ciphertext
in `result.json` is not modified.

!!! warning "Key storage location"
    Keys are stored on the local filesystem at `/tmp/swarm-mcp/.keys/`. They
    are not persisted across reboots by default. If you need the key to survive
    a restart, copy it to a secure location before rebooting.

### Enforcing encryption before processing

Use `guard()` to ensure a ref is encrypted before passing it to another tool:

```python
guard(ref="a1b2c3/agent-0", check="encrypted")
# Raises an error if the ref does not have the "encrypted" stamp set.
```

This is useful in pipelines where a sensitive generation step must encrypt
before any downstream processing can proceed.

---

## Classification

### `classify(ref, level)` — stamp data sensitivity

```python
classify(ref="a1b2c3/agent-0", level="confidential")
```

Classification levels in ascending order of sensitivity:

| Level | Numeric | Meaning |
|---|---|---|
| `public` | 0 | No restrictions. Safe to share externally. |
| `internal` | 1 | Internal use only. Do not expose via public MCPs. |
| `confidential` | 2 | Restricted access. Audit trail required. |
| `restricted` | 3 | Highest sensitivity. Named allowlist required. |

### MCP access control

`classify()` accepts optional `allowed_mcps` and `denied_mcps` lists:

```python
# Only the internal audit MCP may access this ref
classify(
    ref="a1b2c3/agent-0",
    level="restricted",
    allowed_mcps='["audit-mcp"]'
)

# All MCPs allowed except the logging MCP
classify(
    ref="a1b2c3/agent-0",
    level="confidential",
    denied_mcps='["logging-mcp", "analytics-mcp"]'
)
```

When classification is set, downstream combinators that request MCPs check the
allowlist before proceeding. A `guard(check="classification")` call also
enforces this.

### Classification in pipelines

Set a default classification for the entire pipeline:

```json
{
  "name": "sensitive-analysis",
  "classification": "confidential",
  "steps": [...]
}
```

All refs produced by the pipeline inherit this classification unless overridden
per-step.

---

## `guard()` — Enforce Conditions

`guard()` checks a condition on a ref and either passes it through or raises
an error. Use it as a gate before passing a ref to downstream processing.

```python
guard(ref="a1b2c3/agent-0", check="encrypted")
guard(ref="a1b2c3/agent-0", check="validated")
guard(ref="a1b2c3/agent-0", check="classification", value="confidential")
guard(ref="a1b2c3/agent-0", check="budget")
guard(ref="a1b2c3/agent-0", check="exists")
```

| Check | Passes when |
|---|---|
| `encrypted` | The ref has the `encrypted` stamp (i.e. `encrypt()` was called) |
| `validated` | The ref has `validation_verdict == "VALID"` |
| `classification` | The ref's classification level matches `value` |
| `budget` | The ref's budget stamp shows remaining budget >= 0 |
| `exists` | The ref's output directory and `result.json` are present on disk |

### Example: encrypt-then-process gate

```python
# Generate sensitive output
ref = run(prompt="Extract all PII from the uploaded document.", ...)

# Encrypt it
encrypt(ref=ref["ref"])

# Gate: refuse to proceed unless encrypted
guard(ref=ref["ref"], check="encrypted")

# Now safe to pass to downstream pipeline
classify(ref=ref["ref"], level="restricted", allowed_mcps='["redaction-mcp"]')
```

---

## Security Checklist

| Concern | Mitigation |
|---|---|
| Agent reads host files it shouldn't | Use `readonly: true` on mounts; only mount directories the agent needs |
| Agent calls external services | Set `network: false` for air-gapped tasks; use MCP allowlist |
| Sensitive output leaks over MCP protocol | Text stays on disk; refs carry only metadata |
| Sensitive output accessible to wrong downstream tools | Use `classify()` with `allowed_mcps` / `denied_mcps` |
| Output must be protected at rest | Call `encrypt()` immediately after `run()` |
| Pipeline continues past a failed security check | Use `guard()` as a gate step before downstream processing |
| Multiple GPU agents double-book hardware | Use `resources: ["gpu"]` to serialize |
| Agent runs as root inside container | Container user is `ubuntu` (uid 1000) by default |

---

!!! note "See also"
    - [Observability](observability.md) — inspect artifact logs to audit what agents accessed
    - [Concepts: Refs](concepts/refs.md) — full stamp system including Encrypted and Classified stamps
    - [Concepts: Sandboxes](concepts/sandboxes.md) — full sandbox spec including `network`, `tools`, `mounts`
