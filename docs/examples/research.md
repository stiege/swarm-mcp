# Research Fan-Out

Fan out a research task across five topics in parallel, then synthesize the
findings into a single unified briefing — all in one `map_reduce()` call.

---

## Overview

The pattern: one prompt template, many inputs, one synthesis at the end.

```
["quantum computing", "CRISPR", "fusion", ...]
        │
        ▼
map("{input}" × 5 topics)  ──► 5 refs  ──► reduce(synthesis_prompt) ──► 1 ref
                                                                              │
                                                                         unwrap()
                                                                              │
                                                                       output.md
```

`map_reduce()` handles both stages in a single tool call: it runs the map fan-out,
waits for all results, then feeds the collected outputs into the reduce synthesis
agent automatically. No manual plumbing needed.

---

## The Call

```python
map_reduce(
    prompt_template="Research the following topic and produce a structured summary of key findings: {input}\n\nCover:\n- Current state of the field (what works today)\n- Breakthrough developments in the last 2 years\n- Key open problems and limitations\n- Realistic timeline for major milestones\n- Notable researchers, institutions, or companies driving progress\n\nBe specific. Cite concrete examples where possible.",
    inputs='["quantum computing", "CRISPR gene editing", "nuclear fusion", "brain-computer interfaces", "room-temperature superconductors"]',
    synthesis_prompt="You have received research summaries for five frontier technology areas. Synthesize them into a unified research briefing for a technical leadership audience.\n\nThe briefing should:\n1. Open with a 2-paragraph executive summary comparing the maturity levels across all five fields\n2. For each field, a section with: current TRL level, most significant recent breakthrough, biggest remaining barrier, and estimated time to widespread impact\n3. A cross-field analysis: which fields have synergies, which share underlying research challenges\n4. A closing recommendation: which two fields deserve priority investment attention over the next 3 years, and why\n\nTone: authoritative, analytical, evidence-grounded. Avoid hedging language.",
    model="sonnet",
    max_concurrency=5
)
```

!!! tip "Concurrency and cost"
    Setting `max_concurrency=5` launches all five research agents simultaneously.
    Total wall-clock time is roughly the duration of the slowest single agent,
    not the sum. With `model="sonnet"`, expect $0.05–0.15 per topic and
    $0.10–0.20 for the synthesis — around $0.40–0.95 total.

---

## Expected Return Value

`map_reduce()` returns a single ref for the synthesis result, plus a summary of
the map stage:

```json
{
  "ref": "a1b2c3d4/agent-reduce-0",
  "exit_code": 0,
  "duration_seconds": 87.4,
  "cost_usd": 0.78,
  "model": "sonnet",
  "map_results": [
    { "ref": "a1b2c3d4/agent-0", "exit_code": 0, "cost_usd": 0.12, "input": "quantum computing" },
    { "ref": "a1b2c3d4/agent-1", "exit_code": 0, "cost_usd": 0.09, "input": "CRISPR gene editing" },
    { "ref": "a1b2c3d4/agent-2", "exit_code": 0, "cost_usd": 0.11, "input": "nuclear fusion" },
    { "ref": "a1b2c3d4/agent-3", "exit_code": 0, "cost_usd": 0.10, "input": "brain-computer interfaces" },
    { "ref": "a1b2c3d4/agent-4", "exit_code": 0, "cost_usd": 0.14, "input": "room-temperature superconductors" }
  ],
  "map_succeeded": 5,
  "map_failed": 0,
  "provenance": {
    "parent_refs": [
      "a1b2c3d4/agent-0",
      "a1b2c3d4/agent-1",
      "a1b2c3d4/agent-2",
      "a1b2c3d4/agent-3",
      "a1b2c3d4/agent-4"
    ],
    "content_hash": "f3a9e1b2c4d5",
    "timestamp": "2026-03-17T14:22:10Z"
  }
}
```

