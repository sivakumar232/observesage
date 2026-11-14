# ObservaSage — End-to-End System Design, Internals, and Correctness

> **Document type:** technical deep-dive + verification protocol
> **Generated:** 2026-11-14
> **Code state verified:** `HEAD = be5a107` plus uncommitted changes in
> `scripts/rcaeval_adapter.py`, `src/api/static/index.html`, `src/fusion/engine.py`,
> `src/llm/prompt.py`, `src/log_processor/processor.py`
> **Dataset:** 239 complete tri-modal RCAEval cases discovered; 90 `RE2-OB` cases evaluated
> **Verifier:** `scripts/verify_pipeline.py` (added with this document)

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [What the system is, and what it actually does](#2-what-the-system-is-and-what-it-actually-does)
3. [Architecture at a glance](#3-architecture-at-a-glance)
4. [The data model](#4-the-data-model)
5. [Path A — RCAEval replay ingestion](#5-path-a--rcaeval-replay-ingestion)
6. [Path B — Live Docker/Prometheus/Jaeger ingestion](#6-path-b--live-dockerprometheusjaeger-ingestion)
7. [Log retrieval and LogSage preprocessing](#7-log-retrieval-and-logsage-preprocessing)
8. [Metric retrieval and anomaly detection](#8-metric-retrieval-and-anomaly-detection)
9. [Trace retrieval, DAG reconstruction, and culprit extraction](#9-trace-retrieval-dag-reconstruction-and-culprit-extraction)
10. [Multi-signal fusion](#10-multi-signal-fusion)
11. [Prompt assembly and token budgeting](#11-prompt-assembly-and-token-budgeting)
12. [LLM diagnosis and local fallback](#12-llm-diagnosis-and-local-fallback)
13. [Evaluation mathematics](#13-evaluation-mathematics)
14. [Persistence, caching, API, and dashboard](#14-persistence-caching-api-and-dashboard)
15. [How we know it is correct — a seven-layer verification framework](#15-how-we-know-it-is-correct--a-seven-layer-verification-framework)
16. [What is currently verified, and what is not](#16-what-is-currently-verified-and-what-is-not)
17. [Falsification tests you can run](#17-falsification-tests-you-can-run)
18. [Known defects and risks](#18-known-defects-and-risks)
19. [Reproduction runbook](#19-reproduction-runbook)
20. [Glossary](#20-glossary)
21. [Appendix A — Artifact schema](#21-appendix-a--artifact-schema)
22. [Appendix B — File map](#22-appendix-b--file-map)

---

## 1. Executive summary

ObservaSage is a four-stage Root Cause Analysis (RCA) pipeline:

```
telemetry  →  localization  →  fusion + prompt  →  diagnosis  →  evaluation
(ingest)      (per signal)      (correlate)         (LLM/local)    (Top@k/MRR)
```

The important distinction is between two different claims:

| Claim | Status |
|---|---|
| The ingestion and evidence-extraction code is deterministic, typed, and internally consistent | **Largely verified** |
| The reported 90-case accuracy numbers were produced by the code currently in the working tree | **Not verified — cached artifacts predate the current code** |
| The system reasons from telemetry rather than from encoded ground truth | **Not verified for the cached artifacts** (359/362 prompts contain the case ID, which encodes service + fault) |
| The confidence score is calibrated | **No** — Brier 0.2655 vs a constant baseline of 0.2054 |
| The failure category is meaningful | **No** — 78/90 predictions are EC-2 |

The pipeline is best understood as **ranked service localization from three independent anomaly detectors**, followed by a deterministic fusion heuristic. The LLM is optional and, in every persisted result, disabled.

**Bottom line:** the data plane (how logs, metrics, and traces are parsed, localized, and turned into a prompt) is inspectable and mostly correct. The evaluation plane (whether the reported numbers mean what the README says) is not yet end-to-end reproducible, because artifacts are version-less and were generated before the prompt-leakage and trace-sampling fixes.

---

## 2. What the system is, and what it actually does

### 2.1 Research framing

ObservaSage extends **LogSage** (arXiv:2506.03691). LogSage diagnoses CI/CD failures from logs only. ObservaSage adds two more signals:

- **Metrics** from Prometheus / cAdvisor, used for resource exhaustion, latency, and slope detection.
- **Traces** from Jaeger / OpenTelemetry, used for cascading-failure localization via call-graph topology.

The five edge cases (`EC-1` … `EC-5`) are the taxonomy used to justify the extra signals:

| Code | Meaning | Primary signal |
|---|---|---|
| EC-1 | OOM / resource exhaustion | memory slope + cgroup limit |
| EC-2 | Flaky / intermittent latency | latency Z-score + trace bottleneck |
| EC-3 | Cascading service crash | trace leaf culprit |
| EC-4 | Silent data corruption | absence of errors + wrong-output logs |
| EC-5 | Infrastructure drift / network partition | connection/DNS log patterns |

### 2.2 Two execution paths

The repository contains **two different ingestion paths** that share the same processing code:

| Path | Entry point | Input | Used for |
|---|---|---|---|
| **A. RCAEval replay** | `scripts/rcaeval_adapter.py`, `src/pipeline/orchestrator.py` | local `*.parquet` benchmark folders | all published results |
| **B. Live stack** | `scripts/collect_telemetry.py`, `scripts/run_pipeline.py` | Docker API, Prometheus HTTP, Jaeger HTTP | demos and fault injection |

This distinction matters for correctness. Path A is deterministic and replayable. Path B depends on container names, scrape timing, and Jaeger sampling, and has never been used to produce the reported evaluation.

### 2.3 The four stages

| Stage | Code | Output |
|---|---|---|
| 1. Ingest | `RCAEvalAdapter.convert_case` / `collect_telemetry.py` | `TelemetrySnapshot` |
| 2. Localize | `LogSageProcessor`, `MetricsProcessor`, `TraceProcessor` | `LogEvidence`, `MetricEvidence`, `TraceEvidence` |
| 3. Fuse | `FusionEngine` + `build_multimodal_prompt_with_breakdown` | ranked candidates, category, confidence, prompt |
| 4. Diagnose | `GeminiRCAClient.diagnose` or local fallback | `RCAReport` |
| 5. Score | `score_single_case` + `aggregate_scores` | `CaseScore`, CSV, artifact |

---

## 3. Architecture at a glance

```
                    ┌──────────────────────────────┐
                    │  RCAEval case folder          │
                    │  logs.parquet                 │
                    │  metrics.parquet              │
                    │  traces.parquet               │
                    │  inject_time.txt              │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │  RCAEvalAdapter               │
                    │  · column detection           │
                    │  · timestamp normalization    │
                    │  · baseline/incident windows  │
                    │  · whole-trace sampling       │
                    │  · 5-second buckets           │
                    └──────────────┬───────────────┘
                                   │ TelemetrySnapshot
             ┌─────────────────────┼─────────────────────┐
             ▼                     ▼                     ▼
    ┌────────────────┐   ┌──────────────────┐   ┌──────────────────┐
    │ LogSage        │   │ MetricsProcessor │   │ TraceProcessor   │
    │ Processor      │   │                  │   │                  │
    │ Drain3 diff    │   │ z / robust z     │   │ DAG + DFS leaf   │
    │ keywords       │   │ adaptive tau     │   │ self-duration    │
    │ m=3 / n=7      │   │ slope / OOM      │   │ p99 bottleneck   │
    └───────┬────────┘   └────────┬─────────┘   └────────┬─────────┘
            │ LogEvidence         │ MetricEvidence        │ TraceEvidence
            └─────────────────────┼───────────────────────┘
                                  ▼
                    ┌──────────────────────────────┐
                    │  FusionEngine                 │
                    │  weighted score + topology    │
                    │  consensus ×1.35 / ×1.75      │
                    │  category likelihoods         │
                    │  confidence calibration       │
                    └──────────────┬───────────────┘
                                   │ ranked candidates, category, confidence
                                   ▼
                    ┌──────────────────────────────┐
                    │  Prompt assembler             │
                    │  < 2500 BPE tokens            │
                    │  anonymized incident id       │
                    └──────────────┬───────────────┘
                                   │
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
          ┌────────────────────┐      ┌────────────────────┐
          │ GeminiRCAClient    │      │ Local fallback      │
          │ structured JSON    │      │ RCAReport from      │
          │ schema validation  │      │ fusion output       │
          └─────────┬──────────┘      └─────────┬──────────┘
                    └─────────────┬─────────────┘
                                  ▼
                    ┌──────────────────────────────┐
                    │  RCAReport + CaseScore        │
                    │  Top@1 / Top@3 / MRR          │
                    │  artifact JSON + CSV          │
                    └──────────────┬───────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │  FastAPI + dashboard          │
                    │  /api/cases /api/compare      │
                    │  /ws/diagnose                 │
                    └──────────────────────────────┘
```

---

## 4. The data model

Defined in `src/schemas/telemetry.py` and `src/schemas/evidence.py`.

### 4.1 `TelemetrySnapshot`

The single source of truth for one incident:

| Field | Type | Meaning |
|---|---|---|
| `run_id` | `str` | Case identifier (for replay, the folder name) |
| `metadata` | `PipelineRunMeta` | Scenario, status, start/end, synthetic steps |
| `captured_at` | `datetime` | Timestamp of capture |
| `telemetry` | `TelemetryData` | Incident logs/metrics/traces |
| `baseline` | `TelemetryData` | Pre-incident baseline |
| `inject_time` | `float` | Unix seconds of fault injection |
| `time_buckets` | `list[TimeBucketSummary]` | 5-second cross-signal histogram |
| `ground_truth_service` | `str` | Label copied from the registry |
| `ground_truth_fault_type` | `str` | Label copied from the registry |

`TelemetrySnapshot` also exposes convenience properties `logs`, `metrics`, `traces`, `baseline_logs`, `baseline_metrics`, `baseline_traces` (`telemetry.py:106-128`).

**Invariant 1:** `inject_time` is the pivot. Everything before it is baseline; everything from it onward is incident.

### 4.2 `MetricSeries`

```python
class MetricSeries(BaseModel):
    metric_name: str
    query: str
    data: list[dict[str, Any]]   # [{"timestamp": float, "value": float}, ...]
    error: str | None = None
```

A metric is **not** a matrix. It is a flat list of timestamped scalar points. Each service/metric pair is its own series.

### 4.3 `Trace` and `TraceSpan`

```python
class TraceSpan(BaseModel):
    traceID: str
    spanID: str
    operationName: str
    references: list[dict]   # [{"refType": "CHILD_OF", "spanID": parent}]
    startTime: int           # microseconds
    duration: int            # microseconds
    tags: list[dict]         # [{"key": "service.name", "value": "..."}]
    logs: list[dict]
```

A `Trace` is a list of spans sharing a `traceID`. The adapter stores microseconds in `startTime`/`duration`; all downstream code divides by `1e3` to get milliseconds.

### 4.4 `LogEvidence`

| Field | Meaning |
|---|---|
| `service` | Service that emitted the lines |
| `total_raw_lines` | Number of lines considered |
| `novel_templates_count` | Number of **distinct** Drain3 templates not in baseline |
| `snippets` | Selected error/novel lines with asymmetric context |
| `formatted_prompt` | Rendered block injected into the LLM prompt |
| `estimated_tokens` | `len(text) // 4` |

### 4.5 `MetricEvidence`

| Field | Meaning |
|---|---|
| `total_metrics_evaluated` | Number of incident series |
| `alerts` | At most 10 `MetricAlert` objects, sorted by severity |
| `has_oom_alert` | True if any alert has `is_oom_risk` |
| `has_latency_anomaly` | True if any latency metric exceeded its threshold |
| `formatted_prompt` | Rendered alert list |
| `estimated_tokens` | `len(text) // 4` |

### 4.6 `TraceEvidence`

| Field | Meaning |
|---|---|
| `total_traces`, `total_spans` | Counts after sampling |
| `error_spans_count` | Spans flagged by status/error tags |
| `latency_bottlenecks_count` | Spans exceeding baseline p99 × 2 |
| `root_service` | First root span's service |
| `culprit_service` | Service with the most leaf votes |
| `culprit_span` | Deepest anomalous span |
| `service_dependency_graph` | `caller -> [callees]` adjacency |
| `call_hierarchy_summary` | ASCII tree for the prompt |
| `formatted_prompt` | Rendered trace section |

---

## 5. Path A — RCAEval replay ingestion

File: `scripts/rcaeval_adapter.py`.

### 5.1 Case discovery

`ObservaSagePipeline.discover_cases` (`orchestrator.py:93-148`):

1. Reads `data/ground_truth/rcaeval/cases.parquet`.
2. Keeps rows where `has_logs` **and** `has_traces` are true.
3. Applies optional suite and fault filters.
4. Requires the folder to contain all four files:

```
logs.parquet
metrics.parquet
traces.parquet
inject_time.txt
```

Current count (verified):

```
239 complete cases
RE2-OB 90 | RE2-TT 89 | RE3-OB 30 | RE3-TT 30
```

Only 90 of the 239 (the `RE2-OB` suite) have been evaluated.

### 5.2 Injection time and windows

`parse_timestamp_value` (`rcaeval_adapter.py:42-91`) normalizes:

| Input | Divisor | Example |
|---|---|---|
| nanoseconds (`>1e16`) | `1e9` | `1705354566000000000` |
| microseconds (`>1e13`) | `1e6` | `1705354566000000` |
| milliseconds (`>1e10`) | `1e3` | `1705354566000` |
| seconds | `1` | `1705354566` |
| ISO-8601 | parsed | `2024-01-15T20:16:06Z` |

The default windows (constructor, `rcaeval_adapter.py:108-118`) are:

```
baseline_start = inject_time − 600 s
incident_end   = inject_time + 300 s
bucket_size    = 5 s
```

**Invariant 2 (temporal integrity):**

```
max(timestamp of baseline signal) < inject_time <= min(timestamp of incident signal)
```

This is checked by the verifier and passes for logs, metrics, and traces in the sample case.

**Caveat:** a trace whose spans straddle `inject_time` is assigned to baseline or incident based on the **minimum span start time**. A trace that starts before injection and ends after it is entirely baseline. This is a deliberate simplification, but it can hide the first failing span of a cascade.

### 5.3 Log ingestion

`parse_logs` (`rcaeval_adapter.py:154-250`):

1. Prefers `logs.parquet`; falls back to `logs.csv`.
2. Detects columns case-insensitively:

```
time:    timestamp | time | datetime | ts
service: container_name | service | service_name | pod | app
message: message | content | log | text | msg | body
```

3. Converts each row into:

```
[2024-01-15T20:16:06+00:00] <original message>
```

4. Splits rows by `ts < inject_time` into baseline vs incident, grouped by service.
5. Also returns `(timestamp, service)` pairs for bucket quantization.

**Consequence:** the log processor sees ISO timestamps in every line. Drain3 masks timestamps (`<TIMESTAMP>`) because of `config/drain3.ini`, so this is safe for template mining.

**Missing-column behavior:** if `time` or `message` is absent, the function returns empty dicts. There is no error and no diagnostic. This is a silent-failure risk.

### 5.4 Metric ingestion

`parse_metrics` (`rcaeval_adapter.py:252-342`):

1. Prefers `metrics.parquet` (wide table: one `time` column, one column per metric).
2. Falls back to `metrics.json`, supporting three shapes:

```json
{"service": {"metric": [[ts, value], ...]}}
{"metric": [[ts, value], ...]}
[{"metric": "name", "values": [[ts, value], ...]}]
```

3. Flattens everything into `{metric_name: [[ts, value], ...]}`.
4. For each series, splits points by the same baseline/incident windows.
5. Emits `MetricSeries(metric_name, query=f"rcaeval:{metric_name}", data=[...])`.

**Service extraction is not done here.** The adapter keeps the full metric name (e.g. `checkoutservice_cpu`). Service attribution happens later in `extract_service_from_metric`.

**Service-name heuristics** (`metrics_processor/processor.py:15-47`), in order:

1. `service/metric` → `service`.
2. PromQL label selector `name="..."`, `app="..."`, `service="..."`, `pod="..."`, `container="..."`.
3. Prefix before the first underscore, unless it is a generic prefix:
   `container, http, grpc, node, process, system, jvm, go`.
4. Fallback `"system"`.

For RCAEval, `checkoutservice_cpu` becomes `checkoutservice`; `redis_diskio` becomes `redis`.

### 5.5 Trace ingestion

`parse_traces` (`rcaeval_adapter.py:344-573`) is the most delicate part.

**Step 1 — timestamp unit normalization** (same `1e16 / 1e13 / 1e10` cutoffs).

**Step 2 — whole-trace sampling.** If the filtered table has more than 12,000 spans:

```python
incident_trace_ids = unique trace ids with start >= inject_time
baseline_trace_ids = unique trace ids with start <  inject_time
sampled_incident   = 500 random incident traces  (seed 42)
sampled_baseline   = 300 random baseline traces  (seed 42)
keep all spans whose trace_id is in either set
```

This replaced the older span-level sampling that produced ~95% orphaned spans. Verified on `re2ob_checkoutservice_cpu_1`:

```
500 incident traces, 7,759 spans, 0 orphaned spans, 500/500 traces with exactly one root
```

**Step 3 — column detection:**

```
trace_id:  traceID | trace_id | id
span_id:   spanID | span_id
parent_id: parentSpanID | parent_id | parent_span_id
service:   serviceName | service | service_name | app
operation: operationName | methodName | operation | rpc
duration:  duration | duration_ms | duration_us
status:    statusCode | status | status_code | error
```

**Step 4 — span assembly:**

```python
TraceSpan(
    traceID=trace_id,
    spanID=span_id,
    operationName=operation,
    references=[{"refType": "CHILD_OF", "spanID": parent_id}] if parent_id else [],
    startTime=int(ts * 1e6),       # microseconds
    duration=dur_us,
    tags=[
        {"key": "service.name", "value": service},
        {"key": "operation.name", "value": operation},
        # optional: {"key": "error", "value": True}
        # optional: {"key": "http.status_code", "value": int(status)}
    ],
)
```

Status mapping: `error, 500, 502, 503, 504, true, 1, fail` → error tag.

**Step 5 — group by `traceID`** into `Trace` objects, then classify the whole trace by the minimum span start time.

### 5.6 Time-bucket quantization

`compute_time_buckets` (`rcaeval_adapter.py:575-639`) creates 5-second buckets centered on `inject_time`:

```
bucket_index = floor((ts − inject_time) / 5)
negative index = baseline bucket
positive index = incident bucket
```

Each bucket records `log_count`, `metric_points_count`, `span_count`, and `services_involved`.

**Purpose:** this is a *cross-signal alignment* artifact used by the dashboard for temporal context. It is **not** used by the fusion engine. A metric spike and a log error in the same bucket are not explicitly correlated anywhere in the code.

### 5.7 Ground truth

`extract_ground_truth` (`rcaeval_adapter.py:641-700`):

1. If the case is in `cases.parquet`, use `root_cause_service` and `fault`.
2. Otherwise parse the folder name:

```
re2ob_checkoutservice_cpu_1
  → dataset=re2ob, service=checkoutservice, fault=cpu

RE2_online-boutique_cartservice_mem_1
  → dataset=RE2, benchmark=online-boutique, service=cartservice, fault=mem
```

3. Add `inject_time` if `inject_time.txt` exists.

**Current inconsistency:** folder-parsed faults are lowercased (`mem`), while the registry and `src/eval/taxonomy.py` use uppercase (`MEM`). One unit test currently fails because of this (`tests/test_rcaeval_adapter.py:121`).

---

## 6. Path B — Live Docker/Prometheus/Jaeger ingestion

File: `scripts/collect_telemetry.py`.

### 6.1 Topology

`docker-compose.yml` starts:

| Group | Containers |
|---|---|
| Observability | `cadvisor`, `jaeger`, `prometheus`, `otel-collector`, `flagd` |
| Microservices | `redis-cart`, `cartservice`, `productcatalogservice`, `quoteservice`, `shippingservice`, `emailservice`, `currencyservice`, `paymentservice`, `checkoutservice`, `frontend` |

**Gap:** the frontend is configured to call `recommendationservice` at `127.0.0.1:9001` and `adservice` at `127.0.0.1:9555`, but neither service is in the compose file. Those calls always fail, which contaminates the "normal" baseline with connection errors.

### 6.2 Logs

`collect_logs` (`collect_telemetry.py:48-60`):

```python
container.logs(since=start, until=end + 5s, timestamps=True)
```

The service list is hardcoded (`MONITORED_SERVICES`, lines 27-37):

```
cartservice, paymentservice, currencyservice, checkoutservice, frontend,
productcatalogservice, shippingservice, emailservice, quoteservice
```

It excludes `recommendationservice` and `adservice`, which are present in the RCAEval ground truth for other suites.

### 6.3 Metrics

`collect_metrics` (`collect_telemetry.py:62-119`) runs six PromQL queries over a widened window (`−60 s` to `+30 s`), step `5s`, timeout `5s`:

| Name | Query |
|---|---|
| `cpu_usage_rate` | `sum(rate(container_cpu_usage_seconds_total{...}[30s])) by (service)` |
| `memory_rss` | `sum(container_memory_rss{...}) by (service)` |
| `memory_usage` | `sum(container_memory_usage_bytes{...}) by (service)` |
| `memory_limit` | `sum(container_spec_memory_limit_bytes{...}) by (service)` |
| `network_rx_bytes` | `sum(rate(container_network_receive_bytes_total{...}[30s])) by (service)` |
| `network_tx_bytes` | `sum(rate(container_network_transmit_bytes_total{...}[30s])) by (service)` |

The label is `container_label_com_docker_compose_service`, which is why cAdvisor is configured with the compose service label.

### 6.4 Traces

`collect_traces` (`collect_telemetry.py:121-175`):

1. `GET /api/services` to discover registered Jaeger service names.
2. For each monitored service, try three variants:

```
<name>
unknown_service:<name>
unknown_service:node      # Node.js services
```

3. `GET /api/traces?service=...&start=...&end=...&limit=50&lookback=custom`.
4. Deduplicate by `traceID`.

**Limitation:** `limit=50` per service. For a 5-minute incident this is a sample, not a census. Unlike Path A, there is no whole-trace sampling logic here; Jaeger returns full traces, which is good, but the limit is low.

### 6.5 Differences that affect correctness

| Property | Path A (replay) | Path B (live) |
|---|---|---|
| Baseline | 10-minute pre-injection slice from the same parquet | 3 success runs from `data/runs/success` |
| Log baseline | `snapshot.baseline.logs` (same case) | `data/baselines/drain3_baseline_templates.json` (14 templates!) or the 3 newest success runs |
| Metric baseline | same-case pre-injection points | Prometheus query range around the run |
| Trace baseline | 300 pre-injection traces | traces in the run window |
| Determinism | high (fixed seed) | low (scrape timing, sampling) |
| Ground truth | registry | folder name / metadata |

**Critical live-path issue:** `LogSageProcessor.load_or_train_baselines` checks the cached baseline first:

```python
if self.miner.load_baselines(cache_file) and len(self.miner.baseline_templates) > 0:
    self.is_baseline_ready = True
    return True
```

The checked-in cache contains only **14 templates** (verified). If it loads successfully, the processor never trains on the three success runs. In the live path this makes almost every incident line "novel", inflating log scores. The replay path avoids this because `TelemetryRAGRetriever.retrieve` trains directly from `snapshot.baseline.logs` (`rag/retriever.py:74-78`).

---

## 7. Log retrieval and LogSage preprocessing

Files: `src/log_processor/filter.py`, `miner.py`, `processor.py`.

### 7.1 What "retrieval" means here

Log retrieval is not full-text search. It is a four-filter funnel:

```
all incident lines
  → Drain3 template classification
  → keep lines with a LogSage keyword OR a novel template
  → expand each kept line by m=3 before / n=7 after
  → merge overlapping windows
  → truncate to 1,500 tokens
```

The output is a set of **evidence snippets**, not the raw log.

### 7.2 Drain3 configuration

`config/drain3.ini`:

```ini
[DRAIN]
sim_th = 0.4
depth = 4
max_children = 100
max_clusters = 1024
extra_delimiters = ["_", ":", "=", ",", "[", "]"]

[MASKING]
IP        → <IP>
UUID      → <UUID>
timestamp → <TIMESTAMP>
number    → <NUM>
```

The masking rules are aggressive: every digit sequence becomes `<NUM>`. This makes templates stable across request IDs and latencies, but it also erases signal (e.g. `status=500` and `status=200` collapse to the same template).

### 7.3 Baseline training (x = 3)

Two modes:

**Replay** (`rag/retriever.py:73-78`):

```python
log_processor = LogSageProcessor()
if snapshot.baseline and snapshot.baseline.logs:
    log_processor.miner.train_baseline_from_runs([snapshot.baseline.logs])
```

The baseline is the **same case's** pre-injection log slice. This is in-sample baseline calibration: it controls for workload and service version, but it also means baseline and incident come from the same run.

**Live** (`processor.py:45-84`):

1. Try to load `data/baselines/drain3_baseline_templates.json`.
2. If absent, take the 3 newest `data/runs/success/*_telemetry.json` files.
3. Train Drain3 on their lines.
4. Save the resulting template set.

The paper's x=3 is respected in code (`run_files[:x_runs]`), but the cache short-circuit means the number is not always used.

### 7.4 Novelty test

`DrainBaselineMiner.classify_line` (`miner.py:86-101`):

```python
res = self.miner.add_log_message(line)
template = res["template_mined"]
is_novel = template not in self.baseline_templates
```

**Important nuance:** `add_log_message` mutates the miner. A template that is new on line 1 becomes part of the miner's clusters, but `baseline_templates` is a separate frozen set. So the same unseen template is marked novel on every occurrence. The processor deduplicates by template string before counting (`processor.py:104-122`), which is correct:

```python
novel_templates_set.add(template)
novel_count = len(novel_templates_set)
```

### 7.5 Keyword filter

`filter.py:4-17` implements the exact LogSage keyword list:

```
fatal, fail, panic, error, exit, kill,
no such file, err:, err!, failures:, exception, cannot
```

Matching is case-insensitive substring matching; the first match wins.

**Consequence:** a line like `"No errors detected"` matches `error`. The filter is intentionally high-recall and low-precision, and the LLM is expected to disambiguate.

### 7.6 Asymmetric context

`expand_asymmetric_context` (`filter.py:73-129`) with `m=3`, `n=7`.

Two behaviors are enabled by default (`processor.py:159-161`):

1. **Probe filtering** — lines containing `/healthz`, `/ready`, `/live`, `kube-probe`, `health check 200`, `prometheus scraper`, `/metrics` are skipped so they do not displace stack traces.
2. **Dynamic stack-trace extension** — after the 7 lines, if the next lines look like a stack trace (`at ...`, `Caused by:`, `Traceback`, `goroutine`), keep collecting up to `n + 15 = 22` lines.

This is a deliberate deviation from a strict reading of the paper (which specifies fixed m/n). It improves snippet quality but must be disclosed in any fidelity claim.

### 7.7 Window merging

For every candidate index, a window `[idx−3, idx+7]` is created. Overlapping or adjacent windows are merged (`processor.py:138-148`). Each merged window produces **one** `LogSnippet`, anchored at the first keyword-bearing target in that window.

**Consequence:** if 50 error lines are consecutive, they produce one snippet, not 50. That is good for token budget, but it means `len(snippets)` is not proportional to error volume.

### 7.8 Snippet and prompt rendering

Each snippet is rendered as:

```
  [ctx] line before
  [ctx] line before
▶ [ERROR] target line
  [ctx] line after
  ...
```

The service block is:

```
--- Service: checkoutservice ---
<blocks...>
```

### 7.9 Token budget

`max_log_tokens=1500` (`processor.py:37`).

Services are sorted by `(snippet count, novel templates)` descending (`processor.py:210`), then added until the budget is exhausted. The last service may be truncated to the remaining budget if at least 100 tokens remain (`processor.py:217-224`).

Token estimation is `len(text) // 4` (`processor.py:22-24`). The prompt assembler later re-counts with tiktoken, so the two budgets are not the same unit. This is a known approximation.

### 7.10 Worked example — why a CPU case produces no log evidence

For `re2ob_checkoutservice_cpu_1`:

```
incident log lines: 36,514 across 10 services
baseline log lines: 71,596 across 10 services
snippets produced:  0
novel templates:    0
```

Why: CPU stress produces latency but no new error keywords and no new templates. Every line matches a baseline template and none contains a LogSage keyword. The log modality contributes **nothing** to fusion for this case. This explains the 10% logs-only Top@1 result: resource faults are largely invisible in logs.

This is not a bug; it is the motivating observation behind multi-signal RCA. But it also means "logs were evaluated" in an artifact can mean "logs produced zero evidence."

---

## 8. Metric retrieval and anomaly detection

File: `src/metrics_processor/processor.py`.

### 8.1 Baseline construction

For each incident series, the processor looks up baseline statistics by `metric_name`:

```python
if metric_name in baseline_stats:
    mean, std, median, mad = baseline_stats[metric_name]
else:
    split = max(2, int(len(values) * 0.30))
    mean, std = mean_std(values[:split])
    median, mad = median_mad(values[:split])
```

The fallback is an **in-sample** baseline: the first 30% of the incident window is treated as normal. This is used when the adapter produced no baseline series for that metric.

### 8.2 Statistics

`compute_mean_and_std` (`processor.py:87-96`):

```
mean = Σx / n
std  = sqrt( Σ(x − mean)² / (n − 1) )      # sample standard deviation
```

`compute_median_and_mad` (`processor.py:99-117`):

```
median = middle value
MAD    = median(|x − median|)
```

### 8.3 Peak divergence

```python
peak_val = max(values, key=lambda v: abs(v - baseline_mean))
```

The alert reports the single point farthest from the baseline mean, not the mean of the incident window and not the last point. This is sensitive to a single spike.

### 8.4 Z-scores

```python
eps      = max(1e-6, abs(mean) * 1e-4) if mean != 0 else 1e-4
eff_std  = max(std, eps)
eff_mad  = max(mad, eps)

z        = (peak − mean) / eff_std
robust_z = 0.6745 * (peak − median) / eff_mad
```

The `0.6745` factor is the standard normalization for MAD under a normal distribution. However, the code uses **raw MAD**, not `1.4826 × MAD`; the two conventions differ by a factor of ~2.2. As a result, `robust_z` is systematically larger than `z`.

**Consequence:** `effective_divergence = max(|z|, |robust_z|)` is usually the robust value. If the baseline MAD is tiny (near-constant metric), `robust_z` explodes. Verified example:

```
metric: redis_diskio
z        = 3.22
robust_z = 67,174.59
adaptive τ = 4.50
severity = CRITICAL
```

A robust z of 67k is not statistically meaningful; it is an artifact of dividing by a near-zero MAD. This is a known defect (see §18).

### 8.5 Adaptive threshold

```python
def compute_adaptive_threshold(mean, std, default=3.0):
    if mean == 0 or std == 0:
        return default
    cv = abs(std / mean)
    scaling = 1.0 + 0.25 * min(2.0, cv)
    return max(2.5, min(5.0, default * scaling))
```

Range: `3.0` (low noise) to `4.5` (high noise), clamped to `[2.5, 5.0]`.

The intent is to reduce false positives on noisy metrics. The side effect is that a genuine 3σ anomaly on a noisy metric can be missed.

### 8.6 Memory and OOM logic

Only for series whose name contains one of:

```
memory, mem, rss, bytes_used, heap
```

Steps (`processor.py:240-257`):

1. Compute OLS slope `dM/dt` in bytes/second.
2. Look up a cgroup memory limit for the service, collected from any series whose name contains `spec_memory_limit`, `memory_limit`, or `mem_limit`.
3. If a limit exists and `slope > 0`:

```
remaining = max(0, limit − peak)
time_to_oom = remaining / slope
if time_to_oom < 300 s: is_oom_risk = True
```

4. If no limit-based OOM was found, use a dynamic slope threshold:

```
dynamic = max(10_000, min(50_000, mean * 0.05))   # if mean > 0
if slope > dynamic and (z > 1.5 or robust_z > 1.5):
    is_oom_risk = True
```

**Consequence for RCAEval:** the benchmark metrics are named `checkoutservice_cpu`, `checkoutservice_memory`, etc. and contain **no cgroup limit series**. Therefore the limit branch never fires, and `time_to_oom_seconds` is `None` for every alert. Verified: 0 of 920 fusion alerts have a non-null TimeToOOM. The `has_oom_alert` flag can still be set by the slope branch.

### 8.7 Severity

```python
if is_oom_risk or divergence >= 2.5 * tau:  CRITICAL
elif divergence >= 1.6 * tau:               HIGH
elif divergence >= tau:                     MEDIUM
else:                                       LOW
```

The final `LOW` branch is unreachable because `is_threshold_exceeded` already requires `divergence >= tau`.

### 8.8 Sorting and cap

Alerts are sorted by `(severity rank, −max(|z|, |robust_z|))` and truncated to `max_alerts=10`.

This is why the fusion engine sees at most 10 metric alerts, even if 50 metrics are anomalous. The cap is a token-budget decision, not a statistical one.

### 8.9 Worked example

From the artifact `results/artifacts/re2ob_checkoutservice_cpu_1/fusion.json`:

| Service | Metric | z | robust z | Severity |
|---|---|---|---|---|
| redis | redis_diskio | 3.22 | 67,174.59 | CRITICAL |
| currencyservice | currencyservice_socket | 5,000.00 | 3,372.50 | CRITICAL |
| checkoutservice | checkoutservice_socket | 2,222.22 | 1,498.89 | CRITICAL |
| … | … | … | … | … |

`currencyservice_socket` with z=5000 is another near-zero-variance artifact: the baseline standard deviation is essentially zero, so any change produces an enormous z. These alerts then dominate fusion scores.

---

## 9. Trace retrieval, DAG reconstruction, and culprit extraction

File: `src/trace_processor/processor.py`.

### 9.1 Error extraction

`extract_error_info` (`processor.py:21-57`) scans span tags:

| Tag | Effect |
|---|---|
| `error=true` / `otel.status_code=ERROR` | error |
| `http.status_code` / `status_code` / `grpc.status_code` >= 500 | error |
| gRPC codes `2, 4, 14` | error |
| `error.message` / `exception.message` | captures message |
| `error.code` / `exception.type` / `grpc.code` | captures code |

Span logs are also scanned: `message`/`error.message` fields mark the span as error; `event=error` marks it as error.

### 9.2 Tree construction

For each trace:

```python
span_map = {spanID: span}
for span in spans:
    parent = first reference with refType in (CHILD_OF, FOLLOWS_FROM)
    parents_map[spanID] = parent
    if parent in span_map:
        children_map[parent].append(spanID)
        if parent_service != child_service:
            dep_graph[parent_service].add(child_service)

roots = spans with no parent, or parent not in span_map
```

**Invariant 3:** in a correctly sampled trace, every non-root parent resolves within the same trace, and there is exactly one root. The verifier confirms this after the whole-trace sampling fix:

```
0 / 7,759 orphaned spans
500 / 500 traces have exactly one root
```

### 9.3 Depth

DFS from each root (`processor.py:293-304`):

```
depth(root) = 0
depth(child) = depth(parent) + 1
```

A span reachable from multiple roots keeps its first assigned depth.

### 9.4 Self-duration

For every span:

```
self_duration = max(0, duration − Σ duration(children))
```

This is the standard "exclusive time" calculation. Caveats:

- It assumes children do not overlap in time; concurrent children are double-counted, so self-duration can be underestimated (clamped to 0).
- If a child span was not sampled, its time is attributed to the parent, so the parent looks slow.

### 9.5 Timeout inversion

For each error span, the processor checks whether it is a timeout:

```python
is_timeout = status in (504, 4) or any(
    w in error_text for w in ("deadline", "timeout", "timed out", "context deadline exceeded")
)
```

If the span has children, any child that consumed at least 60% of the parent's duration **or** has self-duration at least 50% of the parent's duration is added to the bottleneck list (`processor.py:163-175`).

This is the EC-3 heuristic: the parent reports the timeout, but the child is the real culprit.

### 9.6 Latency bottleneck vs baseline

Only if baseline traces exist:

```python
baseline_durations[(service, operation)] = [dur_ms, ...]
```

For each incident span:

```python
if len(baseline) >= 3:
    p99 = sorted(baseline)[max(0, int(len(baseline) * 0.99) - 1)]
    if self_duration > max(50.0, p99 * 2.0):
        bottleneck
```

This catches CPU/delay faults where status code is 0 (no explicit error). Verified: 21 latency bottlenecks in the sample CPU case.

### 9.7 Leaf culprit selection

Candidate spans are the union of:

1. explicit error spans,
2. timeout-inversion children,
3. latency bottlenecks.

They are sorted by depth descending; the deepest becomes the trace's culprit and receives `+3` votes (`processor.py:206-220`).

Across traces, the service with the most votes wins:

```python
culprit_service = max(culprit_candidates.items(), key=lambda kv: kv[1])[0]
```

Ties are resolved by insertion order (first encountered). This is deterministic only because traces are processed in input order.

### 9.8 Dependency graph

Every parent→child edge with different services becomes `dep_graph[parent].add(child)`.

The graph is used by:

- `FusionEngine` for topological dampening;
- the prompt for call-topology context;
- the dashboard DAG tab.

### 9.9 Prompt rendering

Example block:

```
### Distributed Trace Analysis:
- Total Traces: 500, Failing Spans: 0, Latency Bottlenecks: 21
- Isolated Culprit Service: 'frontendservice'
- Deepest Leaf Culprit Span: frontendservice:HTTP GET (status=LATENCY_ANOMALY, duration=..., depth=...)

Call Hierarchy DAG:
Trace abc123:
frontendservice:HTTP GET (120.5ms)
  ↳ currencyservice:Convert (95.0ms) <--- [LEAF CULPRIT]
  ↳ ...
```

Only the first 5 failing/bottleneck traces are rendered (`max_traces_to_format=5`).

### 9.10 Service-name aliasing — a correctness hazard

Verified on the sample case:

```
trace services:  frontendservice, productcatalogservice, currencyservice,
                 recommendationservice, checkoutservice, emailservice, paymentservice
metric services: adservice, currencyservice, emailservice, frontend,
                 productcatalogservice, recommendationservice, cartservice,
                 checkoutservice, paymentservice, shippingservice, redis, frontend-external
log services:    frontend, currencyservice, cartservice, recommendationservice,
                 adservice, shippingservice, checkoutservice, paymentservice,
                 emailservice, redis
```

Differences:

- `frontendservice` (trace) vs `frontend` (metric/log) — **not normalized**.
- `frontend-external` (metric) is a separate pseudo-service.
- `cartservice` has no trace spans in this case at all.

Fusion compares service strings with exact lowercase equality. An alias therefore prevents:

- the consensus multiplier from firing for the true service,
- the topological dampening from connecting caller and callee,
- the Ground-Truth badge in the UI from matching.

This is a concrete, high-impact defect.

### 9.11 Worked example

`re2ob_checkoutservice_cpu_1` (current code):

```
incident traces: 500
incident spans:  7,759
error spans:     0
bottlenecks:     21
culprit service: frontendservice
```

Note: the ground-truth service is `checkoutservice`, and the trace culprit is `frontendservice`. Trace-only evidence points at the wrong service because the frontend is the entry point and the latency is propagated upward. Topological dampening is supposed to fix this, but only if the culprit has downstream callees with signals.

---

## 10. Multi-signal fusion

File: `src/fusion/engine.py`.

### 10.1 Inputs

```python
correlate_with_debug(log_evidences, metric_evidence, trace_evidence)
```

Each modality contributes a score per service:

| Source | Contribution |
|---|---|
| Logs | `snippets * 1.0 + min(novel_templates, 10) * 2.5` |
| Metrics (OOM) | `+6.0` per OOM alert |
| Metrics (other) | `+3.0 * severity_multiplier` |
| Traces | `+5.0` for the culprit, `+2.0` if its depth > 0 |

Severity multipliers:

```
CRITICAL 1.5 | HIGH 1.2 | MEDIUM 1.0 | LOW 0.5
```

### 10.2 Topological dampening

For every caller in the dependency graph that has a score:

```python
failing_callees = [c for c in callees if c has any signal]
if failing_callees:
    score[caller] *= 0.60
    for c in failing_callees:
        score[c] += 2.0
```

Rationale: a caller that fails because a downstream callee failed is a symptom, not the cause. The callee gets the credit.

**Risk:** if the true culprit is genuinely the caller (e.g. `checkoutservice` CPU saturation), this rule demotes it by 40% and promotes its callees. That directly harms EC-1/EC-2 cases on upstream services.

### 10.3 Consensus multiplier

For each service:

```
2 modalities → × 1.35
3 modalities → × 1.75
```

This rewards cross-signal agreement. Because service names must match exactly, the aliasing defect can suppress the multiplier.

### 10.4 Ranking

```python
ranked = sorted(scores, key=scores.get, reverse=True)
```

Ties are broken by dict insertion order (first modality to mention the service).

### 10.5 Category likelihood

Each category starts with a prior:

```
EC-1 0.10 | EC-2 0.10 | EC-3 0.10 | EC-4 0.05 | EC-5 0.10 | CODE_BUG 0.10
```

Then additive evidence:

| Condition | Category | Delta |
|---|---|---|
| `has_oom_alert` | EC-1 | `+8.0` |
| `time_to_oom < 180 s` | EC-1 | `+4.0` |
| `has_latency_anomaly` | EC-2 | `+6.5` |
| `error_spans >= 2` | EC-3 | `+7.0 + min(4.0, depth*1.5)` |
| `error_spans == 1` | EC-3 | `+3.0` |
| `bottlenecks > 0` and no errors | EC-2 | `+4.0` |
| any snippets | CODE_BUG | `+3.5 + min(4.5, novel*1.5)` |
| infra keywords | EC-5 | `+3.0` per snippet |
| corruption keywords | EC-4 | `+3.0` per snippet |
| no logs, no metrics, no trace anomalies | EC-4 | `+4.0` |

`argmax` wins; if the max is `<= 0.2`, the result is `UNKNOWN`.

### 10.6 Confidence

```python
c_metrics = min(0.95, max(0.40, max_divergence / 8.0))   if metrics else 0
c_traces  = min(0.95, max(0.50, 0.45 + 0.15 * depth))     if trace anomalies else 0
c_logs    = min(0.90, max(0.35, 0.35 + 0.10 * min(5, snippets))) if logs else 0

active = certainties for modalities that voted for the top service
support = 1 − Π(1 − c)

margin = (top_score − runner_up_score) / top_score   # clamp [0.1, 1.0]
confidence = support * (0.65 + 0.35 * margin)
confidence = round(clamp(confidence, 0.50, 0.95), 2)
```

Properties:

- The floor is `0.50`, so the system never reports low confidence.
- The cap is `0.95`, so it never reports certainty.
- Only modalities that **voted for the top service** contribute certainty. A service with one weak metric alert can still get 0.95 if the metric certainty is high.

### 10.7 Worked example — exact arithmetic

Artifact `results/artifacts/re2ob_checkoutservice_cpu_1/fusion.json`:

Inputs:

```
metric alerts (all CRITICAL):
  checkoutservice × 3   → 3 × 4.5 = 13.5
  emailservice    × 2   → 2 × 4.5 =  9.0
  redis           × 2   → 2 × 4.5 =  9.0
  currencyservice × 1   →     4.5
  frontend        × 1   →     4.5
  cartservice     × 1   →     4.5

trace culprit: currencyservice, depth 1 → 5.0 + 2.0 = 7.0
logs: none

dependency edges with signals:
  checkoutservice → currencyservice, emailservice
```

Topological pass:

```
checkoutservice × 0.60 = 8.1
currencyservice + 2.0  = 6.5
emailservice    + 2.0  = 11.0
```

Consensus pass:

```
currencyservice: 6.5 + 7.0 = 13.5 → ×1.35 (METRICS+TRACES) = 18.225
emailservice:    11.0       → ×1.0                      = 11.0
redis:            9.0       → ×1.0                      =  9.0
checkoutservice:  8.1       → ×1.0                      =  8.1
frontend:         4.5       → ×1.0                      =  4.5
cartservice:      4.5       → ×1.0                      =  4.5
```

This matches the stored artifact exactly:

```
ranked: currencyservice 18.225, emailservice 11.0, redis 9.0,
        checkoutservice 8.1, frontend 4.5, cartservice 4.5
```

Category likelihoods:

```
EC-2 = 0.10 (prior) + 6.5 (latency anomaly) + 4.0 (bottlenecks, no errors) = 10.6
EC-1 = 0.10
EC-3 = 0.10
```

Matches the artifact (`EC-2: 10.6`).

**Conclusion:** the fusion arithmetic is deterministic and auditable. The ranking is mathematically correct *given the evidence*. Whether the evidence is trustworthy is a separate question.

### 10.8 Why the artifact confidence is 0.98 but the current code gives 0.67

The artifact was generated on 2026-11-12, before the current `engine.py` was modified. The current code, run on the same case, produces:

```
candidates: emailservice, checkoutservice, redis, ...
confidence: 0.67
```

This is a **reproducibility failure**, not a math error. Cached artifacts are not stamped with a code version, so the dashboard can show a diagnosis that the current code would not reproduce.

---

## 11. Prompt assembly and token budgeting

File: `src/llm/prompt.py`.

### 11.1 Budget equation

```
max_total_tokens       = 2400
system_prompt_overhead = 300
effective_budget       = max(400, 2400 − 300) = 2100
fixed_overhead         = 120
elastic_pool           = max(300, 2100 − 120) = 1980
```

Allocation:

| Section | Cap | Floor |
|---|---|---|
| Traces | `min(needed, 0.45 * elastic_pool)` | 250 |
| Metrics | `min(needed, 0.35 * elastic_pool)` | 200 |
| Logs | all remaining budget | 200 |

The logs absorb whatever traces and metrics do not use.

### 11.2 Section order

```
### Incident Reference ID: INC-XXXXXXXX
### Service Dependency Graph (Caller -> Callees)
### Distributed Trace Analysis
### Metric Anomaly Alerts
### Extracted Log Evidence
Analyze the telemetry evidence above...
```

Order is fixed: traces → metrics → logs. This is deliberate: traces are considered the most structurally informative, logs the most elastic.

### 11.3 Truncation

`truncate_to_tokens` (`prompt.py:63-79`):

```python
if count(text) <= max_tokens: return text
suffix = "\n...[truncated for token budget]"
tokens = encode(text)[: max_tokens − count(suffix) − 2]
return decode(tokens) + suffix
```

Uses `tiktoken` `cl100k_base`. If tiktoken is unavailable, falls back to `len // 3`.

### 11.4 Anonymization

```python
opaque_id = "INC-" + sha256(run_id).hexdigest()[:8].upper()
```

This replaced the raw `run_id` (e.g. `re2ob_checkoutservice_cpu_1`, which encodes service and fault). The current code is correct. The cached artifacts are not (see §16).

### 11.5 Mode behavior

| Mode | Traces | Metrics | Logs |
|---|---|---|---|
| `logs-only` | no | no | yes |
| `logs-metrics` | no | yes | yes |
| `logs-traces` | yes | no | yes |
| `fusion` | yes | yes | yes |

### 11.6 Verified token accounting

Current run on the sample case:

```
header: ### Incident Reference ID: INC-97BF1C72
total: 1,321 tokens
traces: 889 | metrics: 392 | logs: 0
recount with tiktoken: 1,321
```

The accounting matches exactly. The prompt is well under 2,500.

### 11.7 What the prompt does and does not contain

It contains:

- the ranked anomaly evidence (trace bottleneck spans, metric alerts, log snippets),
- the service dependency graph,
- an opaque incident ID.

It does **not** contain:

- the ground-truth service or fault,
- the case ID (current code),
- raw telemetry beyond the selected windows,
- the ranked candidate list from fusion (the LLM must infer it).

The prompt is therefore an evidence packet, not an answer key. The local fallback, however, bypasses the LLM and reads the fusion ranking directly.

---

## 12. LLM diagnosis and local fallback

File: `src/llm/client.py`.

### 12.1 Gemini client

```python
genai.Client(api_key=..., http_options=types.HttpOptions(timeout=30_000))
```

Model default: `gemini-2.5-flash` (env `GEMINI_MODEL`).
Temperature: `0.1`.
Response: `application/json` with `response_schema=RCAReport`.

### 12.2 Structured output

The schema enforces:

```python
class RCAReport(BaseModel):
    run_id: str
    scenario: str
    root_cause_service: str
    culprit_services: list[str]
    failure_category: FailureCategory
    confidence_score: float   # 0..1
    root_cause_summary: str
    evidence_triangulation: EvidenceTriangulation
    remediation_steps: list[RemediationStep]
```

If validation fails, the client retries once with the validation error appended (`max_retries=1`).

### 12.3 Backoff

Transient errors (429, quota, 503, timeout, unavailable, overloaded) are retried up to 3 times with exponential backoff (`2 * 2^attempt` seconds).

### 12.4 Local fallback

This is the path used for **all 362 persisted artifacts**:

```python
RCAReport(
    run_id=snapshot.run_id,
    scenario=rag_ctx.scenario,
    root_cause_service=rag_ctx.candidate_services[0],
    culprit_services=rag_ctx.candidate_services[:3],
    failure_category=rag_ctx.hypothesized_category,
    confidence_score=rag_ctx.confidence,
    root_cause_summary="Telemetry-RAG localized root cause to '<svc>'...",
    evidence_triangulation=rag_ctx.triangulation,
)
```

**Consequence:** for every reported result, the "diagnosis" is a template wrapping the fusion output. The LLM is not in the loop. `engine_type` is `TELEMETRY_RAG_LOCAL` for 362/362 artifacts.

This is honest in the artifact (`engine_type`), but it means:

- the reported accuracy measures **fusion**, not Gemini;
- the prompt is generated and persisted but never consumed by a model in these runs;
- any claim about LLM reasoning is unsupported by the stored evidence.

---

## 13. Evaluation mathematics

Files: `src/eval/metrics.py`, `src/eval/taxonomy.py`, `src/schemas/artifact.py`.

### 13.1 Ranked candidates

`score_single_case` (`metrics.py:41-90`):

```python
candidates = [s.lower() for s in report.culprit_services or [report.root_cause_service]]
pred_top1 = candidates[0]
gt = ground_truth_service.strip().lower()
```

### 13.2 Top@1, Top@3, MRR

```
Top@1 = (candidates[0] == gt)
Top@3 = (gt in candidates[:3])
MRR   = 1 / rank_of_first_match      (0 if absent)
```

Note: Top@3 includes Top@1. A "Top@3 hit" that is not Top@1 is a ranking miss.

### 13.3 Fault mapping

`taxonomy.py:9-23`:

| RCAEval label | ObservaSage category |
|---|---|
| MEM, MEMORY, OOM | EC-1 |
| DELAY, NET_DELAY, LOSS, NET_LOSS, CPU | EC-2 |
| CORRUPT | EC-4 |
| DISK | EC-1 |
| SOCKET, PARTITION | EC-5 |
| CRASH | EC-3 |

`is_fault_category_match` returns `False` if the label is unmapped.

`DISK → EC-1` and `LOSS → EC-2` are debatable: disk I/O is a resource fault but not necessarily OOM, and packet loss is not latency. This mapping directly shapes the category accuracy metric.

### 13.4 Error buckets

`classify_error_bucket` (`artifact.py:32-40`):

| Top@1 | Top@3 | Fault match | Bucket |
|---|---|---|---|
| ✓ | ✓ | ✓ | `hit` |
| ✓ | ✓ | ✗ | `category_miss` |
| ✗ | ✓ | ✓ | `ranking_miss` |
| ✗ | ✓ | ✗ | `localization_and_category_miss` |
| ✗ | ✗ | ✓ | `localization_miss` |
| ✗ | ✗ | ✗ | `localization_and_category_miss` |

### 13.5 Aggregation

```python
top1 = mean(top1_hit)
top3 = mean(top3_hit)
mrr  = mean(mrr)
fault_acc = mean(fault_match)
```

Per-fault breakdown uses the same formulas grouped by `ground_truth_fault`.

### 13.6 Current reported values

`results/ablation_fusion.csv` (90 rows, verified):

| Mode | Top@1 | Top@3 | MRR | Category Acc |
|---|---|---|---|---|
| logs-only | 10.00% | 10.00% | 0.1000 | 0.00% |
| logs-metrics | 60.00% | 93.33% | 0.7481 | 54.44% |
| logs-traces | 24.44% | 30.00% | 0.2722 | 26.67% |
| fusion | 71.11% | 96.67% | 0.8296 | 42.22% |

### 13.7 What these numbers license

They license the claim:

> On 90 `RE2-OB` cases, with the code version that produced these CSVs and with the local fusion fallback, fusion ranked the ground-truth service first 71.1% of the time and in the top three 96.7% of the time.

They do **not** license:

- a claim about Gemini or any LLM;
- a claim about all 239 cases;
- a claim that the system is calibrated;
- a claim that the failure category is reliable (42.2%, and 78/90 predictions are EC-2);
- a claim that the current code reproduces the numbers (cached artifacts predate fixes).

### 13.8 Statistical notes

- n = 90, paired across modes.
- Fusion vs logs-metrics: Top@1 difference is significant under McNemar (p ≈ 0.013), Top@3 difference is not (p ≈ 0.45).
- Confidence calibration is poor:

```
Brier score          0.2655
constant baseline    0.2054   (predict base rate 0.7111 for every case)
confidence > 0.95    44 cases, 52.3% actually correct
```

A constant predictor is better calibrated than the system.

---

## 14. Persistence, caching, API, and dashboard

### 14.1 Artifact

`CaseAnalysisArtifact` (`schemas/artifact.py:98-113`):

```
schema_version, case_id, mode, engine_type, created_at, source,
case_metadata, stage_timings, evidence, fusion, prompt, report, evaluation
```

Stored at:

```
results/artifacts/<case_id>/<mode_with_underscores>.json
```

Example: `results/artifacts/re2ob_checkoutservice_cpu_1/fusion.json`.

Every artifact is self-contained: evidence, fusion scores, prompt preview, report, and score. This is excellent for auditability. What it lacks is a **code version / commit hash**.

### 14.2 CSV

```
results/ablation_<mode>.csv
```

Columns: `case_id`, ground truth, prediction, ranked candidates, `top1_hit`, `top3_hit`, `mrr`, `fault_match`, `confidence`, `prompt_tokens`, `duration_ms`, `engine_type`.

The CSV is written incrementally after each case (`orchestrator.py:469-478`), so a crash does not lose completed cases.

### 14.3 Cache semantics

`load_artifact` (`orchestrator.py:331-343`) loads any existing JSON and sets `source="cache"`.

API behavior:

- `GET /api/case/{id}/analysis` returns the cached artifact if it exists.
- `POST /api/analyze` returns the cached artifact unless `force_recompute=true`.
- `GET /ws/diagnose` returns the cached artifact unless `force_recompute` or `use_llm`.

**Consequence:** the dashboard always shows the oldest artifact, even if the code has changed. There is no `created_at`-vs-code check and no `force_recompute` in the UI's normal inspection path.

### 14.4 API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | status, version, cases available |
| `GET /api/cases` | discovered cases + CSV scores + artifact flag |
| `GET /api/case/{id}` | metadata, telemetry file availability |
| `GET /api/case/{id}/analysis` | full artifact or CSV fallback |
| `GET /api/case/{id}/compare` | all four modes for one case |
| `GET /api/case/{id}/score` | CSV score |
| `POST /api/analyze` | run or load one case |
| `GET /api/evaluate` | aggregate summary for one mode |
| `GET /api/compare` | aggregate summary for several modes |
| `WS /ws/diagnose` | staged live diagnosis |

### 14.5 WebSocket stages

| Stage | Progress | Meaning |
|---|---|---|
| `ingesting` | 25 | parse parquet |
| `retrieving` | 60 | run the three processors |
| `fusing` | 85 | fusion + prompt |
| `complete` | 100 | artifact payload |

### 14.6 Dashboard data flow

```
loadCases()            → GET /api/cases?mode=...&limit=500
selectCase(id)         → GET /api/case/{id}/analysis
                       → GET /api/case/{id}/compare
loadScoreboard()       → GET /api/health
                       → GET /api/compare?modes=...
Run Diagnosis button   → WS /ws/diagnose
```

Known dashboard defects (see §18):

- scoreboard cards are hardcoded and the dynamic update reads the wrong keys (`top1_pct` vs `overall_top1`), so the first card becomes `undefined%` and the loop aborts on `m.mrr.toFixed`;
- stepper stage IDs do not match persisted stage names;
- `resetDetailsForUnscored` writes to legacy element IDs that no longer exist;
- the scoreboard title says `RE2-OB` while the API may serve any suite.

---

## 15. How we know it is correct — a seven-layer verification framework

"Correct" is not one property. It decomposes into seven independent layers. A failure at any layer invalidates claims above it.

### L0 — Reproducibility

**Question:** can the same input produce the same output with the same code?

Checks:

- code version is recorded with every artifact;
- random seeds are fixed (`42`);
- data files are immutable and checksummed;
- cached results are invalidated when code changes.

Current status:

- ❌ Artifacts have `schema_version` but no commit hash.
- ❌ 362 artifacts were generated by older code; the current code produces different rankings and confidence.
- ✅ Sampling seed is fixed.
- ✅ Data folders are immutable.

### L1 — Ingestion fidelity

**Question:** did we read every field correctly, with the right units and types?

Checks:

- column detection matches the actual parquet schema;
- timestamps are normalized to seconds;
- durations are microseconds end-to-end;
- no rows are silently dropped;
- service names are extracted consistently.

Current status:

- ✅ Unit conversions are explicit (`1e9/1e6/1e3`) and verified on the sample case.
- ✅ Counts are plausible: 36,514 incident log lines, 72 metric series, 500 traces / 7,759 spans.
- ⚠️ Missing columns return empty data silently.
- ❌ Service aliases (`frontendservice` vs `frontend`) are not normalized.
- ⚠️ `cartservice` has no trace spans in the sample case (instrumentation or sampling gap).

### L2 — Temporal integrity

**Question:** is baseline strictly before the incident, and is the incident window correct?

Checks:

```
max(baseline ts) < inject_time <= min(incident ts)
```

for logs, metrics, and traces independently.

Current status (verified):

```
metrics: max_base=1705354565.000  min_inc=1705354566.000  inject=1705354566.000  PASS
traces:  max_base=1705354565.464  min_inc=1705354567.400  inject=1705354566.000  PASS
logs:    max_base=1705354565.000  min_inc=1705354566.000  inject=1705354566.000  PASS
```

Residual risk: traces straddling the boundary are assigned wholesale by minimum start time.

### L3 — Algorithmic invariants

**Question:** does each algorithm satisfy its mathematical contract?

Checks and current status:

| Invariant | Check | Status |
|---|---|---|
| z-score | `(peak − mean) / std` reproduces stored `z_score` | ✅ 10/10 alerts |
| prompt token count | tiktoken recount equals stored total | ✅ 1,321 = 1,321 |
| prompt budget | `< 2500` | ✅ 1,321 |
| DAG | no orphaned spans | ✅ 0/7,759 |
| DAG | exactly one root per trace | ✅ 500/500 |
| fusion score | weights + topology + multiplier reproduce ranking | ✅ exact match on the worked example |
| MRR | `1/rank` | ✅ |
| Top@3 | superset of Top@1 | ✅ by construction |
| candidate ranking | every candidate has a modality vote | ⚠️ services can enter via a single alert |

### L4 — Evidence traceability

**Question:** can every prediction be traced back to specific evidence?

Checks:

- every ranked candidate appears in `fusion.ranked_services` with a score and votes;
- every vote corresponds to a `MetricEvidence` alert, a `TraceEvidence` culprit, or a `LogEvidence` snippet;
- every prompt section is derived from stored evidence;
- the prompt preview matches the sections' token breakdown.

Current status:

- ✅ Artifacts store scores, votes, alerts, snippets, and prompt preview.
- ✅ The worked example reproduces the exact score.
- ⚠️ The prompt preview is stored, but the artifact does not store the **rendered prompt's hash**; tampering is undetectable.
- ❌ The artifact does not store the exact candidate→evidence mapping for log snippets (only aggregate counts).

### L5 — Evaluation validity

**Question:** are labels, normalization, denominators, and aggregation correct?

Checks:

- every artifact has a CSV row;
- artifact evaluation equals CSV evaluation;
- ground-truth service matches the registry;
- fault labels are normalized consistently;
- denominator is the intended case set.

Current status:

- ✅ 360/362 artifacts match their CSV row exactly.
- ❌ 2 artifacts have no CSV row (`re2tt_ts-auth-service_cpu_2`, `re3tt_ts-auth-service_f3_3`).
- ❌ fault normalization is inconsistent (`mem` vs `MEM`), causing 1 test failure.
- ⚠️ 90/239 cases evaluated; the denominator is a subset.

### L6 — Statistical validity

**Question:** do the numbers support the claimed inference?

Checks:

- paired tests for mode comparisons;
- confidence calibration (Brier, reliability curve);
- category confusion matrix;
- per-fault sample sizes.

Current status:

- ✅ Fusion vs logs-metrics Top@1: McNemar p ≈ 0.013.
- ❌ Top@3 difference not significant (p ≈ 0.45).
- ❌ Brier 0.2655 vs 0.2054 constant baseline.
- ❌ 44 predictions with confidence > 0.95 are only 52.3% correct.
- ❌ 78/90 category predictions are EC-2; only 1/15 `mem` cases is EC-1.
- ❌ 0/920 alerts have TimeToOOM, so EC-1 has no resource-slope evidence.

### L7 — Claim validity

**Question:** does the README claim only what L0–L6 support?

Current status:

- ❌ README says "Verified" and cites 90 cases, but does not state that the engine is the local fallback, not Gemini.
- ❌ README implies LLM diagnosis; 362/362 artifacts are `TELEMETRY_RAG_LOCAL`.
- ❌ README's category accuracy (42.22%) is presented without the EC-2 collapse.
- ✅ README's Top@1/Top@3/MRR values match the CSV.

---

## 16. What is currently verified, and what is not

### 16.1 Verifier output (actual)

`python scripts/verify_pipeline.py`:

```
=== Case re2ob_checkoutservice_cpu_1 ===
GT: service=checkoutservice fault=cpu inject=1705354566.0
[PASS] metrics baseline strictly before injection
[PASS] traces baseline strictly before injection
[PASS] logs baseline strictly before injection
[PASS] trace DAG has no orphaned spans — 0/7759 (0.0%), 500/500 one root
[PASS] at least one incident trace — 500 traces / 7759 spans
[PASS] trace services overlap metric/log service names — trace_only=['frontendservice']
[PASS] prompt incident id is opaque — INC-97BF1C72
[PASS] run/case id does not appear anywhere in prompt — clean
[PASS] prompt token accounting matches tokenizer — 1321 = 1321
[PASS] prompt under 2500 BPE tokens — 1321
[PASS] ranked candidates are non-empty
[PASS] confidence within [0, 1] — 0.67
[PASS] metric z-scores reproducible from stored stats — 10 alerts

=== Persisted artifacts vs CSV ===
[PASS] artifacts exist — 362 files
[PASS] artifact evaluation matches CSV row — 0 mismatches
[FAIL] every artifact has a CSV row — 2 artifacts without CSV row
[FAIL] no artifact prompt leaks the case id — 359/362 artifacts leak

Artifact engine types: {'TELEMETRY_RAG_LOCAL': 362}
15/17 checks passed
```

### 16.2 Additional measurements

| Measurement | Value |
|---|---|
| Discoverable complete cases | 239 |
| Evaluated cases | 90 (`RE2-OB`) |
| Artifacts | 362 |
| Artifacts leaking case ID in prompt | 359 / 362 |
| Artifact/CSV score mismatches | 0 |
| Artifacts without CSV row | 2 |
| Trace orphan rate (current code) | 0.0% |
| Prompt tokens (sample) | 1,321 / 2,400 |
| Fusion Top@1 | 71.11% |
| Fusion Top@3 | 96.67% |
| Fusion MRR | 0.8296 |
| Category accuracy | 42.22% |
| EC-2 predictions | 78 / 90 |
| `mem` cases predicted EC-1 | 1 / 15 |
| Brier score | 0.2655 |
| Constant-baseline Brier | 0.2054 |
| Confidence > 0.95 accuracy | 52.3% (44 cases) |
| Alerts with TimeToOOM | 0 / 920 |
| Tests | 47 passed, 1 failed |

### 16.3 Verdict by layer

| Layer | Verdict |
|---|---|
| L0 Reproducibility | ❌ cached artifacts are not version-stamped |
| L1 Ingestion | ✅ mostly; ⚠️ aliases and silent empties |
| L2 Temporal | ✅ verified |
| L3 Algorithmic | ✅ math reproduces |
| L4 Traceability | ✅ within an artifact; ❌ no prompt hash |
| L5 Evaluation | ✅ artifact↔CSV; ❌ 2 orphans, ❌ normalization |
| L6 Statistical | ❌ calibration, category collapse, missing OOM |
| L7 Claims | ❌ overstates LLM and generality |

---

## 17. Falsification tests you can run

A correctness claim is only as good as the tests that could break it.

### F1 — Ground-truth permutation

Shuffle the `ground_truth_service` column across cases. Accuracy should collapse to chance (~1/number of services). If it stays high, the model is reading the label.

```bash
python - <<'PY'
# pseudo-code: load CSV, permute ground_truth_service, rescore
PY
```

Expected after fix: fusion Top@1 drops from 71% to roughly 10–20%.

### F2 — Prompt leakage mutation

Replace the opaque ID with the raw `run_id` and rerun the leak check. It must fail.

```
Expected: "run/case id does not appear anywhere in prompt" FAILS
```

The verifier already demonstrates this on old artifacts (359/362 fail).

### F3 — DAG truncation

Remove 10% of spans at random from a trace. The orphan check must fire.

```
Expected: orphan rate > 0
```

### F4 — Z-score tampering

Change one `z_score` in an artifact by 0.01. The recomputation check must fail.

```
Expected: "metric z-scores reproducible" FAILS
```

### F5 — Metric time shuffle

Shuffle metric point order before slope computation. OOM risk should disappear for a genuine memory leak.

### F6 — Known-latency injection

Add a synthetic 3× latency to one child span. The p99 bottleneck and leaf-culprit logic must select that child.

### F7 — Constant predictor baseline

Score a predictor that always outputs the most frequent service. Compare accuracy and Brier. If the system does not beat it, the signal is not adding value.

### F8 — Determinism

Run the same case twice with the same code. Compare ranked candidates and scores. They must be identical (apart from timestamps).

### F9 — Empty modality

Run `logs-only` on a case with zero snippets. The system must return a defined result, not crash.

### F10 — Cross-suite transfer

Run the same code on `RE2-TT`, `RE3-OB`, `RE3-TT`. If performance collapses, the claim does not generalize beyond `RE2-OB`.

---

## 18. Known defects and risks

Ordered by impact on correctness claims.

| ID | Severity | Defect | Evidence | Fix |
|---|---|---|---|---|
| D1 | 🔴 | Cached artifacts predate prompt anonymization and trace sampling fixes | 359/362 prompts contain case ID | Re-run all 90 cases; add `code_commit` to artifact |
| D2 | 🔴 | Confidence is miscalibrated; floor 0.50 and cap 0.95 | Brier 0.2655 vs 0.2054; 52.3% accuracy above 0.95 | Calibrate on a holdout; remove floor; use reliability curve |
| D3 | 🔴 | Category collapse to EC-2 | 78/90 EC-2; 1/15 `mem` → EC-1 | Fix likelihood weights; use metric evidence for EC-1 |
| D4 | 🔴 | Robust z explodes on near-zero MAD | `redis_diskio` robust_z=67,174; `currencyservice_socket` z=5,000 | Use `1.4826*MAD`; clamp; require minimum variance |
| D5 | 🟠 | Service aliasing `frontendservice` ≠ `frontend` | trace_only=['frontendservice'] | Canonical service-name map |
| D6 | 🟠 | TimeToOOM never computed | 0/920 alerts | Add cgroup limit series or drop the metric |
| D7 | 🟠 | 2 artifacts have no CSV row | `re2tt_ts-auth-service_cpu_2`, `re3tt_ts-auth-service_f3_3` | Reconcile or delete |
| D8 | 🟠 | Fault normalization inconsistent (`mem` vs `MEM`) | 1 failing test | Normalize at the boundary |
| D9 | 🟠 | Baseline cache has 14 templates and short-circuits live training | `drain3_baseline_templates.json` | Delete cache or validate coverage |
| D10 | 🟠 | Topological dampening demotes a true caller culprit | fusion ×0.60 rule | Only dampen when callee has stronger evidence |
| D11 | 🟠 | Logs contribute nothing for resource faults | 0 snippets in CPU case | Accept and document; or add latency log patterns |
| D12 | 🟠 | UI scoreboard reads wrong JSON keys | `top1_pct` vs `overall_top1` | Fix keys; render `undefined` guard |
| D13 | 🟠 | Dashboard shows stale cached artifacts | no cache invalidation | Show `created_at`, code commit, and a stale badge |
| D14 | 🟡 | Prompt budget 2400 vs claim 2500 | code/docs mismatch | Pick one constant |
| D15 | 🟡 | `.env` contains a real API key | local file | Rotate/remove; keep `.env.example` only |
| D16 | 🟡 | Dead env vars `LOGSAGE_*` | grep finds no readers | Wire or delete |
| D17 | 🟡 | `evaluate_rcaeval.py` crashes without `--output-csv` | `args.output_csv.parent` | Guard the path |
| D18 | 🟡 | Stale model label "LIVE GEMINI 1.5 PRO" | `evaluate_rcaeval.py:101` | Use configured model name |

---

## 19. Reproduction runbook

### 19.1 Environment

```bash
python -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env        # fill GEMINI_API_KEY only if using live LLM
```

### 19.2 Unit tests

```bash
pytest tests/ -v
```

Expected current state: `47 passed, 1 failed` (fault-case normalization).

### 19.3 Download the benchmark

```bash
python scripts/download_rcaeval.py --dry-run
python scripts/download_rcaeval.py
```

Expected: 239 complete tri-modal cases.

### 19.4 Run the four ablations

```bash
python -m src.pipeline.orchestrator \
  --suite RE2-OB \
  --modes logs-only logs-metrics logs-traces fusion \
  --output-dir results
```

This writes `results/ablation_*.csv`, `results/paper_table.md`, and per-case artifacts.

### 19.5 Verify end to end

```bash
python scripts/verify_pipeline.py
```

Expected after regenerating artifacts:

```
17/17 checks passed
```

With the current cached artifacts:

```
15/17
  FAIL every artifact has a CSV row (2)
  FAIL no artifact prompt leaks the case id (359)
```

### 19.6 Recompute one case without cache

```bash
python scripts/analyze.py --run-id re2ob_checkoutservice_cpu_1 --mode fusion --verbose
```

Or via API:

```bash
uvicorn src.api.app:app --port 8000
curl -X POST localhost:8000/api/analyze \
  -H 'content-type: application/json' \
  -d '{"case_id":"re2ob_checkoutservice_cpu_1","mode":"fusion","force_recompute":true}'
```

### 19.7 Dashboard

```bash
uvicorn src.api.app:app --port 8000
# open http://localhost:8000
```

### 19.8 Live stack (optional)

```bash
docker compose up -d
python scripts/check_stack.py
python scripts/run_pipeline.py --scenario normal
python scripts/inject_fault.py --list
python scripts/inject_fault.py --scenario oom_cart
python scripts/collect_telemetry.py --meta-file data/runs/failed/<run>_meta.json
```

---

## 20. Glossary

| Term | Meaning |
|---|---|
| **Artifact** | Self-contained JSON for one `(case, mode)` with evidence, prompt, report, score |
| **Baseline** | Pre-injection telemetry used as normal reference |
| **Culprit** | Service ranked first by a signal or by fusion |
| **Drain3** | Log template mining algorithm |
| **Evidence** | Localized anomaly selected for the prompt |
| **Fusion** | Weighted combination of log, metric, and trace scores |
| **Incident** | Telemetry from `inject_time` onward |
| **Leaf culprit** | Deepest anomalous span in a trace |
| **MAD** | Median absolute deviation |
| **MRR** | Mean reciprocal rank |
| **Novel template** | Drain3 template absent from the baseline |
| **RCA** | Root cause analysis |
| **Replay** | Evaluating from stored parquet, not live services |
| **Self-duration** | Span duration minus children's durations |
| **Snapshot** | `TelemetrySnapshot`, the typed incident record |
| **Top@k** | Whether ground truth appears in the first k candidates |
| **z-score** | Standard deviations from baseline mean |

---

## 21. Appendix A — Artifact schema

Abridged example:

```json
{
  "schema_version": "1.0.0",
  "case_id": "re2ob_checkoutservice_cpu_1",
  "mode": "fusion",
  "engine_type": "TELEMETRY_RAG_LOCAL",
  "created_at": "2026-11-12T14:45:32.787317+00:00",
  "source": "cache",
  "case_metadata": {
    "run_id": "re2ob_checkoutservice_cpu_1",
    "scenario": "rcaeval_cpu",
    "log_services_raw": 10,
    "metric_series_raw": 72,
    "traces_raw": 2924
  },
  "stage_timings": [
    {"stage": "ingesting", "status": "ok", "duration_ms": 1200.0},
    {"stage": "retrieving", "status": "ok", "duration_ms": 300.0},
    {"stage": "diagnosing", "status": "fallback", "duration_ms": 1.0},
    {"stage": "scoring", "status": "ok", "duration_ms": 1.0}
  ],
  "evidence": {
    "log_evidences": {},
    "metric_evidence": {"alerts": [], "has_oom_alert": false},
    "trace_evidence": {"total_traces": 2924, "total_spans": 6000},
    "summary_counts": {}
  },
  "fusion": {
    "ranked_services": [
      {"service": "currencyservice", "score": 18.225, "votes": ["METRICS", "TRACES"]}
    ],
    "signal_votes": {"LOGS": [], "METRICS": ["..."], "TRACES": ["currencyservice"]},
    "category_likelihoods": {"EC-2: ...": 10.6},
    "hypothesized_category": "EC-2: Flaky / Intermittent Latency",
    "initial_confidence": 0.98
  },
  "prompt": {
    "max_tokens": 2400,
    "total_tokens": 1157,
    "logs_tokens": 0,
    "metrics_tokens": 392,
    "traces_tokens": 724,
    "prompt_preview": "### Incident Reference ID: re2ob_checkoutservice_cpu_1 ..."
  },
  "report": {"root_cause_service": "currencyservice", "confidence_score": 0.98},
  "evaluation": {
    "top1_hit": false,
    "top3_hit": false,
    "mrr": 0.0,
    "fault_match": true,
    "confidence": 0.98,
    "prompt_tokens": 1157
  }
}
```

Red flags visible in this real artifact:

- `prompt_preview` starts with the raw case ID (leak);
- `initial_confidence` 0.98 is not reproducible by current code;
- `source: "cache"`;
- `traces_raw: 2924` but `total_spans: 6000` (old span-level sampling);
- `logs_tokens: 0` despite `logs` being evaluated.

---

## 22. Appendix B — File map

| Path | Responsibility |
|---|---|
| `src/pipeline/orchestrator.py` | batch orchestration, artifact cache, CSV |
| `scripts/rcaeval_adapter.py` | parquet/CSV/JSON → `TelemetrySnapshot` |
| `src/log_processor/filter.py` | keywords, probe filter, asymmetric context |
| `src/log_processor/miner.py` | Drain3 baseline and novelty |
| `src/log_processor/processor.py` | per-service snippets and token budget |
| `src/metrics_processor/processor.py` | z/MAD/slope/OOM detection |
| `src/trace_processor/processor.py` | DAG, depth, self-duration, leaf culprit |
| `src/fusion/engine.py` | scoring, topology, category, confidence |
| `src/rag/retriever.py` | orchestrates the three processors + prompt |
| `src/llm/prompt.py` | token budget and prompt rendering |
| `src/llm/client.py` | Gemini structured output |
| `src/eval/metrics.py` | Top@k, MRR, aggregation |
| `src/eval/taxonomy.py` | fault-label mapping |
| `src/schemas/*` | typed data model |
| `src/api/*` | FastAPI, WebSocket, dashboard |
| `scripts/verify_pipeline.py` | end-to-end integrity checks |
| `results/ablation_*.csv` | per-case scores |
| `results/artifacts/*/*.json` | full diagnostic records |
| `data/ground_truth/rcaeval/` | benchmark cases |
| `data/baselines/drain3_baseline_templates.json` | cached log baseline |
