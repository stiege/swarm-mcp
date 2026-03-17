import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from mcp.server.fastmcp import FastMCP

from . import monads, tools as response_tools
from .agent import AgentResult, run_agent
from .sandbox import SandboxSpec, list_sandboxes, load_sandbox, resolve_sandbox, save_sandbox
from . import registry
from .types import build_validation_prompt, get_type, list_types as _list_types, resolve_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("swarm-mcp")

MAX_CONCURRENT = int(os.environ.get("SWARM_MAX_CONCURRENT", "10"))
_semaphore = threading.Semaphore(MAX_CONCURRENT)

# network defaults to True because every agent needs to reach the Anthropic API.
# Set to False only if using a local model backend (e.g. Ollama) in the future.
NETWORK_DEFAULT = True

# PIPELINE_DIR removed — pipelines now found via registry search paths


def _generate_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _run_with_semaphore(prompt: str, spec: SandboxSpec, run_id: str, agent_id: str) -> AgentResult:
    acquired = _semaphore.acquire(timeout=spec.timeout)
    if not acquired:
        return AgentResult(
            agent_id=agent_id,
            text="",
            exit_code=-1,
            duration_seconds=0,
            cost_usd=None,
            model=spec.model,
            output_dir="",
            error=f"Could not acquire execution slot within {spec.timeout}s (max concurrent: {MAX_CONCURRENT})",
        )
    try:
        return run_agent(prompt, spec, run_id, agent_id)
    finally:
        _semaphore.release()


def _resolve_ref(ref: str) -> str:
    """Resolve a reference like 'run_id/agent_id' to its result text."""
    result_file = os.path.join("/tmp/swarm-mcp", ref, "result.json")
    with open(result_file) as f:
        data = json.load(f)
    return data.get("text", "")


def _extract_texts(items: list) -> list[str]:
    """Extract text from a list that may contain:
    - plain strings (passed through)
    - AgentResult dicts with .text (unwrapped)
    - references {"ref": "run_id/agent_id"} (resolved from disk)

    This is the 'unwrap' / 'bind' step that makes combinators composable.
    """
    texts = []
    for item in items:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict):
            if "ref" in item:
                texts.append(_resolve_ref(item["ref"]))
            elif "text" in item:
                texts.append(item["text"])
            else:
                texts.append(str(item))
        else:
            texts.append(str(item))
    return texts


def _resolve_spec(sandbox: str | None, **kwargs) -> SandboxSpec:
    """Build a SandboxSpec from a sandbox name/JSON and inline overrides."""
    # Parse tools from comma-separated string
    tools_str = kwargs.pop("tools", None)
    tools_list = None
    if tools_str:
        tools_list = [t.strip() for t in tools_str.split(",") if t.strip()]

    # Parse mounts from JSON string or list
    mounts_raw = kwargs.pop("mounts", None)
    mounts_list = None
    if mounts_raw:
        if isinstance(mounts_raw, list):
            mounts_list = mounts_raw
        elif isinstance(mounts_raw, str):
            mounts_list = json.loads(mounts_raw)
        else:
            mounts_list = list(mounts_raw)

    # Parse mcps from JSON string or list
    mcps_raw = kwargs.pop("mcps", None)
    mcps_list = None
    if mcps_raw:
        if isinstance(mcps_raw, list):
            mcps_list = mcps_raw
        elif isinstance(mcps_raw, str):
            mcps_list = json.loads(mcps_raw)
        else:
            mcps_list = list(mcps_raw)

    # Parse input_files from JSON string or dict
    input_files_raw = kwargs.pop("input_files", None)
    input_files_dict = None
    if input_files_raw:
        if isinstance(input_files_raw, dict):
            input_files_dict = input_files_raw
        elif isinstance(input_files_raw, str):
            input_files_dict = json.loads(input_files_raw)

    # Parse output_schema from JSON string or dict
    schema_raw = kwargs.pop("output_schema", None)
    schema_dict = None
    if schema_raw:
        if isinstance(schema_raw, dict):
            schema_dict = schema_raw
        elif isinstance(schema_raw, str):
            schema_dict = json.loads(schema_raw)

    # Parse env_vars from JSON string or dict
    env_raw = kwargs.pop("env_vars", None)
    env_dict = None
    if env_raw:
        if isinstance(env_raw, dict):
            env_dict = env_raw
        elif isinstance(env_raw, str):
            env_dict = json.loads(env_raw)

    overrides = {k: v for k, v in kwargs.items() if v is not None}
    if tools_list is not None:
        overrides["tools"] = tools_list
    if mounts_list is not None:
        overrides["mounts"] = mounts_list
    if mcps_list is not None:
        overrides["mcps"] = mcps_list
    if input_files_dict is not None:
        overrides["input_files"] = input_files_dict
    if schema_dict is not None:
        overrides["output_schema"] = schema_dict
    if env_dict is not None:
        overrides["env_vars"] = env_dict

    return resolve_sandbox(sandbox, **overrides)


def _run_par_internal(task_list: list[dict], max_concurrency: int) -> tuple[str, list[AgentResult]]:
    """Shared parallel execution logic. Returns (run_id, results)."""
    run_id = _generate_run_id()
    effective_concurrency = min(max_concurrency, len(task_list), MAX_CONCURRENT)

    def execute_task(i_task: tuple[int, dict]) -> AgentResult:
        i, task = i_task
        # Only pass keys that are actually in the task dict — let sandbox defaults apply
        overrides = {k: v for k, v in task.items() if k not in ("prompt", "sandbox") and v is not None}
        spec = _resolve_spec(task.get("sandbox"), **overrides)
        return _run_with_semaphore(task["prompt"], spec, run_id, f"agent-{i}")

    with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
        results = list(executor.map(execute_task, enumerate(task_list)))

    return run_id, results


# ── Combinator Tools ──────────────────────────────────────────────


