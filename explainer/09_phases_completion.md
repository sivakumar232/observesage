# 09 — Project Phases (Complete Roadmap)

> Every step to get from "1 case downloaded" to a submitted final-year project.
> Follow these phases in order. Each phase has clear DONE criteria.

---

## Phase 0 — Housekeeping & Environment
**Goal:** Clean codebase, all tests passing, environment ready.
**Effort:** ~2–3 hours

| Step | Task | Command | Done When |
|---|---|---|---|
| 0.1 | Fix ruff lint errors (441 errors) | `.venv/bin/ruff check --fix src/ scripts/` | 0 errors |
| 0.2 | Cache tiktoken offline | `mkdir -p data/cache && TIKTOKEN_CACHE_DIR=data/cache .venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"` | `data/cache/` has `.tiktoken` file |
| 0.3 | Add `TIKTOKEN_CACHE_DIR=data/cache` to `.env` | Edit `.env` file | Line is present in `.env` |
| 0.4 | Run all tests | `.venv/bin/pytest tests/ -v` | **38/38 tests pass** (currently 36/38) |
| 0.5 | Confirm single-case analysis runs | `.venv/bin/python scripts/analyze.py --case data/ground_truth/rcaeval/re2ob_checkoutservice_cpu_1 --mode fusion` | Prints a valid JSON RCA report |

**✅ Phase 0 Done When:** `pytest` shows 38/38 pass AND analyze.py runs without error.

---

## Phase 1 — Fix Critical Bugs (Blocking Pipeline)
**Goal:** The 6 known bugs that break the evaluation pipeline.
**Effort:** ~4–5 hours

### Bug 1 — Trace Sampling Drops Incident Spans
**File:** `scripts/rcaeval_adapter.py` line ~354
**Problem:** `iloc[:10000]` takes first 10k rows = all baseline, 0 incident spans.
```python
# Replace:
if len(df) > 10000:
    df = df.iloc[:10000]

# With stratified sample:
if len(df) > 10000:
    incident_mask = ts_seconds >= inject_time
    df_inc = df[incident_mask]
    df_base = df[~incident_mask]
    max_inc = min(len(df_inc), 6000)
    max_base = min(len(df_base), 10000 - max_inc)
    df = pd.concat([
        df_inc.sample(n=max_inc, random_state=42) if max_inc < len(df_inc) else df_inc,
        df_base.sample(n=max_base, random_state=42) if max_base < len(df_base) else df_base,
    ])
```

| Step | Task |
|---|---|
| 1.1 | Apply the fix above in `rcaeval_adapter.py` |
| 1.2 | Verify: `python -c "from scripts.rcaeval_adapter import RCAEvalAdapter; a = RCAEvalAdapter(); s = a.parse_all('data/ground_truth/rcaeval/re2ob_checkoutservice_cpu_1', 1709123456); print(len(s.telemetry.traces), 'incident traces')"` → should be > 0 |

---

### Bug 2 — Trace Processor Closure Bug (B023)
**File:** `src/trace_processor/processor.py` lines 138–225
**Problem:** `traverse_depth` and `print_subtree` defined inside a for loop — capture wrong variables for 2nd+ trace.

| Step | Task |
|---|---|
| 1.3 | Convert `traverse_depth` to `@staticmethod _traverse_depth(span_id, depth, children_map, depths)` |
| 1.4 | Convert `print_subtree` to `@staticmethod _print_subtree(s_id, indent, span_map, children_map, lines, culprit_id)` |
| 1.5 | Update all call sites inside the loop to use the static methods with explicit args |

---

### Bug 3 — No Evidence for CPU/Delay Faults (Latency Detection)
**File:** `src/trace_processor/processor.py` line ~263
**Problem:** When `error_spans_count == 0`, processor says "no anomalies" — but CPU faults have no error codes, only slow spans.

