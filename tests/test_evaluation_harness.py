"""
Unit tests for Checkpoint 5: Automated Evaluation & Ablation Harness.
Verifies:
1. Bidirectional taxonomy mapping between RCAEval fault labels and ObservaSage FailureCategory.
2. Scientific scoring calculations: Top@1, Top@3, MRR (Mean Reciprocal Rank), and fault classification.
3. Result aggregation and Markdown ablation table formatting for paper publication.
4. End-to-end evaluation pipeline execution across all 4 ablation modes.
"""

from scripts.evaluate_rcaeval import run_diagnosis_pipeline
from src.eval import (
    CaseScore,
    aggregate_scores,
    format_ablation_table,
    is_fault_category_match,
    map_rcaeval_to_failure_category,
    score_single_case,
)
from src.llm import GeminiRCAClient
from src.schemas import (
    FailureCategory,
    RCAReport,
    TelemetrySnapshot,
)


def test_taxonomy_mapping():
    assert map_rcaeval_to_failure_category("MEM") == FailureCategory.EC_1_OOM
    assert map_rcaeval_to_failure_category("DELAY") == FailureCategory.EC_2_LATENCY
    assert map_rcaeval_to_failure_category("LOSS") == FailureCategory.EC_2_LATENCY
    assert map_rcaeval_to_failure_category("CRASH") == FailureCategory.EC_3_CASCADE
    assert map_rcaeval_to_failure_category("SOCKET") == FailureCategory.EC_5_INFRA
    assert map_rcaeval_to_failure_category("UNKNOWN_FAULT") == FailureCategory.UNKNOWN

    assert is_fault_category_match(FailureCategory.EC_1_OOM, "MEM") is True
    assert is_fault_category_match(FailureCategory.EC_2_LATENCY, "DELAY") is True
    assert is_fault_category_match(FailureCategory.EC_1_OOM, "DELAY") is False


def test_case_scoring_top1_top3_and_mrr():
    # Scenario 1: Perfect Top@1 match
    report1 = RCAReport(
        run_id="case1",
        scenario="test",
        root_cause_service="cartservice",
        culprit_services=["cartservice", "redis-cart"],
        failure_category=FailureCategory.EC_1_OOM,
        confidence_score=0.95,
        root_cause_summary="summary",
        evidence_triangulation={"primary_signal": "METRICS", "triangulation_reasoning": "reasoning"},
    )
    score1 = score_single_case("case1", "cartservice", "MEM", report1)
    assert score1.top1_hit is True
    assert score1.top3_hit is True
    assert score1.mrr == 1.0
    assert score1.fault_match is True

    # Scenario 2: Ground truth in 2nd rank (Top@3 hit, MRR = 0.5)
    report2 = RCAReport(
        run_id="case2",
        scenario="test",
        root_cause_service="frontend",
        culprit_services=["frontend", "paymentservice", "checkoutservice"],
        failure_category=FailureCategory.EC_3_CASCADE,
        confidence_score=0.8,
        root_cause_summary="summary",
        evidence_triangulation={"primary_signal": "TRACES", "triangulation_reasoning": "reasoning"},
    )
    score2 = score_single_case("case2", "paymentservice", "CRASH", report2)
    assert score2.top1_hit is False
    assert score2.top3_hit is True
    assert score2.mrr == 0.5
    assert score2.fault_match is True

    # Scenario 3: Miss (not in candidates)
    score3 = score_single_case("case3", "emailservice", "MEM", report1)
    assert score3.top1_hit is False
    assert score3.top3_hit is False
    assert score3.mrr == 0.0


def test_aggregate_scores_and_markdown_table():
    scores = [
        CaseScore("c1", "cartservice", "cartservice", ["cartservice"], "MEM", "EC-1: OOM", True, True, 1.0, True, 0.9, 100),
        CaseScore("c2", "currencyservice", "frontend", ["frontend", "currencyservice"], "DELAY", "EC-2", False, True, 0.5, True, 0.8, 120),
        CaseScore("c3", "paymentservice", "paymentservice", ["paymentservice"], "CRASH", "EC-3", True, True, 1.0, True, 0.95, 110),
    ]

    summary = aggregate_scores(scores)
    assert summary["total_cases"] == 3
    assert abs(summary["overall_top1"] - 2 / 3) < 1e-4
    assert summary["overall_top3"] == 1.0
    assert abs(summary["overall_mrr"] - (1.0 + 0.5 + 1.0) / 3) < 1e-4

    table_md = format_ablation_table(scores, mode_label="fusion")
    assert "MEM" in table_md
    assert "DELAY" in table_md
    assert "Overall" in table_md
    assert "Top@1 Accuracy" in table_md


def test_diagnosis_pipeline_all_ablation_modes():
    """Verifies that run_diagnosis_pipeline succeeds across all 4 ablation configurations."""
    client = GeminiRCAClient(api_key=None)
    client._genai_client = None  # Ensure simulated offline runner for unit test

    data = {
        "run_id": "test_ablation_case",
        "logs": {"cartservice": ["[ERROR] KILLED: Out of memory"]},
        "metrics": [
            {
                "metric_name": "cartservice/container_memory_bytes",
                "query": "rcaeval",
                "data": [
                    {"timestamp": 100.0, "value": 50000000.0},
                    {"timestamp": 105.0, "value": 150000000.0},
                ],
            }
        ],
        "traces": [
            {
                "traceID": "t1",
                "spans": [
                    {
                        "traceID": "t1",
                        "spanID": "s1",
                        "operationName": "/cart",
                        "startTime": 100000,
                        "duration": 50000,
                        "tags": [{"key": "service.name", "value": "cartservice"}, {"key": "error", "value": True}],
                    }
                ],
            }
        ],
    }
    snapshot = TelemetrySnapshot(**data)

    for mode in ["logs-only", "logs-metrics", "logs-traces", "fusion"]:
        report = run_diagnosis_pipeline(
            snapshot=snapshot,
            mode=mode,
            client=client,
            strict_live=False,
        )
        assert report.run_id == "test_ablation_case"
        assert len(report.culprit_services) > 0
