# Type System

The swarm-mcp type system lets you define **structured expectations** for agent inputs and outputs using plain markdown files. Types are resolved, composed, and enforced at runtime by a dedicated checker agent — no schemas, no code generation, no compilation step.

---

## Type Definitions as Markdown Files

A type is a `.md` file that describes what a value should contain. The format is deliberately free-form: write whatever natural language a Claude agent needs to understand the structure.

```markdown
# AnalysisReport

A structured report produced by the analysis agent.

## Required sections

- **summary**: One paragraph, ≤150 words
- **findings**: Bulleted list of 3–10 distinct findings
- **confidence**: One of `high`, `medium`, or `low`
- **sources**: List of URLs or file paths consulted

## Optional sections

- **caveats**: Known limitations or data gaps
- **next_steps**: Recommended follow-up actions
```

Save this file as `types/AnalysisReport.md` in your project (or `~/.claude/types/`). That's all — no registration step is needed if you use `wrap_project()`.

---

## Recursive `[type-name]` References

Type definitions can embed other types using the `[TypeName]` syntax. The resolver expands references recursively up to a depth of 3 (`MAX_RESOLVE_DEPTH`).

```markdown
# ReviewPackage

A bundle passed to the review pipeline.

## Fields

- **report**: [AnalysisReport]
- **metadata**: [RunMetadata]
- **reviewer_notes**: Free text
```

When the type system resolves `ReviewPackage`, it inlines the full definitions of `AnalysisReport` and `RunMetadata` so the checker agent has all the context it needs in one prompt. Circular references are detected and stopped at the depth limit.

---

## Type Registry Search Paths

Types are looked up in order:

1. Project directory — `<project>/types/`
2. User global directory — `~/.claude/types/`

Project types shadow global types with the same name. You can inspect what is registered with:

```
list_type_registry()
```

This lists every type visible to the current session, showing which search path each came from.

### `wrap_project()`

To register a project's `types/` directory in the current session:

```
wrap_project(path="/path/to/my-project")
```

This adds the project's `types/` directory to the front of the search path. Types defined there take precedence over global types for the rest of the session.

### `get_type_definition(name, resolve_refs?)`

Retrieve the raw markdown for a type:

```
get_type_definition(name="AnalysisReport")
get_type_definition(name="ReviewPackage", resolve_refs=true)
```

With `resolve_refs=true` the response contains the fully expanded definition with all `[TypeName]` references inlined — useful for debugging what the checker agent will actually see.

---

## Validating with `validate()`

```
validate(ref, type_name) → {status, feedback}
```

`validate()` runs a **type-checker agent** that reads both the type definition and the actual value, then returns one of three verdicts:

| Status | Meaning |
|---|---|
| `VALID` | The value satisfies all requirements in the type definition |
| `PARTIAL` | The value meets some but not all requirements; `feedback` explains what is missing |
| `INVALID` | The value does not satisfy the type; `feedback` explains why |

### Example

```python
result = validate(
    ref="The analysis found three anomalies: ...",   # the value to check
    type_name="AnalysisReport"
)
# → {"status": "PARTIAL", "feedback": "Missing 'confidence' field"}
```

The checker agent uses `build_validation_prompt()` internally, which constructs a prompt containing:

- The expanded type definition (with all refs resolved)
- A list of explicit criteria derived from the type
- The value to evaluate

This means complex, nested types with prose descriptions are validated as reliably as strict JSON schemas — because Claude is the validator.

---

## Filtering with `filter()`

```
filter(refs, type_name) → list
```

`filter()` runs `validate()` on each item in `refs` **in parallel** and returns only the items that receive a `VALID` verdict.

```python
candidates = [report_a, report_b, report_c, report_d]

valid_reports = filter(
    refs=candidates,
    type_name="AnalysisReport"
)
# → [report_a, report_c]   (report_b and report_d failed validation)
```

This is the primary building block for **type-gated pipelines**: fan out work with [Combinators](combinators.md), collect results, then filter to keep only conforming outputs before passing them downstream.

```json
{
  "steps": [
    {
      "id": "generate-reports",
      "prompt": "Generate analysis reports for each dataset in /shared/datasets/"
    },
    {
      "id": "filter-valid",
      "prompt": "Use filter() to keep only outputs that conform to the AnalysisReport type"
    },
    {
      "id": "summarise",
      "prompt": "Summarise the valid reports collected in the previous step"
    }
  ]
}
```

---

## Using Types in Sandboxes

Sandbox specs accept `input_type` and `output_type` fields that attach type expectations to a single agent call:

```json
{
  "model": "sonnet",
  "tools": ["Read", "Write"],
  "input_type": "DatasetSpec",
  "output_type": "AnalysisReport"
}
```

When `output_type` is set, the type context is injected into the agent's prompt via `build_type_context()`, which adds a dedicated section describing exactly what output structure is expected. This guides the agent without requiring a separate validation step.

For stricter enforcement, combine `output_type` with an explicit `validate()` call in a downstream step.

---

## Example: Full Type Workflow

### 1. Define the type

`types/CodeReview.md`:
```markdown
# CodeReview

A structured code review produced by a reviewer agent.

## Required fields

- **verdict**: One of `approve`, `request-changes`, `comment`
- **summary**: One paragraph summarising the overall quality
- **issues**: List of issues, each with:
  - `severity`: `critical`, `major`, or `minor`
  - `file`: The file path
  - `line`: Line number (integer)
  - `description`: What is wrong and why

## Quality bar

- Every `critical` issue must include a suggested fix
- The `summary` must not simply restate the issue list
```

### 2. Register the project

```
wrap_project(path="/workspace/my-project")
```

### 3. Run an agent with output type guidance

```json
{
  "model": "sonnet",
  "system_prompt": "You are an expert code reviewer.",
  "output_type": "CodeReview",
  "tools": ["Read", "Glob", "Grep"]
}
```

### 4. Validate the result

```
validate(ref=agent_output, type_name="CodeReview")
```

```json
{
  "status": "VALID",
  "feedback": "All required fields present. Critical issue includes a suggested fix."
}
```

### 5. Filter a batch

```python
reviews = par(prompts=[...], sandbox={"output_type": "CodeReview", ...})
good_reviews = filter(refs=reviews, type_name="CodeReview")
```

---

## Tool Reference

| Tool | Signature | Description |
|---|---|---|
| `validate` | `(ref, type_name)` | Run checker agent on a single value |
| `filter` | `(refs, type_name)` | Parallel validate, return only VALID items |
| `get_type_definition` | `(name, resolve_refs?)` | Retrieve type markdown, optionally expanded |
| `list_type_registry` | `()` | List all registered types and their paths |
| `wrap_project` | `(path)` | Add project `types/` to registry search path |

---

## Related Pages

- [Pipelines](pipelines.md) — use `filter()` between pipeline steps
- [Sandboxes](sandboxes.md) — `input_type` and `output_type` sandbox fields
- [Combinators](combinators.md) — fan-out patterns that produce batches for `filter()`