@mcp.tool()
def run(
    prompt: str,
    sandbox: str | None = None,
    network: bool = NETWORK_DEFAULT,
    tools: str = "Read,Write,Glob,Grep,Bash",
    mounts: str = "[]",
    model: str = "sonnet",
    timeout: int = 120,
    system_prompt: str | None = None,
    claude_md: str | None = None,
    output_schema: str | None = None,
    mcps: str | list | None = None,
    effort: str | None = None,
    max_budget: float | None = None,
    env_vars: str | None = None,
    input_files: str | None = None,
    memory: str | None = None,
    cpus: float | None = None,
    input_type: str | None = None,
    output_type: str | None = None,
) -> str:
    """Run a single Claude agent in a Docker container. Returns the agent's text output and metadata.

    Args:
        prompt: The task prompt for the agent.
        sandbox: Named sandbox spec (from ~/.claude/sandboxes/) or inline JSON. Overrides below are merged on top.
        network: Whether the container has network access (default: true — needed for API calls).
        tools: Comma-separated list of allowed Claude tools (default: Read,Write,Glob,Grep,Bash).
        mounts: JSON array of mount specs: [{"host_path": "...", "container_path": "...", "readonly": true}].
        model: Claude model to use (default: sonnet). Options: haiku, sonnet, opus.
        timeout: Max execution time in seconds (default: 120).
        system_prompt: System prompt injected via --system-prompt (role, persona, instructions).
        claude_md: Project instructions written to workspace CLAUDE.md.
        output_schema: JSON schema string for structured output (--json-schema).
        mcps: JSON array of MCP server names to attach: ["database-mcp", "whatsapp"].
        effort: Effort level: low, medium, high, max.
        max_budget: Explicit USD budget cap.
        env_vars: JSON object of environment variables: {"KEY": "value"}.
        input_files: JSON object of files to inject: {"/path": "content"}.
        memory: Docker memory limit (e.g. "2g").
        cpus: Docker CPU limit (e.g. 2.0).
        input_type: Natural language type describing what the agent receives (e.g. "research notes", "[code-review]").
        output_type: Natural language type describing what the agent must produce (e.g. "[mcp-server] with [test-suite]").
    """
    try:
        spec = _resolve_spec(
            sandbox, network=network, tools=tools, mounts=mounts, model=model,
            timeout=timeout, system_prompt=system_prompt, claude_md=claude_md,
            output_schema=output_schema, mcps=mcps, effort=effort,
            max_budget=max_budget, env_vars=env_vars, input_files=input_files,
            memory=memory, cpus=cpus, input_type=input_type, output_type=output_type,
        )
        run_id = _generate_run_id()
        result = _run_with_semaphore(prompt, spec, run_id, "agent-0")
        return json.dumps(result.to_ref_dict(run_id), default=str)
    except Exception as e:
        logger.exception("run failed")
        return json.dumps(response_tools.error_response("run_error", str(e)))


@mcp.tool()
def par(
    tasks: str,
    max_concurrency: int = 5,
) -> str:
    """Run multiple Claude agents in parallel. Each task can have its own config.

    Args:
        tasks: JSON array of task objects. Each supports all sandbox fields (prompt, model, tools, sandbox, system_prompt, claude_md, output_schema, mcps, effort, etc.).
        max_concurrency: Max agents running simultaneously (default: 5).
    """
    try:
        task_list = json.loads(tasks)
        if not isinstance(task_list, list) or len(task_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "tasks must be a non-empty JSON array"))

        run_id, results = _run_par_internal(task_list, max_concurrency)

        data = {
            "run_id": run_id,
            "total": len(results),
            "succeeded": sum(1 for r in results if r.error is None),
            "failed": sum(1 for r in results if r.error is not None),
            "results": [r.to_ref_dict(run_id) for r in results],
        }
        return json.dumps(data, default=str)

    except json.JSONDecodeError as e:
        return json.dumps(response_tools.error_response("json_error", f"Failed to parse tasks JSON: {e}"))
    except Exception as e:
        logger.exception("par failed")
        return json.dumps(response_tools.error_response("par_error", str(e)))


@mcp.tool()
def map(
    prompt_template: str,
    inputs: str,
    sandbox: str | None = None,
    network: bool = NETWORK_DEFAULT,
    tools: str = "Read,Write,Glob,Grep,Bash",
    model: str = "sonnet",
    timeout: int = 120,
    max_concurrency: int = 5,
    system_prompt: str | None = None,
    claude_md: str | None = None,
    output_schema: str | None = None,
    mcps: str | list | None = None,
    effort: str | None = None,
) -> str:
    """Apply a prompt template to each input in parallel. Use {input} as the placeholder.

    Args:
        prompt_template: Prompt template with {input} placeholder(s).
        inputs: JSON array of input strings: ["input1", "input2", ...].
        sandbox: Named sandbox spec or inline JSON.
        network: Whether containers have network access (default: true — needed for API calls).
        tools: Comma-separated list of allowed Claude tools.
        model: Claude model to use (default: sonnet).
        timeout: Max execution time per agent in seconds (default: 120).
        max_concurrency: Max agents running simultaneously (default: 5).
        system_prompt: System prompt injected via --system-prompt.
        claude_md: Project instructions written to workspace CLAUDE.md.
        output_schema: JSON schema string for structured output.
        mcps: JSON array of MCP server names to attach.
        effort: Effort level: low, medium, high, max.
    """
    try:
        input_list = json.loads(inputs)
        if not isinstance(input_list, list) or len(input_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "inputs must be a non-empty JSON array"))

        task_list = [
            {
                "prompt": prompt_template.replace("{input}", str(inp)),
                "sandbox": sandbox,
                "network": network,
                "tools": tools,
                "model": model,
                "timeout": timeout,
                "system_prompt": system_prompt,
                "claude_md": claude_md,
                "output_schema": output_schema,
                "mcps": mcps,
                "effort": effort,
            }
            for inp in input_list
        ]

        return par(json.dumps(task_list), max_concurrency=max_concurrency)

    except json.JSONDecodeError as e:
        return json.dumps(response_tools.error_response("json_error", f"Failed to parse inputs JSON: {e}"))
    except Exception as e:
        logger.exception("map failed")
        return json.dumps(response_tools.error_response("map_error", str(e)))


