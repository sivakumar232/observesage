# 08 — Datasets to Download

## Current State (What We Have)

```
data/ground_truth/rcaeval/
├── cases.parquet                       ← ✅ Downloaded (registry of all 735 cases, 29 KB)
└── re2ob_checkoutservice_cpu_1/        ← ✅ Downloaded (11 MB, one case only)
    ├── logs.parquet
    ├── metrics.parquet
    ├── traces.parquet
    └── inject_time.txt
```

**We have 1 case out of 735 total.**

---

## Which Cases Matter for Us (and Which Don't)

Out of 735 cases in `cases.parquet`, many are **useless for us** because they are
metrics-only (no logs, no traces). We need **Logs + Metrics + Traces** (tri-modal).

| Suite | Cases | Has Logs | Has Traces | Useful? |
|---|---:|:---:|:---:|:---:|
| RE1-OB | 125 | ❌ | ❌ | ❌ Skip |
| RE1-SS | 125 | ❌ | ❌ | ❌ Skip |
| RE1-TT | 125 | ❌ | ❌ | ❌ Skip |
| **RE2-OB** | **90** | **✅** | **✅** | **✅ PRIORITY** |
| RE2-SS | 90 | ✅ | ❌ | ⚠️ Optional |
| **RE2-TT** | **89** | **✅** | **✅** | **✅ Secondary** |
| **RE3-OB** | **30** | **✅** | **✅** | **✅ Secondary** |
| **RE3-TT** | **30** | **✅** | **✅** | **✅ Secondary** |

**Total useful tri-modal cases: 239**
**We currently have: 1**
**Still need to download: 238 more**

---

## Priority Order for Downloads

### 🟢 MUST HAVE — RE2-OB (89 cases remaining)
**89 cases** · ~1 GB total · Each ~11 MB

This is the **Online Boutique** microservice app (Google's demo app — same one we
already have 1 case from). It's perfectly balanced:

| Fault Type | Cases | Services Affected | Maps to EC |
|---|---:|---|---|
| `cpu` | 15 | checkoutservice (✅ done 1), currencyservice, emailservice, productcatalogservice, recommendationservice | EC-2 Latency |
| `mem` | 15 | same 5 services | EC-1 OOM |
| `delay` | 15 | same 5 services | EC-2 Latency |
| `loss` | 15 | same 5 services | EC-5 Infra |
| `disk` | 15 | same 5 services | EC-5 Infra |
| `socket` | 15 | same 5 services | EC-3 Cascade |
| **Total** | **90** | 5 services × 6 faults × 3 repetitions | |

**Full list of the 89 remaining RE2-OB cases:**

```
re2ob_checkoutservice_cpu_2         re2ob_currencyservice_cpu_1
re2ob_checkoutservice_cpu_3         re2ob_currencyservice_cpu_2
re2ob_checkoutservice_delay_1       re2ob_currencyservice_cpu_3
re2ob_checkoutservice_delay_2       re2ob_currencyservice_delay_1
re2ob_checkoutservice_delay_3       re2ob_currencyservice_delay_2
re2ob_checkoutservice_disk_1        re2ob_currencyservice_delay_3
re2ob_checkoutservice_disk_2        re2ob_currencyservice_disk_1
re2ob_checkoutservice_disk_3        re2ob_currencyservice_disk_2
re2ob_checkoutservice_loss_1        re2ob_currencyservice_disk_3
re2ob_checkoutservice_loss_2        re2ob_currencyservice_loss_1
re2ob_checkoutservice_loss_3        re2ob_currencyservice_loss_2
re2ob_checkoutservice_mem_1         re2ob_currencyservice_loss_3
re2ob_checkoutservice_mem_2         re2ob_currencyservice_mem_1
re2ob_checkoutservice_mem_3         re2ob_currencyservice_mem_2
re2ob_checkoutservice_socket_1      re2ob_currencyservice_mem_3
re2ob_checkoutservice_socket_2      re2ob_currencyservice_socket_1
re2ob_checkoutservice_socket_3      re2ob_currencyservice_socket_2
                                    re2ob_currencyservice_socket_3

re2ob_emailservice_cpu_1            re2ob_productcatalogservice_cpu_1
re2ob_emailservice_cpu_2            re2ob_productcatalogservice_cpu_2
re2ob_emailservice_cpu_3            re2ob_productcatalogservice_cpu_3
re2ob_emailservice_delay_1          re2ob_productcatalogservice_delay_1
re2ob_emailservice_delay_2          re2ob_productcatalogservice_delay_2
re2ob_emailservice_delay_3          re2ob_productcatalogservice_delay_3
re2ob_emailservice_disk_1           re2ob_productcatalogservice_disk_1
re2ob_emailservice_disk_2           re2ob_productcatalogservice_disk_2
re2ob_emailservice_disk_3           re2ob_productcatalogservice_disk_3
re2ob_emailservice_loss_1           re2ob_productcatalogservice_loss_1
re2ob_emailservice_loss_2           re2ob_productcatalogservice_loss_2
re2ob_emailservice_loss_3           re2ob_productcatalogservice_loss_3
re2ob_emailservice_mem_1            re2ob_productcatalogservice_mem_1
re2ob_emailservice_mem_2            re2ob_productcatalogservice_mem_2
re2ob_emailservice_mem_3            re2ob_productcatalogservice_mem_3
re2ob_emailservice_socket_1         re2ob_productcatalogservice_socket_1
re2ob_emailservice_socket_2         re2ob_productcatalogservice_socket_2
re2ob_emailservice_socket_3         re2ob_productcatalogservice_socket_3

re2ob_recommendationservice_cpu_1
re2ob_recommendationservice_cpu_2
re2ob_recommendationservice_cpu_3
re2ob_recommendationservice_delay_1
re2ob_recommendationservice_delay_2
re2ob_recommendationservice_delay_3
re2ob_recommendationservice_disk_1
re2ob_recommendationservice_disk_2
re2ob_recommendationservice_disk_3
re2ob_recommendationservice_loss_1
re2ob_recommendationservice_loss_2
re2ob_recommendationservice_loss_3
re2ob_recommendationservice_mem_1
re2ob_recommendationservice_mem_2
re2ob_recommendationservice_mem_3
re2ob_recommendationservice_socket_1
re2ob_recommendationservice_socket_2
re2ob_recommendationservice_socket_3
```

