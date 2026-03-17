import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass

from . import docker
from .sandbox import SandboxSpec

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    agent_id: str
    text: str
    exit_code: int
    duration_seconds: float
    cost_usd: float | None
    model: str
    output_dir: str
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_CLAUDE_SUBDIRS = [
    "backups", "cache", "debug", "downloads", "file-history",
    "plans", "projects", "sessions", "statsig", "tasks", "telemetry",
    "usage-data",
]


def _setup_agent_home(output_dir: str, spec: SandboxSpec) -> str:
    """Generate a minimal HOME directory for the container agent.

    Writes claude config, MCP config, CLAUDE.md, and input files
    based on the sandbox spec.
    """
    home_dir = os.path.join(output_dir, "home")
    claude_dir = os.path.join(home_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    for subdir in _CLAUDE_SUBDIRS:
        os.makedirs(os.path.join(claude_dir, subdir), exist_ok=True)

    # Copy credentials (OAuth tokens) — must be readable by container user
    creds_src = os.path.join(docker.CLAUDE_DIR, ".credentials.json")
    if os.path.exists(creds_src):
        creds_dst = os.path.join(claude_dir, ".credentials.json")
        shutil.copy2(creds_src, creds_dst)
        os.chmod(creds_dst, 0o644)

    # Extract oauthAccount from host .claude.json, build minimal config
    oauth_account = {}
    if os.path.exists(docker.CLAUDE_JSON):
        with open(docker.CLAUDE_JSON) as f:
            host_config = json.load(f)
        oauth_account = host_config.get("oauthAccount", {})

    # If MCPs requested, build mcpServers config from host settings
    mcp_servers = {}
    if spec.mcps:
        mcp_servers = _resolve_mcp_config(spec.mcps)

    minimal_config = {
        "hasCompletedOnboarding": True,
        "oauthAccount": oauth_account,
    }
    if mcp_servers:
        minimal_config["mcpServers"] = mcp_servers

    with open(os.path.join(home_dir, ".claude.json"), "w") as f:
        json.dump(minimal_config, f)

    # Write CLAUDE.md to workspace if specified
    if spec.claude_md:
        workspace_dir = os.path.join(output_dir, "workspace")
        os.makedirs(workspace_dir, exist_ok=True)
        with open(os.path.join(workspace_dir, "CLAUDE.md"), "w") as f:
            f.write(spec.claude_md)

    # Write input files to workspace
    if spec.input_files:
        workspace_dir = os.path.join(output_dir, "workspace")
        os.makedirs(workspace_dir, exist_ok=True)
        for container_path, content in spec.input_files.items():
            # Strip leading / to make relative to workspace
            rel_path = container_path.lstrip("/")
            full_path = os.path.join(workspace_dir, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)

    return home_dir


def _resolve_mcp_config(mcp_names: list[str]) -> dict:
    """Read host .claude.json mcpServers and extract configs for requested MCPs."""
    if not os.path.exists(docker.CLAUDE_JSON):
        return {}
    with open(docker.CLAUDE_JSON) as f:
        host_config = json.load(f)
    host_mcps = host_config.get("mcpServers", {})
    result = {}
    for name in mcp_names:
        if name in host_mcps:
            result[name] = host_mcps[name]
        else:
            logger.warning("MCP server %s not found in host config", name)
    return result


def run_agent(prompt: str, spec: SandboxSpec, run_id: str, agent_id: str) -> AgentResult:
    output_dir = os.path.join("/tmp/swarm-mcp", run_id, agent_id)
    os.makedirs(output_dir, exist_ok=True)

    # Write prompt to file for stdin piping
    prompt_file = os.path.join(output_dir, "prompt.txt")
    with open(prompt_file, "w") as f:
        f.write(prompt)

    # Generate minimal HOME with claude config for the container
    _setup_agent_home(output_dir, spec)

    docker.ensure_image()

    cmd, container_name = docker.get_docker_run_cmd(
        run_id=run_id,
        agent_id=agent_id,
        output_dir=output_dir,
        spec=spec,
    )

    start = time.monotonic()

    try:
        with open(prompt_file) as stdin_f:
            result = subprocess.run(
                cmd,
                stdin=stdin_f,
                capture_output=True,
                text=True,
                timeout=spec.timeout + 10,
            )

        duration = time.monotonic() - start

        # Parse claude JSON output
        text = ""
        cost = None
        try:
            output = json.loads(result.stdout)
            text = output.get("result", result.stdout)
            cost = output.get("cost_usd") or output.get("total_cost_usd")
        except (json.JSONDecodeError, TypeError):
            text = result.stdout

        if result.returncode != 0:
            error_text = result.stderr or f"Exit code {result.returncode}"
            logger.warning("Agent %s/%s failed: %s", run_id, agent_id, error_text)
            return AgentResult(
                agent_id=agent_id,
                text=text,
                exit_code=result.returncode,
                duration_seconds=round(duration, 2),
                cost_usd=cost,
                model=spec.model,
                output_dir=output_dir,
                error=error_text,
            )

        # Write result to output dir
        result_file = os.path.join(output_dir, "result.json")
        agent_result = AgentResult(
            agent_id=agent_id,
            text=text,
            exit_code=0,
            duration_seconds=round(duration, 2),
            cost_usd=cost,
            model=spec.model,
            output_dir=output_dir,
        )
        with open(result_file, "w") as f:
            json.dump(agent_result.to_dict(), f, indent=2, default=str)

        return agent_result

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        logger.error("Agent %s/%s timed out after %ds", run_id, agent_id, spec.timeout)
        docker.kill_container(container_name)
        return AgentResult(
            agent_id=agent_id,
            text="",
            exit_code=-1,
            duration_seconds=round(duration, 2),
            cost_usd=None,
            model=spec.model,
            output_dir=output_dir,
            error=f"Timed out after {spec.timeout}s",
        )

    except Exception as e:
        duration = time.monotonic() - start
        logger.exception("Agent %s/%s unexpected error", run_id, agent_id)
        return AgentResult(
            agent_id=agent_id,
            text="",
            exit_code=-1,
            duration_seconds=round(duration, 2),
            cost_usd=None,
            model=spec.model,
            output_dir=output_dir,
            error=str(e),
        )