@mcp.tool()
def chain(
    stages: str,
) -> str:
    """Run agents sequentially as a pipeline. Each stage receives the prior stage's output as context.

    Args:
        stages: JSON array of stage objects. Each supports all sandbox fields (prompt, model, tools, sandbox, system_prompt, etc.).
    """
    try:
        stage_list = json.loads(stages)
        if not isinstance(stage_list, list) or len(stage_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "stages must be a non-empty JSON array"))

        run_id = _generate_run_id()
        intermediates = []
        previous_text = None

        for i, stage in enumerate(stage_list):
            prompt = stage["prompt"]
            if previous_text is not None:
                prompt += f"\n\n# Context from previous stage:\n{previous_text}"

            overrides = {k: v for k, v in stage.items() if k not in ("prompt", "sandbox", "id", "on_fail", "next", "condition", "max_retries") and v is not None}
            spec = _resolve_spec(stage.get("sandbox"), **overrides)

            result = _run_with_semaphore(prompt, spec, run_id, f"stage-{i}")
            intermediates.append(result.to_ref_dict(run_id))

            if result.error is not None:
                data = {
                    "run_id": run_id,
                    "completed_stages": i,
                    "total_stages": len(stage_list),
                    "error": f"Stage {i} failed: {result.error}",
                    "intermediates": intermediates,
                }
                return json.dumps(data, default=str)

            previous_text = result.text

        data = {
            "run_id": run_id,
            "completed_stages": len(stage_list),
            "total_stages": len(stage_list),
            "final": intermediates[-1],
            "intermediates": intermediates,
        }
        return json.dumps(data, default=str)

    except json.JSONDecodeError as e:
        return json.dumps(response_tools.error_response("json_error", f"Failed to parse stages JSON: {e}"))
    except Exception as e:
        logger.exception("chain failed")
        return json.dumps(response_tools.error_response("chain_error", str(e)))


@mcp.tool()
def reduce(
    results: str,
    synthesis_prompt: str,
    sandbox: str | None = None,
    network: bool = NETWORK_DEFAULT,
    tools: str = "Read,Write,Glob,Grep,Bash",
    model: str = "sonnet",
    timeout: int = 120,
    system_prompt: str | None = None,
    mcps: str | list | None = None,
) -> str:
    """Synthesise multiple results into one. Accepts plain strings or structured AgentResult objects
    (auto-extracts .text fields), so you can pipe par/map output directly without manual unwrapping.

    Args:
        results: JSON array — either plain strings ["text1", "text2"] or AgentResult objects [{"text": "...", ...}].
        synthesis_prompt: Instructions for how to synthesise the results.
        sandbox: Named sandbox spec or inline JSON.
        network: Whether the container has network access (default: true — needed for API calls).
        tools: Comma-separated list of allowed Claude tools.
        model: Claude model to use (default: sonnet).
        timeout: Max execution time in seconds (default: 120).
        mcps: JSON array of MCP server names to attach to the reducer agent.
        system_prompt: System prompt for the reducer agent.
    """
    try:
        result_list = json.loads(results)
        if not isinstance(result_list, list) or len(result_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "results must be a non-empty JSON array"))

        texts = _extract_texts(result_list)

        sections = []
        for i, text in enumerate(texts):
            sections.append(f"## Input {i + 1}\n{text}")

        full_prompt = synthesis_prompt + "\n\n# Inputs to synthesise:\n\n" + "\n\n---\n\n".join(sections)

        spec = _resolve_spec(
            sandbox, network=network, tools=tools, model=model,
            timeout=timeout, system_prompt=system_prompt, mcps=mcps,
        )
        run_id = _generate_run_id()
        result = _run_with_semaphore(full_prompt, spec, run_id, "reducer")

        data = {
            "run_id": run_id,
            "input_count": len(texts),
            "result": result.to_ref_dict(run_id),
        }
        return json.dumps(data, default=str)

    except json.JSONDecodeError as e:
        return json.dumps(response_tools.error_response("json_error", f"Failed to parse results JSON: {e}"))
    except Exception as e:
        logger.exception("reduce failed")
        return json.dumps(response_tools.error_response("reduce_error", str(e)))


@mcp.tool()
def map_reduce(
    prompt_template: str,
    inputs: str,
    synthesis_prompt: str,
    sandbox: str | None = None,
    network: bool = NETWORK_DEFAULT,
    tools: str = "Read,Write,Glob,Grep,Bash",
    model: str = "sonnet",
    reduce_model: str = "",
    timeout: int = 120,
    max_concurrency: int = 5,
    system_prompt: str | None = None,
    reduce_system_prompt: str | None = None,
    output_schema: str | None = None,
    mcps: str | list | None = None,
    effort: str | None = None,
) -> str:
    """Map a prompt over inputs in parallel, then reduce results into one — all in a single call.
    This is the monadic bind: map produces N results, reduce consumes them, no manual plumbing.

    Args:
        prompt_template: Prompt template with {input} placeholder(s).
        inputs: JSON array of input strings: ["input1", "input2", ...].
        synthesis_prompt: Instructions for how to synthesise the map results.
        sandbox: Named sandbox spec or inline JSON (used for map agents).
        network: Whether containers have network access (default: true — needed for API calls).
        tools: Comma-separated list of allowed Claude tools for map agents.
        model: Claude model for map agents (default: sonnet).
        reduce_model: Claude model for the reduce agent (default: same as model).
        timeout: Max execution time per agent in seconds (default: 120).
        max_concurrency: Max map agents running simultaneously (default: 5).
        system_prompt: System prompt for map agents.
        reduce_system_prompt: System prompt for the reduce agent.
        output_schema: JSON schema for structured reduce output.
        mcps: JSON array of MCP server names for map agents.
        effort: Effort level for map agents.
    """
    try:
        input_list = json.loads(inputs)
        if not isinstance(input_list, list) or len(input_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "inputs must be a non-empty JSON array"))

        task_list = [
            {
                "prompt": prompt_template.replace("{input}", str(inp)),
                "sandbox": sandbox,
                "network": network,
                "tools": tools,
                "model": model,
                "timeout": timeout,
                "system_prompt": system_prompt,
                "mcps": mcps,
                "effort": effort,
            }
            for inp in input_list
        ]

        map_run_id, map_results = _run_par_internal(task_list, max_concurrency)

        failed = [r for r in map_results if r.error is not None]
        succeeded = [r for r in map_results if r.error is None]

        if not succeeded:
            data = {
                "run_id": map_run_id,
                "phase": "map",
                "error": "All map agents failed",
                "results": [r.to_ref_dict(map_run_id) for r in map_results],
            }
            return json.dumps(data, default=str)

        texts = [r.text for r in succeeded]
        sections = []
        for i, text in enumerate(texts):
            sections.append(f"## Input {i + 1}\n{text}")

        full_prompt = synthesis_prompt + "\n\n# Inputs to synthesise:\n\n" + "\n\n---\n\n".join(sections)

        reduce_spec = _resolve_spec(
            sandbox, network=network, tools=tools,
            model=reduce_model or model, timeout=timeout,
            system_prompt=reduce_system_prompt, output_schema=output_schema,
        )
        reduce_result = _run_with_semaphore(full_prompt, reduce_spec, map_run_id, "reducer")

        data = {
            "run_id": map_run_id,
            "map_total": len(map_results),
            "map_succeeded": len(succeeded),
            "map_failed": len(failed),
            "result": reduce_result.to_ref_dict(map_run_id),
        }
        if failed:
            data["map_errors"] = [{"agent_id": r.agent_id, "error": r.error} for r in failed]

        return json.dumps(data, default=str)

    except json.JSONDecodeError as e:
        return json.dumps(response_tools.error_response("json_error", f"Failed to parse JSON: {e}"))
    except Exception as e:
        logger.exception("map_reduce failed")
        return json.dumps(response_tools.error_response("map_reduce_error", str(e)))


