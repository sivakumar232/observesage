# 04 — How the Pipeline Works (Step by Step)

> This walks through one real case from the dataset:
> `re2ob_checkoutservice_cpu_1` — a CPU fault injected into checkoutservice.

---

## The Input Files

```
data/ground_truth/rcaeval/re2ob_checkoutservice_cpu_1/
├── logs.parquet        → 171,322 log lines  (timestamp, container_name, message)
├── metrics.parquet     → 1,441 rows × 73 cols (checkoutservice_cpu, cartservice_mem, ...)
├── traces.parquet      → 391,997 span rows  (traceID, spanID, parentSpanID, duration, ...)
└── inject_time.txt     → "1709123456.789"   (Unix timestamp when fault was injected)
```

---

## Step 0: Time-Window Slicing (RCAEvalAdapter)

```
inject_time = 1709123456 seconds

BASELINE window: [inject_time − 600s, inject_time]
  → 10 minutes before the fault = "normal" behavior

INCIDENT window: [inject_time, inject_time + 300s]
  → 5 minutes after fault injection = "broken" behavior

All three files are split into baseline and incident halves.
```

This gives us a comparison: **what did normal look like, vs what happened during the fault?**

---

## Step 1: Log Processing

```
Input: 171,322 raw log lines (incident + baseline)

Step 1a — Keyword Filter:
  Keep only lines containing: fatal, fail, error, kill, panic, ...
  → ~3,400 lines survive

Step 1b — Drain3 on Baseline:
  Feed 3 recent "healthy" runs' logs into Drain3
  Drain3 learns templates like:
    "Starting HTTP server on port <*>"
    "Connected to <*> pod <*>"
  These are NORMAL patterns.

Step 1c — Drain3 on Incident:
  Run each incident log line through Drain3.
  Lines matching a known healthy template → DISCARD (normal noise)
  Lines with NO matching template → NOVEL → KEEP

Step 1d — Context Expansion:
  For each kept line, grab:
    3 lines before it  (what triggered it?)
    7 lines after it   (what does the stack trace say?)

Output: LogEvidence per service
  checkoutservice: 12 snippets, 4 novel templates
  cartservice: 2 snippets, 0 novel templates
```

---

## Step 2: Metrics Processing

```
Input: 1,441 rows × 73 columns (wide format)
  Columns: checkoutservice_cpu, checkoutservice_mem, cartservice_cpu, ...

Step 2a — Reshape to Long Format:
  Each column = one (service, metric_type) pair
  checkoutservice_cpu → service="checkoutservice", type="cpu"

Step 2b — Baseline Statistics:
  For each (service, metric) during BASELINE window:
    compute mean (μ) and stddev (σ)
  Example:
    checkoutservice_cpu baseline: μ=12%, σ=2%

Step 2c — Z-Score Anomaly Detection:
  During INCIDENT window, for each data point:
    z = (value − μ) / σ
  If |z| > 3.0 → ALERT

Step 2d — Memory Slope → OOM Risk:
  Fit a line to memory timeseries during incident.
  If slope > 2.5 σ/min → "memory rising fast"
  Project: when does memory hit the cgroup limit?
  If TimeToOOM < 5 minutes → OOM alert (EC-1)

Output: MetricEvidence
  alerts: [
    {service: "checkoutservice", metric: "cpu", z_score: 8.2, severity: "CRITICAL"}
  ]
```

---

## Step 3: Trace Processing

```
Input: 391,997 span rows, filtered to incident window

Step 3a — Build Call Tree (DAG):
  Each span has: spanID, parentSpanID, serviceName, duration, statusCode
  Link children to parents using parentSpanID.
  
  Example tree for one trace:
    frontend (200ms, OK)
      └─ checkoutservice (195ms, OK)
           ├─ cartservice (3ms, OK)
           └─ paymentservice (190ms, OK)
                └─ emailservice (1ms, OK)

Step 3b — Self-Duration:
  checkoutservice self-duration = 195ms − (3ms + 190ms) = 2ms
  paymentservice self-duration  = 190ms − 1ms = 189ms ← slowest!

Step 3c — Latency Anomaly (vs Baseline):
  checkoutservice baseline p99 self-duration = 5ms
  checkoutservice incident self-duration = 180ms → 36× normal → BOTTLENECK

Step 3d — DFS Deepest Culprit:
  Walk the tree depth-first.
  Find the deepest node with an error or latency bottleneck.
  → checkoutservice is the culprit

Output: TraceEvidence
  culprit_service: "checkoutservice"
  error_spans_count: 0  (CPU fault, not an error code)
  latency_bottlenecks: 1
  call_hierarchy_summary: "frontend → checkoutservice [BOTTLENECK]"
```

