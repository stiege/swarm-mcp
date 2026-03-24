# Governors

A governor is an LLM-powered control-flow hook evaluated at pipeline trigger points. Instead of hardcoding `on_fail: "fix-step"`, you write a natural language policy and a Claude model reads the live pipeline state to decide what happens next.

This separates *what the pipeline does* (the steps) from *how it should respond to events* (the governors). The same pipeline can be governed by different governors for different deployment contexts.

---

## Continuation Algebra

Every governor evaluation returns exactly one of five actions:

| Action | Effect |
|--------|--------|
| `next` | Proceed to the next step normally |
| `jump` | Jump to a named step (`target` field required) |
| `halt` | Stop the pipeline cleanly (`status: "done"`) |
| `broken` | Stop and mark the pipeline broken (`status: "broken"`, `broken_reason` set) |
| `patch_pipeline` | Deep-merge patch the pipeline definition, then continue |

The `context` dict is free-form. It accumulates across the entire pipeline run and is written to `/shared/governor-context.json` after each evaluation so steps can read it.

---

## Governor Spec

A governor spec has three fields:

```json
{
  "description": "Governs QLoRA train step failures",
  "model": "claude-haiku-4-5-20251001",
  "spec": "You govern the train step of a QLoRA fine-tuning pipeline. OOM errors → broken. NaN/loss divergence → broken. Transient errors (disk, timeout) → jump to the step before train to retry data preparation. Inspect exit_code and error output."
}
```

| Field | Description |
|---|---|
| `description` | Human-readable summary (used in listings) |
| `model` | Claude model to use for evaluation. Haiku is sufficient for most policy decisions. |
| `spec` | Natural language policy. The model receives the full pipeline state — step definition, prior results, error output — and must return a continuation JSON. |

---

## Inline Pipeline Governors

Define governors directly in the pipeline JSON under the top-level `governors` key. They are version-controlled alongside the pipeline and take priority over global governors in `~/.claude/governors/`.

```json
{
  "name": "train-loop",
  "governors": {
    "TrainingFailure": {
      "description": "Governs QLoRA train step failures",
      "model": "claude-haiku-4-5-20251001",
      "spec": "You govern the train step of a QLoRA fine-tuning pipeline. OOM errors → broken. NaN/loss divergence → broken. Transient errors (disk, timeout) → jump to the step before train to retry data preparation. Inspect exit_code and error output."
    },
    "QualityGate": {
      "description": "Decides whether the current model iteration is good enough",
      "model": "claude-haiku-4-5-20251001",
      "spec": "You govern the evaluation step. If the pass rate is 5/5, return halt (we're done). If 3-4/5, jump to the training step for another iteration. If 0-2/5, return broken — the model is not converging."
    }
  },
  "steps": [
    {"id": "train",    "prompt": "...", "on_fail":    {"governor": "TrainingFailure"}},
    {"id": "evaluate", "prompt": "...", "on_success": {"governor": "QualityGate"}}
  ]
}
```

---

## `on_fail` and `on_success`

| Field | When evaluated |
|---|---|
| `on_fail: {"governor": "Name"}` | When the step exits with an error or non-zero exit code |
| `on_success: {"governor": "Name"}` | When the step completes without error |

Plain step IDs still work for `on_fail` — `on_fail: "fix-step"` is unchanged. Use a governor when the decision is non-trivial (e.g., "OOM vs transient failure") or when you want the policy in human-readable form rather than hardcoded routing.

---

## `patch_pipeline` Action

A governor can surgically modify the running pipeline definition using [JSON Merge Patch (RFC 7396)](https://datatracker.ietf.org/doc/html/rfc7396): null values delete keys, objects recurse, scalars and arrays replace.

```json
{
  "action": "patch_pipeline",
  "pipeline_patch": {
    "steps": [
      {"id": "train", "prompt": "Run training with --batch-size 4 instead of 8"}
    ]
  },
  "context": {"adjusted_batch_size": true},
  "reason": "Detected instability in loss curve — reducing batch size"
}
```

The interpreter applies the patch to the in-memory pipeline definition and continues from the next step. Later steps (and future governor evaluations) see the patched definition.

---

## governor_context

The `context` dict from each continuation is merged into a running `governor_context` that persists for the lifetime of the pipeline. After each evaluation the accumulated context is written to `/shared/governor-context.json`.

Steps can read this file to act on structured observations from previous governor evaluations — iteration count, last loss value, retry history, or any other signal the governor chose to record.

---

## Global Registry

Governors not defined inline fall back to the global registry in `~/.claude/governors/`. Use the MCP tools to manage it:

| Tool | Description |
|---|---|
| `save_governor_spec(name, spec, model?, description?)` | Save a governor to the global registry |
| `list_governor_specs()` | List all globally registered governors |

Inline definitions always take priority over global ones with the same name.

---

## Related Pages

- [Pipelines](pipelines.md) — the pipeline interpreter that evaluates governors
