A structured analytical report derived from [research-notes].

An `analysis-report` interprets and evaluates the findings from a prior research phase.
Where [research-notes] captures raw observations, an `analysis-report` draws conclusions,
assesses risks, and produces actionable recommendations. It is the deliverable passed to
decision-makers or downstream pipeline steps.

## Structure

- **executive_summary**: 1–3 paragraphs written for a non-technical reader. Must convey the
  key conclusions without jargon. Does not simply list findings — it synthesises them into
  a coherent narrative.

- **findings**: Each significant finding from the research, re-stated with analytical
  interpretation. For each finding, include:
  - The finding itself (what was observed)
  - The evidence supporting it (specific sources or data points)
  - The implication (why it matters)

- **risks**: At least 2 identified risks, ordered by severity (highest first). For each risk:
  - A clear description of the risk
  - The conditions under which it is likely to materialise
  - At least one concrete, actionable mitigation (not "follow best practices")

- **recommendations**: A prioritised list of action items. Each recommendation must be
  specific enough to act on — who should do what, and why.

- **confidence**: The analyst's overall confidence in the conclusions. Must be exactly one
  of `high`, `medium`, or `low`, followed by a one-sentence justification.

## Example entry

```
executive_summary: |
  WebAssembly provides strong memory isolation between instances but remains
  vulnerable to timing-channel attacks that can leak data across the isolation
  boundary. Deploying WASM in multi-tenant environments requires additional
  mitigations beyond the default sandbox guarantees.

findings:
  - finding: WASM linear memory isolation prevents direct cross-instance reads
    evidence: WebAssembly spec §3.2.1; confirmed in V8 and Wasmtime implementations
    implication: Direct memory corruption attacks between tenants are not feasible

  - finding: Spectre-class timing channels remain exploitable within WASM sandboxes
    evidence: arXiv:1902.05178; reproduced in Chrome 73 before mitigations
    implication: Secrets processed in WASM should be assumed observable via timing

risks:
  - description: Tenant A leaks secrets from Tenant B via speculative execution
    conditions: Shared physical core, high-frequency timer available, secrets in WASM memory
    mitigation: Disable high-resolution timers; run tenants on separate physical cores

  - description: Runtime version divergence leaves some deployments unpatched
    conditions: Organisations running mixed Wasmtime/V8/Wasmer without version pinning
    mitigation: Pin runtime versions in CI; subscribe to security advisories for each runtime

recommendations:
  - Adopt Wasmtime with epoch-based interruption to reduce timing-channel surface
  - Audit all multi-tenant deployments for high-resolution timer availability
  - Pin runtime versions and establish a patch cadence of ≤30 days for CVEs

confidence: medium — research covered three major runtimes but did not include Wasmer 3.x
```

## Validity Criteria

- `executive_summary` must be present, non-empty, and at least 2 sentences long
- `executive_summary` must not simply restate the findings list verbatim
- `findings` must contain at least 2 entries
- Each finding must include an `evidence` field and an `implication` field
- `risks` must contain at least 2 entries
- Each risk must include at least one concrete mitigation — generic advice (e.g. "apply
  patches promptly") without specifics does not count
- `recommendations` must contain at least 1 item
- Each recommendation must be specific enough to act on without further clarification
- `confidence` must be exactly one of: `high`, `medium`, `low`
- `confidence` must include a justification (not just the rating alone)
