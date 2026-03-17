"""Core agent runner — launches Claude inside a Docker container and captures output.

This module is the heart of swarm-mcp.  :func:`run_agent` handles the full
lifecycle of a single agent execution:

1. Writing the prompt to disk and staging a minimal ``HOME`` directory with
   Claude credentials and MCP configuration (:func:`_setup_agent_home`).
2. Calling :func:`~swarm_mcp.docker.ensure_image` to guarantee the Docker
   image is present.
3. Running ``docker run`` via :func:`~swarm_mcp.docker.get_docker_run_cmd`,
   streaming ``stream-json`` output to a ``.jsonl`` file.
4. Parsing the accumulated stream output into final text and cost
   (:func:`_parse_stream_output`).
5. Returning an :class:`AgentResult` dataclass (and writing ``result.json``
   to the output directory for later ref-resolution).

Timeout handling ensures partial output is captured even when a container
is killed mid-run.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass

from . import docker
from .monads import enrich_ref
from .sandbox import SandboxSpec
from .types import build_type_context

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Result of a single agent execution inside a Docker container.

    Attributes:
        agent_id: Identifier for this agent within its run (e.g.
            ``"agent-0"``, ``"stage-1"``).
        text: Full text output produced by the agent (may be partial if the
            run timed out).
        exit_code: Docker container exit code.  ``0`` indicates success;
            ``-1`` indicates a timeout or internal error.
        duration_seconds: Wall-clock execution time in seconds (rounded to
            two decimal places).
        cost_usd: Estimated API cost in US dollars, or ``None`` if cost
            information was not emitted by Claude.
        model: Claude model alias used for this run (e.g. ``"sonnet"``).
        output_dir: Absolute host path to the agent's output directory,
            containing ``result.json``, ``stream.jsonl``, and any files
            written by the agent.
        error: Human-readable error message if the run failed, or ``None``
            on success.
    """

    agent_id: str
    text: str
    exit_code: int
    duration_seconds: float
    cost_usd: float | None
    model: str
    output_dir: str
    error: str | None = None

    def to_dict(self) -> dict:
        """Serialise the result to a plain dict (all fields, including ``None`` values).

        Returns:
            A dict representation suitable for JSON serialisation.
        """
        return asdict(self)

    def to_ref_dict(self, run_id: str, **monadic_context) -> dict:
        """Return a lightweight ref dict — metadata and optional monadic stamps, without text.

        The full text remains on disk at ``output_dir/result.json`` and can be
        retrieved with the MCP ``unwrap`` tool.  This keeps MCP protocol
        messages small.

        Args:
            run_id: Run identifier used to build the ``"ref"`` path and passed
                to :func:`~swarm_mcp.monads.enrich_ref`.
            **monadic_context: Optional keyword arguments forwarded to
                :func:`~swarm_mcp.monads.enrich_ref` (e.g. ``budget_limit``,
                ``classification``, ``encrypt``).

        Returns:
            A dict with ``agent_id``, ``ref``, ``exit_code``,
            ``duration_seconds``, ``cost_usd``, ``model``, ``output_dir``,
            and ``error`` fields, plus any monadic stamps applied.
        """
        ref = {
            "agent_id": self.agent_id,
            "ref": f"{run_id}/{self.agent_id}",
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "cost_usd": self.cost_usd,
            "model": self.model,
            "output_dir": self.output_dir,
            "error": self.error,
        }
        # Apply monadic enrichment if context provided
        if monadic_context:
            enrich_ref(ref, run_id, text=self.text, **monadic_context)
        return ref


_CLAUDE_SUBDIRS = [
    "backups", "cache", "debug", "downloads", "file-history",
    "plans", "projects", "sessions", "statsig", "tasks", "telemetry",
    "usage-data",
]
"""Subdirectories pre-created inside the container's ``~/.claude/`` to prevent
Claude from complaining about missing directories on first run."""


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

    # Inject PostToolUse hook for artifact logging
    settings = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "mcp__.*|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/opt/swarm/hooks/log-artifacts.sh",
                            "timeout": 5,
                        }
                    ],
                }
            ]
        }
    }
    with open(os.path.join(claude_dir, "settings.json"), "w") as f:
        json.dump(settings, f)

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


def _parse_stream_output(stream_file: str) -> tuple[str, float | None]:
    """Parse accumulated stream-json lines into final text and cost.

    stream-json emits one JSON object per line. We look for:
    - {"type": "assistant", "message": {"content": [{"text": "..."}]}} — content chunks
    - {"type": "result", ...} — final result with cost info

    Returns (accumulated_text, cost_usd).
    """
    text_parts = []
    cost = None

    if not os.path.exists(stream_file):
        return "", None

    with open(stream_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")

            if msg_type == "result":
                # Final result line — has the complete text and cost
                text_parts = [obj.get("result", "")]
                cost = obj.get("cost_usd") or obj.get("total_cost_usd")
                break

            if msg_type == "assistant":
                # Content chunk
                message = obj.get("message", {})
                for block in message.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif isinstance(block, str):
                        text_parts.append(block)

            if msg_type == "content_block_delta":
                delta = obj.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))

    return "".join(text_parts), cost


