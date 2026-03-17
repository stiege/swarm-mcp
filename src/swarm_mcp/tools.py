"""MCP response helpers — truncation and error formatting.

This module provides two small utilities used by every MCP tool handler in
:mod:`swarm_mcp.server` to produce consistent, well-formed JSON responses:

- :func:`truncate_response` — caps large payloads that would exceed the MCP
  protocol's comfortable message size, writing the full content to a temp file
  and returning a compact summary instead.
- :func:`error_response` — constructs a standard ``{"error": ..., "message":
  ...}`` dict for all failure paths.
"""

import json
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

TRUNCATE_DIR = "/tmp/swarm-mcp"
"""Base directory where oversized response payloads are spilled to disk."""

MAX_RESPONSE_SIZE = 50_000
"""Character threshold above which a response is considered too large to return inline."""


def truncate_response(data: Any, operation: str) -> dict:
    """Return *data* as-is, or a compact summary if it exceeds ``MAX_RESPONSE_SIZE``.

    When the JSON-serialised form of *data* exceeds ``MAX_RESPONSE_SIZE``
    characters the full payload is written to a uniquely named file under
    ``TRUNCATE_DIR`` and a summary dict is returned instead.  The summary
    includes the file path so callers can ``Read()`` it if needed.

    Args:
        data: Any JSON-serialisable value (typically a list or dict).
        operation: Short label used as the filename prefix (e.g.
            ``"validate_abc123"``).

    Returns:
        The original *data* unchanged if small enough, otherwise a dict with
        keys ``"truncated"`` (``True``), ``"summary"`` (human-readable
        message), ``"file"`` (absolute path to the full payload), and
        ``"preview"`` (first three items for lists, or the dict itself).
    """
    text = json.dumps(data, indent=2, default=str)
    if len(text) <= MAX_RESPONSE_SIZE:
        return data

    os.makedirs(TRUNCATE_DIR, exist_ok=True)
    filename = f"{operation}_{uuid.uuid4().hex[:8]}.json"
    filepath = os.path.join(TRUNCATE_DIR, filename)
    with open(filepath, "w") as f:
        f.write(text)

    row_count = len(data) if isinstance(data, list) else 1
    logger.info("Response truncated, full output written to %s", filepath)
    return {
        "truncated": True,
        "summary": f"Response too large ({len(text)} chars). {row_count} results written to {filepath}",
        "file": filepath,
        "preview": data[:3] if isinstance(data, list) else data,
    }


def error_response(error_type: str, message: str) -> dict:
    """Build a standard error response dict.

    Args:
        error_type: Short machine-readable error code (e.g.
            ``"not_found"``, ``"json_error"``).
        message: Human-readable description of the error.

    Returns:
        A dict ``{"error": error_type, "message": message}``.
    """
    return {"error": error_type, "message": message}