# ── Unwrap (monadic extract) ──────────────────────────────────────


@mcp.tool()
def unwrap(ref: str) -> str:
    """Unwrap an agent result ref — writes the full text to a file and returns the path.

    All combinators return refs (metadata without text). Use unwrap to
    extract the text when you need it. The text is written to a .md file
    alongside the result, so you can Read() it, Grep it, or pass it to
    other tools without bloating the MCP protocol.

    Args:
        ref: A ref string like "run_id/agent_id", or a JSON object with a "ref" field.
    """
    try:
        if ref.startswith("{"):
            ref_data = json.loads(ref)
            ref = ref_data.get("ref", ref)

        text = _resolve_ref(ref)

        # Write to a file so the caller can Read() it
        output_path = os.path.join("/tmp/swarm-mcp", ref, "output.md")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            f.write(text)

        return json.dumps({
            "ref": ref,
            "file": output_path,
            "size": len(text),
        })
    except FileNotFoundError:
        return json.dumps(response_tools.error_response("not_found", f"No result found for ref: {ref}"))
    except Exception as e:
        logger.exception("unwrap failed")
        return json.dumps(response_tools.error_response("unwrap_error", str(e)))


@mcp.tool()
def inspect(ref: str) -> str:
    """Inspect an agent's full execution state — partial output, stream log, files produced.

    Use after a timeout, crash, or unexpected result to understand what happened.
    Writes a human-readable debug report to output_dir/inspect.md.

    Args:
        ref: A ref string like "run_id/agent_id".
    """
    try:
        if ref.startswith("{"):
            ref_data = json.loads(ref)
            ref = ref_data.get("ref", ref)

        output_dir = os.path.join("/tmp/swarm-mcp", ref)
        if not os.path.isdir(output_dir):
            return json.dumps(response_tools.error_response("not_found", f"No output dir for ref: {ref}"))

        report_parts = [f"# Inspection: {ref}\n"]

        # Result metadata
        result_file = os.path.join(output_dir, "result.json")
        if os.path.exists(result_file):
            with open(result_file) as f:
                result_data = json.load(f)
            report_parts.append("## Result")
            report_parts.append(f"- exit_code: {result_data.get('exit_code')}")
            report_parts.append(f"- error: {result_data.get('error')}")
            report_parts.append(f"- duration: {result_data.get('duration_seconds')}s")
            report_parts.append(f"- cost: ${result_data.get('cost_usd') or 'unknown'}")
            text = result_data.get("text", "")
            report_parts.append(f"- text length: {len(text)} chars")
            if text:
                report_parts.append(f"\n### Output Text (first 2000 chars)\n```\n{text[:2000]}\n```")

        # Stream log summary
        stream_file = os.path.join(output_dir, "stream.jsonl")
        if os.path.exists(stream_file):
            tool_calls = []
            thinking_snippets = []
            text_chunks = []
            errors = []
            with open(stream_file) as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    msg_type = obj.get("type")
                    if msg_type == "assistant":
                        for block in obj.get("message", {}).get("content", []):
                            if isinstance(block, dict):
                                if block.get("type") == "tool_use":
                                    tool_calls.append(block.get("name", "?"))
                                elif block.get("type") == "thinking":
                                    snippet = block.get("thinking", "")[:150]
                                    thinking_snippets.append(snippet)
                                elif block.get("type") == "text":
                                    text_chunks.append(block.get("text", "")[:200])

            report_parts.append(f"\n## Stream Log ({os.path.getsize(stream_file)} bytes)")
            if tool_calls:
                report_parts.append(f"- Tool calls: {', '.join(tool_calls)}")
            if thinking_snippets:
                report_parts.append(f"- Thinking steps: {len(thinking_snippets)}")
                for i, s in enumerate(thinking_snippets[:5]):
                    report_parts.append(f"  {i}: {s}...")
            if text_chunks:
                report_parts.append(f"- Text chunks: {len(text_chunks)}")

        # Artifacts from PostToolUse hooks
        artifacts_file = os.path.join(output_dir, "artifacts.jsonl")
        if os.path.exists(artifacts_file):
            artifacts = []
            with open(artifacts_file) as f:
                for line in f:
                    try:
                        artifacts.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        continue
            report_parts.append(f"\n## Artifacts ({len(artifacts)} logged)")
            for a in artifacts:
                tool = a.get("tool", "?")
                resp = a.get("response", {})
                # Summarise the key info per tool type
                if "message_id" in str(resp) or "success" in str(resp):
                    report_parts.append(f"- {tool}: {json.dumps(resp)[:200]}")
                else:
                    report_parts.append(f"- {tool}: {json.dumps(a.get('input', {}))[:150]}")

        # Files in output dir
        files = []
        for entry in os.scandir(output_dir):
            if entry.is_file():
                files.append({"name": entry.name, "size": entry.stat().st_size})
            elif entry.is_dir():
                files.append({"name": entry.name + "/", "type": "dir"})
        report_parts.append(f"\n## Files in {output_dir}")
        for f_info in files:
            report_parts.append(f"- {f_info['name']} ({f_info.get('size', '?')} bytes)")

        # Write report
        report = "\n".join(report_parts)
        inspect_path = os.path.join(output_dir, "inspect.md")
        with open(inspect_path, "w") as f:
            f.write(report)

        return json.dumps({
            "ref": ref,
            "file": inspect_path,
            "tool_calls": tool_calls if 'tool_calls' in dir() else [],
            "has_partial_output": bool(text) if 'text' in dir() else False,
        })

    except Exception as e:
        logger.exception("inspect failed")
        return json.dumps(response_tools.error_response("inspect_error", str(e)))