def run_agent(prompt: str, spec: SandboxSpec, run_id: str, agent_id: str) -> AgentResult:
    """Run a single Claude agent inside a Docker container and return its result.

    The full execution flow:

    1. Creates ``/tmp/swarm-mcp/<run_id>/<agent_id>/`` as the output directory.
    2. Prepends type-context to the prompt if ``spec.input_type`` or
       ``spec.output_type`` is set.
    3. Stages a minimal ``HOME`` directory with Claude credentials and MCP
       config via :func:`_setup_agent_home`.
    4. Calls :func:`~swarm_mcp.docker.ensure_image` (no-op if image exists).
    5. Launches the container, streaming ``stream-json`` output to
       ``stream.jsonl`` in the output directory.
    6. Waits up to ``spec.timeout + 10`` seconds; kills the container on
       ``TimeoutExpired`` and captures any partial output.
    7. Parses the stream log, writes ``result.json``, and returns an
       :class:`AgentResult`.

    Args:
        prompt: The task description sent to the agent via stdin.
        spec: Fully resolved :class:`~swarm_mcp.sandbox.SandboxSpec` that
            controls model, tools, resource limits, and environment.
        run_id: Shared run identifier grouping agents from the same combinator
            call (used to construct output paths and ref strings).
        agent_id: Unique agent identifier within the run (e.g. ``"agent-0"``,
            ``"stage-2"``).

    Returns:
        An :class:`AgentResult` with the agent's text output, exit code,
        duration, cost, and any error message.  Partial output is preserved
        even on timeout or unexpected exit.
    """
    output_dir = os.path.join("/tmp/swarm-mcp", run_id, agent_id)
    os.makedirs(output_dir, exist_ok=True)

    # Inject type context into prompt if types are declared
    type_context = build_type_context(spec.input_type, spec.output_type)
    full_prompt = f"{type_context}\n\n{prompt}" if type_context else prompt

    # Write prompt to file for stdin piping
    prompt_file = os.path.join(output_dir, "prompt.txt")
    with open(prompt_file, "w") as f:
        f.write(full_prompt)

    # Generate minimal HOME with claude config for the container
    _setup_agent_home(output_dir, spec)

    docker.ensure_image()

    cmd, container_name = docker.get_docker_run_cmd(
        run_id=run_id,
        agent_id=agent_id,
        output_dir=output_dir,
        spec=spec,
    )

    stream_file = os.path.join(output_dir, "stream.jsonl")
    start = time.monotonic()
    timed_out = False

    try:
        with open(prompt_file) as stdin_f, open(stream_file, "w") as stream_f:
            proc = subprocess.Popen(
                cmd,
                stdin=stdin_f,
                stdout=stream_f,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stderr_output = proc.communicate(timeout=spec.timeout + 10)[1]
            except subprocess.TimeoutExpired:
                timed_out = True
                stderr_output = ""
                logger.error("Agent %s/%s timed out after %ds", run_id, agent_id, spec.timeout)
                docker.kill_container(container_name)
                proc.kill()
                proc.wait()

        duration = time.monotonic() - start

        # Parse whatever we captured — works for both complete and partial output
        text, cost = _parse_stream_output(stream_file)

        if timed_out:
            error_msg = f"Timed out after {spec.timeout}s"
            if text:
                error_msg += f" (partial output captured: {len(text)} chars)"
            agent_result = AgentResult(
                agent_id=agent_id,
                text=text,  # Partial output — the key improvement
                exit_code=-1,
                duration_seconds=round(duration, 2),
                cost_usd=cost,
                model=spec.model,
                output_dir=output_dir,
                error=error_msg,
            )
        elif proc.returncode != 0:
            error_text = stderr_output or f"Exit code {proc.returncode}"
            logger.warning("Agent %s/%s failed: %s", run_id, agent_id, error_text)
            agent_result = AgentResult(
                agent_id=agent_id,
                text=text,
                exit_code=proc.returncode,
                duration_seconds=round(duration, 2),
                cost_usd=cost,
                model=spec.model,
                output_dir=output_dir,
                error=error_text,
            )
        else:
            agent_result = AgentResult(
                agent_id=agent_id,
                text=text,
                exit_code=0,
                duration_seconds=round(duration, 2),
                cost_usd=cost,
                model=spec.model,
                output_dir=output_dir,
            )

        # Always write result to output dir
        result_file = os.path.join(output_dir, "result.json")
        with open(result_file, "w") as f:
            json.dump(agent_result.to_dict(), f, indent=2, default=str)

        return agent_result

    except Exception as e:
        duration = time.monotonic() - start
        logger.exception("Agent %s/%s unexpected error", run_id, agent_id)

        # Try to salvage partial output
        text, cost = _parse_stream_output(stream_file)

        return AgentResult(
            agent_id=agent_id,
            text=text,
            exit_code=-1,
            duration_seconds=round(duration, 2),
            cost_usd=cost,
            model=spec.model,
            output_dir=output_dir,
            error=str(e),
        )
