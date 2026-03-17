# GPU Pipeline

Run a GPU-accelerated ML training and evaluation pipeline using `pipeline()`.
The training step acquires the GPU resource, trains a model, and writes
checkpoints to a shared directory. The evaluation step reads those checkpoints
and reports metrics.

---

## Overview

```
pipeline steps
─────────────
  train   →  gpu=true, resources=["gpu"], mounts=[dataset]
              writes /shared/checkpoints/
                │
  evaluate →   reads /shared/checkpoints/
                writes /shared/metrics.json
                │
  report   →   reads /shared/metrics.json
                produces final summary ref
```

The `"gpu"` resource pool is a semaphore with capacity configurable via
`SWARM_RESOURCE_gpu`. Only one training step can hold the GPU at a time; any
other GPU jobs queue until it finishes.

---

## Prerequisites

### Enable GPU support in Docker

Your Docker daemon must be configured with the NVIDIA Container Toolkit. Verify:

```bash
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

### Configure the resource pool

```bash
export SWARM_RESOURCE_gpu=1   # one agent may use the GPU at a time (default)
```

If you have multiple GPUs and want to allow more concurrent GPU agents, set this
to the number of available GPUs.

---

## Full Pipeline Definition

```python
pipeline(
    definition='{
      "name": "ml-training-pipeline",
      "budget": 5.00,
      "deadline_seconds": 7200,
      "steps": [
        {
          "id": "train",
          "prompt": "Train a sentiment classification model using the dataset at /data/train.csv. Use PyTorch with a pre-trained DistilBERT backbone. Fine-tune for 3 epochs. Save the final checkpoint to /shared/checkpoints/model_final.pt and save training metrics (loss per epoch, final accuracy on /data/val.csv) to /shared/training_metrics.json.",
          "model": "sonnet",
          "tools": "Read,Write,Bash",
          "gpu": true,
          "resources": ["gpu"],
          "mounts": [
            {"host_path": "/data/sentiment-dataset", "container_path": "/data", "readonly": true}
          ],
          "memory": "16g",
          "timeout": 3600,
          "on_fail": "train-retry"
        },
        {
          "id": "train-retry",
          "prompt": "The training step failed. Read the error output from /shared/training_metrics.json if it exists, or diagnose from context. Retry training with a smaller batch size (use --batch-size 8) and reduced learning rate. Save checkpoint to /shared/checkpoints/model_final.pt.",
          "model": "sonnet",
          "tools": "Read,Write,Bash",
          "gpu": true,
          "resources": ["gpu"],
          "mounts": [
            {"host_path": "/data/sentiment-dataset", "container_path": "/data", "readonly": true}
          ],
          "memory": "16g",
          "timeout": 3600,
          "condition": "prev.error",
          "next": "evaluate",
          "max_retries": 1
        },
        {
          "id": "evaluate",
          "prompt": "Evaluate the trained model checkpoint at /shared/checkpoints/model_final.pt on the test set at /data/test.csv. Report: accuracy, precision, recall, F1 per class, and a confusion matrix. Write all results to /shared/metrics.json. Also write a human-readable summary to /shared/eval_report.md.",
          "model": "sonnet",
          "tools": "Read,Write,Bash",
          "gpu": true,
          "resources": ["gpu"],
          "mounts": [
            {"host_path": "/data/sentiment-dataset", "container_path": "/data", "readonly": true}
          ],
          "memory": "8g",
          "timeout": 600,
          "on_fail": "report"
        },
        {
          "id": "report",
          "prompt": "Read /shared/metrics.json and /shared/training_metrics.json (if available) and /shared/eval_report.md. Produce a final pipeline report covering: training summary, evaluation results, model quality assessment, and recommendations for improvement. If evaluation failed, explain what went wrong based on available logs.",
          "model": "sonnet",
          "tools": "Read,Write",
          "timeout": 300
        }
      ]
    }'
)
```

---

## Key Parameters Explained

### `gpu: true`

Passes `--gpus all` to the Docker `run` command. Without this flag the
container cannot access the host GPU.

```json
"gpu": true
```

### `resources: ["gpu"]`

Acquires the `"gpu"` named resource pool before the container starts. This is
the serialization mechanism — when two pipeline steps or independent `run()`
calls both declare `resources: ["gpu"]`, they queue instead of competing for
the device.

```json
"resources": ["gpu"]
```

!!! info "Why both gpu and resources?"
    `gpu: true` is the Docker flag. `resources: ["gpu"]` is the swarm-mcp
    semaphore. You can set `gpu: true` without `resources: ["gpu"]` (no
    serialization) or set `resources: ["gpu"]` without `gpu: true` (serialization
    for a logical resource that isn't the GPU device itself). In a real GPU
    pipeline, set both.

### `memory: "16g"`

Docker memory limit. Training large models requires more RAM than the default
container allocation. Match this to your GPU VRAM and system RAM.

---

## Environment Configuration

```bash
# One GPU, serialize all GPU work
export SWARM_RESOURCE_gpu=1