# ── Higher-Order Combinators ──────────────────────────────────────


@mcp.tool()
def filter(
    refs: str,
    declared_type: str,
    model: str = "sonnet",
    timeout: int = 120,
) -> str:
    """Filter refs by type validation — keep only results that match the declared type.

    Runs validate on each ref in parallel. Returns only refs with VALID verdict.
    This is the type-gated composition primitive: ensures only correct results flow downstream.

    Args:
        refs: JSON array of ref objects: [{"ref": "run_id/agent_id"}, ...].
        declared_type: Type name or description to validate against.
        model: Model for the validator agents (default: sonnet).
        timeout: Timeout per validation (default: 120).
    """
    try:
        ref_list = json.loads(refs)
        if not isinstance(ref_list, list) or len(ref_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "refs must be a non-empty JSON array"))

        run_id = _generate_run_id()
        valid_refs = []
        invalid_refs = []

        # Validate each ref in parallel
        tasks = []
        for r in ref_list:
            ref_str = r.get("ref") if isinstance(r, dict) else r
            text = _resolve_ref(ref_str)
            prompt = build_validation_prompt(text, declared_type)
            tasks.append({
                "prompt": prompt,
                "model": model,
                "tools": "Read,Glob,Grep,Bash",
                "timeout": timeout,
            })

        _, results = _run_par_internal(tasks, max_concurrency=min(len(tasks), MAX_CONCURRENT))

        for r, ref_obj, result in zip(ref_list, ref_list, results):
            ref_str = ref_obj.get("ref") if isinstance(ref_obj, dict) else ref_obj
            verdict = "UNKNOWN"
            if result.text:
                for line in result.text.split("\n"):
                    line = line.strip()
                    if line in ("VALID", "PARTIAL", "INVALID"):
                        verdict = line
                        break
                    if line.startswith(("VALID", "PARTIAL", "INVALID")):
                        verdict = line.split()[0]
                        break

            enriched = dict(ref_obj) if isinstance(ref_obj, dict) else {"ref": ref_obj}
            monads.stamp_validated(enriched, declared_type, verdict, f"{run_id}/{result.agent_id}")

            if verdict == "VALID":
                valid_refs.append(enriched)
            else:
                invalid_refs.append(enriched)

        data = {
            "run_id": run_id,
            "total": len(ref_list),
            "valid": len(valid_refs),
            "invalid": len(invalid_refs),
            "results": valid_refs,
            "rejected": invalid_refs,
        }
        return json.dumps(data, default=str)

    except Exception as e:
        logger.exception("filter failed")
        return json.dumps(response_tools.error_response("filter_error", str(e)))


@mcp.tool()
def race(
    tasks: str,
    max_concurrency: int = 5,
) -> str:
    """Run multiple approaches in parallel, return the first to succeed.

    All tasks start simultaneously. As soon as one completes without error,
    its ref is returned. Remaining tasks are abandoned (their containers are
    killed). Use for speculative execution or when multiple strategies might work.

    Args:
        tasks: JSON array of task objects (same format as par).
        max_concurrency: Max agents running simultaneously (default: 5).
    """
    try:
        task_list = json.loads(tasks)
        if not isinstance(task_list, list) or len(task_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "tasks must be a non-empty JSON array"))

        # Run all in parallel — same as par
        run_id, results = _run_par_internal(task_list, max_concurrency)

        # Find first success (by completion order — which is preserved by ThreadPoolExecutor)
        winner = None
        losers = []
        for r in results:
            if r.error is None and winner is None:
                winner = r
            else:
                losers.append(r)

        if winner is None:
            data = {
                "run_id": run_id,
                "error": "All approaches failed",
                "results": [r.to_ref_dict(run_id) for r in results],
            }
        else:
            data = {
                "run_id": run_id,
                "winner": winner.to_ref_dict(run_id),
                "attempted": len(results),
                "failed": len([r for r in results if r.error]),
            }

        return json.dumps(data, default=str)

    except Exception as e:
        logger.exception("race failed")
        return json.dumps(response_tools.error_response("race_error", str(e)))


