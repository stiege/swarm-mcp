# MCP Access from Agents

swarm agents can use any MCP server that is configured in your Claude settings. The `mcps` sandbox field tells the runner which servers to attach — the agent container is wired up automatically.

---

## How MCP access works

When you set `mcps: ["database-mcp"]` in a sandbox spec, swarm-mcp does three things before starting the container:

1. **Mounts `~/projects/mcp` at the same host path.** The MCP server code lives here and is bind-mounted into the container unchanged, so the same absolute paths work inside and out.

2. **Copies MCP server configs from your host `~/.claude.json`.** swarm reads the `mcpServers` block and extracts the entries you named. The container gets its own `~/.claude.json` containing exactly those entries — including `command`, `args`, and any `env` values.

3. **Claude inside the container spawns the MCP server as a subprocess.** The server runs inside the container, with access to whatever is mounted. There is no tunnel back to the host.

The key implication: the MCP server process runs entirely inside the container. It can only see the filesystem that Docker has mounted into that container.

---

## The data path problem

MCP servers are typically configured with absolute paths that point to data on your host — a SQLite database, a directory of notes, a credentials file. Those paths are embedded in the server's `command` or `args` in `~/.claude.json`.

When the server runs inside the container, those absolute paths must exist inside the container. Since the MCP config is copied in verbatim, the container path must be the **same** as the host path.

**If a data path is not mounted, the MCP server will fail to open its files.**

### Solution: mount the data path at the same location

```json
{
  "mcps": ["database-mcp"],
  "mounts": [
    {
      "host_path": "~/.local/share/database-mcp",
      "container_path": "~/.local/share/database-mcp",
      "readonly": true
    }
  ]
}
```

This makes the MCP server's data directory visible at exactly the path it expects, with read-only protection so agents cannot corrupt the data.

---

## Common MCP patterns

### Knowledge database (database-mcp)

A local SQLite knowledge base that agents can search for context during research tasks.

```json
{
  "mcps": ["database-mcp"],
  "mounts": [
    {
      "host_path": "~/.local/share/database-mcp",
      "container_path": "~/.local/share/database-mcp",
      "readonly": true
    }
  ]
}
```

With this sandbox, an agent can:

- Run full-text search across all knowledge entries
- Query specific topics or tags
- Pull source summaries and citations to ground a research task

Example call:

```
run(
  prompt: "Search the knowledge base for prior art on rate-limiting algorithms. Summarise the top 5 relevant entries.",
  sandbox: {
    "mcps": ["database-mcp"],
    "mounts": [{"host_path": "~/.local/share/database-mcp", "container_path": "~/.local/share/database-mcp", "readonly": true}]
  }
)
```

### File-based MCPs (Logseq, Obsidian, etc.)

MCPs that read from a directory of files on your host (markdown notes, a Logseq graph, a wiki).

```json
{
  "mcps": ["logseq-mcp"],
  "mounts": [
    {
      "host_path": "~/notes/logseq",
      "container_path": "~/notes/logseq",
      "readonly": true
    }
  ]
}
```

Use `"readonly": false` if the MCP needs to create or update notes (e.g. writing a summary back into the graph). Be aware that write-mounted directories are shared across any concurrent agents using the same sandbox — coordinate with resource pools if contention is a concern.

### Network MCPs (Google Workspace, Slack, WhatsApp, etc.)

MCPs that talk to external APIs do not need any data mounts. They only need outbound network access, which is on by default (`"network": true`).

```json
{
  "mcps": ["google-workspace-mcp", "slack-mcp"]
}
```

No `mounts` needed — the MCP server connects to the external API directly from inside the container.

!!! note "Credentials in `mcpServers` env"
    If your MCP server reads API keys from its `env` block in `~/.claude.json`, those values are copied into the container automatically. You do not need to pass them via `env_vars`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "MCP server `foo` not found in host config" (logged at WARNING) | The name in `mcps` does not match any key in `~/.claude.json` `mcpServers` | Check the exact key name in your host config |
| Agent cannot call MCP tools at all | MCP server failed to start inside the container | Check that `~/projects/mcp` exists on the host and contains the server code |
| "No such file or directory" error in MCP server | The server's data path is not mounted | Add the path to `mounts` at the same container path |
| "Permission denied" error in MCP server | Mount is `readonly: true` but the server needs write access | Set `"readonly": false` on the relevant mount |

---

## Saving named sandbox specs for MCP setups

If you use the same MCP configuration repeatedly, save it as a named spec so you do not repeat the JSON on every call.

### Save the spec

```
save_sandbox_spec(
  name: "with-knowledge-db",
  spec: '{
    "mcps": ["database-mcp"],
    "mounts": [
      {
        "host_path": "~/.local/share/database-mcp",
        "container_path": "~/.local/share/database-mcp",
        "readonly": true
      }
    ]
  }'
)
```

The spec is stored in `~/.claude/sandboxes/with-knowledge-db.json` (or your project's `sandboxes/` directory if a project is registered via `wrap_project`).

### Use it anywhere

```
run(prompt: "What do my notes say about distributed consensus?", sandbox: "with-knowledge-db")
```

```
par(
  tasks: '[
    {"prompt": "Summarise knowledge base entries on CAP theorem", "sandbox": "with-knowledge-db"},
    {"prompt": "Summarise knowledge base entries on CRDT data structures", "sandbox": "with-knowledge-db"}
  ]'
)
```

Named specs merge cleanly with inline overrides — you can add extra fields or swap the model without redefining the whole spec:

```
run(
  prompt: "Deep-dive on the Raft consensus entries",
  sandbox: "with-knowledge-db",
  model: "opus"
)
```

---

## Security considerations

- **Each agent gets its own container** but MCP data directories are shared across any containers that mount them simultaneously. Use read-only mounts for data you do not want agents to modify.

- **Use `readonly: true` by default.** Only set `readonly: false` when the MCP server genuinely needs write access. A write-mounted directory can be modified by any agent that uses the sandbox.

- **Sensitive MCP results can be protected.** Use the `classify()` tool to tag outputs from agents that read sensitive data, and `encrypt()` to protect results at rest. The `guard()` tool can enforce that a classification level is present before passing results downstream.

---

## Related pages

- [Sandboxes](sandboxes.md) — full reference for all sandbox spec fields, including `mcps` and `mounts`
- [Resources](resources.md) — coordinate access to shared resources like databases using named resource pools
- [Security](../security.md) — isolation boundaries, network policy, and data classification