---

## Step 4: Fusion

```
Score each service:

checkoutservice:
  Logs:    12 snippets × 1.0 + 4 novel × 2.5 = 22.0
  Metrics: CPU z=8.2 → 3.0 × 1.5 (CRITICAL) = 4.5
  Traces:  leaf culprit depth=1 → 5.0 + 2.0 = 7.0
  Subtotal: 33.5
  3 signals agree → × 1.75 = 58.6

cartservice:
  Logs: 2 snippets = 2.0
  No metric or trace evidence
  Subtotal: 2.0

Ranked candidates: ["checkoutservice", "cartservice", ...]

Failure Category:
  EC-1 OOM likelihood: 0.1 (no memory slope for CPU fault)
  EC-2 Latency likelihood: 0.1 + 4.0 (trace shows no errors, latency bottleneck) = 4.1 ← WINNER
  → Category: EC-2 (Flaky/Intermittent Latency)

Confidence (Bayesian):
  c_logs    = 0.75  (5 snippets → high)
  c_metrics = 0.85  (z=8.2/8.0 → 85%)
  c_traces  = 0.50  (depth=1, no error codes)
  confidence = 1 − (1−0.75)(1−0.85)(1−0.50) = 1 − 0.019 = 0.98 → capped at 0.95
```

---

## Step 5: LLM Prompt Assembly

```
Budget: 2,500 tokens total

[Incident Reference ID: re2ob_checkoutservice_cpu_1]

### Service Dependency Graph:
- frontend ➔ [checkoutservice]
- checkoutservice ➔ [cartservice, paymentservice, emailservice]

### Distributed Trace Analysis:
- Total Traces: 847, Latency Bottlenecks: 1
- Isolated Culprit Service: 'checkoutservice'

### Metric Anomalies:
- checkoutservice: cpu z=+8.2 (CRITICAL), baseline 12% → incident 54%

### Log Evidence (Drain3 Novel Templates):
[checkoutservice] NOVEL: "process throttled: cpu.cfs_quota <*> exceeded"
  context: ...3 lines before...
            ...7 lines after...

Analyze and return JSON RCA report.

→ Total: ~680 tokens (well under 2,500)
```

---

## Step 6: Gemini Response (RCA Report)

```json
{
  "root_cause_service": "checkoutservice",
  "failure_category": "EC-2: Flaky / Intermittent Latency",
  "confidence": 0.91,
  "summary": "checkoutservice is experiencing severe CPU throttling (54% vs 12% baseline). The cfs_quota CPU limit is being exceeded, causing all downstream requests to slow down.",
  "evidence_triangulation": {
    "logs": "Novel template 'cpu.cfs_quota exceeded' appeared 4 times — never seen in healthy runs.",
    "metrics": "CPU z-score of 8.2 — the highest anomaly across all services.",
    "traces": "checkoutservice self-duration 36× above baseline p99; all call paths bottleneck here."
  },
  "remediation_steps": [
    "Increase CPU limit for checkoutservice from 200m to 500m in Kubernetes resources.limits",
    "Add horizontal pod autoscaling (HPA) triggered at 70% CPU usage",
    "Profile the Go binary for unexpected hot loops in PlaceOrder handler"
  ]
}
```

---

## The Full Pipeline in One Diagram

```
logs.parquet  ──► Keyword Filter ──► Drain3 Diff ──► Context Expand ──► LogEvidence
metrics.parquet ► Reshape ──────► Z-Score ───────► Slope/OOM ───────► MetricEvidence
traces.parquet ─► DAG Build ────► Self-Duration ──► DFS Culprit ─────► TraceEvidence
                                                                              │
                                                                              ▼
                                                                       Fusion Engine
                                                                       (score + rank)
                                                                              │
                                                                              ▼
                                                                    Prompt Builder
                                                                    (< 2500 tokens)
                                                                              │
                                                                              ▼
                                                                    Gemini LLM API
                                                                              │
                                                                              ▼
                                                                       RCA Report JSON
```
