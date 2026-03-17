# Installation

## Prerequisites

| Requirement | Notes |
|---|---|
| **Docker** | Engine 24+ recommended. The Docker socket must be accessible to the user running swarm-mcp. |
| **Claude Code** | Must be logged in via OAuth (`claude login`). Credentials are read from `~/.claude/.credentials.json` at runtime. |
| **uv** | Python package installer/runner. Install with `curl -LsSf https://astral.sh/uv/install.sh \| sh` or see [uv docs](https://docs.astral.sh/uv/). |

## Install swarm-mcp

Install from PyPI:

```bash
uv tool install swarm-mcp
```

Or install from a local clone of the repository:

```bash
git clone https://github.com/ahodge/swarm-mcp
cd swarm-mcp
uv tool install .
```

Verify the command is available:

```bash
swarm-mcp --help
```

## Configure Claude Code

Add swarm-mcp to Claude Code's MCP server list. Edit `~/.claude/settings.json` (or the project-level `.claude/settings.json`):

```json
{
  "mcpServers": {
    "swarm-mcp": {
      "command": "swarm-mcp",
      "type": "stdio"
    }
  }
}
```

Restart Claude Code after saving. You should see swarm-mcp listed under active MCP servers.

## Build the Docker Image

Each agent runs inside the `swarm-agent` Docker image. The image needs the `claude` and `uv` binaries, which you must supply from your host (they are not downloaded at build time so the image works offline and stays reproducible).

1. Copy the binaries to the repository root:

    ```bash
    cp $(which claude) /path/to/swarm-mcp/claude
    cp $(which uv)     /path/to/swarm-mcp/uv
    ```

2. Build the image from the repository root:

    ```bash
    cd /path/to/swarm-mcp
    docker build -t swarm-agent .
    ```

3. Confirm the image exists:

    ```bash
    docker image inspect swarm-agent --format '{{.Id}}'
    ```

!!! note "Image updates"
    Rebuild the image whenever you update `claude` or `uv` on your host, or when the `Dockerfile` changes.

## Verify the Installation

Open Claude Code and ask it to run a quick smoke test:

```
Use swarm-mcp to run a single agent with the prompt "Reply with the word HELLO and nothing else."
Then unwrap the result and show me the text.
```

You should see `HELLO` returned within a few seconds. If you see an error, check the troubleshooting section below.

## Environment Variables

These variables are read by the swarm-mcp server process (not inside containers). Set them in your shell profile or in the `env` block of the MCP server config.

| Variable | Default | Description |
|---|---|---|
| `SWARM_MAX_CONCURRENT` | `10` | Maximum number of Docker containers running simultaneously across all active calls. |
| `SWARM_QUEUE_TIMEOUT` | `3600` | Seconds a queued agent will wait for a slot before timing out. |
| `SWARM_RESOURCE_<name>` | — | Capacity of a named resource pool. Example: `SWARM_RESOURCE_GPU=2` allows at most 2 agents that request the `gpu` resource to run at once. |
| `SWARM_PROJECT_DIR` | — | Primary search path for pipeline, sandbox, and type registry files. Falls back to `~/.claude/` when not set. |

To pass environment variables through the MCP config:

```json
{
  "mcpServers": {
    "swarm-mcp": {
      "command": "swarm-mcp",
      "type": "stdio",
      "env": {
        "SWARM_MAX_CONCURRENT": "20",
        "SWARM_PROJECT_DIR": "/home/user/my-project"
      }
    }
  }
}
```

## Troubleshooting

### Authentication errors inside containers

swarm-mcp injects OAuth credentials from `~/.claude/.credentials.json` into each container at runtime. If agents fail with authentication errors:

- Confirm you are logged in on the host: `claude whoami`
- Check the credentials file exists: `ls -la ~/.claude/.credentials.json`
- Re-authenticate if needed: `claude login`

### Docker socket permission denied

The user running swarm-mcp must have access to the Docker socket:

```bash
# Add your user to the docker group (requires a new login session)
sudo usermod -aG docker $USER
```

Or, if using Docker Desktop, ensure it is running and the socket path matches what Docker expects.

### Image not found: swarm-agent

The Docker build step was either skipped or used a different tag. Confirm with:

```bash
docker images swarm-agent
```

If the image is missing, follow the [Build the Docker Image](#build-the-docker-image) steps above. If you used a custom tag, set the image name in your sandbox spec's `image` field.

### Agents time out before completing

The default per-agent timeout is 1800 seconds (30 minutes). For long-running tasks, increase it via the `timeout` field in your `SandboxSpec`, or raise `SWARM_QUEUE_TIMEOUT` if the agent is spending time waiting in the queue rather than running.