@mcp.tool()
def retry(
    prompt: str,
    max_attempts: int = 3,
    sandbox: str | None = None,
    model: str = "sonnet",
    timeout: int = 120,
    declared_type: str | None = None,
    mcps: str | list | None = None,
) -> str:
    """Run a single agent with automatic retries on failure.

    If declared_type is set, retries until the output validates as that type
    (not just until exit code 0). Each attempt receives the prior error as context.

    Args:
        prompt: The task prompt.
        max_attempts: Maximum number of attempts (default: 3).
        sandbox: Named sandbox spec or inline JSON.
        model: Claude model (default: sonnet).
        timeout: Timeout per attempt (default: 120).
        declared_type: If set, validates output and retries if not VALID.
        mcps: JSON array of MCP server names to attach.
    """
    try:
        run_id = _generate_run_id()
        prior_errors = []

        for attempt in range(1, max_attempts + 1):
            # Build prompt with retry context
            full_prompt = prompt
            if prior_errors:
                full_prompt += "\n\n# Prior attempts failed with these errors:\n"
                for i, err in enumerate(prior_errors):
                    full_prompt += f"\n## Attempt {i + 1}\n{err}\n"
                full_prompt += "\nPlease fix the issues and try again."

            spec = _resolve_spec(sandbox, model=model, timeout=timeout, mcps=mcps)
            result = _run_with_semaphore(full_prompt, spec, run_id, f"attempt-{attempt}")

            # Check for success
            if result.error:
                prior_errors.append(result.error)
                continue

            # If declared_type, validate the output
            if declared_type and result.text:
                validation_prompt = build_validation_prompt(result.text, declared_type)
                val_spec = _resolve_spec(None, model=model, timeout=60)
                val_result = _run_with_semaphore(validation_prompt, val_spec, run_id, f"validate-{attempt}")

                verdict = "UNKNOWN"
                if val_result.text:
                    for line in val_result.text.split("\n"):
                        line = line.strip()
                        if line in ("VALID", "PARTIAL", "INVALID"):
                            verdict = line
                            break
                        if line.startswith(("VALID", "PARTIAL", "INVALID")):
                            verdict = line.split()[0]
                            break

                if verdict != "VALID":
                    prior_errors.append(f"Type validation failed ({verdict}): {val_result.text[:500]}")
                    continue

                # Stamp as validated
                ref = result.to_ref_dict(run_id)
                monads.stamp_validated(ref, declared_type, verdict, f"{run_id}/validate-{attempt}")
                monads.stamp_retry(ref, attempt, max_attempts, prior_errors)
                return json.dumps(ref, default=str)

            # Success without type checking
            ref = result.to_ref_dict(run_id)
            monads.stamp_retry(ref, attempt, max_attempts, prior_errors)
            return json.dumps(ref, default=str)

        # All attempts failed
        data = {
            "run_id": run_id,
            "error": f"All {max_attempts} attempts failed",
            "prior_errors": prior_errors,
            "last_ref": result.to_ref_dict(run_id) if result else None,
        }
        return json.dumps(data, default=str)

    except Exception as e:
        logger.exception("retry failed")
        return json.dumps(response_tools.error_response("retry_error", str(e)))


@mcp.tool()
def guard(
    ref: str,
    check: str,
    value: str | None = None,
) -> str:
    """Check a monadic condition on a ref. Returns the ref if the guard passes, error if not.

    Use to enforce constraints before passing refs to downstream combinators.

    Args:
        ref: A ref string or JSON object.
        check: The guard to check — one of: "validated", "budget", "classification", "exists".
        value: Required for some checks — e.g. the type name for "validated", the classification level for "classification".
    """
    try:
        if ref.startswith("{"):
            ref_data = json.loads(ref)
        else:
            ref_data = {"ref": ref}

        if check == "validated":
            if not monads.is_validated(ref_data, value):
                validated_as = ref_data.get("validated_as", "not validated")
                return json.dumps(response_tools.error_response(
                    "guard_failed",
                    f"Ref not validated as '{value}' (current: {validated_as})"
                ))

        elif check == "budget":
            if not monads.check_budget(ref_data):
                budget = ref_data.get("budget", {})
                return json.dumps(response_tools.error_response(
                    "guard_failed",
                    f"Budget exceeded: spent {budget.get('spent_so_far', '?')}, limit {budget.get('limit', '?')}"
                ))

        elif check == "classification":
            if value:
                mcps = json.loads(value) if value.startswith("[") else [value]
                allowed, reason = monads.check_classification(ref_data, mcps)
                if not allowed:
                    return json.dumps(response_tools.error_response("guard_failed", reason))

        elif check == "exists":
            ref_str = ref_data.get("ref", ref)
            result_file = os.path.join("/tmp/swarm-mcp", ref_str, "result.json")
            if not os.path.exists(result_file):
                return json.dumps(response_tools.error_response("guard_failed", f"Ref does not exist: {ref_str}"))

        # Guard passed — return the ref unchanged
        return json.dumps(ref_data, default=str)

    except Exception as e:
        logger.exception("guard failed")
        return json.dumps(response_tools.error_response("guard_error", str(e)))


@mcp.tool()
def classify(
    ref: str,
    level: str,
    allowed_mcps: str | list | None = None,
    denied_mcps: str | list | None = None,
) -> str:
    """Set the classification level on a ref. Controls which MCPs can access the data.

    Use for data sensitivity enforcement — e.g. mark original legal documents as
    'confidential' (no WhatsApp MCP), mark synthetic outputs as 'public'.

    Args:
        ref: A ref string or JSON object.
        level: Classification level: public, internal, confidential, restricted.
        allowed_mcps: JSON array of MCP names allowed to access this ref.
        denied_mcps: JSON array of MCP names denied access.
    """
    try:
        if ref.startswith("{"):
            ref_data = json.loads(ref)
        else:
            ref_data = {"ref": ref}

        allowed = None
        if allowed_mcps:
            allowed = json.loads(allowed_mcps) if isinstance(allowed_mcps, str) else allowed_mcps
        denied = None
        if denied_mcps:
            denied = json.loads(denied_mcps) if isinstance(denied_mcps, str) else denied_mcps

        monads.stamp_classification(ref_data, level, allowed, denied)

        # Also write classification to the result.json on disk
        ref_str = ref_data.get("ref", "")
        result_file = os.path.join("/tmp/swarm-mcp", ref_str, "result.json")
        if os.path.exists(result_file):
            with open(result_file) as f:
                result_data = json.load(f)
            result_data["classification"] = ref_data["classification"]
            with open(result_file, "w") as f:
                json.dump(result_data, f, indent=2, default=str)

        return json.dumps(ref_data, default=str)

    except Exception as e:
        logger.exception("classify failed")
        return json.dumps(response_tools.error_response("classify_error", str(e)))


# ── Pipeline Tool (Free Monad interpreter) ───────────────────────


