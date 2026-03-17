#!/bin/bash
# PostToolUse hook that logs MCP tool interactions to /output/artifacts.jsonl
# Injected into swarm agent containers for artifact tracing.

set -e

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# Only log MCP tool calls (mcp__*) and file writes
case "$TOOL_NAME" in
  mcp__*|Write)
    TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "$INPUT" | jq -c '{
      timestamp: "'"$TIMESTAMP"'",
      tool: .tool_name,
      tool_use_id: .tool_use_id,
      input: .tool_input,
      response: .tool_response
    }' >> /output/artifacts.jsonl 2>/dev/null || true
    ;;
esac

exit 0
