# AGENTS.md — Development & Architecture Guidelines for ObservaSage

## 1. Project Mission & Identity
ObservaSage is an academic and production-oriented Root Cause Analysis (RCA) framework extending **LogSage** (*arXiv:2506.03691*, ByteDance, 2025). While LogSage uses logs exclusively, ObservaSage unifies **Logs, Metrics, and Distributed Traces** to diagnose failures in CI/CD pipelines and microservices, specifically targeting 5 edge cases where logs alone fail.

---

## 2. Core Behavioral & Architectural Principles

1. **Academic Fidelity to LogSage**:
   - For log preprocessing, strictly adhere to the LogSage paper specifications:
     - Log template mining using **Drain3**.
     - Retain exactly $x=3$ recent success runs for baseline log diffing.
     - Apply asymmetric context expansion: $m=3$ lines before, $n=7$ lines after ($n > m$).
     - Retain paper keywords: `fatal`, `fail`, `panic`, `error`, `exit`, `kill`, `no such file`, `err:`, `err!`, `failures:`, `exception`, `cannot`.
   - Never replace the LogSage log preprocessing pipeline with arbitrary heuristic scripts.

2. **Multi-Signal Triangulation**:
   - Never diagnose solely on logs if metrics or traces are present.
   - For resource failures (EC-1 OOM), prioritize Prometheus memory slope & cgroup limit alerts.
   - For cascading microservice failures (EC-3), prioritize the Jaeger span tree leaf culprit rather than the top-level error.

3. **Strict Token Budgeting**:
   - The combined multi-signal diagnostic prompt delivered to Gemini/LLM must stay strictly under **2,500 tokens**.
   - Raw logs, metrics timeseries, and span traces must be localized, summarized, and pruned before prompt injection.

4. **Reproducibility & Zero Fabrication**:
   - Telemetry must originate from real Docker containers, Prometheus PromQL queries, and Jaeger trace spans (or curated replay fixtures in `data/ground_truth/`).
   - Mock data should only be used in isolated unit tests (`tests/`).

---

## 3. Directory Layout & Module Responsibilities

```
final_year_project/
├── config/                  # Observability & algorithm configs (Prometheus, OTel, Drain3)
├── scripts/                 # CLI tools for stack verification, pipeline runs, and fault injection
├── data/
│   ├── baselines/           # Precomputed Drain3 templates & metric normal distributions
│   ├── ground_truth/        # Annotated benchmark datasets (EC1 - EC5)
│   └── runs/                # Captured telemetry per simulated pipeline run (success / failed)
├── src/
│   ├── log_processor/       # LogSage baseline: Drain3 diff, keyword filter, expansion, pruning
│   ├── metrics_processor/   # Prometheus client, Z-score detector, cgroup threshold & slope alerts
│   ├── trace_processor/     # Jaeger client, span DAG builder, DFS leaf culprit extractor
│   ├── fusion/              # Multi-signal correlation engine & dynamic prompt assembler
│   ├── llm/                 # Gemini 1.5 Pro client & structured Pydantic schema validation
│   ├── schemas/             # Pydantic models for runs, alerts, spans, and RCA reports
│   └── api/                 # FastAPI backend & web visualization dashboard
└── tests/                   # Pytest test suites per component
```

---

## 4. Key Developer Commands

- **Run Stack Healthcheck**:
  ```bash
  python scripts/check_stack.py
  ```
- **Simulate Local CI/CD Pipeline Run**:
  ```bash
  python scripts/run_pipeline.py
  ```
- **Inject Fault for Testing**:
  ```bash
  python scripts/inject_fault.py --fault oom --service cartservice
  ```
- **Run Unit Tests**:
  ```bash
  pytest tests/ -v
  ```

---

## 5. Coding Standards
- Python 3.11+, typed with Pydantic v2 schemas.
- External network requests (Prometheus, Jaeger, Docker) must use explicit timeouts.
- Never write credentials or API keys directly in source code; always consume via `.env`.