@mcp.tool()
def pipeline(
    definition: str,
) -> str:
    """Execute a pipeline — a sequence of steps with conditions and loops.
    This is the free monad interpreter: the pipeline definition is data, this tool evaluates it.

    The definition is a JSON object or a pipeline name (loaded from ~/.claude/pipelines/).

    Pipeline format:
    {
        "name": "optional-name",
        "sandbox": "optional-default-sandbox",
        "steps": [
            {"id": "step-0", "prompt": "...", "model": "sonnet", "sandbox": "...", ...},
            {"id": "test", "prompt": "Run tests", "tools": "Bash",
             "on_fail": "fix"},
            {"id": "fix", "prompt": "Fix failing tests", "tools": "Read,Edit,Bash",
             "condition": "prev.error", "next": "test", "max_retries": 3}
        ]
    }

    Step fields: prompt (required), plus any sandbox fields (model, tools, system_prompt, etc.).
    Control flow: on_fail (jump to step id on error), next (jump after success), condition ("prev.error" = only run if previous failed), max_retries.

    Args:
        definition: Pipeline name (loaded from ~/.claude/pipelines/<name>.json) or inline JSON definition.
    """
    try:
        # Load pipeline definition
        if definition.startswith("{") or definition.startswith("["):
            pipeline_def = json.loads(definition)
        else:
            path = registry.find_resource("pipelines", definition, ".json")
            if path is None:
                return json.dumps(response_tools.error_response("not_found", f"Pipeline '{definition}' not found in search paths: {registry._search_paths.get('pipelines', [])}"))
            with open(path) as f:
                pipeline_def = json.load(f)

        steps = pipeline_def.get("steps", [])
        if not steps:
            return json.dumps(response_tools.error_response("invalid_input", "Pipeline has no steps"))

        default_sandbox = pipeline_def.get("sandbox")
        run_id = _generate_run_id()
        results = []
        step_results = {}  # id -> AgentResult
        retry_counts = {}  # step_id -> count

        # Budget and deadline tracking (monadic context)
        budget_limit = pipeline_def.get("budget")  # total USD limit
        spent_so_far = 0.0
        deadline = None
        if pipeline_def.get("deadline_seconds"):
            deadline = time.time() + pipeline_def["deadline_seconds"]
        classification = pipeline_def.get("classification")  # default data classification

        # Create a shared directory that all pipeline steps can read/write.
        # Mounted at /shared/ in each container — use this for inter-step artifacts.
        shared_dir = os.path.join("/tmp/swarm-mcp", run_id, "shared")
        os.makedirs(shared_dir, exist_ok=True)
        logger.info("Pipeline %s shared dir: %s", run_id, shared_dir)

        i = 0
        while i < len(steps):
            step = steps[i]
            step_id = step.get("id", f"step-{i}")

            # Check condition
            condition = step.get("condition")
            if condition == "prev.error":
                if results and results[-1].error is None:
                    # Previous step succeeded, skip this conditional step
                    i += 1
                    continue

            # Check max_retries
            max_retries = step.get("max_retries", 3)
            if retry_counts.get(step_id, 0) >= max_retries:
                logger.warning("Step %s exceeded max retries (%d)", step_id, max_retries)
                results.append(AgentResult(
                    agent_id=step_id, text="", exit_code=-1, duration_seconds=0,
                    cost_usd=None, model="", output_dir="",
                    error=f"Exceeded max retries ({max_retries})",
                ))
                i += 1
                continue

            # Build prompt with previous context
            prompt = step["prompt"]
            if results:
                prev = results[-1]
                if prev.text:
                    prompt += f"\n\n# Context from previous step ({prev.agent_id}):\n{prev.text}"
                if prev.error:
                    prompt += f"\n\n# Error from previous step ({prev.agent_id}):\n{prev.error}"

            # Budget guard — stop before overspending
            if budget_limit and spent_so_far >= budget_limit:
                results.append(AgentResult(
                    agent_id=step_id, text="", exit_code=-1, duration_seconds=0,
                    cost_usd=None, model="", output_dir="",
                    error=f"Budget exhausted: spent ${spent_so_far:.2f} of ${budget_limit:.2f} limit",
                ))
                break

            # Deadline guard — stop if time's up
            if deadline:
                remaining = monads.remaining_time(deadline)
                if remaining is not None and remaining <= 0:
                    results.append(AgentResult(
                        agent_id=step_id, text="", exit_code=-1, duration_seconds=0,
                        cost_usd=None, model="", output_dir="",
                        error="Pipeline deadline exceeded",
                    ))
                    break

            overrides = {k: v for k, v in step.items() if k not in ("prompt", "sandbox", "id", "on_fail", "next", "condition", "max_retries", "input_type", "output_type") and v is not None}

            # Propagate deadline as step timeout if tighter than step's own timeout
            if deadline:
                remaining = monads.remaining_time(deadline)
                if remaining is not None:
                    step_timeout = overrides.get("timeout", 300)
                    overrides["timeout"] = int(min(step_timeout, remaining))

            spec = _resolve_spec(step.get("sandbox", default_sandbox), **overrides)

            # Inject shared directory mount for inter-step file communication
            shared_mount = {"host_path": shared_dir, "container_path": "/shared", "readonly": False}
            spec = spec.merge({"mounts": (spec.mounts or []) + [shared_mount]})

            result = _run_with_semaphore(prompt, spec, run_id, step_id)
            results.append(result)
            step_results[step_id] = result

            # Update budget tracking
            spent_so_far += result.cost_usd or 0

            # Control flow
            if result.error is not None:
                on_fail = step.get("on_fail")
                if on_fail:
                    # Jump to the named step
                    target_idx = next((j for j, s in enumerate(steps) if s.get("id") == on_fail), None)
                    if target_idx is not None:
                        retry_counts[on_fail] = retry_counts.get(on_fail, 0) + 1
                        i = target_idx
                        continue
                # No on_fail handler — pipeline stops
                break
            else:
                next_step = step.get("next")
                if next_step:
                    target_idx = next((j for j, s in enumerate(steps) if s.get("id") == next_step), None)
                    if target_idx is not None:
                        retry_counts[next_step] = retry_counts.get(next_step, 0) + 1
                        i = target_idx
                        continue

            i += 1

        data = {
            "run_id": run_id,
            "steps_executed": len(results),
            "total_steps": len(steps),
            "final": results[-1].to_ref_dict(run_id) if results else None,
            "all_results": [r.to_ref_dict(run_id) for r in results],
            "total_cost_usd": spent_so_far,
            "total_duration_seconds": sum(r.duration_seconds for r in results),
            "budget": {"spent": spent_so_far, "limit": budget_limit} if budget_limit else None,
            "deadline_met": (time.time() < deadline) if deadline else None,
        }
        return json.dumps(data, default=str)

    except json.JSONDecodeError as e:
        return json.dumps(response_tools.error_response("json_error", f"Failed to parse pipeline: {e}"))
    except Exception as e:
        logger.exception("pipeline failed")
        return json.dumps(response_tools.error_response("pipeline_error", str(e)))


# ── Sandbox Management Tools ─────────────────────────────────────


