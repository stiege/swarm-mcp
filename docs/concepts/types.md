# Natural Language Types

The swarm-mcp type system lets you define **semantic contracts** for agent inputs and outputs using plain Markdown files. Unlike JSON Schema or Pydantic, types are written in prose — for an LLM to read and understand — and enforced by a dedicated validator agent that reasons about meaning, not just structure.

This is the most powerful underused feature in swarm-mcp. It lets you validate things that are impossible with any schema language:

- "The analysis must identify at least 3 risk factors"
- "The code must not contain any hardcoded credentials"
- "The summary must be under 100 words and cite at least one source"

---

## What a Type Is

A type is a **Markdown file** that describes what something *is*, what it *contains*, and how to *verify* it. The format is deliberately free-form — you write whatever a Claude agent needs to understand the expected structure and quality bar.

```markdown
# research-notes

Raw research output from an initial investigation phase.

## Structure

- **topic**: The subject being researched (one line)
- **key_findings**: Bulleted list of 3–10 distinct findings, each with a source
- **open_questions**: Things that need further investigation
- **sources**: All URLs, file paths, or document references consulted

## Validity Criteria

- Must contain at least 3 key findings
- Every finding must cite at least one source
- Sources section must not be empty
- open_questions may be empty only if the topic is fully resolved
```

Save this as `types/research-notes.md` in your project. The **filename stem** (`research-notes`) is the type name. No registration step is needed beyond calling `wrap_project()`.

!!! note "Naming convention"
    Use lowercase hyphenated names — `research-notes`, `code-review`, `analysis-report`. This matches how `[refs]` look in prose and avoids case-sensitivity surprises across file systems.

---

## Where Types Live

Types are discovered in this priority order:

| Priority | Location | How to register |
|---|---|---|
| 1 | `<project>/types/` | `wrap_project(project_dir="/path/to/project")` |
| 2 | `$SWARM_PROJECT_DIR/types/` | Set the environment variable |
| 3 | `~/.claude/types/` | Place files there directly (global fallback) |

Project types shadow global types with the same name. A project's local `code-review.md` overrides any `~/.claude/types/code-review.md`.

```
wrap_project(project_dir="/workspace/my-project")
```

This registers the project's `types/`, `sandboxes/`, and `pipelines/` directories in one call.

---

## Why Types Matter

### Types guide agent behaviour

When you set `input_type` or `output_type` on a sandbox, the type definition is automatically injected into the agent's prompt. The agent always knows what it's supposed to produce — with full context — without you having to repeat it in every prompt.

### Types enforce semantic correctness

The validator agent *reads* your type definition and *reasons* about the output. It can catch things no schema validator ever could:

- A report that lists findings but forgets to rate their confidence
- A code review that mentions issues but omits line numbers
- A summary that's technically valid JSON but misses the point entirely

### Types compose

Use `[ref]` syntax to build complex types from simpler ones. An `analysis-report` can reference `[research-notes]` and `[severity-rating]`, each defined in their own focused file.

---

## How Types Are Injected into Prompts

When a sandbox has `input_type` or `output_type` set, the runner calls `build_type_context()` before executing the agent. This prepends a structured section to the agent's task prompt:

```
# Input Type
Your input is: <resolved type definition>

# Output Type
You must produce: <resolved type definition>
```

All `[ref]` syntax in the type is resolved (refs inlined recursively) before injection, so the agent sees the complete, self-contained definition — no dangling references.

**Example — setting types on a sandbox spec:**

```json
{
  "model": "sonnet",
  "tools": ["Read", "Write", "Glob", "Grep"],
  "input_type": "[research-notes]",
  "output_type": "[analysis-report]"
}
```

The `[research-notes]` and `[analysis-report]` brackets tell the runner to look up those type files and inline their content. You can also use plain prose instead of a ref:

```json
{
  "output_type": "A JSON object with a 'verdict' field (approve/reject) and a 'reason' field (one sentence)."
}
```

!!! tip "Inline vs. ref"
    Use `[type-name]` refs when you want a reusable, versioned definition that can be validated with `validate()`. Use plain prose when the type is one-off or trivial enough not to need a separate file.

---

## Type Composition with `[ref]` Syntax

Type files can reference other types using `[type-name]` in brackets. The resolver inlines the referenced type's full content recursively, up to `MAX_RESOLVE_DEPTH = 3` levels deep.

**Example — `security-audit.md` referencing two sub-types:**

```markdown
# security-audit

A comprehensive security audit of a codebase or system.

## Sections

- **scope**: What was audited (files, services, APIs)
- **vulnerabilities**: List of [vulnerability] items found
- **severity_summary**: A [severity-rating] for the overall audit
- **recommendations**: Ordered list of mitigations, highest severity first

## Validity Criteria

- Must contain at least one vulnerability entry (even if "none found")
- severity_summary must match the highest individual vulnerability severity
- Every critical vulnerability must have a concrete mitigation in recommendations
```

When `resolve_type("security-audit")` runs, it fetches `vulnerability.md` and `severity-rating.md` and inlines them:

```
**vulnerability**: <full content of vulnerability.md>
**severity-rating**: <full content of severity-rating.md>
```

If the same type appears a second time anywhere in the resolved tree, subsequent occurrences are replaced with `**type-name** (see above)` to avoid repetition.

!!! warning "Depth limit"
    References are resolved up to 3 levels deep (`MAX_RESOLVE_DEPTH = 3`). Deeper references are left as-is. Unknown `[references]` that don't match any registered type file are also left unchanged — the LLM can still interpret them as intent.

---

## The Type Enforcement Triad

### `validate()` — check a single artifact

```
validate(artifact, declared_type) → {verdict, result}
```

Spawns a validator agent that reads the type definition and the artifact, then checks every criterion in the type's `## Validity Criteria` section. The verdict is one of:

| Verdict | Meaning |
|---|---|
| `VALID` | All criteria passed |
| `PARTIAL` | Some criteria passed; issues listed |
| `INVALID` | Critical criteria failed |

The validator always responds in this exact format:

```markdown
## Checks
- [required fields present]: PASS — all four required sections found
- [minimum findings count]: PASS — 5 findings identified
- [sources cited]: FAIL — finding #3 has no source
- [confidence rated]: PASS — rated "medium"

## Verdict
PARTIAL

## Issues
- Finding #3 ("Cache invalidation race") is missing a source citation.
  Add a URL or document reference.
```

**Example:**

```
validate(
  artifact='{"topic": "LLM hallucination", "key_findings": [...], "sources": [...]}',
  declared_type="research-notes"
)
```

Returns:
```json
{
  "run_id": "abc123",
  "declared_type": "research-notes",
  "verdict": "VALID",
  "result": { "text": "## Checks\n- [minimum findings]: PASS ..." }
}
```

!!! note "Model choice"
    `validate()` defaults to `sonnet`. You can override with `model="opus"` for complex types that require deeper reasoning, or `model="haiku"` when speed matters more than thoroughness.

---

### `filter()` — keep only valid items from a batch

```
filter(refs, declared_type) → {results: [...valid refs...], rejected: [...invalid refs...]}
```

Runs `validate()` on every ref in the input list **in parallel**, then partitions the results. Only refs that receive a `VALID` verdict appear in `results`. This is the primary building block for type-gated pipelines.

```
filter(
  refs='[{"ref": "abc123/agent-0"}, {"ref": "abc123/agent-1"}, {"ref": "abc123/agent-2"}]',
  declared_type="research-notes"
)
```

Returns:
```json
{
  "run_id": "def456",
  "total": 3,
  "valid": 2,
  "invalid": 1,
  "results": [
    {"ref": "abc123/agent-0", "validated_as": "research-notes", "verdict": "VALID"},
    {"ref": "abc123/agent-2", "validated_as": "research-notes", "verdict": "VALID"}
  ],
  "rejected": [
    {"ref": "abc123/agent-1", "validated_as": "research-notes", "verdict": "INVALID"}
  ]
}
```

!!! tip "Fan-out → filter pattern"
    Use `par()` or a pipeline fan-out to generate N results, then `filter()` to keep only conforming ones. This is much cheaper than retrying failures — you get the best results from a batch without blocking on the weakest ones.

---

### `retry()` — loop until output validates

```
retry(prompt, declared_type, max_attempts) → ref
```

Runs a single agent repeatedly until its output validates as the declared type — or until `max_attempts` is exhausted. Each failed attempt feeds its error and validation feedback back into the next attempt's prompt.

```
retry(
  prompt="Research the current state of WebAssembly tooling. Produce research notes.",
  declared_type="research-notes",
  max_attempts=3
)
```

On a validation failure, the next attempt receives:

```
# Prior attempts failed with these errors:

## Attempt 1
Type validation failed (PARTIAL): ## Checks
- [minimum findings count]: FAIL — only 2 findings, need at least 3
...

Please fix the issues and try again.
```

The agent sees exactly what the validator said and can address the specific failures.

!!! warning "Cost vs. correctness"
    `retry()` runs a validator agent after each production attempt, so a 3-attempt retry can cost up to 6 agent runs. Use it for high-stakes outputs where correctness matters more than cost. For batch work, prefer `filter()` instead.

---

## Complete Worked Example

A three-step research-and-analysis pipeline with type enforcement at every boundary.

### Step 1 — Define the types

`types/research-notes.md`:
```markdown
# research-notes

Raw research output from an initial investigation phase.

## Structure

- **topic**: The research subject (one line)
- **key_findings**: 3–10 findings, each citing at least one source
- **open_questions**: Unresolved questions for further investigation
- **sources**: All references consulted (URLs, file paths, document names)

## Validity Criteria

- Must contain at least 3 key findings
- Every finding must cite at least one source
- sources list must not be empty
```

