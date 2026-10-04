# ObservaSage

> **Multi-Signal AI Root Cause Analysis** — Extending LogSage ([arXiv:2506.03691](https://arxiv.org/abs/2506.03691)) with Logs + Metrics + Distributed Traces

---

## 1. Project Mission & Overview

When microservices or CI/CD pipelines fail, identifying the culprit service quickly is critical. The current state-of-the-art paper **LogSage** (*ByteDance & ECNU, 2025*) diagnoses failures automatically using **logs alone** and achieves high accuracy on standard application bugs.

**However, logs have fundamental blind spots.** ObservaSage targets the **5 edge cases where logs alone fail**:

| # | Edge Case Category | Why Logs Fail | How ObservaSage Solves It |
|---|---|---|---|
| **EC-1** | **OOM / Resource Exhaustion** | Process killed unceremoniously with `exit code 137`. The dead process cannot write a log explaining why it died. | **Prometheus metrics**: Pre-crash memory slope ($dM/dt$) and cgroup limit alerts. |
| **EC-2** | **Flaky / Intermittent Latency** | Logs look completely normal or report identical output to passing runs. | **Jaeger traces**: Span duration percentiles ($p99$) and client timeout waterfalls. |
| **EC-3** | **Cascading Dependency Failure** | Root caller (e.g. `frontend`) logs a generic 500 error; intermediate services log connection errors. Real culprit is buried. | **Jaeger trace DAG**: Depth-First Search (DFS) traverses the span tree to identify the **leaf culprit span**. |
| **EC-4** | **Silent Data Corruption** | Pipeline exits with code 0. No errors logged, but business data is corrupt (e.g. cart cleared to $0.00). | **Jaeger trace attributes & Redis inspection**: Detects missing cart payloads and empty orders. |
| **EC-5** | **Infrastructure Drift / Partition** | App logs only report vague connection timeouts. The problem is in the underlying network/host. | **Prometheus cAdvisor & network status**: Detects bridge disconnects and CFS CPU throttling. |

---

## 2. Architecture & Data Flow

```
┌────────────────────────────────────────────────────────────────────────┐
│                      Microservices Under Test                          │
│               OpenTelemetry Astronomy Shop (10 Services)               │
│        frontend ➔ checkout ➔ payment / shipping / cart (Redis)        │
└────────────────────────────────────────────────────────────────────────┘
          │ (logs)               │ (metrics)               │ (traces)
          ▼                      ▼                         ▼
    Docker Daemon           Prometheus / cAdvisor       Jaeger Tracing
   (stdout/stderr)             (Ports 9090 / 8081)       (Port 16686)
          │                      │                         │
          └──────────────────────┼─────────────────────────┘
                                 │
                                 ▼
                     scripts/collect_telemetry.py
              Harvests time-windowed snapshot [t_start, t_end]
                                 │
                                 ▼
                    data/runs/<status>/<run_id>.json
                                 │
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
src/log_processor/     src/metrics_processor/   src/trace_processor/
• Drain3 Diffing       • Z-score Anomaly        • Span DAG Builder
• Keyword Filter       • Memory Slope (dM/dt)   • DFS Leaf Culprit
• m=3, n=7 Expansion   • cgroup Limit Alerts    • Error Span Locator
        │                        │                        │
        └────────────────────────┼────────────────────────┘
                                 │
                                 ▼
                          src/fusion/
               Multi-Signal Evidence Triangulation
                   (Budget: ≤ 2,500 Tokens)
                                 │
                                 ▼
                            src/llm/
                  Gemini 1.5 Pro (Structured JSON)
                                 │
                                 ▼
                         scripts/analyze.py
                    Final Root Cause Report (JSON)
```

---

## 3. Directory Layout & Module Responsibilities

```
final_year_project/
├── docker-compose.yml              # 16-container stack: microservices + Prometheus + Jaeger + cAdvisor + flagd
├── pyproject.toml                  # Python 3.11+ dependencies managed by uv
├── .env.example                    # Template for GEMINI_API_KEY and service ports
│
├── config/                         # Core algorithm and telemetry configurations
│   ├── drain3.ini                  # Drain3 log miner config (masking regex for IPs, UUIDs, timestamps)
│   ├── prometheus.yml              # Scrape jobs for cAdvisor (:8080) and OTel Collector (:8889)
│   ├── otel-collector-config.yml   # OTel routing to Jaeger (4317) and Prometheus
│   └── demo.flagd.json             # Dynamic OpenFeature feature flags for fault injection
│
├── scripts/                        # Automated CLI tools
│   ├── check_stack.py              # Health check for all 15 containers + 4 HTTP endpoints
│   ├── run_pipeline.py             # CI/CD test simulator (Browse ➔ Cart ➔ Checkout)
│   ├── inject_fault.py             # 28 fault scenarios across EC-1 to EC-5, plus 'clear'
│   ├── collect_telemetry.py        # Harvests logs, metrics, and traces for exact run window
│   └── analyze.py                  # Standalone CLI to run Root Cause Analysis on any run
│
├── src/                            # Core Framework Modules
│   ├── schemas/                    # Pydantic v2 data models
│   │   ├── telemetry.py            # Raw logs, metric series, trace spans, and snapshot models
│   │   ├── evidence.py             # LogSnippet (m=3, n=7), MetricAlert, and TraceEvidence
│   │   └── rca_report.py           # Structured Gemini output contract: FailureCategory, confidence, remediation
│   ├── log_processor/              # LogSage Paper Baseline Implementation
│   │   ├── filter.py               # Exact paper keywords & asymmetric context expansion (m=3, n=7)
│   │   ├── miner.py                # Drain3 template clustering & baseline novel template diffing
│   │   └── processor.py            # End-to-end log processor with token budget management
│   ├── metrics_processor/          # Prometheus Z-score detector & memory slope (dM/dt) analyzer
│   ├── trace_processor/            # Jaeger span DAG builder & DFS leaf culprit locator
│   ├── fusion/                     # Multi-signal triangulation engine (enforces ≤2,500 token budget)
│   ├── llm/                        # Gemini 1.5 Pro client with structured JSON output & offline fallback
│   └── api/                        # FastAPI backend and web visualization dashboard
│
├── data/
│   ├── runs/
│   │   ├── success/                # Healthy baseline runs (requires x=3 runs for Drain3 training)
│   │   └── failed/                 # Captured telemetry for failed pipeline runs
│   ├── baselines/                  # Trained Drain3 templates (drain3_baseline_templates.json)
│   └── ground_truth/               # Annotated benchmark datasets across EC-1 to EC-5
│
└── tests/                          # Automated Pytest unit test suites
    ├── test_schemas.py             # Telemetry, evidence, and report serialization tests
    └── test_log_processor.py       # Drain3 mining, template diffing, and asymmetric expansion tests
```

---

## 4. Quick Start (Get Running in 3 Minutes)

### 1. Prerequisites
- Linux OS with Docker & Docker Compose
- [`uv`](https://docs.astral.sh/uv/) (fast Python package manager)
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### 2. Setup Environment
```bash
cp .env.example .env       # (Optional) Add your GEMINI_API_KEY
uv sync                    # Installs all Python dependencies into .venv
```

> **Note on Commands:** Always run scripts using `uv run python scripts/<script_name>.py`. You do not need to activate the virtual environment manually.

### 3. Start the Observability Stack
```bash
docker compose up -d
```

### 4. Verify Stack Health
```bash
uv run python scripts/check_stack.py
```
Ensure all 15 containers and all 4 HTTP endpoints report **HEALTHY**.

---

## 5. End-to-End Walkthrough: Triggering a Failure & Running RCA

### Step 1: Establish Healthy Baselines ($x=3$ Runs)
The LogSage algorithm requires 3 clean success runs to train Drain3 templates and compute normal metric distributions:
```bash
uv run python scripts/run_pipeline.py --scenario baseline_1
uv run python scripts/collect_telemetry.py --meta-file data/runs/success/<run_id_1>_meta.json

# (Repeat 3 times — precomputed templates are saved to data/baselines/drain3_baseline_templates.json)
```

### Step 2: Inject a Real Fault (e.g. EC-3 Cascading Crash)
Crash the `paymentservice` container:
```bash
uv run python scripts/inject_fault.py --scenario crash_payment
```

### Step 3: Run the CI/CD Pipeline Simulator
Execute the integration test. The checkout step will fail with a 500 error:
```bash
uv run python scripts/run_pipeline.py --scenario ec3_crash_payment
```
A metadata file is saved to: `data/runs/failed/<failed_run_id>_meta.json`.

### Step 4: Harvest Failure Telemetry
Extract logs, Prometheus metrics, and Jaeger traces for the exact window of the failure:
```bash
uv run python scripts/collect_telemetry.py --meta-file data/runs/failed/<failed_run_id>_meta.json
```
Saves: `data/runs/failed/<failed_run_id>_telemetry.json`.

### Step 5: Run Automated Root Cause Analysis
Run the ObservaSage diagnostic engine on the harvested telemetry:
```bash
uv run python scripts/analyze.py --run-id <failed_run_id>
```

ObservaSage will:
1. Parse logs with Drain3 and strip out all normal baseline templates.
2. Filter for LogSage error keywords (`fail`, `unavailable`, `dial tcp`).
3. Apply asymmetric context expansion ($m=3$ lines before, $n=7$ lines after).
4. Run Gemini 1.5 Pro to diagnose the guilty service, confidence score, and remediation steps.
5. Print a visual report card and save `data/runs/failed/<failed_run_id>_rca.json`.

### Step 6: Restore Stack to Healthy
```bash
uv run python scripts/inject_fault.py --scenario clear
```

---

## 6. How to Inspect & Check Every Component

Use this reference section to inspect any individual component or signal:

### A. How to Check Containers & Logs
```bash
# 1. View container status
docker compose ps

# 2. View real-time logs for a specific service
docker logs -f paymentservice
docker logs --tail 100 cartservice

# 3. Check container cgroup memory limits
docker stats --no-stream
```

### B. How to Check Traces in Jaeger
- **URL**: [http://localhost:16686](http://localhost:16686)
- **Step 1**: In the left sidebar, click the **Service** dropdown.
- **Step 2**: Select `unknown_service:frontend` or `unknown_service:checkoutservice`.
- **Step 3**: Click **Find Traces**.
- **Step 4**: Click any trace to view the waterfall / span tree. Red dots indicate error spans.
- **Step 5**: Expand any span to see HTTP status codes, latency durations, and attached exception logs.

### C. How to Check Metrics in Prometheus
- **URL**: [http://localhost:9090](http://localhost:9090)
- **Step 1**: Click **Status** $\to$ **Targets** and verify `cadvisor` and `otel-collector` are **UP**.
- **Step 2**: Click **Graph** and enter any PromQL query:
  - **Memory Usage by Service**:
    ```promql
    container_memory_rss{container_label_com_docker_compose_service="cartservice"}
    ```
  - **Container Memory Limit**:
    ```promql
    container_spec_memory_limit_bytes{container_label_com_docker_compose_service="cartservice"}
    ```
  - **CPU Utilization Rate**:
    ```promql
    rate(container_cpu_usage_seconds_total{container_label_com_docker_compose_service="checkoutservice"}[1m])
    ```
- **Step 3**: Switch to the **Graph** tab to view the live time-series chart.

### D. How to Check Container Hardware in cAdvisor
- **URL**: [http://localhost:8081](http://localhost:8081)
- Click on **Docker Containers** $\to$ select any container (e.g., `cartservice`).
- Inspect live graphs of CPU usage, memory breakdown (RSS vs cache), and network throughput.

### E. How to Check Feature Flags in flagd
- **Config File**: [`config/demo.flagd.json`](file:///home/sivakumar/Documents/final_year_project/config/demo.flagd.json)
- Check live flag status over HTTP:
  ```bash
  curl -s http://localhost:8013/flagd.evaluation.v1.Service/ResolveBoolean \
    -H "Content-Type: application/json" \
    -d '{"flagKey": "paymentServiceFailure"}'
  ```

### F. How to Run Automated Unit Tests
Verify all schemas, Drain3 template miners, and LogSage filters:
```bash
uv run pytest tests/ -v
```

---

## 7. Fault Injection Scenarios Reference

View all available fault scenarios with:
```bash
uv run python scripts/inject_fault.py --list
```

| Edge Case Category | Available Scenarios | What it Simulates |
|---|---|---|
| **EC-1: OOM / Resource Exhaustion** | `oom_cart`, `oom_checkout`, `oom_payment`, `cpu_throttle_cart`, `cpu_throttle_checkout` | Restricts container memory limits using Linux cgroup v2 until kernel OOM-killer fires (`SIGKILL 137`). |
| **EC-2: Flaky Latency** | `latency_payment`, `latency_cart`, `latency_currency`, `packet_loss_payment`, `flag_load_spike` | Injects network latency and packet loss using Linux `tc netem` or load generator floods. |
| **EC-3: Cascading Crash** | `crash_payment`, `crash_currency`, `crash_redis`, `crash_shipping`, `restart_loop_checkout`, `flag_payment_failure` | Stops downstream dependencies or triggers unhandled exceptions, causing cascading upstream timeouts. |
| **EC-4: Silent Data Corruption** | `redis_flush`, `redis_corrupt_cart`, `flag_cart_failure`, `flag_product_failure` | Wipes or corrupts cart data in Redis. The checkout exits 0 but orders empty/invalid carts. |
| **EC-5: Infrastructure Drift** | `net_partition_cart`, `net_partition_payment` | Disconnects containers from the Docker bridge network to simulate cloud network partitions. |
| **Restore** | `clear` | Reverts all containers, memory limits, network interfaces, and feature flags to healthy defaults. |

---

## 8. Academic References

1. **LogSage Paper**: Xu et al., *"LogSage: An LLM-Based Framework for CI/CD Failure Detection and Remediation with Industrial Validation"*, [arXiv:2506.03691](https://arxiv.org/abs/2506.03691), ByteDance & ECNU, 2025.
2. **Drain3 Template Miner**: He et al., *"Drain: An Online Log Parsing Approach with Fixed Depth Tree"*, IEEE ICWS, 2017.
3. **OpenTelemetry Demo**: Official OpenTelemetry Astronomy Shop Benchmark — [https://github.com/open-telemetry/opentelemetry-demo](https://github.com/open-telemetry/opentelemetry-demo).
