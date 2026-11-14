# 06 — How We Test

## Overview

We use **pytest** (Python's standard test framework) to test every component in isolation.

```bash
# Run all tests:
.venv/bin/pytest tests/ -v

# Current result:
# 36 passed, 2 failed
# (the 2 failures are known token-counting bugs — fix in progress)
```

---

## Test Directory Structure

```
tests/
├── test_log_processor.py     → tests for Drain3 + keyword filter + context expansion
├── test_metrics_processor.py → tests for Z-score, slope, OOM detection
├── test_trace_processor.py   → tests for DAG building, DFS culprit, latency detection
├── test_fusion_engine.py     → tests for scoring, topology damping, confidence calc
├── test_prompt_builder.py    → tests for token counting, truncation, budget enforcement
├── test_schemas.py           → tests for Pydantic model validation
└── test_eval_metrics.py      → tests for Top@1/Top@3/MRR calculation
```

---

## What Each Test File Covers

### `test_log_processor.py`
```
✅ Drain3 mines correct templates from healthy logs
✅ Novel templates are detected when incident logs differ
✅ Keyword filter keeps exactly the 12 paper keywords
✅ Asymmetric expansion: 3 lines before, 7 lines after
✅ Deduplication removes duplicate snippets
✅ Empty log list returns empty LogEvidence
```

### `test_metrics_processor.py`
```
✅ Z-score alert fires when value > 3σ from baseline mean
✅ No alert when value is within normal range
✅ OOM alert fires when memory slope projects < 300s to limit
✅ MAD (Median Absolute Deviation) used for latency metrics
✅ has_oom_alert and has_latency_anomaly flags set correctly
✅ Empty metrics returns empty MetricEvidence
```

### `test_trace_processor.py`
```
✅ Call tree correctly built from parentSpanID links
✅ Root span identified when parentSpanID is null/None
✅ Self-duration = span_duration − sum(children_durations)
✅ DFS finds deepest error span as leaf culprit
✅ Timeout inversion: child consuming >60% of parent latency flagged
✅ service_dependency_graph built correctly (caller → callees)
```

### `test_fusion_engine.py`
```
✅ Log-only score = snippets × 1.0 + novel × 2.5
✅ OOM metric score = 6.0, regular alert = 3.0–4.5
✅ Trace leaf culprit score = 5.0 + 2.0 (depth > 0)
✅ Caller score dampened ×0.60 when downstream callee has errors
✅ 2-signal agreement → ×1.35 multiplier
✅ 3-signal agreement → ×1.75 multiplier
✅ Failure category EC-1 wins when has_oom_alert=True
✅ Failure category EC-3 wins when multiple error spans in traces
```

### `test_prompt_builder.py`
```
✅ Total prompt stays under 2,500 BPE tokens
✅ Truncation works correctly (doesn't go over limit)
✅ Traces get up to 45% of budget
✅ Metrics get up to 35% of budget
✅ Logs absorb remaining budget
✅ All 4 modes produce valid prompts: logs-only, logs-metrics, logs-traces, fusion
❌ FAILING: BPE count test (tiktoken offline in sandbox — uses len//3 fallback)
❌ FAILING: Budget guarantee test (suffix tokens not deducted before truncation)
```

### `test_schemas.py`
```
✅ TelemetrySnapshot validates correctly with Pydantic v2
✅ RCAReport validates with all required fields
✅ LogEvidence, MetricEvidence, TraceEvidence schema validation
✅ Invalid data raises ValidationError (not silently passes)
✅ FailureCategory enum covers EC-1 through EC-5 + CODE_BUG + UNKNOWN
```

### `test_eval_metrics.py`
```
✅ Top@1 = 1 when correct service is first in ranked list
✅ Top@1 = 0 when correct service is not first
✅ Top@3 = 1 when correct service is in positions 1, 2, or 3
✅ MRR = 1.0 for position 1, 0.5 for position 2, 0.33 for position 3
✅ Category accuracy = 1 when predicted matches ground truth
✅ aggregate_scores() averages correctly over multiple cases
```

---

## Testing Philosophy

### Unit Tests (what we have)
Each module is tested **in isolation** with fake/small data.
No real parquet files, no network calls, no LLM API in tests.

```python
# Example: testing Drain3 diff in isolation
def test_novel_template_detected():
    baseline_logs = [
        "2024-01-01 INFO Server started on port 8080",
        "2024-01-01 INFO Health check OK",
    ]
    incident_logs = [
        "2024-01-01 ERROR OutOfMemoryError: Java heap space",  # Novel!
        "2024-01-01 INFO Health check OK",                     # Known template
    ]
    processor = LogSageProcessor()
    processor.train_baseline(baseline_logs)
    evidence = processor.process_incident(incident_logs, service="app")
    
    assert evidence.novel_templates_count == 1    # Only the OOM is novel
    assert len(evidence.snippets) == 1
    assert "OutOfMemoryError" in evidence.snippets[0].target_line
```

### Integration Tests (planned)
Run the full pipeline on the 1 real case we have:
```bash
python scripts/analyze.py \
    --case data/ground_truth/rcaeval/re2ob_checkoutservice_cpu_1 \
    --mode fusion
```
Checks that it produces a valid RCA report JSON without crashing.

### Evaluation Tests (run after dataset download)
```bash
python scripts/evaluate_rcaeval.py --mode fusion --limit 10
```
Sanity check: Top@1 > 0.3 on first 10 cases (better than random chance = 1/11 services).

---

## The 2 Known Failing Tests

### Failure 1: `test_bpe_token_counting_and_truncation`
**Why:** tiktoken library cannot download the encoding file without internet access.
Falls back to `len(text) // 3` instead of real BPE count.
**Fix:** Pre-cache the tiktoken file → `data/cache/cl100k_base.tiktoken`

### Failure 2: `test_multimodal_prompt_token_budget_guarantee`
**Why:** The truncation suffix `"\n...[truncated for token budget]"` adds ~8 tokens
but those tokens are not deducted from the budget before slicing.
Result: truncated text + suffix = 2408 tokens, but test asserts ≤ 2400.
**Fix:** Reserve suffix token count before slicing (5-line code change).

---

## Running Tests

```bash
# All tests
.venv/bin/pytest tests/ -v

# One specific file
.venv/bin/pytest tests/test_fusion_engine.py -v

# One specific test
.venv/bin/pytest tests/test_prompt_builder.py::test_multimodal_prompt_token_budget_guarantee -v

# With coverage report
.venv/bin/pytest tests/ --cov=src --cov-report=term-missing
```
