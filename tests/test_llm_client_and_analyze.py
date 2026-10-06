"""
Unit tests for Checkpoint 4: Structured LLM Generation & Resilience Layer.
Verifies:
1. Strict evaluation gating (fails loudly when allow_heuristic=False without live API key).
2. Synchronization of culprit_services (Top-k) with root_cause_service.
3. End-to-end multi-signal execution of analyze pipeline across all 3 modalities.
"""

import pytest
from src.llm import GeminiRCAClient
from src.schemas import (
    FailureCategory,
    RCAReport,
    TelemetrySnapshot,
)
from src.fusion import FusionEngine
from src.log_processor import LogSageProcessor
from src.metrics_processor import MetricsProcessor
from src.trace_processor import TraceProcessor
from src.llm.prompt import build_multimodal_prompt


def test_client_strict_evaluation_gating():
    # Force client with no API key
    client = GeminiRCAClient(api_key=None)
    client._genai_client = None

    # In strict evaluation mode, MUST raise RuntimeError
    with pytest.raises(RuntimeError, match="Academic benchmark evaluation requires a live Gemini API key"):
        client.diagnose(run_id="run_eval_001", scenario="test", user_prompt="test prompt", allow_heuristic=False)

    # In offline fallback mode, returns deterministic heuristic report
    report = client.diagnose(
        run_id="run_eval_001", scenario="test", user_prompt="oom memory error", allow_heuristic=True
    )
    assert report.root_cause_service == "cartservice"
    assert report.failure_category == FailureCategory.EC_1_OOM
    assert len(report.culprit_services) > 0


def test_rca_report_ranked_culprits_sync():
    # Test 1: Only root_cause_service provided -> culprit_services populated automatically
    report1 = RCAReport(
        run_id="r1",
        scenario="s1",
        root_cause_service="paymentservice",
        failure_category=FailureCategory.EC_3_CASCADE,
        confidence_score=0.9,
        root_cause_summary="summary",
        evidence_triangulation={
            "primary_signal": "TRACES",
            "triangulation_reasoning": "reasoning",
        },
    )
    assert report1.culprit_services == ["paymentservice"]

    # Test 2: Ranked list provided -> root_cause_service matches top element
    report2 = RCAReport(
        run_id="r2",
        scenario="s2",
        root_cause_service="",
        culprit_services=["cartservice", "redis-cart", "frontend"],
        failure_category=FailureCategory.EC_1_OOM,
        confidence_score=0.95,
        root_cause_summary="summary",
        evidence_triangulation={
            "primary_signal": "METRICS",
            "triangulation_reasoning": "reasoning",
        },
    )
    assert report2.root_cause_service == "cartservice"


def test_tri_modal_analysis_pipeline_integration():
    """
    Verifies that the entire tri-modal pipeline runs seamlessly:
    LogProcessor + MetricsProcessor + TraceProcessor + FusionEngine + MultimodalPrompt.
    """
    # 1. Synthesize minimal snapshot
    data = {
        "run_id": "test_pipeline_run",
        "logs": {
            "cartservice": ["2026-10-06T10:00:00Z [ERROR] KILLED: Out of memory"],
            "frontend": ["2026-10-06T10:00:01Z [WARN] Failed to fetch cart items"],
        },
        "metrics": [
            {
                "metric_name": "cartservice/container_memory_bytes",
                "query": "rcaeval",
                "data": [
                    {"timestamp": 100.0, "value": 50000000.0},
                    {"timestamp": 105.0, "value": 51000000.0},
                    {"timestamp": 110.0, "value": 52000000.0},
                    {"timestamp": 115.0, "value": 200000000.0},
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
                        "startTime": 100000000,
                        "duration": 500000,
                        "tags": [
                            {"key": "service.name", "value": "cartservice"},
                            {"key": "error", "value": True},
                        ],
                    }
                ],
            }
        ],
    }
    snapshot = TelemetrySnapshot(**data)

    # 2. Run log processor
    log_proc = LogSageProcessor()
    log_ev = log_proc.process_all_logs(snapshot.logs)
    assert "cartservice" in log_ev

    # 3. Run metric processor
    metric_proc = MetricsProcessor()
    metric_ev = metric_proc.process(snapshot.metrics)
    assert metric_ev.total_metrics_evaluated == 1

    # 4. Run trace processor
    trace_proc = TraceProcessor()
    trace_ev = trace_proc.process(snapshot.traces)
    assert trace_ev.culprit_service == "cartservice"

    # 5. Run fusion engine
    fusion = FusionEngine()
    ranked, failure_cat, triangulation, conf = fusion.correlate(log_ev, metric_ev, trace_ev)
    assert ranked[0] == "cartservice"
    assert triangulation.primary_signal in ["METRICS", "FUSION"]

    # 6. Build multimodal prompt
    prompt = build_multimodal_prompt(
        run_id=snapshot.run_id,
        log_evidences=log_ev,
        metric_evidence=metric_ev,
        trace_evidence=trace_ev,
        mode="fusion",
    )
    assert "cartservice" in prompt
    assert "Metric Anomaly Alerts" in prompt
    assert "Distributed Trace Analysis" in prompt