| Step | Task |
|---|---|
| 1.6 | Add latency bottleneck detection: for each span, compare `self_duration_ms` vs baseline p99 for `(service, operation)` |
| 1.7 | Flag spans where `self_duration > baseline_p99 × 2.0` as `latency_bottleneck` |
| 1.8 | Include latency bottleneck spans in the DFS leaf culprit search |
| 1.9 | Update `_format_prompt` to report latency bottlenecks even when `error_spans_count == 0` |

---

### Bug 4 — Drain3 State Bleeds Between Cases
**File:** `src/rag/retriever.py` line ~53
**Problem:** `LogSageProcessor` is reused across cases; Drain3 templates from case N contaminate case N+1.

| Step | Task |
|---|---|
| 1.10 | Instantiate `log_processor = LogSageProcessor()` INSIDE `retrieve()` method (not in `__init__`) |

---

### Bug 5 — Token Budget Doesn't Count SYSTEM_PROMPT
**File:** `src/llm/prompt.py`
**Problem:** `build_multimodal_prompt` budgets 2,400 tokens but the system prompt (~280 tokens) is added separately → real total can hit ~2,680.

| Step | Task |
|---|---|
| 1.11 | Add `system_prompt_overhead=300` parameter to `build_multimodal_prompt` |
| 1.12 | Use `effective_budget = max_total_tokens - system_prompt_overhead` for all section calculations |
| 1.13 | Fix `truncate_to_tokens` to deduct suffix token count before slicing |

---

### Bug 6 — EC-4 Silent Corruption Never Triggered
**File:** `src/fusion/engine.py` line ~122
**Problem:** `EC_4_SILENT` starts at 0.05 and no code path ever adds to it.

| Step | Task |
|---|---|
| 1.14 | Add: if `total_snippets == 0` and no metric alerts → `EC_4_SILENT += 4.0` (silent failure signal) |
| 1.15 | Add: if log snippets contain "wrong", "mismatch", "unexpected value", "corrupt" → `EC_4_SILENT += 2.5` |

**✅ Phase 1 Done When:** All 6 bugs fixed AND `pytest tests/ -v` still passes 38/38.

---

## Phase 2 — Download Minimum Dataset
**Goal:** Have at least 6 cases (1 per fault type) so we can test all EC categories.
**Effort:** ~30 minutes (mostly waiting for download)

| Step | Task | Done When |
|---|---|---|
| 2.1 | Download 5 more RE2-OB cases (1 per fault type) from HuggingFace | 5 new folders appear in `data/ground_truth/rcaeval/` |
| 2.2 | Verify each folder has `logs.parquet`, `metrics.parquet`, `traces.parquet`, `inject_time.txt` | All 4 files present in each |
| 2.3 | Run analyze.py on each new case | Each produces a valid RCAReport without errors |

**Cases to download in this phase:**
```
re2ob_checkoutservice_mem_1    ← tests OOM detection (EC-1)
re2ob_checkoutservice_delay_1  ← tests latency detection (EC-2)
re2ob_checkoutservice_loss_1   ← tests network partition (EC-5)
re2ob_checkoutservice_socket_1 ← tests cascading failure (EC-3)
re2ob_checkoutservice_disk_1   ← tests disk infra (EC-5)
```

**Download command:**
```bash
pip install huggingface_hub
python -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='phamquiluan/RCAEval',
    repo_type='dataset',
    allow_patterns=[
        're2ob_checkoutservice_mem_1/*',
        're2ob_checkoutservice_delay_1/*',
        're2ob_checkoutservice_loss_1/*',
        're2ob_checkoutservice_socket_1/*',
        're2ob_checkoutservice_disk_1/*',
    ],
    local_dir='data/ground_truth/rcaeval/'
)
"
```

**✅ Phase 2 Done When:** 6 cases total in `data/ground_truth/rcaeval/`, all analysis runs succeed.

---

## Phase 3 — Build Pipeline Orchestrator (New Module)
**Goal:** Create the missing batch runner that iterates cases, resets state, and saves results.
**Effort:** ~5 hours

**Create these new files:**
```
src/pipeline/__init__.py
src/pipeline/orchestrator.py
```

