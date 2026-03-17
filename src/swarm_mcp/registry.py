"""Registry — wraps external files/directories into the swarm ref system.

:func:`wrap_file` brings host objects *into* the monadic context
(files / directories → ref IDs).  The MCP ``unwrap`` tool takes them back out
(ref IDs → on-disk paths / text).

The registry also manages **search paths** for the three named-resource types:

- ``"pipelines"`` — JSON pipeline definitions.
- ``"sandboxes"`` — JSON sandbox spec files.
- ``"types"`` — Markdown type definitions.

Search order (first match wins)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. Paths explicitly registered via :func:`add_search_path`.
2. Subdirectories of ``SWARM_PROJECT_DIR`` env var (if set and exists).
3. ``~/.claude/<resource_type>/`` (global fallback, always appended).

The registry is initialised from the environment automatically on first import.
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


def _init_search_paths() -> None:
    """Initialise search paths from environment variables and global defaults.

    Called once at module import time.  Safe to call multiple times — paths
    are only added when not already present, so re-importing is idempotent.
    """
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


def add_search_path(resource_type: str, path: str) -> None:
    """Prepend a search path for a named resource type.

    The new path is inserted at position 0 so it takes priority over any
    previously registered paths (including the global fallback).

    Args:
        resource_type: One of ``"pipelines"``, ``"sandboxes"``, or
            ``"types"``.  Unknown types create a new entry.
        path: Absolute directory path to add.  Duplicates are silently
            ignored.
    """
    if resource_type not in _search_paths:
        _search_paths[resource_type] = []
    if path not in _search_paths[resource_type]:
        _search_paths[resource_type].insert(0, path)  # project paths take priority
        logger.info("Added %s search path: %s", resource_type, path)


def find_resource(resource_type: str, name: str, extension: str = ".json") -> str | None:
    """Find a named resource file by searching all registered paths.

    Searches each path for ``<name><extension>`` in the order they were
    registered (project paths first, global fallback last).

    Args:
        resource_type: One of ``"pipelines"``, ``"sandboxes"``, or
            ``"types"``.
        name: Resource name without extension (e.g. ``"web-researcher"``).
        extension: File extension to append (default: ``".json"``).

    Returns:
        The absolute path to the first matching file, or ``None`` if not found
        in any search path.
    """
    for search_dir in _search_paths.get(resource_type, []):
        candidate = os.path.join(search_dir, f"{name}{extension}")
        if os.path.exists(candidate):
            return candidate
    return None


def list_resources(resource_type: str, extension: str = ".json") -> list[dict]:
    """List all resource files of a given type across all registered search paths.

    Each file name is only reported once — if the same name appears in multiple
    search paths the first (highest-priority) occurrence is returned.

    Args:
        resource_type: One of ``"pipelines"``, ``"sandboxes"``, or
            ``"types"``.
        extension: File extension filter (default: ``".json"``).

    Returns:
        A list of dicts sorted by filename within each search path, each with:

        - ``"name"`` — resource name (filename without extension).
        - ``"path"`` — absolute path to the file.
        - ``"source"`` — directory the file was found in.
    """
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
    """Wrap a host file or directory into the swarm ref system.

    Copies the file (or directory tree) into ``/tmp/swarm-mcp/wrapped/<id>/``
    and writes a synthetic ``result.json`` so that the standard ref-resolution
    machinery can read its content.

    Args:
        path: Absolute path to the host file or directory to wrap.

    Returns:
        A ref ID string of the form ``"wrapped/<12-hex-chars>"``.

    Raises:
        FileNotFoundError: If *path* does not exist on the host.
    """
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
    """Register a project's resource directories with the swarm registry.

    Looks for ``pipelines/``, ``sandboxes/``, and ``types/`` subdirectories
    inside *project_dir* and calls :func:`add_search_path` for each one that
    exists.  After this call, named resources from the project are discoverable
    by all swarm tools.

    Args:
        project_dir: Absolute path to the project root directory.

    Returns:
        A dict mapping resource-type names to registration info.  Only
        resource types with an existing subdirectory are included.  Each
        value has ``"path"`` (the registered directory) and ``"count"``
        (number of files found there).  An empty dict means nothing was
        registered.
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