@mcp.tool()
def save_sandbox_spec(name: str, spec: str) -> str:
    """Save a reusable sandbox spec to ~/.claude/sandboxes/<name>.json.

    Args:
        name: Name for the sandbox spec (e.g. "web-researcher", "code-reviewer").
        spec: JSON object with sandbox fields: model, tools, mcps, system_prompt, claude_md, output_schema, effort, max_budget, mounts, workdir, input_files, network, memory, cpus, timeout, env_vars.
    """
    try:
        data = json.loads(spec)
        sandbox = resolve_sandbox(None, **data)
        path = save_sandbox(name, sandbox)
        return json.dumps({"saved": True, "name": name, "path": path})
    except Exception as e:
        logger.exception("save_sandbox_spec failed")
        return json.dumps(response_tools.error_response("save_error", str(e)))


@mcp.tool()
def list_sandbox_specs() -> str:
    """List all saved sandbox specs from ~/.claude/sandboxes/."""
    try:
        specs = list_sandboxes()
        return json.dumps(specs)
    except Exception as e:
        logger.exception("list_sandbox_specs failed")
        return json.dumps(response_tools.error_response("list_error", str(e)))


# ── Wrap / Registry Tools ─────────────────────────────────────────


@mcp.tool()
def wrap(path: str) -> str:
    """Wrap a file or directory into the swarm ref system.

    This is how you bring external objects INTO the monadic context.
    The wrapped file gets a ref that can be passed to any combinator.

    Args:
        path: Absolute path to a file or directory on the host.
    """
    try:
        ref_id = registry.wrap_file(path)
        return json.dumps({"ref": ref_id, "source": path})
    except FileNotFoundError as e:
        return json.dumps(response_tools.error_response("not_found", str(e)))
    except Exception as e:
        logger.exception("wrap failed")
        return json.dumps(response_tools.error_response("wrap_error", str(e)))


@mcp.tool()
def wrap_project(project_dir: str) -> str:
    """Register a project directory's pipelines, sandboxes, and types with the swarm.

    Looks for pipelines/, sandboxes/, types/ subdirectories and adds them
    to the search paths. After wrapping, named resources from the project
    are discoverable by all swarm tools (pipeline, run, validate, etc.).

    Args:
        project_dir: Absolute path to a project root containing pipelines/, sandboxes/, and/or types/ directories.
    """
    try:
        registered = registry.wrap_project(project_dir)
        if not registered:
            return json.dumps(response_tools.error_response("empty", f"No pipelines/, sandboxes/, or types/ found in {project_dir}"))
        return json.dumps({"project": project_dir, "registered": registered})
    except Exception as e:
        logger.exception("wrap_project failed")
        return json.dumps(response_tools.error_response("wrap_project_error", str(e)))


# ── Type System Tools ─────────────────────────────────────────────


@mcp.tool()
def list_type_registry() -> str:
    """List all registered types from ~/.claude/types/."""
    try:
        types = _list_types()
        return json.dumps(types)
    except Exception as e:
        logger.exception("list_type_registry failed")
        return json.dumps(response_tools.error_response("list_error", str(e)))


@mcp.tool()
def get_type_definition(name: str, resolve_refs: bool = True) -> str:
    """Get a type definition by name, optionally resolving [references] to other types.

    Args:
        name: Type name (e.g. "mcp-server", "tarball", "code-review").
        resolve_refs: Whether to inline [referenced] types (default: true).
    """
    try:
        content = get_type(name)
        if content is None:
            return json.dumps(response_tools.error_response("not_found", f"Type '{name}' not found in ~/.claude/types/"))
        if resolve_refs:
            content = resolve_type(content)
        return json.dumps({"name": name, "definition": content})
    except Exception as e:
        logger.exception("get_type_definition failed")
        return json.dumps(response_tools.error_response("get_error", str(e)))


@mcp.tool()
def validate(
    artifact: str,
    declared_type: str,
    sandbox: str | None = None,
    model: str = "sonnet",
    timeout: int = 120,
) -> str:
    """Validate an artifact against a declared type. Runs a type-checker agent that inspects
    the artifact and reports VALID/PARTIAL/INVALID with per-criterion results.

    Use this after a pipeline step to verify the output matches expectations.
    If validation fails, you know which agent to blame and can retry.

    Args:
        artifact: Description of what to validate — e.g. the agent's output text, a file path, or a ref {"ref": "run_id/agent_id"}.
        declared_type: The type to validate against — either a type name (e.g. "mcp-server") or inline natural language description.
        sandbox: Named sandbox spec or inline JSON for the validator agent.
        model: Model for the validator (default: sonnet — needs to be good at analysis).
        timeout: Timeout for the validation agent.
    """
    try:
        # Resolve artifact if it's a ref
        if artifact.startswith("{"):
            ref_data = json.loads(artifact)
            if "ref" in ref_data:
                artifact = _resolve_ref(ref_data["ref"])

        # Resolve type — check registry first, fall back to inline description
        type_content = get_type(declared_type)
        if type_content:
            type_desc = declared_type  # use the name, build_validation_prompt will resolve
        else:
            type_desc = declared_type  # inline description

        prompt = build_validation_prompt(artifact, type_desc)

        spec = _resolve_spec(
            sandbox, model=model, timeout=timeout,
            tools="Read,Glob,Grep,Bash",  # validator may need to inspect files
            network=True,
        )
        run_id = _generate_run_id()
        result = _run_with_semaphore(prompt, spec, run_id, "validator")

        # Parse verdict from output
        verdict = "UNKNOWN"
        if result.text:
            for line in result.text.split("\n"):
                line = line.strip()
                if line in ("VALID", "PARTIAL", "INVALID"):
                    verdict = line
                    break
                if line.startswith("VALID") or line.startswith("PARTIAL") or line.startswith("INVALID"):
                    verdict = line.split()[0]
                    break

        data = {
            "run_id": run_id,
            "declared_type": declared_type,
            "verdict": verdict,
            "result": result.to_dict(),
        }
        return json.dumps(response_tools.truncate_response(data, f"validate_{run_id[:8]}"), default=str)

    except Exception as e:
        logger.exception("validate failed")
        return json.dumps(response_tools.error_response("validate_error", str(e)))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
