# GPU & Resource Pools

Many workloads need more than CPU and memory limits — they need **coordination**. A machine with one GPU should run at most one GPU-intensive agent at a time. An API with a rate limit should have at most five concurrent callers. A shared database should have at most ten active connections.

swarm-mcp implements this through **named semaphore pools**: lightweight counters that agents acquire before starting and release when they finish. The mechanism is the same for every resource type, from GPUs to database connections.

---

## Named Semaphore Pattern

Each resource pool has a name and a capacity (the number of agents that can hold the resource simultaneously). Internally, each pool is a `threading.Semaphore`.

When an agent requests a resource:

1. It joins the queue for that resource.
2. When a slot becomes available, it acquires the slot and starts running.
3. When the agent finishes (success or error), the slot is released — always, in a `finally` block.

This means resource slots are never leaked even if an agent crashes or times out.

### Global Concurrency Limit

Before acquiring any named resources, every agent must acquire a slot from the **global semaphore**:

| Environment Variable | Default | Description |
|---|---|---|
| `SWARM_MAX_CONCURRENT` | `10` | Maximum total agents running at any moment |
| `SWARM_QUEUE_TIMEOUT` | `3600` | Seconds an agent will wait in queue before giving up |

The global limit is a hard ceiling on system load regardless of how many named pools are configured.

---

## The `gpu` Shorthand

Setting `gpu: true` in a sandbox spec is syntactic sugar for adding `"gpu"` to the `resources` list:

```json
{"gpu": true}
// equivalent to:
{"gpu": false, "resources": ["gpu"]}
```

