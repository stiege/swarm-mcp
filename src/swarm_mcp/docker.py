"""Docker integration layer for the swarm-mcp agent runner.

This module is responsible for everything Docker-related:

- Image management: checking whether the ``swarm-agent`` image exists and
  building it on first use (:func:`ensure_image`).
- Command construction: assembling the ``docker run`` invocation for a given
  :class:`~swarm_mcp.sandbox.SandboxSpec` (:func:`get_docker_run_cmd`).
- Container lifecycle: killing a running container (:func:`kill_container`).
- Cleanup: removing old run directories from ``/tmp/swarm-mcp``
  (:func:`cleanup_old_runs`).

Module-level constants define the image name and well-known host paths that
are mounted into every container.
"""

import json
import logging
import os
import shutil
import subprocess
import time

from .sandbox import SandboxSpec

logger = logging.getLogger(__name__)

IMAGE_NAME = "swarm-agent"
"""Docker image tag built from the project's Dockerfile."""

HOOKS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "hooks")
"""Absolute path to the project-level ``hooks/`` directory, mounted read-only into every container."""

CLAUDE_DIR = os.path.expanduser("~/.claude")
"""Host path for Claude's configuration directory, used to copy credentials."""

CLAUDE_JSON = os.path.expanduser("~/.claude.json")
"""Host path for the primary Claude JSON config (OAuth tokens, MCP server list)."""

CONTAINER_HOME = "/home/ubuntu"
"""Home directory path *inside* the container; the staged home dir is bind-mounted here."""

# Default budget-per-second used when no explicit max_budget is set.
# The formula: max(0.10, timeout * _BUDGET_PER_SECOND)
_BUDGET_PER_SECOND = 0.005


def image_exists(name: str = IMAGE_NAME) -> bool:
    """Check whether a Docker image with the given tag exists locally.

    Args:
        name: Docker image tag to inspect (default: ``IMAGE_NAME``).

    Returns:
        ``True`` if the image exists, ``False`` otherwise.
    """
    result = subprocess.run(
        ["docker", "image", "inspect", name],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def build_image(dockerfile_dir: str) -> None:
    """Build the ``swarm-agent`` Docker image from a Dockerfile directory.

    Runs ``docker build -t IMAGE_NAME <dockerfile_dir>`` with a 10-minute
    timeout and raises :class:`subprocess.CalledProcessError` on failure.

    Args:
        dockerfile_dir: Path to the directory containing the ``Dockerfile``.
    """
    logger.info("Building %s image from %s", IMAGE_NAME, dockerfile_dir)
    subprocess.run(
        ["docker", "build", "-t", IMAGE_NAME, dockerfile_dir],
        check=True,
        timeout=600,
    )


def ensure_image(dockerfile_dir: str | None = None) -> None:
    """Ensure the ``swarm-agent`` Docker image is available, building it if necessary.

    If the image already exists locally this is a fast no-op (a single
    ``docker image inspect`` call).  Otherwise :func:`build_image` is invoked.

    Args:
        dockerfile_dir: Directory containing the ``Dockerfile``.  When
            ``None``, defaults to the project root (three levels above this
            module file).
    """
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
    """Construct the ``docker run`` command for a single agent execution.

    Assembles all flags required to run the ``swarm-agent`` image with the
    appropriate mounts, resource limits, environment variables, and Claude CLI
    flags derived from *spec*.

    Args:
        run_id: Unique run identifier (used to name the container).
        agent_id: Agent identifier within the run (used to name the container).
        output_dir: Host-side output directory that will be bind-mounted at
            ``/output`` inside the container.
        spec: Fully resolved :class:`~swarm_mcp.sandbox.SandboxSpec`.

    Returns:
        A ``(cmd, container_name)`` tuple where *cmd* is the list of
        arguments for :func:`subprocess.Popen` and *container_name* is the
        ``--name`` given to the container (used to kill it on timeout).
    """
    container_name = f"swarm-{run_id[:8]}-{agent_id[:8]}"
    allowed_tools = spec.tools or ["Read", "Write", "Glob", "Grep", "Bash"]

    # agent.py stages a home dir at output_dir/home/ with generated
    # .claude/ and .claude.json. Mount it as the container HOME so
    # claude can write freely (no bind-mount permission issues).
    home_dir = os.path.join(output_dir, "home")

    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        f"--network={spec.network_mode if spec.network_mode else ('host' if spec.network else 'none')}",
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
    if spec.gpu or "gpu" in spec.resources:
        cmd.extend(["--gpus", "all"])

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
        mcp_base = os.path.expanduser("~/projects/mcp")
        if os.path.isdir(mcp_base):
            cmd.extend(["-v", f"{mcp_base}:{mcp_base}"])

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
        max_budget = max(0.10, spec.timeout * _BUDGET_PER_SECOND)
        cmd.extend(["--max-budget-usd", f"{max_budget:.2f}"])

    return cmd, container_name


def kill_container(name: str) -> None:
    """Send ``docker kill`` to a running container, suppressing all errors.

    This is a best-effort operation used during timeout handling.  If the
    container has already exited or the kill fails for any reason the error is
    logged at WARNING level and execution continues.

    Args:
        name: Docker container name to kill.
    """
    try:
        subprocess.run(
            ["docker", "kill", name],
            capture_output=True,
            timeout=10,
        )
        logger.info("Killed container %s", name)
    except Exception:
        logger.warning("Failed to kill container %s", name, exc_info=True)


def kill_pipeline_containers(run_id: str) -> list[str]:
    """Kill all Docker containers associated with a pipeline run.

    Containers are named ``swarm-{run_id[:8]}-*``.  Returns a list of
    container IDs that were killed.
    """
    prefix = f"swarm-{run_id[:8]}"
    killed = []
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={prefix}", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        container_ids = [c for c in result.stdout.strip().splitlines() if c]
        for cid in container_ids:
            try:
                subprocess.run(["docker", "kill", cid], capture_output=True, timeout=10)
                killed.append(cid)
                logger.info("Killed pipeline container %s (run %s)", cid, run_id)
            except Exception:
                logger.warning("Failed to kill container %s", cid, exc_info=True)
    except Exception:
        logger.warning("Failed to list containers for run %s", run_id, exc_info=True)
    return killed


def cleanup_old_runs(base_dir: str = "/tmp/swarm-mcp", max_age_hours: int = 24) -> int:
    """Remove run directories older than *max_age_hours* from *base_dir*.

    Intended for periodic housekeeping.  Each top-level subdirectory of
    *base_dir* is removed with ``shutil.rmtree`` if its ``mtime`` is older
    than the cutoff.  Errors during individual directory removal are silently
    ignored (``ignore_errors=True``).

    Args:
        base_dir: Root directory containing per-run subdirectories.
        max_age_hours: Age threshold in hours (default: 24).

    Returns:
        Number of directories removed.
    """
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


