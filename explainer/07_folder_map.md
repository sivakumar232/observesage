# 07 — Folder Map (Every File Explained)

```
final_year_project/
│
├── explainer/                    ← YOU ARE HERE — simple English docs
│   ├── 00_README.md              → Index of this folder
│   ├── 01_what_we_are_doing.md   → Problem statement + 5 edge cases
│   ├── 02_base_paper_logsage.md  → LogSage paper explained simply
│   ├── 03_how_we_extend_it.md    → What we add: metrics + traces + fusion
│   ├── 04_how_the_pipeline_works.md → Step-by-step diagnosis walkthrough
│   ├── 05_how_we_evaluate.md     → Dataset, Top@1/MRR, ablation study
│   ├── 06_how_we_test.md         → pytest, what passes, what fails
│   └── 07_folder_map.md          → This file
│
├── src/                          ← All Python source code
│   ├── schemas/                  → Pydantic data models (the "shapes" of our data)
│   │   ├── telemetry.py          → LogEntry, MetricSeries, TraceSpan, TelemetrySnapshot
│   │   ├── evidence.py           → LogEvidence, MetricEvidence, TraceEvidence
│   │   └── rca_report.py         → RCAReport, FailureCategory, EvidenceTriangulation
│   │
│   ├── log_processor/            → Stage 2: LogSage faithful implementation
│   │   ├── miner.py              → Drain3 baseline template mining
│   │   ├── filter.py             → 12-keyword filter + asymmetric expansion
│   │   └── processor.py          → Orchestrates miner + filter → LogEvidence
│   │
│   ├── metrics_processor/        → Stage 3: Metrics anomaly detection
│   │   └── processor.py          → Z-score, MAD, slope, OOM risk → MetricEvidence
│   │
│   ├── trace_processor/          → Stage 4: Distributed trace analysis
│   │   └── processor.py          → DAG build, self-duration, DFS culprit → TraceEvidence
│   │
│   ├── fusion/                   → Stage 5: Multi-signal correlation
│   │   └── engine.py             → Scoring, topology damping, Bayesian confidence
│   │
│   ├── llm/                      → Stage 6: LLM integration
│   │   ├── prompt.py             → Token-budgeted prompt builder (< 2500 BPE)
│   │   └── client.py             → Gemini API client + JSON schema enforcement
│   │
│   ├── rag/                      → Retrieval-Augmented Generation layer
│   │   └── retriever.py          → Calls all 3 processors, assembles RAGContext
│   │
│   ├── eval/                     → Evaluation metrics
│   │   ├── metrics.py            → Top@1, Top@3, MRR, category accuracy
│   │   └── taxonomy.py           → Maps fault_type strings → FailureCategory enums
│   │
│   └── api/                      → FastAPI web dashboard (NOT YET BUILT)
│       ├── __init__.py            → Empty (0 bytes)
│       └── static/               → Empty folder
│
├── scripts/                      ← CLI tools (run from terminal)
│   ├── rcaeval_adapter.py        → Reads parquet files → TelemetrySnapshot
│   ├── analyze.py                → Diagnose a single case
│   ├── evaluate_rcaeval.py       → Run benchmark on all cases + print results
│   ├── run_pipeline.py           → Run against live Docker services (not used now)
│   ├── collect_telemetry.py      → Collect from live Prometheus/Jaeger (not used now)
│   ├── inject_fault.py           → Inject faults into live services (not used now)
│   └── check_stack.py            → Health check for live services (not used now)
│
├── data/                         ← Datasets and precomputed state
│   ├── ground_truth/
│   │   └── rcaeval/
│   │       ├── cases.parquet     → 735 cases registry with ground truth labels
│   │       └── re2ob_checkoutservice_cpu_1/
│   │           ├── logs.parquet     → 171,322 log lines
│   │           ├── metrics.parquet  → 1,441 rows × 73 metric columns
│   │           ├── traces.parquet   → 391,997 trace spans
│   │           └── inject_time.txt  → Unix timestamp of fault injection
│   │
│   └── baselines/                → Precomputed Drain3 templates (cached)
│
├── tests/                        ← Unit tests (pytest)
│   ├── test_log_processor.py
│   ├── test_metrics_processor.py
│   ├── test_trace_processor.py
│   ├── test_fusion_engine.py
│   ├── test_prompt_builder.py
│   ├── test_schemas.py
│   └── test_eval_metrics.py
│
├── config/                       ← Configuration files
│   ├── drain3.ini                → Drain3 algorithm settings (depth, similarity)
│   └── observability.yaml        → Prometheus scrape config (for live mode)
│
├── results/                      ← Evaluation output (CSV + Markdown tables)
│   └── (empty until eval runs)
│
├── docs/                         ← Additional documentation
│   ├── plan.md
│   └── stage1_stage2_plan.md
│
├── .env.example                  ← Template for secrets (copy to .env)
│                                    GEMINI_API_KEY=your-key-here
│                                    TIKTOKEN_CACHE_DIR=data/cache
│
├── pyproject.toml                ← Python project config (dependencies, ruff rules)
├── requirements.txt              ← Python dependencies list
├── AGENTS.md                     ← Rules for AI coding assistants on this project
├── README.md                     ← Main project documentation
└── docker-compose.yml            ← Docker setup for live services (not used now)
```

---

## The Two Virtualenvs

```
.venv/   ← USE THIS ONE
         Has: tiktoken, pyarrow, ruff, drain3, fastapi, pydantic, rich
         Activate: source .venv/bin/activate

venv/    ← OLD, INCOMPLETE — do not use
         Missing: tiktoken, pyarrow, ruff
```

---

## Key Commands

```bash
# Run a diagnosis on the one real case we have:
.venv/bin/python scripts/analyze.py \
    --case data/ground_truth/rcaeval/re2ob_checkoutservice_cpu_1 \
    --mode fusion

# Run all tests:
.venv/bin/pytest tests/ -v

# Check code style (ruff linter):
.venv/bin/ruff check src/ scripts/

# Run evaluation on all downloaded cases:
.venv/bin/python scripts/evaluate_rcaeval.py --mode fusion

# (Future) Start the API dashboard:
.venv/bin/uvicorn src.api.app:app --reload --port 8000
```

---

## What's Done vs What's Left

```
DONE ✅
  Schema layer (Pydantic v2)           → production quality
  Log processor (LogSage faithful)     → Drain3 + keywords + expansion
  Metrics processor                    → Z-score + MAD + slope + OOM
  Fusion engine                        → scoring + topology + Bayesian
  Eval metrics (Top@1/3, MRR)          → complete
  36/38 unit tests                     → passing

IN PROGRESS ⚠️
  Trace processor                      → needs latency-based detection added
  RCAEval adapter                      → 1 critical trace sampling bug to fix
  Token budget                         → 2 small test failures to fix

NOT STARTED ❌
  Pipeline orchestrator                → needs new src/pipeline/orchestrator.py
  API dashboard (FastAPI)              → src/api/ is empty
  Full dataset download                → only 1 of 735 cases downloaded
  Real evaluation numbers              → need full dataset to run
```