The `gpu` pool has a default capacity of **1**, meaning only one GPU-enabled agent runs at a time on a single-GPU machine. Increase the capacity if your machine has multiple GPUs (see [Configuring Pool Capacity](#configuring-pool-capacity) below).

### Example

```json
{
  "sandbox": {
    "model": "opus",
    "gpu": true,
    "memory": "16g",
    "timeout": 3600
  },
  "prompt": "Run the CUDA-accelerated image embedding pipeline"
}
```

---

## Configuring Pool Capacity

Set the capacity of any named pool with an environment variable:

```
SWARM_RESOURCE_<NAME>=<capacity>
```

The name is uppercased automatically. Examples:

| Variable | Effect |
|---|---|
| `SWARM_RESOURCE_GPU=2` | Allow 2 concurrent GPU agents (for a 2-GPU machine) |
| `SWARM_RESOURCE_DB_POOL=10` | Allow 10 concurrent agents using `db-pool` |
| `SWARM_RESOURCE_OPENAI_API=5` | Allow 5 concurrent agents using `openai-api` |
| `SWARM_MAX_CONCURRENT=20` | Raise the global ceiling to 20 |

Pool capacity is set on first access and cannot be changed at runtime without restarting the server.

!!! note "Default capacities"
    If `SWARM_RESOURCE_GPU` is not set, the `gpu` pool defaults to capacity `1`. All other pools default to the value of `SWARM_MAX_CONCURRENT` unless explicitly configured — effectively unconstrained within the global limit.

---

## Queue Semantics

```
SWARM_QUEUE_TIMEOUT=3600  (default: 1 hour)
```

An agent that cannot acquire its resources within `SWARM_QUEUE_TIMEOUT` seconds returns an error instead of running. This prevents unbounded queue growth under sustained overload.

### Queue Timeout vs Execution Timeout

These are two separate timers with different scopes:

| Timer | Configured by | Starts when | Fires when |
|---|---|---|---|
| Queue timeout | `SWARM_QUEUE_TIMEOUT` | Agent is submitted | Agent has waited too long for a resource slot |
| Execution timeout | `timeout` in SandboxSpec | Agent starts running | Agent's container has been running too long |

An agent that times out waiting in the queue never starts and never consumes resources. An agent that times out during execution has its container stopped and the resource slot released.

---

## Multi-Resource Locking

An agent can request multiple named resources simultaneously via the `resources` array:

```json
{
  "sandbox": {
    "gpu": true,
    "resources": ["db-pool", "openai-api"],
    "timeout": 1800
  },
  "prompt": "Embed documents from the database using the OpenAI API on GPU"
}
```

The acquisition order is:

1. Global semaphore slot
2. Named resources, **in the order listed** in `resources` (with `gpu` appended last if `gpu: true`)

If any resource acquisition fails — because the queue timeout expires while waiting — all previously acquired resources for that agent are **released immediately** before returning the error. There are no partial holds that could cause deadlock.

!!! warning "Consistent ordering prevents deadlock"
    If agent A acquires `["db-pool", "openai-api"]` and agent B acquires `["openai-api", "db-pool"]` in opposite order, they can deadlock. Always list resources in the same order across all your sandbox specs, or use a single composite resource name instead.

---

## Common Patterns

### GPU Serialisation

One GPU shared across all GPU-intensive pipelines. Each pipeline step that needs GPU sets `gpu: true`; all others skip the resource entirely.

```bash
# No env var needed — gpu pool defaults to capacity 1
```

```json
{
  "name": "ml-training-pipeline",
  "steps": [
    {
      "id": "preprocess",
      "prompt": "Preprocess the dataset (CPU only)",
      "gpu": false
    },
    {
      "id": "train",
      "prompt": "Train the model",
      "gpu": true,
      "memory": "16g",
      "timeout": 7200
    },
    {
      "id": "evaluate",
      "prompt": "Evaluate the trained model",
      "gpu": true,
      "memory": "8g"
    }
  ]
}
```

Only one of `train` and `evaluate` will execute at a time if multiple pipelines are running concurrently.

### Database Connection Pool

Cap concurrent database access at 10 connections to match the server's pool size:

```bash
export SWARM_RESOURCE_DB_POOL=10
```

```json
{
  "sandbox": {
    "resources": ["db-pool"],
    "env_vars": {"DB_URL": "postgres://..."}
  }
}
```

Any `run`, `par`, or `map` call that includes `"db-pool"` in `resources` will queue rather than overwhelm the database.

### API Rate Limiting

Limit parallel calls to an external API to 3 concurrent requests:

```bash
export SWARM_RESOURCE_SEARCH_API=3
```

```json
{
  "sandbox": {"resources": ["search-api"]},
  "prompts": [
    "Search for: topic A",
    "Search for: topic B",
    "Search for: topic C",
    "Search for: topic D",
    "Search for: topic E"
  ]
}
```

With `par`, all five prompts are submitted simultaneously but at most three will run at any moment. The other two queue and start as slots free up.

### Staged Pipeline with Mixed Resources

A pipeline where some steps are CPU-only, one step uses GPU, and a final step queries the database:

```json
{
  "name": "ingest-and-embed",
  "sandbox": {"model": "sonnet", "tools": ["Read", "Write", "Bash"]},
  "steps": [
    {
      "id": "fetch",
      "prompt": "Download and clean the raw documents to /shared/docs/",
      "network": true
    },
    {
      "id": "embed",
      "prompt": "Generate embeddings for all files in /shared/docs/ and write to /shared/embeddings.npy",
      "gpu": true,
      "memory": "12g",
      "network": false
    },
    {
      "id": "store",
      "prompt": "Load /shared/embeddings.npy and insert into the vector database",
      "resources": ["db-pool"],
      "network": true
    }
  ]
}
```

The `embed` step holds the GPU slot only for the duration of that step. `fetch` and `store` do not touch the GPU pool at all.

---

## Observability

When an agent is queued waiting for a resource, the server logs which resource it is waiting on and for how long. Use these log lines to tune pool capacities — if `db-pool` shows agents regularly waiting more than a few seconds, raise `SWARM_RESOURCE_DB_POOL`.

See the [Observability](../observability.md) page for structured log formats.

---

## Related Pages

- [Sandboxes](sandboxes.md) — the `gpu`, `resources`, `memory`, `cpus`, and `timeout` fields
- [Pipelines](pipelines.md) — per-step resource specifications
- [Combinators](combinators.md) — `par` and `map` fan-out with resource-bounded concurrency
