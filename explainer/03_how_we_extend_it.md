# 03 — How We Extend LogSage

## The Short Answer

LogSage uses **only logs**. We add **metrics** and **distributed traces**
and fuse all three signals together before asking the AI.

---

## Side-by-Side: LogSage vs ObservaSage

```
LogSage:
  Logs (filtered) ──────────────────► LLM ──► RCA Report

ObservaSage:
  Logs (filtered)  ─┐
  Metrics (alerts)  ├──► Fusion Engine ──► Prompt ──► LLM ──► RCA Report
  Traces (DAG)     ─┘
```

---

## What Each Signal Tells Us

### Signal 1: Logs (From LogSage — kept exactly)
- Drain3 template mining on last 3 green runs
- Keyword filter (12 exact keywords from paper)
- Asymmetric expansion: 3 before, 7 after each error line
- Novel templates = things that never appeared in healthy runs

**Best at:** Application errors, exceptions, process crashes, missing files

---

### Signal 2: Metrics (New in ObservaSage)
Metrics are numbers collected every 5–15 seconds:
- CPU usage per service (%)
- Memory usage per service (MB)
- Request latency per service (ms, p50/p90/p99)
- Disk I/O, network bytes

**What we do with metrics:**

```
For each metric (e.g., checkoutservice_memory):
  1. Compute BASELINE: mean + stddev from the 10 minutes before the fault
  2. During INCIDENT: check if value spikes more than 3× stddev
  3. If memory rising steeply: project TimeToOOM
     (if memory hits limit in < 5 minutes → OOM alert)
  4. Use MAD (Median Absolute Deviation) for jittery latency metrics
     (more robust than standard deviation for noisy data)
```

**Best at:** OOM detection, CPU throttling, latency regressions (EC-1, EC-2)

---

### Signal 3: Distributed Traces (New in ObservaSage)
A "trace" records every network call during one user request.

Example — one checkout request:
```
frontend.checkout (500ms)
  └─ checkoutservice.PlaceOrder (490ms)
       ├─ cartservice.GetCart (3ms) ✓
       ├─ paymentservice.Charge (2ms) ✓
       └─ emailservice.Send (480ms) ← SLOW / ERROR
```

**What we do with traces:**

```
1. Build the call tree (DAG) from parentSpanID links
2. Calculate "self-duration" of each span:
   self_duration = span_total_duration − sum(children_durations)
3. Find error spans (HTTP 500, gRPC error codes)
4. Find latency bottlenecks:
   compare each span's self-duration against baseline p99
   if 2× slower than normal → flag as bottleneck
5. Walk to the DEEPEST failing node in the tree (DFS)
   → that's the REAL root cause, not the top-level caller
```

**Best at:** Cascading failures, blaming the right service when many logs are noisy (EC-3)

---

## The Fusion Step

After getting evidence from all 3 signals, we **score each service**:

```
score(checkoutservice) =
    (log snippets × 1.0) +
    (novel log templates × 2.5) +
    (metric alerts × 3.0–6.0) +
    (trace leaf culprit × 5.0+)

Then DAMPEN callers:
  If checkoutservice called emailservice and emailservice has errors:
    checkoutservice.score × 0.60  ← it's a victim, not a cause
    emailservice.score + 2.0      ← it's the real source

Then BOOST multi-signal agreement:
  If 2 signals agree on same service: score × 1.35
  If 3 signals agree:                 score × 1.75
```

The service with the highest final score = **predicted root cause**.

---

## Token Budget: Fitting 3 Signals into 2,500 Tokens

The LLM has a strict token limit. We allocate it dynamically:

```
Total budget: 2,500 BPE tokens

Traces section:   up to 45% of budget  (~1,000 tokens)
Metrics section:  up to 35% of budget  (~800 tokens)
Logs section:     remaining budget     (~700 tokens)

If traces only need 300 tokens → the extra 700 rolls into logs.
Final safety check: truncate anything that overflows.
```

This is counted using the **tiktoken** library (same tokenizer as GPT-4),
not a rough character estimate.
