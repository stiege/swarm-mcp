import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from mcp.server.fastmcp import FastMCP

from . import tools as response_tools
from .agent import AgentResult, run_agent
from .sandbox import SandboxSpec, list_sandboxes, load_sandbox, resolve_sandbox, save_sandbox
from .types import build_validation_prompt, get_type, list_types as _list_types, resolve_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("swarm-mcp")

MAX_CONCURRENT = int(os.environ.get("SWARM_MAX_CONCURRENT", "10"))
_semaphore = threading.Semaphore(MAX_CONCURRENT)

# network defaults to True because every agent needs to reach the Anthropic API.
# Set to False only if using a local model backend (e.g. Ollama) in the future.
NETWORK_DEFAULT = True

PIPELINE_DIR = os.path.expanduser("~/.claude/pipelines")


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

    # Parse mounts from JSON string
    mounts_str = kwargs.pop("mounts", None)
    mounts_list = None
    if mounts_str:
        mounts_list = json.loads(mounts_str) if isinstance(mounts_str, str) else mounts_str

    # Parse mcps from JSON string
    mcps_str = kwargs.pop("mcps", None)
    mcps_list = None
    if mcps_str:
        mcps_list = json.loads(mcps_str) if isinstance(mcps_str, str) else mcps_str

    # Parse input_files from JSON string
    input_files_str = kwargs.pop("input_files", None)
    input_files_dict = None
    if input_files_str:
        input_files_dict = json.loads(input_files_str) if isinstance(input_files_str, str) else input_files_str

    # Parse output_schema from JSON string
    schema_str = kwargs.pop("output_schema", None)
    schema_dict = None
    if schema_str:
        schema_dict = json.loads(schema_str) if isinstance(schema_str, str) else schema_str

    # Parse env_vars from JSON string
    env_str = kwargs.pop("env_vars", None)
    env_dict = None
    if env_str:
        env_dict = json.loads(env_str) if isinstance(env_str, str) else env_str

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
    mcps: str | None = None,
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
    mcps: str | None = None,
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
            timeout=timeout, system_prompt=system_prompt,
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
    mcps: str | None = None,
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
            path = os.path.join(PIPELINE_DIR, f"{definition}.json")
            if not os.path.exists(path):
                return json.dumps(response_tools.error_response("not_found", f"Pipeline not found: {path}"))
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

            overrides = {k: v for k, v in step.items() if k not in ("prompt", "sandbox", "id", "on_fail", "next", "condition", "max_retries") and v is not None}
            spec = _resolve_spec(step.get("sandbox", default_sandbox), **overrides)

            result = _run_with_semaphore(prompt, spec, run_id, step_id)
            results.append(result)
            step_results[step_id] = result

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
            "total_cost_usd": sum(r.cost_usd or 0 for r in results),
            "total_duration_seconds": sum(r.duration_seconds for r in results),
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
