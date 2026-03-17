import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import docker

logger = logging.getLogger(__name__)


@dataclass
class EnvConfig:
    network: bool = False
    tools: list[str] = field(default_factory=lambda: ["Read", "Write", "Glob", "Grep", "Bash"])
    mounts: list[dict] = field(default_factory=list)
    model: str = "sonnet"
    timeout: int = 120


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


def parse_env_config(
    network: bool = False,
    tools: str = "Read,Write,Glob,Grep,Bash",
    mounts: str = "[]",
    model: str = "sonnet",
    timeout: int = 120,
) -> EnvConfig:
    tool_list = [t.strip() for t in tools.split(",") if t.strip()]
    mount_list = json.loads(mounts) if isinstance(mounts, str) else mounts
    return EnvConfig(
        network=network,
        tools=tool_list,
        mounts=mount_list,
        model=model,
        timeout=timeout,
    )


_CLAUDE_SUBDIRS = [
    "backups", "cache", "debug", "downloads", "file-history",
    "plans", "projects", "sessions", "statsig", "tasks", "telemetry",
    "usage-data",
]


def _setup_agent_home(output_dir: str) -> str:
    """Generate a minimal HOME directory for the container agent.

    Returns the path to the home directory (output_dir/home/).
    """
    home_dir = os.path.join(output_dir, "home")
    claude_dir = os.path.join(home_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    # Create subdirectories claude expects to write to
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

    minimal_config = {
        "hasCompletedOnboarding": True,
        "oauthAccount": oauth_account,
    }
    with open(os.path.join(home_dir, ".claude.json"), "w") as f:
        json.dump(minimal_config, f)

    return home_dir


def run_agent(prompt: str, env: EnvConfig, run_id: str, agent_id: str) -> AgentResult:
    output_dir = os.path.join("/tmp/swarm-mcp", run_id, agent_id)
    os.makedirs(output_dir, exist_ok=True)

    # Write prompt to file for stdin piping
    prompt_file = os.path.join(output_dir, "prompt.txt")
    with open(prompt_file, "w") as f:
        f.write(prompt)

    # Generate minimal HOME with claude config for the container
    _setup_agent_home(output_dir)

    docker.ensure_image()

    cmd, container_name = docker.get_docker_run_cmd(
        run_id=run_id,
        agent_id=agent_id,
        output_dir=output_dir,
        network=env.network,
        mounts=env.mounts,
        model=env.model,
        tools=env.tools,
        timeout=env.timeout,
    )

    start = time.monotonic()

    try:
        with open(prompt_file) as stdin_f:
            result = subprocess.run(
                cmd,
                stdin=stdin_f,
                capture_output=True,
                text=True,
                timeout=env.timeout + 10,
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
                model=env.model,
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
            model=env.model,
            output_dir=output_dir,
        )
        with open(result_file, "w") as f:
            json.dump(agent_result.to_dict(), f, indent=2, default=str)

        return agent_result

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        logger.error("Agent %s/%s timed out after %ds", run_id, agent_id, env.timeout)
        docker.kill_container(container_name)
        return AgentResult(
            agent_id=agent_id,
            text="",
            exit_code=-1,
            duration_seconds=round(duration, 2),
            cost_usd=None,
            model=env.model,
            output_dir=output_dir,
            error=f"Timed out after {env.timeout}s",
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
            model=env.model,
            output_dir=output_dir,
            error=str(e),
        )
