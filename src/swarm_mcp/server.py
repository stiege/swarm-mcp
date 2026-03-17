import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from mcp.server.fastmcp import FastMCP

from . import tools as response_tools
from .agent import AgentResult, EnvConfig, parse_env_config, run_agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("swarm-mcp")

MAX_CONCURRENT = int(os.environ.get("SWARM_MAX_CONCURRENT", "10"))
_semaphore = threading.Semaphore(MAX_CONCURRENT)

# network defaults to True because every agent needs to reach the Anthropic API.
# Set to False only if using a local model backend (e.g. Ollama) in the future.
NETWORK_DEFAULT = True


def _generate_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _run_with_semaphore(prompt: str, env: EnvConfig, run_id: str, agent_id: str) -> AgentResult:
    acquired = _semaphore.acquire(timeout=env.timeout)
    if not acquired:
        return AgentResult(
            agent_id=agent_id,
            text="",
            exit_code=-1,
            duration_seconds=0,
            cost_usd=None,
            model=env.model,
            output_dir="",
            error=f"Could not acquire execution slot within {env.timeout}s (max concurrent: {MAX_CONCURRENT})",
        )
    try:
        return run_agent(prompt, env, run_id, agent_id)
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


def _run_par_internal(task_list: list[dict], max_concurrency: int) -> tuple[str, list[AgentResult]]:
    """Shared parallel execution logic. Returns (run_id, results)."""
    run_id = _generate_run_id()
    effective_concurrency = min(max_concurrency, len(task_list), MAX_CONCURRENT)

    def execute_task(i_task: tuple[int, dict]) -> AgentResult:
        i, task = i_task
        env = parse_env_config(
            network=task.get("network", NETWORK_DEFAULT),
            tools=task.get("tools", "Read,Write,Glob,Grep,Bash"),
            mounts=task.get("mounts", "[]"),
            model=task.get("model", "sonnet"),
            timeout=task.get("timeout", 120),
        )
        return _run_with_semaphore(task["prompt"], env, run_id, f"agent-{i}")

    with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
        results = list(executor.map(execute_task, enumerate(task_list)))

    return run_id, results


@mcp.tool()
def run(
    prompt: str,
    network: bool = NETWORK_DEFAULT,
    tools: str = "Read,Write,Glob,Grep,Bash",
    mounts: str = "[]",
    model: str = "sonnet",
    timeout: int = 120,
) -> str:
    """Run a single Claude agent in a Docker container. Returns the agent's text output and metadata.

    Args:
        prompt: The task prompt for the agent.
        network: Whether the container has network access (default: true — needed for API calls).
        tools: Comma-separated list of allowed Claude tools (default: Read,Write,Glob,Grep,Bash).
        mounts: JSON array of mount specs: [{"host_path": "...", "container_path": "...", "readonly": true}].
        model: Claude model to use (default: sonnet). Options: haiku, sonnet, opus.
        timeout: Max execution time in seconds (default: 120).
    """
    try:
        env = parse_env_config(network=network, tools=tools, mounts=mounts, model=model, timeout=timeout)
        run_id = _generate_run_id()
        result = _run_with_semaphore(prompt, env, run_id, "agent-0")
        return json.dumps(response_tools.truncate_response(result.to_dict(), f"run_{run_id[:8]}"), default=str)
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
        tasks: JSON array of task objects: [{"prompt": "...", "tools": "...", "model": "sonnet", "timeout": 120}, ...].
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
            # Full results inline
            "results": [r.to_dict() for r in results],
            # Refs for passing to reduce/chain without copying text
            "refs": [{"ref": f"{run_id}/{r.agent_id}"} for r in results if r.error is None],
        }
        return json.dumps(response_tools.truncate_response(data, f"par_{run_id[:8]}"), default=str)

    except json.JSONDecodeError as e:
        return json.dumps(response_tools.error_response("json_error", f"Failed to parse tasks JSON: {e}"))
    except Exception as e:
        logger.exception("par failed")
        return json.dumps(response_tools.error_response("par_error", str(e)))