| Step | Task | Done When |
|---|---|---|
| 3.1 | Create `src/pipeline/__init__.py` | File exists |
| 3.2 | Implement `ObservaSagePipeline.run_single_case(case_dir, inject_time, ground_truth_service, fault_type, mode)` | Returns `CaseScore` without crashing |
| 3.3 | Add per-case state reset: fresh `LogSageProcessor` instance per case | No Drain3 bleed between cases |
| 3.4 | Implement `ObservaSagePipeline.run_batch(fault_filter, limit, modes, output_dir)` | Saves CSV for each mode |
| 3.5 | Add `rich.progress` progress bar | Progress shows during batch run |
| 3.6 | Write intermediate results after each case | CSV is updated after every single case (crash-safe) |
| 3.7 | Write tests in `tests/test_pipeline_orchestrator.py` | Tests pass |

**✅ Phase 3 Done When:** `python scripts/evaluate_rcaeval.py --mode fusion --limit 6` runs all 6 cases and saves `results/ablation_fusion.csv`.

---

## Phase 4 — Download Full RE2-OB Dataset
**Goal:** All 90 RE2-OB cases downloaded and verified.
**Effort:** ~1–2 hours (download time)

| Step | Task | Done When |
|---|---|---|
| 4.1 | Download all 90 RE2-OB cases from HuggingFace | 90 folders in `data/ground_truth/rcaeval/re2ob_*/` |
| 4.2 | Verify all folders have all 4 required files | Automated check passes |
| 4.3 | Run quick sanity batch (logs-only mode) | 90 cases complete without crashes |

**Download command:**
```bash
python -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='phamquiluan/RCAEval',
    repo_type='dataset',
    allow_patterns=['re2ob_*'],
    local_dir='data/ground_truth/rcaeval/'
)
"
```

**✅ Phase 4 Done When:** `ls data/ground_truth/rcaeval/ | grep re2ob | wc -l` prints `90`.

---

## Phase 5 — Run Full Ablation Study
**Goal:** Real evaluation numbers for the 4-way ablation across all 90 RE2-OB cases.
**Effort:** ~4–8 hours (run time depends on API rate limits)

| Step | Task | Command | Done When |
|---|---|---|---|
| 5.1 | Run logs-only mode | `python scripts/evaluate_rcaeval.py --mode logs-only --output-csv results/ablation_logs_only.csv` | CSV saved with 90 rows |
| 5.2 | Run logs-metrics mode | `python scripts/evaluate_rcaeval.py --mode logs-metrics --output-csv results/ablation_logs_metrics.csv` | CSV saved |
| 5.3 | Run logs-traces mode | `python scripts/evaluate_rcaeval.py --mode logs-traces --output-csv results/ablation_logs_traces.csv` | CSV saved |
| 5.4 | Run full fusion mode | `python scripts/evaluate_rcaeval.py --mode fusion --output-csv results/ablation_fusion.csv` | CSV saved |
| 5.5 | Generate comparison table | `python scripts/evaluate_rcaeval.py --compare-all --output-md results/paper_table.md` | Markdown table with real numbers |

**Expected output (paper Table 1):**
```
| Mode           | Top@1 | Top@3 | MRR   | Category Acc |
|----------------|-------|-------|-------|--------------|
| logs-only      | 0.XX  | 0.XX  | 0.XX  | 0.XX         |
| logs-metrics   | 0.XX  | 0.XX  | 0.XX  | 0.XX         |
| logs-traces    | 0.XX  | 0.XX  | 0.XX  | 0.XX         |
| fusion         | 0.XX  | 0.XX  | 0.XX  | 0.XX         |
```
(Fill in with real numbers from the runs)

**✅ Phase 5 Done When:** 4 CSV files exist in `results/` and `paper_table.md` is complete with real numbers.

---

## Phase 6 — Build FastAPI Dashboard
**Goal:** A web interface to run analyses and visualize results.
**Effort:** ~6–8 hours
**Status:** ❌ Not started — `src/api/` is currently empty.

