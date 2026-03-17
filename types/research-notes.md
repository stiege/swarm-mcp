Raw research output from an initial investigation phase.

A `research-notes` artifact is the direct output of a research agent. It captures what was found,
where it came from, and what still needs investigation. It is not interpreted or analysed — that
is the job of a downstream analysis step.

## Structure

- **topic**: The subject being researched (one line, no padding)
- **key_findings**: A list of 3–10 distinct findings. Each finding is a concrete, specific
  statement — not a vague observation. Each finding must cite at least one source.
- **open_questions**: A list of questions that came up during research but were not answered.
  May be empty if the topic is fully resolved, but must be present.
- **sources**: All references consulted — URLs, file paths, document names, or API endpoints.
  Must list every source that was actually used, not just the most important ones.

## Example entry

```
topic: WebAssembly runtime security boundaries in multi-tenant environments

key_findings:
  - WASM linear memory is isolated per instance, preventing direct cross-instance reads
    (source: https://webassembly.org/docs/security/)
  - Spectre-class attacks can still leak data via timing channels even within WASM sandboxes
    (source: https://arxiv.org/abs/1902.05178)
  - V8's WASM implementation adds a software guard page at offset 0 to catch null-deref
    (source: V8 blog post "WebAssembly trap handling", 2019)

open_questions:
  - Does Wasmtime's epoch-based interruption affect the timing-channel surface?
  - Are guard page mitigations consistent across runtimes (Wasmer, WAMR, WasmEdge)?

sources:
  - https://webassembly.org/docs/security/
  - https://arxiv.org/abs/1902.05178
  - https://v8.dev/blog/trap-handling
```

## Validity Criteria

- `topic` must be present and non-empty
- `key_findings` must contain at least 3 entries
- Every finding in `key_findings` must include a source citation in-line or reference a
  named entry in `sources`
- `open_questions` key must be present (empty list is acceptable)
- `sources` must not be empty — at minimum must list the sources cited in `key_findings`
- Findings must be specific and concrete, not generic observations (e.g. "security is
  important" is not a valid finding)
- No finding may be duplicated or trivially paraphrase another finding