The `provenance.parent_refs` field traces the synthesis back to its five source agents.
Each source ref is independently readable via `unwrap()` or `inspect()`.

---

## Extracting the Report

```python
# Get the synthesis text
unwrap(ref="a1b2c3d4/agent-reduce-0")
# → { "ref": "a1b2c3d4/agent-reduce-0", "file": "/tmp/swarm-mcp/a1b2c3d4/agent-reduce-0/output.md", "size": 8241 }

# Now Read() the file in Claude Code
Read("/tmp/swarm-mcp/a1b2c3d4/agent-reduce-0/output.md")
```

To read an individual topic's raw research before synthesis:

```python
unwrap(ref="a1b2c3d4/agent-0")   # quantum computing findings
unwrap(ref="a1b2c3d4/agent-2")   # nuclear fusion findings
```

---

## Expected Output Structure

The synthesis agent should produce something shaped like this:

```markdown
# Frontier Technology Research Briefing

## Executive Summary

Quantum computing and CRISPR gene editing have crossed from research curiosity
into engineering discipline — the open questions are now about engineering scale,
not fundamental feasibility. Nuclear fusion achieved a net-energy milestone in
2022 and is now in an intense engineering phase. Brain-computer interfaces face
the hardest long-term biological challenges; room-temperature superconductors
remain at the discovery stage with contested reproducibility.

Fields that looked evenly matched two years ago have diverged sharply on
deployment timelines. CRISPR therapies are in clinical trials today; room-
temperature superconductors are still at the replication-and-characterization
stage.

---

## Quantum Computing

**TRL Level:** 4–5 (limited operational prototypes)
**Breakthrough:** 1000+ qubit processors with error-correction demonstrations
**Biggest barrier:** Fault-tolerant logical qubit count at scale
**Impact timeline:** Narrow commercial advantage 2028–2032; broad impact 2035+

...

## Cross-Field Analysis

CRISPR and BCI share a regulatory bottleneck: both require long-term human
safety data before widespread approval. Quantum computing and superconductors
share a materials science dependency — better superconducting materials
accelerate both fields simultaneously.

...

## Investment Recommendation

Priority fields: **Quantum Computing** and **CRISPR Gene Editing**. Both have
crossed the feasibility threshold and now face engineering and scale challenges
where capital and talent density create decisive advantage.
```

---

## Variations

### Research with web access

If your agent image has network access and a web search MCP:

```python
map_reduce(
    prompt_template="Use web search to find the latest developments (last 6 months) on: {input}. Focus on peer-reviewed publications and credible technical sources.",
    inputs='["quantum error correction", "CRISPR off-target effects", "tokamak plasma stability"]',
    synthesis_prompt="Synthesize these research updates into a technical newsletter section.",
    mcps='["web-search-mcp"]',
    tools='["Read", "Bash"]',
    model="sonnet"
)
```

### Higher-quality synthesis with Opus

Use a cheaper model for the fan-out and a more capable model for synthesis:

```python
map_reduce(
    prompt_template="Research {input} — key facts, timeline, players.",
    inputs='["quantum computing", "CRISPR", "fusion", "BCI", "superconductors"]',
    synthesis_prompt="Write a 2000-word authoritative briefing synthesizing all five reports.",
    model="haiku",          # cheap for the 5 parallel workers
    synthesis_model="opus"  # premium for the final synthesis
)
```

### Saving individual reports

After `map_reduce()` returns, access each agent's individual ref from
`map_results` to unwrap individual topic reports before or after reading the
synthesis:

```python
# Unwrap the nuclear fusion deep-dive independently
unwrap(ref="a1b2c3d4/agent-2")
```

---

!!! note "See also"
    - [map() and reduce() separately](../concepts/combinators.md#map) — when you need to inspect map results before synthesizing
    - [Parallel Code Review](code-review.md) — map over files instead of topics
    - [Observability](../observability.md) — use `inspect()` if any map agent fails