# Allow 2 concurrent GPU jobs (if you have 2 GPUs)
export SWARM_RESOURCE_gpu=2

# Increase queue timeout — GPU jobs may wait a long time
export SWARM_QUEUE_TIMEOUT=7200
```

Set these before starting the swarm-mcp server.

!!! warning "Queue timeout vs execution timeout"
    `SWARM_QUEUE_TIMEOUT` is how long an agent will wait *in the queue* for a
    resource to become available. `timeout` in the step spec is the execution
    time once the resource is acquired. A job waiting 30 minutes for the GPU
    still gets its full `timeout` once the GPU is free.

---

## The `/shared/` Directory

Every step in a pipeline shares `/shared/` read-write. This is the file-passing
channel between steps:

| Step | Writes | Reads |
|---|---|---|
| `train` | `/shared/checkpoints/model_final.pt`, `/shared/training_metrics.json` | `/data/train.csv`, `/data/val.csv` |
| `evaluate` | `/shared/metrics.json`, `/shared/eval_report.md` | `/shared/checkpoints/model_final.pt`, `/data/test.csv` |
| `report` | (final output) | `/shared/metrics.json`, `/shared/training_metrics.json`, `/shared/eval_report.md` |

Steps do not pass text through MCP protocol. They write files and the next
step reads them. This handles large artifacts (model checkpoints, datasets)
without hitting protocol size limits.

---

## Reading the Results

The pipeline returns a ref for the last completed step:

```python
# Get the final report
unwrap(ref="pipeline-run01/report")
Read("/tmp/swarm-mcp/pipeline-run01/report/output.md")
```

To read intermediate results:

```python
# Read training metrics directly
unwrap(ref="pipeline-run01/train")
Read("/tmp/swarm-mcp/pipeline-run01/train/output.md")
```

---

## Resuming a Pipeline

If training completes but evaluation fails, resume from the evaluation step
without re-running training:

```python
pipeline(
    definition='{ "name": "ml-training-pipeline", "steps": [...] }',
    resume="pipeline-run01/evaluate"
)
```

The shared directory from `pipeline-run01` is reused, so the checkpoint written
by the `train` step is still available at `/shared/checkpoints/model_final.pt`.

See [Pipelines](../concepts/pipelines.md#resume-support) for full resume semantics.

---

## Multiple Independent GPU Jobs

To run multiple independent GPU training jobs that serialize on the GPU
resource, use `par()` with `resources`:

```python
par(
    tasks='[
        {
            "prompt": "Train model A on /data/dataset-a.csv. Save to /output/model-a.pt.",
            "gpu": true,
            "resources": ["gpu"],
            "mounts": [{"host_path": "/data", "container_path": "/data", "readonly": true},
                       {"host_path": "/output", "container_path": "/output", "readonly": false}],
            "memory": "16g",
            "timeout": 3600
        },
        {
            "prompt": "Train model B on /data/dataset-b.csv. Save to /output/model-b.pt.",
            "gpu": true,
            "resources": ["gpu"],
            "mounts": [{"host_path": "/data", "container_path": "/data", "readonly": true},
                       {"host_path": "/output", "container_path": "/output", "readonly": false}],
            "memory": "16g",
            "timeout": 3600
        }
    ]'
)
```

With `SWARM_RESOURCE_gpu=1`, the two jobs run sequentially on the GPU even
though `par()` would normally run them simultaneously. The second job queues
until the first releases the `"gpu"` resource.

---

!!! note "See also"
    - [Resources & Sandboxes](../concepts/resources.md) — resource pool configuration
    - [Pipelines](../concepts/pipelines.md) — full pipeline step reference
    - [Observability](../observability.md) — debugging failed training steps