@mcp.tool()
def map(
    prompt_template: str,
    inputs: str,
    network: bool = NETWORK_DEFAULT,
    tools: str = "Read,Write,Glob,Grep,Bash",
    model: str = "sonnet",
    timeout: int = 120,
    max_concurrency: int = 5,
) -> str:
    """Apply a prompt template to each input in parallel. Use {input} as the placeholder.

    Args:
        prompt_template: Prompt template with {input} placeholder(s).
        inputs: JSON array of input strings: ["input1", "input2", ...].
        network: Whether containers have network access (default: true — needed for API calls).
        tools: Comma-separated list of allowed Claude tools.
        model: Claude model to use (default: sonnet).
        timeout: Max execution time per agent in seconds (default: 120).
        max_concurrency: Max agents running simultaneously (default: 5).
    """
    try:
        input_list = json.loads(inputs)
        if not isinstance(input_list, list) or len(input_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "inputs must be a non-empty JSON array"))

        task_list = [
            {
                "prompt": prompt_template.replace("{input}", str(inp)),
                "network": network,
                "tools": tools,
                "model": model,
                "timeout": timeout,
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
        stages: JSON array of stage objects: [{"prompt": "...", "tools": "...", "model": "sonnet", "timeout": 120}, ...].
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

            env = parse_env_config(
                network=stage.get("network", NETWORK_DEFAULT),
                tools=stage.get("tools", "Read,Write,Glob,Grep,Bash"),
                mounts=stage.get("mounts", "[]"),
                model=stage.get("model", "sonnet"),
                timeout=stage.get("timeout", 120),
            )

            result = _run_with_semaphore(prompt, env, run_id, f"stage-{i}")
            intermediates.append(result.to_dict())

            if result.error is not None:
                data = {
                    "run_id": run_id,
                    "completed_stages": i,
                    "total_stages": len(stage_list),
                    "error": f"Stage {i} failed: {result.error}",
                    "intermediates": intermediates,
                }
                return json.dumps(response_tools.truncate_response(data, f"chain_{run_id[:8]}"), default=str)

            previous_text = result.text

        data = {
            "run_id": run_id,
            "completed_stages": len(stage_list),
            "total_stages": len(stage_list),
            "final": intermediates[-1],
            "intermediates": intermediates,
        }
        return json.dumps(response_tools.truncate_response(data, f"chain_{run_id[:8]}"), default=str)

    except json.JSONDecodeError as e:
        return json.dumps(response_tools.error_response("json_error", f"Failed to parse stages JSON: {e}"))
    except Exception as e:
        logger.exception("chain failed")
        return json.dumps(response_tools.error_response("chain_error", str(e)))


@mcp.tool()
def reduce(
    results: str,
    synthesis_prompt: str,
    network: bool = NETWORK_DEFAULT,
    tools: str = "Read,Write,Glob,Grep,Bash",
    model: str = "sonnet",
    timeout: int = 120,
) -> str:
    """Synthesise multiple results into one. Accepts plain strings or structured AgentResult objects
    (auto-extracts .text fields), so you can pipe par/map output directly without manual unwrapping.

    Args:
        results: JSON array — either plain strings ["text1", "text2"] or AgentResult objects [{"text": "...", ...}].
        synthesis_prompt: Instructions for how to synthesise the results.
        network: Whether the container has network access (default: true — needed for API calls).
        tools: Comma-separated list of allowed Claude tools.
        model: Claude model to use (default: sonnet).
        timeout: Max execution time in seconds (default: 120).
    """
    try:
        result_list = json.loads(results)
        if not isinstance(result_list, list) or len(result_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "results must be a non-empty JSON array"))

        # Unwrap: accept both plain strings and AgentResult dicts
        texts = _extract_texts(result_list)

        sections = []
        for i, text in enumerate(texts):
            sections.append(f"## Input {i + 1}\n{text}")

        full_prompt = synthesis_prompt + "\n\n# Inputs to synthesise:\n\n" + "\n\n---\n\n".join(sections)

        env = parse_env_config(network=network, tools=tools, model=model, timeout=timeout)
        run_id = _generate_run_id()
        result = _run_with_semaphore(full_prompt, env, run_id, "reducer")

        data = {
            "run_id": run_id,
            "input_count": len(texts),
            "result": result.to_dict(),
        }
        return json.dumps(response_tools.truncate_response(data, f"reduce_{run_id[:8]}"), default=str)

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
    network: bool = NETWORK_DEFAULT,
    tools: str = "Read,Write,Glob,Grep,Bash",
    model: str = "sonnet",
    reduce_model: str = "",
    timeout: int = 120,
    max_concurrency: int = 5,
) -> str:
    """Map a prompt over inputs in parallel, then reduce results into one — all in a single call.
    This is the monadic bind: map produces N results, reduce consumes them, no manual plumbing.

    Args:
        prompt_template: Prompt template with {input} placeholder(s).
        inputs: JSON array of input strings: ["input1", "input2", ...].
        synthesis_prompt: Instructions for how to synthesise the map results.
        network: Whether containers have network access (default: true — needed for API calls).
        tools: Comma-separated list of allowed Claude tools for map agents.
        model: Claude model for map agents (default: sonnet).
        reduce_model: Claude model for the reduce agent (default: same as model).
        timeout: Max execution time per agent in seconds (default: 120).
        max_concurrency: Max map agents running simultaneously (default: 5).
    """
    try:
        input_list = json.loads(inputs)
        if not isinstance(input_list, list) or len(input_list) == 0:
            return json.dumps(response_tools.error_response("invalid_input", "inputs must be a non-empty JSON array"))

        # Phase 1: Map
        task_list = [
            {
                "prompt": prompt_template.replace("{input}", str(inp)),
                "network": network,
                "tools": tools,
                "model": model,
                "timeout": timeout,
            }
            for inp in input_list
        ]

        map_run_id, map_results = _run_par_internal(task_list, max_concurrency)

        # Check for failures
        failed = [r for r in map_results if r.error is not None]
        succeeded = [r for r in map_results if r.error is None]

        if not succeeded:
            data = {
                "run_id": map_run_id,
                "phase": "map",
                "error": "All map agents failed",
                "results": [r.to_dict() for r in map_results],
            }
            return json.dumps(response_tools.truncate_response(data, f"map_reduce_{map_run_id[:8]}"), default=str)

        # Phase 2: Reduce — feed successful texts into synthesis
        texts = [r.text for r in succeeded]
        sections = []
        for i, text in enumerate(texts):
            sections.append(f"## Input {i + 1}\n{text}")

        full_prompt = synthesis_prompt + "\n\n# Inputs to synthesise:\n\n" + "\n\n---\n\n".join(sections)

        reduce_env = parse_env_config(
            network=network,
            tools=tools,
            model=reduce_model or model,
            timeout=timeout,
        )
        reduce_result = _run_with_semaphore(full_prompt, reduce_env, map_run_id, "reducer")

        data = {
            "run_id": map_run_id,
            "map_total": len(map_results),
            "map_succeeded": len(succeeded),
            "map_failed": len(failed),
            "result": reduce_result.to_dict(),
        }
        if failed:
            data["map_errors"] = [{"agent_id": r.agent_id, "error": r.error} for r in failed]

        return json.dumps(response_tools.truncate_response(data, f"map_reduce_{map_run_id[:8]}"), default=str)

    except json.JSONDecodeError as e:
        return json.dumps(response_tools.error_response("json_error", f"Failed to parse JSON: {e}"))
    except Exception as e:
        logger.exception("map_reduce failed")
        return json.dumps(response_tools.error_response("map_reduce_error", str(e)))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
