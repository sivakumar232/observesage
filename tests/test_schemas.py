"""
Unit tests for ObservaSage Pydantic v2 schemas.
"""

import json
from pathlib import Path
from src.schemas import (
    PipelineRunMeta,
    TelemetrySnapshot,
    LogSnippet,
    LogEvidence,
    MetricAlert,
    MetricEvidence,
    TraceSpanEvidence,
    TraceEvidence,
    FailureCategory,
    EvidenceTriangulation,
    RemediationStep,
    RCAReport,
)


def test_real_telemetry_snapshot_deserialization():
    file_path = Path("data/runs/success/run_20261004_202823_a1f6cc_telemetry.json")
    assert file_path.exists(), "Telemetry fixture missing"
    with open(file_path) as f:
        raw = json.load(f)

    snapshot = TelemetrySnapshot(**raw)
    assert snapshot.run_id == "run_20261004_202823_a1f6cc"
    assert len(snapshot.logs) > 0
    assert len(snapshot.metrics) > 0
    assert len(snapshot.traces) > 0


def test_evidence_schemas():
    log_snip = LogSnippet(
        service="cartservice",
        line_number=42,
        matched_keyword="error",
        template_id="cluster_1",
        is_novel=True,
        context_before=["info 1", "info 2", "info 3"],
        target_line="error: Redis connection timeout",
        context_after=["recovery attempted", "failed", "giving up"],
    )
    log_evidence = LogEvidence(
        service="cartservice",
        total_raw_lines=100,
        novel_templates_count=1,
        snippets=[log_snip],
        formatted_prompt="[cartservice] error: Redis connection timeout",
        estimated_tokens=50,
    )
    assert log_evidence.novel_templates_count == 1
    assert len(log_evidence.snippets) == 1

    metric_alert = MetricAlert(
        metric_name="container_memory_rss",
        service="cartservice",
        current_value=29000000.0,
        baseline_mean=18000000.0,
        baseline_std=1000000.0,
        z_score=11.0,
        is_threshold_exceeded=True,
        is_oom_risk=True,
        slope_dM_dt=150000.0,
        severity="CRITICAL",
        description="Memory usage nearing cgroup threshold with steep slope",
    )
    assert metric_alert.is_oom_risk is True

    span_evidence = TraceSpanEvidence(
        trace_id="a1b2c3d4",
        span_id="span_leaf_99",
        parent_span_id="span_root_01",
        service_name="paymentservice",
        operation_name="/Charge",
        duration_ms=45.2,
        status_code=500,
        error_code="UNAVAILABLE",
        error_message="dial tcp paymentservice:50051: connect: connection refused",
        is_leaf_culprit=True,
        depth=3,
    )
    assert span_evidence.is_leaf_culprit is True


def test_rca_report_serialization():
    report = RCAReport(
        run_id="run_test_001",
        scenario="ec3_crash_payment",
        root_cause_service="paymentservice",
        failure_category=FailureCategory.EC_3_CASCADE,
        confidence_score=0.98,
        root_cause_summary="PaymentService container unceremoniously terminated, causing upstream checkout to fail.",
        evidence_triangulation=EvidenceTriangulation(
            primary_signal="TRACES",
            logs_insight="CheckoutService logs show connection refused to paymentservice:50051.",
            metrics_insight="No resource anomalies detected prior to failure.",
            traces_insight="Jaeger trace reveals leaf error span at paymentservice with UNAVAILABLE status code.",
            triangulation_reasoning="Trace DFS identified paymentservice as the deepest failing span despite checkoutservice failing first.",
        ),
        culprit_trace_id="a1b2c3d4",
        culprit_span_id="span_leaf_99",
        remediation_steps=[
            RemediationStep(
                action="Restart and inspect container health",
                target_service="paymentservice",
                command_or_config="docker restart paymentservice",
                expected_impact="Restores gRPC checkout processing",
            )
        ],
        prompt_tokens_used=1200,
        total_tokens_used=1450,
    )

    data = report.model_dump()
    assert data["confidence_score"] == 0.98
    assert data["failure_category"] == "EC-3: Cascading Service Crash"
    assert len(data["remediation_steps"]) == 1
