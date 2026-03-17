import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)

TRUNCATE_DIR = "/tmp/swarm-mcp"
MAX_RESPONSE_SIZE = 50_000  # characters


def truncate_response(data: any, operation: str) -> dict:
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
    return {"error": error_type, "message": message}
