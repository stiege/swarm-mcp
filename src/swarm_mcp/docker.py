import logging
import os
import shutil
import subprocess
import time

from .sandbox import SandboxSpec

logger = logging.getLogger(__name__)

IMAGE_NAME = "swarm-agent"
HOOKS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "hooks")
CLAUDE_DIR = os.path.expanduser("~/.claude")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")
CONTAINER_HOME = "/home/ubuntu"


def image_exists(name: str = IMAGE_NAME) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", name],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def build_image(dockerfile_dir: str) -> None:
    logger.info("Building %s image from %s", IMAGE_NAME, dockerfile_dir)
    subprocess.run(
        ["docker", "build", "-t", IMAGE_NAME, dockerfile_dir],
        check=True,
        timeout=600,
    )


def ensure_image(dockerfile_dir: str | None = None) -> None:
    if image_exists():
        return
    if dockerfile_dir is None:
        # Go up from src/swarm_mcp/docker.py to project root (3 levels)
        dockerfile_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    build_image(dockerfile_dir)


def get_docker_run_cmd(
    *,
    run_id: str,
    agent_id: str,
    output_dir: str,
    spec: SandboxSpec,
) -> tuple[list[str], str]:
    container_name = f"swarm-{run_id[:8]}-{agent_id[:8]}"
    allowed_tools = spec.tools or ["Read", "Write", "Glob", "Grep", "Bash"]

    # agent.py stages a home dir at output_dir/home/ with generated
    # .claude/ and .claude.json. Mount it as the container HOME so
    # claude can write freely (no bind-mount permission issues).
    home_dir = os.path.join(output_dir, "home")

    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        f"--network={'host' if spec.network else 'none'}",
        "-v", f"{home_dir}:{CONTAINER_HOME}",
        "-v", f"{output_dir}:/output:rw",
        "-e", f"HOME={CONTAINER_HOME}",
        "-e", "CLAUDECODE=",
        # Mount artifact logging hooks
        "-v", f"{HOOKS_DIR}:/opt/swarm/hooks:ro",
    ]

    # Resource limits
    if spec.memory:
        cmd.extend(["--memory", spec.memory])
    if spec.cpus:
        cmd.extend(["--cpus", str(spec.cpus)])

    # Custom environment variables
    for key, value in spec.env_vars.items():
        cmd.extend(["-e", f"{key}={value}"])

    # Mount workspace with input files / CLAUDE.md if prepared
    workspace_dir = os.path.join(output_dir, "workspace")
    if os.path.isdir(workspace_dir):
        cmd.extend(["-v", f"{workspace_dir}:{spec.workdir}:rw"])
        cmd.extend(["-w", spec.workdir])

    # Mount MCP project directories if MCPs are requested.
    # Mount at same host path so MCP configs work unchanged.
    if spec.mcps:
        MCP_BASE = os.path.expanduser("~/projects/mcp")
        if os.path.isdir(MCP_BASE):
            cmd.extend(["-v", f"{MCP_BASE}:{MCP_BASE}"])

    # Add user-specified mounts
    if spec.mounts:
        for mount in spec.mounts:
            host_path = mount["host_path"]
            container_path = mount["container_path"]
            mode = "ro" if mount.get("readonly", True) else "rw"
            cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])

    # Image and claude flags
    cmd.extend([
        IMAGE_NAME,
        "--print",
        "--permission-mode", "bypassPermissions",
        "--model", spec.model,
        "--allowedTools", ",".join(allowed_tools),
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
    ])

    # System prompt
    if spec.system_prompt:
        cmd.extend(["--system-prompt", spec.system_prompt])

    # Structured output schema
    if spec.output_schema:
        cmd.extend(["--json-schema", json.dumps(spec.output_schema)])

    # Effort level
    if spec.effort:
        cmd.extend(["--effort", spec.effort])

    # Budget
    if spec.max_budget is not None:
        cmd.extend(["--max-budget-usd", f"{spec.max_budget:.2f}"])
    elif spec.timeout > 0:
        max_budget = max(0.10, spec.timeout * 0.005)
        cmd.extend(["--max-budget-usd", f"{max_budget:.2f}"])

    return cmd, container_name


def kill_container(name: str) -> None:
    try:
        subprocess.run(
            ["docker", "kill", name],
            capture_output=True,
            timeout=10,
        )
        logger.info("Killed container %s", name)
    except Exception:
        logger.warning("Failed to kill container %s", name, exc_info=True)


def cleanup_old_runs(base_dir: str = "/tmp/swarm-mcp", max_age_hours: int = 24) -> int:
    if not os.path.isdir(base_dir):
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0

    for entry in os.scandir(base_dir):
        if entry.is_dir() and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
            logger.info("Removed old run directory: %s", entry.path)

    return removed


# Needed for json.dumps in get_docker_run_cmd
import json  # noqa: E402
