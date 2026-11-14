# 02 — The Base Paper: LogSage

> Paper: *"LogSage: An LLM-Based Framework for CI/CD Failure Detection and Remediation with Industrial Validation"*  
> Authors: ByteDance + East China Normal University (2025)  
> Link: https://arxiv.org/abs/2506.03691

---

## What LogSage Does (Plain English)

LogSage is an AI system that reads CI/CD build logs and automatically:
1. Finds what went wrong
2. Suggests how to fix it
3. Actually executes the fix via tool-calling

It was tested at ByteDance production scale — 1.07 million pipeline runs — and
achieved **80%+ end-to-end precision** and **98% precision** in the RCA stage.

---

## The Core Problem LogSage Solves

A CI/CD log file can be **100,000+ lines long**.
Feeding all of that raw text to an AI model is impossible — it would exceed the
model's token limit (usually ~128k tokens) and cost a fortune.

LogSage's key innovation is a **smart preprocessing pipeline** that reduces
100,000 lines down to ~100 lines of meaningful content before the AI sees any of it.

---

## LogSage's 4-Stage Pipeline

```
RAW LOGS (100,000+ lines)
        │
        ▼
┌─────────────────────────────────────────┐
│ Stage 1: Keyword Filter                 │
│                                         │
│ Keep only lines containing:             │
│   fatal · fail · panic · error · exit   │
│   kill · no such file · err: · err!     │
│   failures: · exception · cannot        │
│                                         │
│ 100,000 lines → ~2,000 lines            │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│ Stage 2: Drain3 Template Mining         │
│                                         │
│ Drain3 is an algorithm that learns      │
│ "patterns" from logs.                   │
│                                         │
│ Example:                                │
│   "Connected to db-host-1 port 5432"    │
│   "Connected to db-host-2 port 5432"    │
│   "Connected to db-host-3 port 5432"    │
│       ↓ Drain3 learns the template:     │
│   "Connected to <*> port <*>"           │
│                                         │
│ LogSage mines templates from the last   │
│ 3 successful ("green") runs.            │
│ Any line matching a known template      │
│ is NORMAL → throw it away.             │
│ Lines that DON'T match = NOVEL = keep.  │
│                                         │
│ 2,000 lines → ~50 novel lines           │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│ Stage 3: Context Expansion              │
│                                         │
│ For each kept line, grab:               │
│   • 3 lines BEFORE it (m = 3)           │
│   • 7 lines AFTER it  (n = 7)           │
│                                         │
│ Why more after? Stack traces and        │
│ exception details appear BELOW the      │
│ error trigger line.                     │
│                                         │
│ 50 lines → ~200 lines with context      │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│ Stage 4: LLM Diagnosis (GPT / Gemini)   │
│                                         │
│ The ~200 lines are sent to an LLM       │
│ with a structured prompt asking:        │
│   "What is the root cause?              │
│    Which service failed?                │
│    How should we fix it?"               │
│                                         │
│ The LLM returns structured JSON.        │
└─────────────────────────────────────────┘
```

---

## Key Numbers from the Paper

| Setting | Value | Why |
|---|---|---|
| Baseline runs kept | **x = 3** | Last 3 green builds for Drain3 template learning |
| Lines before error | **m = 3** | Capture what triggered the error |
| Lines after error | **n = 7** | Capture stack traces (n > m intentionally) |
| Token budget | **< 2,500** | Fits within LLM context with room for response |
| Benchmark size | **367 GitHub CI/CD failures** | Real public failures from open source repos |

---

## LogSage's Results (from paper)

- **98% precision** in finding the failing component from logs
- **38 percentage point improvement** in F1 score vs a plain LLM baseline
- **80% end-to-end precision** in production at ByteDance (1.07M traces)

---

## What LogSage Does NOT Do

This is exactly where ObservaSage extends it:

| Gap | LogSage | ObservaSage |
|---|---|---|
| Metrics | ❌ Ignores CPU, memory, latency timeseries | ✅ Reads Prometheus/parquet metrics |
| Traces | ❌ No distributed trace analysis | ✅ Builds call DAG, finds leaf culprit |
| OOM detection | ❌ Only sees logs (which are silent on OOM) | ✅ Detects memory slope + cgroup limit |
| Cascading failures | ❌ Top-level error logs mislead it | ✅ DFS trace traversal finds real origin |

---

## The Exact LogSage Keywords We Use (Unchanged from Paper)

```python
KEYWORDS = [
    "fatal", "fail", "panic", "error", "exit", "kill",
    "no such file", "err:", "err!", "failures:", "exception", "cannot"
]
```

We keep this list **exactly as in the paper** — no additions, no removals.
