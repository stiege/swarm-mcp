# swarm-mcp

**swarm-mcp** is an MCP server that lets Claude Code orchestrate fleets of parallel Claude AI agents running in isolated Docker containers. You describe the work; swarm-mcp schedules, runs, and composes the results.

## The Core Insight: Refs as Lazy Computation

Every combinator in swarm-mcp returns a **ref** — a lightweight metadata handle — rather than raw text. A ref records where the output lives (`run_id`, `agent_id`) without materialising it. Text is only extracted when you explicitly call `unwrap()`.

This is the monadic architecture: orchestration and output are decoupled. Because refs are plain data, you can pass them between combinators, store them, inspect them, and compose them before a single byte of text is consumed. The result is pipelines that compose cleanly and scale horizontally without holding large strings in memory.

```
┌─────────────┐     MCP Protocol      ┌──────────────────────┐
│             │ ───────────────────►  │                      │
│ Claude Code │                       │   swarm-mcp server   │
│             │ ◄───────────────────  │                      │
└─────────────┘      refs / text      └──────────┬───────────┘
                                                 │ Docker API
                                    ┌────────────▼────────────┐
                                    │     Docker Engine        │
                                    │                          │
                                    │  ┌────────┐ ┌────────┐  │
                                    │  │agent-1 │ │agent-2 │  │
                                    │  └───┬────┘ └───┬────┘  │
                                    │  ┌───▼────┐ ┌───▼────┐  │
                                    │  │agent-3 │ │agent-N │  │
                                    │  └────────┘ └────────┘  │
                                    └─────────────────────────┘
                                              │
                                         refs │  unwrap()
                                              ▼
                                    ┌─────────────────────────┐
                                    │      text / JSON         │
                                    └─────────────────────────┘
```

## Key Features

- **Parallel execution** — `par()` fans out to N agents simultaneously; `map()` applies a prompt to a list of inputs in parallel.
- **Composable combinators** — `chain()`, `reduce()`, `map_reduce()`, `filter()`, `race()`, `retry()`, and `guard()` let you build arbitrarily complex workflows from simple building blocks.
- **Declarative pipelines** — YAML/JSON pipeline definitions are interpreted by a free monad evaluator, so pipelines are data that can be stored, versioned, and composed.
- **Structured type system** — Markdown files with `[name]` references define input and output schemas, resolved recursively, so agents communicate through validated structured data.
- **Security by default** — Each agent runs in a fresh, isolated Docker container. OAuth credentials are injected at runtime and never baked into images.
- **Resource pools** — Named resource slots (`SWARM_RESOURCE_<name>`) let you cap concurrency on GPUs, API keys, or any scarce resource, with automatic queuing.
- **Full observability** — Every agent emits `result.json`, `stream.jsonl`, `artifacts.jsonl`, `output.md`, and `prompt.txt`, giving you a complete audit trail.

## Get Started

- [Installation](installation.md) — install swarm-mcp, build the Docker image, and wire it into Claude Code.
- [Quickstart](quickstart.md) — run your first agent and pipeline in ten minutes.