### 🟡 NICE TO HAVE — RE2-TT, RE3-OB, RE3-TT (149 cases)
Download these after completing RE2-OB evaluation, to show cross-system generalization.

| Suite | Cases | App | Extra Value |
|---|---:|---|---|
| RE2-TT | 89 | Train Ticket (Java microservices) | Different stack from Google OB |
| RE3-OB | 30 | Online Boutique (code faults f1–f5) | Harder faults beyond infra |
| RE3-TT | 30 | Train Ticket (code faults f1–f5) | Harder faults |

---

## Where to Download From

The RCAEval dataset is available from 3 official sources:

### Option 1 — Hugging Face (Recommended — Parquet format)
```
https://huggingface.co/datasets/phamquiluan/RCAEval
```
Full dataset ~3.4 GB in Parquet format (same format our adapter already reads).

```python
# Install huggingface_hub
pip install huggingface_hub

# Download specific cases
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="phamquiluan/RCAEval",
    repo_type="dataset",
    allow_patterns=["re2ob_*"],       # only RE2-OB cases
    local_dir="data/ground_truth/rcaeval/"
)
```

### Option 2 — GitHub Repository
```
https://github.com/phamquiluan/RCAEval
```
Clone repo and use their utility functions:
```bash
git clone https://github.com/phamquiluan/RCAEval.git
pip install -e RCAEval/
```

### Option 3 — Zenodo / Figshare (zip archives)
```
Zenodo:  https://zenodo.org/records/14590730
Figshare: https://figshare.com/articles/dataset/RCAEval_A_Benchmark_for_Root_Cause_Analysis_of_Microservice_Systems/31048672
```
Download the zip file for RE2 and extract into `data/ground_truth/rcaeval/`.

---

## Minimum Download for a Working Demo (6 cases)

If you don't want to download all 89 cases right now, download at least **1 case per fault type** so you can test all 5 edge cases (EC-1 through EC-5):

| Download this case | Fault | Tests |
|---|---|---|
| ✅ `re2ob_checkoutservice_cpu_1` | cpu | EC-2 Latency (already downloaded) |
| `re2ob_checkoutservice_mem_1` | mem | EC-1 OOM |
| `re2ob_checkoutservice_delay_1` | delay | EC-2 Latency (network) |
| `re2ob_checkoutservice_loss_1` | loss | EC-5 Network Partition |
| `re2ob_checkoutservice_socket_1` | socket | EC-3 Cascade |
| `re2ob_checkoutservice_disk_1` | disk | EC-5 Infra |

**5 more cases = ~55 MB total. Download these first.**

---

## Download Progress Tracker

| Suite | Total | Downloaded | Remaining | Status |
|---|---:|---:|---:|---|
| cases.parquet (registry) | 1 file | ✅ 1 | 0 | Done |
| RE2-OB (Priority) | 90 | 1 | **89** | 🔴 Needed |
| RE2-TT (Secondary) | 89 | 0 | 89 | 🟡 Later |
| RE3-OB (Secondary) | 30 | 0 | 30 | 🟡 Later |
| RE3-TT (Secondary) | 30 | 0 | 30 | 🟡 Later |
| **Total needed for paper** | **239** | **1** | **238** | |

Update this table as you download cases.
