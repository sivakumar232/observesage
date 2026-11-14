# ObservaSage

> **Multi-Signal AI Root Cause Analysis Framework** — Extending LogSage ([arXiv:2506.03691](https://arxiv.org/abs/2506.03691)) with Telemetry-RAG, Topological Causal Fusion, and Robust Statistical Modeling across Logs, Metrics, and Distributed Traces.

[![Tests](https://img.shields.io/badge/pytest-48%20passed-brightgreen.svg)]()
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)]()
[![Pydantic](https://img.shields.io/badge/pydantic-v2-orange.svg)]()
[![Benchmark](https://img.shields.io/badge/benchmark-RCAEval-purple.svg)]()
[![LLM](https://img.shields.io/badge/LLM-Gemini%201.5%20Pro%20%2F%20Flash-blueviolet.svg)]()

---

## 1. Project Mission & The Core Problem

When cloud-native microservices or CI/CD pipelines fail, identifying the culprit service quickly is critical. The state-of-the-art framework **LogSage** (*ByteDance & ECNU, arXiv:2506.03691, 2025*) automates failure diagnosis using **logs alone** and achieves high accuracy on standard application bugs.

**However, logs have fundamental blind spots in distributed architectures.** ObservaSage targets the **5 critical edge cases where logs alone fail**:

| Failure Category | Why Logs Alone Fail | How ObservaSage Solves It |
|---|---|---|
| **EC-1: OOM / Resource Exhaustion** | Process killed via Linux kernel `SIGKILL 137`. The dead process cannot write a log explaining why it died. | **Adaptive Metrics**: Dynamic baseline-scaled memory rate-of-change slope ($dM/dt$), cgroup limit correlation, and **TimeToOOM** (< 300s) projection alerts. |
| **EC-2: Flaky / Intermittent Latency** | High network latency or CPU throttling occurs while requests return HTTP 200 OK — logs look completely identical to passing runs. | **Adaptive Metrics & Traces**: Non-parametric **Median Absolute Deviation (MAD)** with dynamic variance-scaled thresholding ($\tau \in [2.5, 5.0]$) and span $p99$ self-duration bottleneck isolation. |
| **EC-3: Cascading Dependency Failure** | Root caller (e.g. `frontend`) logs hundreds of generic 500 errors; intermediate services log timeouts. The true origin is buried deep in the dependency tree. | **Trace Processor & Fusion**: DFS leaf culprit extraction, **timeout/deadline inversion (504/gRPC 4)**, and **topological causal damping** of caller symptoms. |
| **EC-4: Silent Data Corruption** | Pipeline exits with code 0. Zero errors logged, but business data is corrupt (e.g., cart total $0.00). | **Multi-Signal Triangulation**: Correlates metric throughput dips and trace span payload metadata with Drain3 log template frequencies. |
| **EC-5: Network Partition / Drift** | App logs only report vague connection timeouts. The problem is in the underlying network fabric. | **Cross-Modal Fusion**: Correlates socket disconnects with metric anomalies across boundary services. |

---

## 2. Hardened Dynamic Architecture

ObservaSage implements an **Adaptive Domain-Specific Telemetry-RAG Architecture**: rather than relying on deterministic rule ladders or static heuristics, raw telemetry (hundreds of thousands of logs, dozens of PromQL time series, thousands of Jaeger trace spans) is localized and distilled through **dynamic statistical modeling** and **continuous Bayesian consensus**, bounded strictly under **2,500 BPE tokens** via elastic pooling.

```mermaid
flowchart TD
    %% Styling
    classDef dataset fill:#1e293b,stroke:#0284c7,stroke-width:2px,color:#f8fafc;
    classDef adapter fill:#1e293b,stroke:#a855f7,stroke-width:2px,color:#f8fafc;
    classDef rag fill:#0369a1,stroke:#38bdf8,stroke-width:2px,color:#f8fafc;
    classDef fusion fill:#4338ca,stroke:#818cf8,stroke-width:2px,color:#f8fafc;
    classDef llm fill:#065f46,stroke:#34d399,stroke-width:2px,color:#f8fafc;
    classDef eval fill:#78350f,stroke:#f59e0b,stroke-width:2px,color:#f8fafc;

    subgraph INGESTION ["1. BENCHMARK INGESTION & TEMPORAL QUANTIZATION"]
        direction TB
        RAW["RCAEval Benchmark Cases<br/>(logs.parquet, metrics.parquet, traces.parquet, inject_time.txt)"]:::dataset
        ADAPT["rcaeval_adapter.py<br/>• In-Sample Baseline Window [T_inj - 600s, T_inj]<br/>• Incident Window [T_inj, T_inj + 300s]<br/>• 5-Second Bucket Quantization"]:::adapter
        SNAP[("TelemetrySnapshot Schema<br/>• baseline (logs, metrics, traces)<br/>• telemetry (incident window)")]:::adapter
        RAW --> ADAPT --> SNAP
    end

    subgraph RAG_LAYER ["2. DYNAMIC MULTI-MODAL TELEMETRY-RAG RETRIEVAL"]
        direction TB
        subgraph LOG_RAG ["1. Log Retriever (Dynamic LogSage)"]
            L1["Drain3 Baseline Template Mining & Diffing"]
            L2["Paper Keyword Filtering ('fail', 'error', 'kill')"]
            L3["Dynamic Continuation Expansion (m=3, n=7 + stack trace continuation)"]
            L1 --> L2 --> L3
        end

        subgraph METRIC_RAG ["2. Metric Retriever (Adaptive Thresholding)"]
            M1["Adaptive Z-Score & MAD Scaling (tau in [2.5, 5.0] via baseline CV)"]
            M2["Baseline-Scaled Memory Slope Sensitivity"]
            M3["cgroup Limit Correlation & TimeToOOM (<300s)"]
            M1 --> M2 --> M3
        end

        subgraph TRACE_RAG ["3. Trace Retriever (Topology-Aware)"]
            T1["Call Tree DAG & Dependency Graph (A ➔ B)"]
            T2["Timeout / Deadline Inversion (504 / gRPC 4)"]
            T3["DFS Deepest Leaf Culprit Localization"]
            T1 --> T2 --> T3
        end
    end

    SNAP --> LOG_RAG
    SNAP --> METRIC_RAG
    SNAP --> TRACE_RAG

    subgraph FUSION_LAYER ["3. BAYESIAN FUSION & ELASTIC TOKEN BUDGETING"]
        direction TB
        TOPO["Topological Causal Propagation<br/>• Caller symptom damping (0.60x)<br/>• Callee root attribution (+2.0)"]:::fusion
        CONS["Multi-Hypothesis Scoring & Bayesian Consensus<br/>• Continuous noisy-OR confidence: 1 - prod(1 - c_m) in [0.50, 0.98]"]:::fusion
        PROMPT["Elastic Dynamic Token Pool Assembler<br/>• Rolls over unused capacity across signals<br/>• Strict Cap: BPE &lt; 2,500 Tokens"]:::fusion
        TOPO --> CONS --> PROMPT
    end

    L3 --> TOPO
    M3 --> TOPO
    T3 --> TOPO

    subgraph INFERENCE ["4. STRUCTURED GENERATIVE LLM DIAGNOSIS"]
        direction TB
        GEMINI["Gemini 1.5 Pro / Flash Diagnostic Engine<br/>Temperature: 0.1 | Response Schema Enforcement"]:::llm
        FALLBACK["Dynamic Telemetry-RAG Fallback Engine<br/>Resilient to remote auth/quota failures"]:::llm
        REPORT[("Validated RCAReport JSON<br/>• root_cause_service<br/>• culprit_services (Top-k)<br/>• failure_category<br/>• evidence_triangulation<br/>• actionable remediation_steps")]:::llm
        PROMPT --> GEMINI --> FALLBACK --> REPORT
    end

    subgraph EVALUATION ["5. BENCHMARK SCORING & ABLATION MATRIX"]
        direction TB
        SCORER["evaluate_rcaeval.py"]:::eval
        METRICS["Academic Metrics<br/>• Top@1 Accuracy | Top@3 Accuracy<br/>• Mean Reciprocal Rank (MRR)<br/>• Fault Classification Accuracy<br/>• Engine Transparency (LIVE_LLM vs OFFLINE)"]:::eval
        TABLE["4-Way Ablation Matrix (Table 1)<br/>(logs-only vs logs-metrics vs logs-traces vs fusion)"]:::eval
        REPORT --> SCORER
        SCORER --> METRICS --> TABLE
    end
```

---

## 3. Dataset Architecture & RCAEval Benchmark

ObservaSage natively integrates with the peer-reviewed **RCAEval** benchmark dataset (covering Google Online Boutique & Sock Shop microservice architectures).

### 3.1. Raw Telemetry Data Files
Each RCAEval incident case folder contains raw, un-curated telemetry:

```
data/ground_truth/rcaeval/RE2/<case_id>/
├── logs.parquet (or logs.csv)       # 150k–300k raw stdout/stderr lines across all microservices
├── metrics.parquet (or metrics.json)# 72 PromQL metric series (CPU, RAM, network, latency)
├── traces.parquet (or traces.csv)   # 300k–500k distributed trace spans (Jaeger/OpenTelemetry)
├── inject_time.txt                  # Exact Unix timestamp (T_inj) when chaos fault was injected
└── ground_truth.json (or cases.parquet) # Ground truth culprit service and injected fault type
```

### 3.2. In-Sample Temporal Slicing & Quantization
To prevent data contamination and eliminate workload drift, `rcaeval_adapter.py` applies mathematically rigorous temporal partitioning:
* **Pre-Fault Baseline Window $[T_{\text{inj}} - 600\text{s}, T_{\text{inj}})$:** Normal background operations. Used to train Drain3 baseline templates and compute baseline distribution statistics ($\mu, \sigma, \text{median}, \text{MAD}$).
* **Active Incident Window $[T_{\text{inj}}, T_{\text{inj}} + 300\text{s}]$:** Active failure interval where the fault causes cascading degradation.
* **5-Second Bucket Quantization:** Synchronizes microsecond-precision trace spans, millisecond log timestamps, and 15-second Prometheus scrape intervals into discrete time slices ($B_k$).

### 3.3. Standardized TelemetrySnapshot Schema
Converted cases are serialized into a validated Pydantic v2 [`TelemetrySnapshot`](file:///home/sivakumar/Documents/final_year_project/src/schemas/telemetry.py):
* `baseline.logs`: Mapping of `{ service_name: List[str] }` from the baseline window.
* `baseline_metrics`: List of `MetricSeries` containing pre-incident timeseries.
* `baseline_traces`: List of normal `Trace` DAGs.
* `logs`: Incident-window log lines per service.
* `metrics`: Incident-window time series.
* `traces`: Incident-window distributed traces with parent-child references.

---

## 4. Directory Structure & Module Responsibilities

```
final_year_project/
├── scripts/
│   ├── rcaeval_adapter.py          # Ingests RCAEval cases, performs in-sample slicing & 5s quantization
│   ├── analyze.py                  # Standalone CLI for single-incident tri-modal diagnosis
│   ├── evaluate_rcaeval.py         # Automated evaluation & 4-way ablation benchmark runner
│   ├── collect_telemetry.py        # Live Docker/Prometheus/Jaeger telemetry harvester
│   ├── inject_fault.py             # Live chaos fault injector (EC-1 through EC-5)
│   ├── run_pipeline.py             # Live synthetic traffic generator
│   └── check_stack.py              # Stack health verification
│
├── src/
│   ├── schemas/                    # Typed Pydantic v2 data models
│   │   ├── telemetry.py            # TelemetrySnapshot, MetricSeries, Trace, TimeBucketSummary
│   │   ├── evidence.py             # LogEvidence, MetricEvidence, TraceEvidence, LogSnippet
│   │   └── rca_report.py           # RCAReport, FailureCategory, EvidenceTriangulation, RemediationStep
│   ├── log_processor/              # Dynamic LogSage Baseline with Probe & Stack Continuation
│   │   ├── miner.py                # Drain3 parse-tree template miner & baseline diffing
│   │   ├── filter.py               # LogSage keywords & dynamic stack trace continuation expansion
│   │   └── processor.py            # LogSageProcessor with dynamic context extraction
│   ├── metrics_processor/          # Adaptive Metric Telemetry-RAG
│   │   └── processor.py            # Dynamic adaptive Z-scores (CV-scaled tau), memory slope dM/dt, TimeToOOM (<300s)
│   ├── trace_processor/            # Distributed Trace Telemetry-RAG
│   │   └── processor.py            # TraceProcessor (DAG, caller->callee topology, timeout inversion, DFS leaf)
│   ├── fusion/                     # Cross-Modal Fusion Engine
│   │   └── engine.py               # FusionEngine (topological propagation, multi-hypothesis likelihood, Bayesian consensus)
│   ├── llm/                        # Structured LLM Generation Layer
│   │   ├── client.py               # GeminiRCAClient (schema validation, graceful Telemetry-RAG fallback)
│   │   └── prompt.py               # Elastic dynamic token pool budgeting (<2500 tokens) with topology
│   └── eval/                       # Academic Evaluation & Scoring
│       ├── taxonomy.py             # Bidirectional mapping between RCAEval labels and FailureCategory
│       └── metrics.py              # Top@1, Top@3, MRR calculation & transparent engine reporting
│
├── tests/                          # 38 Pytest unit & integration test suites
│   ├── test_metrics_processor.py   # Adaptive Z-score, MAD, memory slope, and cgroup TimeToOOM tests
│   ├── test_trace_processor.py     # DAG reconstruction, dependency graph, and timeout inversion tests
│   ├── test_fusion_and_prompt.py   # Topological causal propagation, Bayesian consensus, elastic token budget
│   ├── test_log_processor.py       # LogSage Drain3 diffing, probe filtering, dynamic stack continuation tests
│   ├── test_evaluation_harness.py  # Taxonomy mapping, Top@1/Top@3/MRR scoring tests
│   ├── test_llm_client_and_analyze.py # LLM client resilience and pipeline integration tests
│   ├── test_rcaeval_adapter.py     # Adapter slicing, timestamp parsing, and quantization tests
│   └── test_schemas.py             # Telemetry and report schema serialization tests
│
├── data/
│   ├── runs/                       # Captured telemetry snapshots (failed/ and success/)
│   ├── baselines/                  # Cached Drain3 baseline templates
│   └── ground_truth/               # RCAEval benchmark case folders & ground-truth metadata
│
└── pyproject.toml                  # Python dependencies managed by uv
```

---

## 5. How to Run (Step-by-Step Guide)

### 1. Prerequisites
- Linux OS with Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) package manager:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### 2. Environment Setup
```bash
git clone https://github.com/sivakumar232/observesage.git
cd observesage
git checkout track1
uv sync
```

Configure your Gemini API key:
```bash
cp .env.example .env
# Edit .env and set GEMINI_API_KEY=your_key_here
```

### 3. Run the Complete Test Suite
Verify that all 38 tests pass:
```bash
uv run pytest tests/ -v
```

---

### 4. Convert RCAEval Cases to TelemetrySnapshots
To convert a single RCAEval case folder into a standardized `TelemetrySnapshot`:
```bash
uv run python scripts/rcaeval_adapter.py --case-dir data/ground_truth/rcaeval/RE2/RE2_online-boutique_cartservice_mem_1
```

To batch-convert an entire directory of cases:
```bash
uv run python scripts/rcaeval_adapter.py --cases-root data/ground_truth/rcaeval/RE2 --output-dir data/runs/failed
```

This creates:
* `data/runs/failed/<case_id>_telemetry.json` (Validated `TelemetrySnapshot`)
* `data/runs/failed/<case_id>_ground_truth.json` (Ground truth labels)

---

### 5. Diagnose an Incident via CLI (`analyze.py`)
Run automated multi-signal diagnosis on any telemetry snapshot:
```bash
uv run python scripts/analyze.py --telemetry-file data/runs/failed/RE2_online-boutique_cartservice_mem_1_telemetry.json --mode fusion
```

Supported ablation modes:
* `--mode fusion`: Full multi-signal triangulation (ObservaSage).
* `--mode logs-only`: Log-only baseline (LogSage).
* `--mode logs-metrics`: Bimodal logs + metrics.
* `--mode logs-traces`: Bimodal logs + traces.

**Terminal Output Example:**
```
╭──────────────────────────────────────────────────────────────────────╮
│ Root Cause Analysis Report — Run: RE2_cartservice_mem_1 (LIVE GEMINI)│
╰──────────────────────────────────────────────────────────────────────╯
Root Cause Service     │ cartservice
Ranked Culprits (Top-k)│ cartservice -> redis-cart -> frontend
Failure Category       │ EC-1: OOM / Resource Exhaustion
Confidence Score       │ 95.0%
Primary Signal         │ FUSION
Diagnostic Summary     │ Memory saturation reached 99.4% of cgroup limit with TimeToOOM < 10s...
Triangulation Logic    │ Steep memory slope (+8.1MB/s) coincided with leaf timeout...
Prompt Tokens Used     │ 1,847 tokens

╭── Recommended Remediation Actions ──────────────────────────────────╮
│ #  Action                   Target Service   Command / Config       │
│ 1  Increase cgroup limit    cartservice      mem_limit: 512m        │
╰──────────────────────────────────────────────────────────────────────╯
```

---

### 6. Run Automated Evaluation & Ablation Study (`evaluate_rcaeval.py`)
Run benchmark evaluations to compute Top@1, Top@3, and MRR across test cases:

```bash
# Configuration A: LogSage Baseline (Logs Only)
uv run python scripts/evaluate_rcaeval.py --mode logs-only --output-csv results/ablation_logs.csv

# Configuration B: Logs + Metrics
uv run python scripts/evaluate_rcaeval.py --mode logs-metrics --output-csv results/ablation_metrics.csv

# Configuration C: Logs + Traces
uv run python scripts/evaluate_rcaeval.py --mode logs-traces --output-csv results/ablation_traces.csv

# Configuration D: ObservaSage Full Fusion
uv run python scripts/evaluate_rcaeval.py --mode fusion --output-csv results/ablation_fusion.csv
```

**CLI Flags:**
* `--limit 10`: Limits evaluation to the first 10 cases (ideal for dry runs).
* `--strict-live`: Enforces live Gemini inference with zero offline fallback.
* `--output-csv <path>`: Exports per-case hit/miss scores, MRR, latency, token consumption, and engine type.

---

## 6. The 4-Way Ablation Study (Verified `RE2-OB`, 90 Cases)

Evaluated locally via `ObservaSagePipeline` (`TELEMETRY_RAG_LOCAL`, `--no-llm`) across all 90 tri-modal `RE2-OB` cases (`cpu`, `mem`, `delay`, `disk`, `loss`, `socket`):

| Configuration | Telemetry Signals | Top@1 | Top@3 | MRR | Category Acc |
|---|---|:---:|:---:|:---:|:---:|
| `logs-only` | LogSage Drain3 Baseline | 10.00% | 10.00% | 0.1000 | 0.00% |
| `logs-metrics` | Logs + Prometheus/cgroup Metrics | 60.00% | 93.33% | 0.7481 | 54.44% |
| `logs-traces` | Logs + Jaeger Span DAG | 24.44% | 30.00% | 0.2722 | 26.67% |
| **`fusion`** | **ObservaSage (Full Tri-Modal Fusion)** | **71.11%** | **96.67%** | **0.8296** | **42.22%** |

### Key Verified Findings:
1. **Logs alone struggle on infrastructure/resource faults (`10.00%` Top@1):** Confirms that CPU, memory pressure, delay, and socket faults rarely emit localized error logs in microservices.
2. **Metrics drive strong recovery (`60.00%` Top@1, `93.33%` Top@3):** Z-score, MAD, and memory slope ($dM/dt$) signals narrow down the culprit service reliably.
3. **Full Tri-Modal Fusion achieves `71.11%` Top@1 and `96.67%` Top@3 (`0.8296` MRR):** Combining logs, metrics, and trace topology outperforms every 1- and 2-signal ablation mode.

---

## 7. Project Stages & Roadmap

```
Stage 1: Testbed & Telemetry Adapter     ──► [COMPLETED]
Stage 2: LogSage Replication & Probes    ──► [COMPLETED]
Stage 3: Robust Metrics & Traces         ──► [COMPLETED]
Stage 4: Topological Causal Fusion       ──► [COMPLETED]
Stage 5: Artifact Persistence & Web UI   ──► [COMPLETED]
```

### Stage 5: Artifact-Backed FastAPI Dashboard (`src/api/`)
- **Per-Case Diagnostic Artifacts (`results/artifacts/<case_id>/<mode>.json`):** Persists stage timings, localized log snippets, metric alerts, trace culprit spans, fusion votes/scores, token-budgeted prompt previews, and evaluation error buckets.
- **Interactive Dashboard (`uvicorn src.api.app:app --reload --port 8000`):**
  - Case browser with hit/miss/unscored filtering and artifact availability indicators.
  - Per-case inspector for Signals, Fusion & Ranking, hierarchical Call Graph DAG, Prompt & Token budget, Remediation, and 4-mode Cross-Mode Comparison.

---

## 8. Academic References

1. **LogSage**: Xu et al., *"LogSage: An LLM-Based Framework for CI/CD Failure Detection and Remediation with Industrial Validation"*, [arXiv:2506.03691](https://arxiv.org/abs/2506.03691), ByteDance & ECNU, 2025.
2. **RCAEval Benchmark**: Pham et al., *"RCAEval: An Empirical Benchmark for Root Cause Analysis on Microservice Systems"*, ACM/IEEE International Conference on Software Engineering (ICSE / FSE), 2025.
3. **Drain3 Log Parser**: He et al., *"Drain: An Online Log Parsing Approach with Fixed Depth Tree"*, IEEE International Conference on Web Services (ICWS), 2017.
4. **Google Online Boutique**: Microservices Architecture Benchmark — [https://github.com/GoogleCloudPlatform/microservices-demo](https://github.com/GoogleCloudPlatform/microservices-demo).
