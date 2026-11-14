### ObservaSage 4-Way Ablation Comparison

- **Dataset Scope:** RE2-OB
- **Engine Mode:** `TELEMETRY_RAG_LOCAL`

| Mode | Cases | Top@1 | Top@3 | MRR | Category Acc |
|---|:---:|:---:|:---:|:---:|:---:|
| `logs-only` | 90 | 10.00% | 10.00% | 0.1000 | 0.00% |
| `logs-metrics` | 90 | 60.00% | 93.33% | 0.7481 | 54.44% |
| `logs-traces` | 90 | 24.44% | 30.00% | 0.2722 | 26.67% |
| `fusion` | 90 | 71.11% | 96.67% | 0.8296 | 42.22% |

### ObservaSage Benchmark Results (logs-only)
- Total Evaluated Cases: 90
- Overall Top@1 Accuracy: 10.00%
- Overall Top@3 Accuracy: 10.00%
- Overall Mean Reciprocal Rank (MRR): 0.1000

| Fault Type | Cases | Top@1 Accuracy | Top@3 Accuracy | MRR | Fault Class Acc |
|---|:---:|:---:|:---:|:---:|:---:|
| **CPU** | 15 | 0.00% | 0.00% | 0.000 | 0.00% |
| **DELAY** | 15 | 6.67% | 6.67% | 0.067 | 0.00% |
| **DISK** | 15 | 20.00% | 20.00% | 0.200 | 0.00% |
| **LOSS** | 15 | 26.67% | 26.67% | 0.267 | 0.00% |
| **MEM** | 15 | 6.67% | 6.67% | 0.067 | 0.00% |
| **SOCKET** | 15 | 0.00% | 0.00% | 0.000 | 0.00% |
| **Overall** | **90** | **10.00%** | **10.00%** | **0.100** | **0.00%** |

- Error Buckets: `category_miss`=9, `localization_and_category_miss`=81

### ObservaSage Benchmark Results (logs-metrics)
- Total Evaluated Cases: 90
- Overall Top@1 Accuracy: 60.00%
- Overall Top@3 Accuracy: 93.33%
- Overall Mean Reciprocal Rank (MRR): 0.7481

| Fault Type | Cases | Top@1 Accuracy | Top@3 Accuracy | MRR | Fault Class Acc |
|---|:---:|:---:|:---:|:---:|:---:|
| **CPU** | 15 | 73.33% | 93.33% | 0.822 | 80.00% |
| **DELAY** | 15 | 40.00% | 80.00% | 0.567 | 66.67% |
| **DISK** | 15 | 86.67% | 100.00% | 0.933 | 86.67% |
| **LOSS** | 15 | 33.33% | 86.67% | 0.556 | 26.67% |
| **MEM** | 15 | 60.00% | 100.00% | 0.789 | 66.67% |
| **SOCKET** | 15 | 66.67% | 100.00% | 0.822 | 0.00% |
| **Overall** | **90** | **60.00%** | **93.33%** | **0.748** | **54.44%** |

- Error Buckets: `category_miss`=21, `hit`=33, `localization_and_category_miss`=20, `localization_miss`=2, `ranking_miss`=14

### ObservaSage Benchmark Results (logs-traces)
- Total Evaluated Cases: 90
- Overall Top@1 Accuracy: 24.44%
- Overall Top@3 Accuracy: 30.00%
- Overall Mean Reciprocal Rank (MRR): 0.2722

| Fault Type | Cases | Top@1 Accuracy | Top@3 Accuracy | MRR | Fault Class Acc |
|---|:---:|:---:|:---:|:---:|:---:|
| **CPU** | 15 | 6.67% | 13.33% | 0.100 | 80.00% |
| **DELAY** | 15 | 33.33% | 40.00% | 0.367 | 66.67% |
| **DISK** | 15 | 26.67% | 26.67% | 0.267 | 0.00% |
| **LOSS** | 15 | 33.33% | 46.67% | 0.400 | 13.33% |
| **MEM** | 15 | 20.00% | 20.00% | 0.200 | 0.00% |
| **SOCKET** | 15 | 26.67% | 33.33% | 0.300 | 0.00% |
| **Overall** | **90** | **24.44%** | **30.00%** | **0.272** | **26.67%** |

- Error Buckets: `category_miss`=16, `hit`=6, `localization_and_category_miss`=50, `localization_miss`=18

### ObservaSage Benchmark Results (fusion)
- Total Evaluated Cases: 90
- Overall Top@1 Accuracy: 71.11%
- Overall Top@3 Accuracy: 96.67%
- Overall Mean Reciprocal Rank (MRR): 0.8296

| Fault Type | Cases | Top@1 Accuracy | Top@3 Accuracy | MRR | Fault Class Acc |
|---|:---:|:---:|:---:|:---:|:---:|
| **CPU** | 15 | 80.00% | 86.67% | 0.833 | 100.00% |
| **DELAY** | 15 | 73.33% | 100.00% | 0.856 | 100.00% |
| **DISK** | 15 | 80.00% | 100.00% | 0.889 | 0.00% |
| **LOSS** | 15 | 46.67% | 100.00% | 0.711 | 46.67% |
| **MEM** | 15 | 60.00% | 93.33% | 0.756 | 6.67% |
| **SOCKET** | 15 | 86.67% | 100.00% | 0.933 | 0.00% |
| **Overall** | **90** | **71.11%** | **96.67%** | **0.830** | **42.22%** |

- Error Buckets: `category_miss`=37, `hit`=27, `localization_and_category_miss`=15, `localization_miss`=2, `ranking_miss`=9
