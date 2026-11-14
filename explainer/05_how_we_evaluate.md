# 05 — How We Evaluate

## The Dataset: RCAEval

We evaluate on **RCAEval** — a public academic benchmark of annotated microservice failures.

```
data/ground_truth/rcaeval/
├── cases.parquet          → Registry: 735 cases with ground-truth labels
└── re2ob_checkoutservice_cpu_1/
    ├── logs.parquet
    ├── metrics.parquet
    ├── traces.parquet
    └── inject_time.txt
```

**What's in cases.parquet:**
```
case_id                              | fault_service      | fault_type | dataset
re2ob_checkoutservice_cpu_1          | checkoutservice    | cpu        | RE2-OB
re2ob_cartservice_mem_2              | cartservice        | mem        | RE2-OB
re2ob_paymentservice_delay_1         | paymentservice     | delay      | RE2-OB
...735 rows total
```

**The 5 fault types we care about:**

| Fault Type | What Was Injected | Maps To |
|---|---|---|
| `cpu` | CPU stress / throttling | EC-2 Latency |
| `mem` | Memory pressure, OOM | EC-1 OOM |
| `delay` | Network delay added | EC-2 Latency |
| `loss` | Network packet loss | EC-5 Infra |
| `socket` | Socket errors | EC-3 Cascade |

---

## What We Measure

### Metric 1: Top@1 Accuracy
> "Did we name the correct service as #1?"

```
Ground truth: fault_service = "checkoutservice"
Our output:   ranked_culprits = ["checkoutservice", "cartservice", "frontend"]

Top@1 = 1  (checkoutservice is in position 1 → correct!)

If our output was: ["cartservice", "checkoutservice", "frontend"]
Top@1 = 0  (wrong #1 guess)
```

### Metric 2: Top@3 Accuracy
> "Was the correct service anywhere in our top 3?"

```
Our output: ["cartservice", "checkoutservice", "frontend"]
Top@3 = 1  (checkoutservice is in position 2, within top 3 → credit)
```

### Metric 3: Mean Reciprocal Rank (MRR)
> "On average, how close to #1 is the correct service?"

```
Correct service at position 1 → reciprocal rank = 1/1 = 1.0  (perfect)
Correct service at position 2 → reciprocal rank = 1/2 = 0.5
Correct service at position 3 → reciprocal rank = 1/3 = 0.33

MRR = average over all test cases
MRR = 1.0 → always gets it exactly right
MRR = 0.5 → usually gets it in second place
```

### Metric 4: Fault Category Accuracy
> "Did we correctly classify the type of failure (EC-1, EC-2, etc.)?"

```
Ground truth (inferred from fault_type):
  mem → EC-1 OOM
  cpu → EC-2 Latency

Our output: failure_category = "EC-2: Flaky / Intermittent Latency"
Category Accuracy = 1 for this case
```

---

## The Ablation Study: 4 Configurations

We run the same 735 cases 4 ways to prove that adding signals helps:

| Mode | What It Uses | Purpose |
|---|---|---|
| `logs-only` | Only LogSage log processing | Replicates the original paper baseline |
| `logs-metrics` | Logs + Metrics | Shows metrics add value |
| `logs-traces` | Logs + Traces | Shows traces add value |
| `fusion` | Logs + Metrics + Traces | ObservaSage full system |

**Expected result pattern:**
```
logs-only    → Top@1: ~42%  (baseline, matches LogSage paper)
logs-metrics → Top@1: ~56%  (OOM and latency faults get better)
logs-traces  → Top@1: ~61%  (cascading failures get better)
fusion       → Top@1: ~71%  (best overall)
```

The improvement from `logs-only` → `fusion` is our main claim.

---

## How the Evaluation Script Works

```bash
# Run all 4 modes, save results to CSV
python scripts/evaluate_rcaeval.py --mode fusion --output-csv results/ablation_fusion.csv
python scripts/evaluate_rcaeval.py --mode logs-only --output-csv results/ablation_logs.csv
```

**What happens inside:**

```
For each case in cases.parquet:
  1. Load the parquet files for that case
  2. Run the full pipeline in the given mode
  3. Compare our ranked_culprits[0] against ground truth fault_service
  4. Record: top1_correct, top3_correct, reciprocal_rank, category_correct

After all cases:
  Compute averages per fault_type
  Print a Markdown table (ready to paste into the paper)
  Save CSV for analysis
```

---

## The Paper Table We're Building

```
Table 1: Ablation Study — RE2-OB Dataset (90 cases)

| Configuration  | Top@1 | Top@3 | MRR   | Cat. Acc |
|----------------|-------|-------|-------|----------|
| Logs-Only      | 0.42  | 0.68  | 0.52  | 0.55     |
| Logs + Metrics | 0.56  | 0.74  | 0.63  | 0.68     |
| Logs + Traces  | 0.61  | 0.78  | 0.68  | 0.72     |
| Full Fusion    | 0.71  | 0.85  | 0.76  | 0.78     |
```

(Numbers above are targets — will be replaced with real numbers after full eval run)

---

## Per Fault-Type Breakdown

We also break down results by fault type to show when each signal matters:

```
EC-1 (OOM/mem faults):
  logs-only accuracy: 0.28  ← logs are silent on OOM
  fusion accuracy:    0.75  ← metrics catch memory slope

EC-3 (cascade/socket faults):
  logs-only accuracy: 0.35  ← top-level logs mislead
  fusion accuracy:    0.70  ← traces find the real origin
```

This per-fault analysis is the academic contribution of ObservaSage.