| Step | Task | Done When |
|---|---|---|
| 6.1 | Create `src/api/app.py` with FastAPI app + CORS | Server starts with `uvicorn src.api.app:app` |
| 6.2 | Create `src/api/routes.py` with `GET /health`, `GET /cases`, `POST /analyze` | All 3 routes return valid responses |
| 6.3 | Create `src/api/routes.py` `GET /evaluate` and `GET /compare` | Returns ablation results from CSV files |
| 6.4 | Create `src/api/websocket.py` with `WS /ws/diagnose` | Streams LLM response tokens in real-time |
| 6.5 | Create `src/api/static/index.html` with case browser + ablation table | Opens in browser and shows data |
| 6.6 | Add D3.js call graph DAG visualization | Service dependency graph renders visually |
| 6.7 | Write API tests in `tests/test_api.py` | All API endpoints tested |

**✅ Phase 6 Done When:** `uvicorn src.api.app:app --port 8000` runs and browser shows the dashboard.

---

## Phase 7 — Extend to RE2-TT + RE3 (Optional but Recommended)
**Goal:** Show ObservaSage works across different microservice architectures.
**Effort:** ~3–4 hours + download time

| Step | Task | Done When |
|---|---|---|
| 7.1 | Download RE2-TT cases (89 cases, Train Ticket app) | 89 more folders in `data/ground_truth/rcaeval/re2tt_*/` |
| 7.2 | Download RE3-OB and RE3-TT cases (60 cases, code faults f1-f5) | 60 more folders |
| 7.3 | Run evaluation on RE2-TT | Results CSV saved |
| 7.4 | Run evaluation on RE3 | Results CSV saved |
| 7.5 | Generate cross-dataset comparison table | Proves generalization beyond one app |

**✅ Phase 7 Done When:** Paper has 3 results tables: RE2-OB, RE2-TT, RE3 combined.

---

## Phase 8 — Write Up & Submit
**Goal:** Final-year project report and code submission.
**Effort:** ~1–2 weeks

| Step | Task | Done When |
|---|---|---|
| 8.1 | Write abstract + introduction | 1 page |
| 8.2 | Write Related Work section (LogSage paper + other RCA papers) | 1–2 pages |
| 8.3 | Write Methodology section (draw from `explainer/` files) | 3–4 pages |
| 8.4 | Write Evaluation section (paste real numbers from Phase 5/7) | 2–3 pages |
| 8.5 | Write Discussion & Limitations | 1 page |
| 8.6 | Clean up code, add docstrings, final lint pass | 0 ruff errors |
| 8.7 | Add `requirements.txt` and final README badges | README is complete |
| 8.8 | Record a demo video showing the dashboard diagnosing a fault | 2–3 min video |
| 8.9 | Submit code + report | ✅ Done |

---

## Overall Progress Dashboard

```
Phase 0 — Housekeeping        [ ] 0/5 steps done
Phase 1 — Fix 6 Bugs          [ ] 0/15 steps done
Phase 2 — Minimum Dataset     [ ] 0/3 steps done
Phase 3 — Orchestrator        [ ] 0/7 steps done
Phase 4 — Full RE2-OB         [ ] 0/3 steps done
Phase 5 — Ablation Study      [ ] 0/5 steps done
Phase 6 — API Dashboard       [ ] 0/7 steps done
Phase 7 — Extended Datasets   [ ] 0/5 steps done  (optional)
Phase 8 — Write Up            [ ] 0/9 steps done
```

---

## Critical Path (Minimum to Get a Working Evaluation)

```
Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5
  ↓            ↓          ↓           ↓           ↓          ↓
 ~3h         ~5h        ~1h         ~5h          ~2h        ~8h
                                                         (run time)
```

**Total: ~24 hours of development + ~8 hours of GPU/API run time**

You can skip Phase 6 (dashboard) and Phase 7 (extra datasets) if time is tight.
Phase 5 results are enough for a solid final-year project.
