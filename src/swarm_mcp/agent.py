import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass

from . import docker
from .sandbox import SandboxSpec
from .types import build_type_context

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