`types/analysis-report.md`:
```markdown
# analysis-report

A structured analytical report derived from [research-notes].

## Structure

- **executive_summary**: 1–3 paragraph overview suitable for a non-technical reader
- **findings**: Each finding from the research, interpreted with evidence
- **risks**: At least 2 identified risks, each with a concrete mitigation
- **recommendations**: Prioritised action items
- **confidence**: One of "high", "medium", or "low", with justification

## Validity Criteria

- executive_summary must be present and non-empty
- risks section must identify at least 2 distinct risks
- Each risk must include at least one concrete mitigation
- confidence field must be one of: high, medium, low
- recommendations must contain at least one item
```

### Step 2 — Register the project

```
wrap_project(project_dir="/workspace/my-project")
```

### Step 3 — Run the research step

```json
{
  "model": "sonnet",
  "tools": ["Read", "Glob", "Grep", "Bash"],
  "output_type": "[research-notes]",
  "prompt": "Research the security implications of using LLMs to generate Kubernetes configurations. Produce research notes."
}
```

Because `output_type` is set, the agent's prompt is automatically prepended with:

```
# Output Type
You must produce: research-notes

Raw research output from an initial investigation phase.
...
```

### Step 4 — Validate research output

```
validate(artifact='{"ref": "run-abc/agent-0"}', declared_type="research-notes")
```

If `PARTIAL` or `INVALID`, use `retry()` instead of bare `run()` to get a corrected output.

### Step 5 — Run analysis with type-gated input

```json
{
  "model": "sonnet",
  "input_type": "[research-notes]",
  "output_type": "[analysis-report]",
  "prompt": "Analyse the research notes and produce a structured analysis report."
}
```

### Step 6 — Filter a batch of analyses

If you ran analysis on multiple research inputs in parallel:

```
filter(
  refs='[{"ref": "run-xyz/agent-0"}, {"ref": "run-xyz/agent-1"}, {"ref": "run-xyz/agent-2"}]',
  declared_type="analysis-report"
)
```

Only the analyses that meet all validity criteria flow into the next step.

---

## Discovering and Inspecting Types

### List all registered types

```
list_type_registry()
```

Returns every type visible to the current session:

```json
[
  {"name": "research-notes", "summary": "Raw research output from an initial investigation phase.", "source": "/workspace/my-project/types"},
  {"name": "analysis-report", "summary": "A structured analytical report derived from research notes.", "source": "/workspace/my-project/types"},
  {"name": "code-review", "summary": "Structured code review output from a reviewer agent.", "source": "/home/user/.claude/types"}
]
```

The `summary` field is the **first line** of the type file — keep it concise and descriptive.

### Inspect a type definition

```
get_type_definition(name="analysis-report")
get_type_definition(name="analysis-report", resolve_refs=true)
```

With `resolve_refs=true` (the default), all `[references]` are inlined — this is exactly what the validator agent and `build_type_context()` see. Use it to debug unexpected validation behaviour.

---

## Writing Good Types

**Be specific about structure and content.** Don't just say "a report" — say what sections it has, how long each should be, and what makes each section valid.

```markdown
## Validity Criteria

- executive_summary must be 1–3 paragraphs (not a single sentence, not a page)
- risks section must identify at least 2 concrete mitigations, not vague advice
- confidence must be exactly one of: high, medium, low — no free text
```

**Include both what to include AND what makes it invalid.** Validators catch failures better when the type explicitly states disqualifying conditions.

```markdown
## Invalid if

- Any required field is null or missing
- The summary simply restates the issue list verbatim
- Recommendations are generic (e.g. "follow best practices") with no specifics
```

**Keep types focused — one concept per type.** Compose with `[refs]` rather than cramming everything into one file. A `security-audit` that references `[vulnerability]` and `[severity-rating]` is easier to maintain than a monolithic file.

**Always add a `## Validity Criteria` section** with a bullet list. The validator agent uses this as its checklist. Without it, validation is vague and unreliable.

!!! tip "First line matters"
    The first line of your type file becomes the `summary` in `list_type_registry()` output and appears in agent context. Make it a single, descriptive sentence that captures the essence of the type.

!!! note "Types are for LLMs, not parsers"
    There is no formal parser. You can use any Markdown structure that reads clearly to a Claude agent. Headers, bullets, tables, code blocks — all are fine. The validator reads the whole definition and reasons about it semantically.

---

## Tool Reference

| Tool | Signature | Description |
|---|---|---|
| `validate` | `(artifact, declared_type, model?, sandbox?)` | Run a validator agent on a single artifact |
| `filter` | `(refs, declared_type, model?)` | Parallel validate; return only VALID refs |
| `retry` | `(prompt, declared_type, max_attempts?)` | Re-run until output validates or attempts exhausted |
| `get_type_definition` | `(name, resolve_refs?)` | Get type Markdown, optionally with refs inlined |
| `list_type_registry` | `()` | List all registered types with summaries and sources |
| `wrap_project` | `(project_dir)` | Register a project's `types/` directory |

---

## Related Pages

- [Pipelines](pipelines.md) — use `filter()` between pipeline steps
- [Sandboxes](sandboxes.md) — `input_type` and `output_type` sandbox fields
- [Combinators](combinators.md) — fan-out patterns that produce batches for `filter()`
- [Refs](refs.md) — how artifact references work across pipeline steps
