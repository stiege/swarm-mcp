"""Registry — wraps external files/directories into the swarm ref system.

wrap() brings objects into the monadic context (files → refs).
unwrap() takes them out (refs → files).

The registry also provides search paths for pipelines, sandboxes, and types.
Instead of hardcoding ~/.claude/, projects can register their own directories.

Search order for any named resource:
  1. Explicitly registered paths (via wrap or add_search_path)
  2. SWARM_PROJECT_DIR env var (if set)
  3. ~/.claude/ (global fallback)
"""

import json
import logging
import os
import shutil
import uuid

logger = logging.getLogger(__name__)

# Global search paths for each resource type.
# Each list is searched in order; first match wins.
_search_paths: dict[str, list[str]] = {
    "pipelines": [],
    "sandboxes": [],
    "types": [],
}

# Wrapped refs: ref_id → file path on disk
_wrapped: dict[str, str] = {}

GLOBAL_BASE = os.path.expanduser("~/.claude")


def _init_search_paths():
    """Initialize search paths from environment and defaults."""
    # SWARM_PROJECT_DIR — a project root containing pipelines/, sandboxes/, types/
    project_dir = os.environ.get("SWARM_PROJECT_DIR")
    if project_dir and os.path.isdir(project_dir):
        for resource_type in _search_paths:
            candidate = os.path.join(project_dir, resource_type)
            if os.path.isdir(candidate) and candidate not in _search_paths[resource_type]:
                _search_paths[resource_type].insert(0, candidate)

    # Global fallback
    for resource_type in _search_paths:
        global_dir = os.path.join(GLOBAL_BASE, resource_type)
        if global_dir not in _search_paths[resource_type]:
            _search_paths[resource_type].append(global_dir)


def add_search_path(resource_type: str, path: str):
    """Add a search path for a resource type (pipelines, sandboxes, types)."""
    if resource_type not in _search_paths:
        _search_paths[resource_type] = []
    if path not in _search_paths[resource_type]:
        _search_paths[resource_type].insert(0, path)  # project paths take priority
        logger.info("Added %s search path: %s", resource_type, path)


def find_resource(resource_type: str, name: str, extension: str = ".json") -> str | None:
    """Find a named resource by searching all registered paths.

    Returns the full file path, or None if not found.
    """
    for search_dir in _search_paths.get(resource_type, []):
        candidate = os.path.join(search_dir, f"{name}{extension}")
        if os.path.exists(candidate):
            return candidate
    return None


def list_resources(resource_type: str, extension: str = ".json") -> list[dict]:
    """List all resources of a given type across all search paths."""
    seen = set()
    result = []
    for search_dir in _search_paths.get(resource_type, []):
        if not os.path.isdir(search_dir):
            continue
        for entry in sorted(os.scandir(search_dir), key=lambda e: e.name):
            if entry.name.endswith(extension) and entry.name not in seen:
                seen.add(entry.name)
                name = entry.name[: -len(extension)]
                result.append({"name": name, "path": entry.path, "source": search_dir})
    return result


def wrap_file(path: str) -> str:
    """Wrap a file into the ref system. Returns a ref ID."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Cannot wrap: {path}")

    ref_id = f"wrapped/{uuid.uuid4().hex[:12]}"

    # Copy to /tmp/swarm-mcp/ so it's in the standard ref namespace
    ref_dir = os.path.join("/tmp/swarm-mcp", ref_id)
    os.makedirs(ref_dir, exist_ok=True)

    if os.path.isfile(path):
        dest = os.path.join(ref_dir, os.path.basename(path))
        shutil.copy2(path, dest)
        # Also write a result.json so unwrap works
        with open(os.path.join(ref_dir, "result.json"), "w") as f:
            with open(path) as src:
                content = src.read()
            json.dump({
                "agent_id": ref_id.split("/")[1],
                "text": content,
                "exit_code": 0,
                "duration_seconds": 0,
                "cost_usd": 0,
                "model": "wrapped",
                "output_dir": ref_dir,
                "source_path": path,
            }, f, indent=2)
    elif os.path.isdir(path):
        # Copy entire directory
        shutil.copytree(path, ref_dir, dirs_exist_ok=True)
        # Write a result.json listing the contents
        files = [e.name for e in os.scandir(ref_dir) if e.is_file()]
        with open(os.path.join(ref_dir, "result.json"), "w") as f:
            json.dump({
                "agent_id": ref_id.split("/")[1],
                "text": f"Directory wrapped: {path}\nFiles: {', '.join(files)}",
                "exit_code": 0,
                "duration_seconds": 0,
                "cost_usd": 0,
                "model": "wrapped",
                "output_dir": ref_dir,
                "source_path": path,
                "files": files,
            }, f, indent=2)

    _wrapped[ref_id] = path
    logger.info("Wrapped %s → %s", path, ref_id)
    return ref_id


def wrap_project(project_dir: str) -> dict:
    """Wrap an entire project — registers its pipelines/, sandboxes/, types/ directories.

    Returns a summary of what was registered.
    """
    project_dir = os.path.abspath(project_dir)
    registered = {}

    for resource_type in ["pipelines", "sandboxes", "types"]:
        candidate = os.path.join(project_dir, resource_type)
        if os.path.isdir(candidate):
            add_search_path(resource_type, candidate)
            count = len([e for e in os.scandir(candidate) if e.is_file()])
            registered[resource_type] = {"path": candidate, "count": count}

    return registered


# Initialize on import
_init_search_paths()
