# 01 — What We Are Doing

## The Problem (One Sentence)

When a microservice or CI/CD pipeline breaks, finding *which service* caused it and *why*
takes engineers hours. We want an AI to do that in seconds.

---

## The Real-World Scenario

Imagine a shopping app built from 11 small services:

```
Browser → frontend → checkout → payment
                              → cart
                              → email
                   → shipping
                   → product catalog
                   → recommendation
                   → currency
                   → adservice
```

One service crashes. Every service that depends on it also starts failing.
The logs flood in — tens of thousands of lines. Metrics spike everywhere.
Traces show timeouts in five different services.

**A human engineer has to:**
1. SSH into each service
2. Grep through mountains of logs
3. Check dashboards for memory/CPU spikes
4. Trace the call chain backwards

This takes 30–90 minutes on average.

---

## What ObservaSage Does

ObservaSage reads the same data an engineer would look at — **logs, metrics, traces** —
processes it automatically, and gives back:

```
Root Cause: checkoutservice
Fault Type: EC-1 (Out-of-Memory — process killed by Linux kernel)
Confidence: 87%
Evidence:
  - Logs:    "SIGKILL 137" found in checkoutservice (novel log template)
  - Metrics: Memory rising 42 MB/min, would hit limit in 68 seconds
  - Traces:  All downstream timeouts trace back to checkoutservice as leaf culprit
Fix:         Increase memory limit from 256Mi to 512Mi; add circuit breaker
```

---

## The 5 Problems We Target

Logs alone sometimes cannot explain what happened. We call these **5 edge cases**:

| # | Name | Why Logs Alone Fail | Example |
|---|---|---|---|
| EC-1 | Out-of-Memory | The process is killed before it can write a final log | Container gets SIGKILL 137, no log saying "out of memory" |
| EC-2 | Flaky Latency | Requests succeed (HTTP 200) but are slow; logs look identical to healthy runs | CPU throttling, network jitter |
| EC-3 | Cascading Crash | The real failure is deep in the call chain; top-level logs only show timeouts | Service A crashes → B, C, D all log timeouts |
| EC-4 | Silent Corruption | No errors at all; but output data is wrong | Cart total shows $0.00 but exits with code 0 |
| EC-5 | Network Partition | App logs say "connection timed out" but root cause is the network fabric | DNS failure, pod eviction, node failure |

---

## In One Sentence

> **ObservaSage extends the LogSage AI paper by adding metrics and distributed traces
> so it can catch failures that logs alone cannot see, evaluated on a real benchmark
> dataset of 735 annotated microservice failures.**

---

## What We Are NOT Doing

- We are **not** building a monitoring system (like Prometheus or Datadog)
- We are **not** running live services — we replay saved incident data from files
- We are **not** replacing human engineers — we assist them with a first draft diagnosis
