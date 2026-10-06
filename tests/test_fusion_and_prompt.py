"""
Unit tests for Checkpoint 3: Multi-Signal Fusion & Dynamic Token Gate.
Verifies:
1. Exact BPE token counting with tiktoken.
2. Strict < 2,500 token ceiling guarantee under heavy telemetry payloads.
3. 4-way ablation prompt modes (logs-only, logs-metrics, logs-traces, fusion).
4. Zero scenario name leakage into the diagnostic prompt.
5. Cross-modal consensus scoring and culprit prioritization in FusionEngine.
"""

from src.fusion import FusionEngine
from src.llm.prompt import (
    build_multimodal_prompt,
    count_bpe_tokens,
    truncate_to_tokens,
)
from src.schemas.evidence import (
    LogEvidence,
    LogSnippet,
    MetricAlert,
    MetricEvidence,
    TraceEvidence,
    TraceSpanEvidence,
)
from src.schemas.rca_report import FailureCategory


def test_bpe_token_counting_and_truncation():
    sample_text = "Service 'cartservice' threw System.InvalidOperationException: Connection timed out after 3000ms."
    token_count = count_bpe_tokens(sample_text)
    assert token_count > 5
    assert token_count < 30

    long_text = "error line with identifier " * 500
    truncated = truncate_to_tokens(long_text, max_tokens=100)
    assert count_bpe_tokens(truncated) <= 100
    assert "[truncated" in truncated


def test_multimodal_prompt_token_budget_guarantee():
    # Construct large payloads
    huge_log = "2023-03-28T12:00:00Z [ERROR] Failed to query productcatalog items with exception details\n" * 400
    log_ev = {
        "frontend": LogEvidence(
            service="frontend",
            total_raw_lines=500,
            novel_templates_count=10,
            snippets=[LogSnippet(service="frontend", line_number=1, target_line="error", context_after=[])],
            formatted_prompt=huge_log,
        )
    }

    metric_ev = MetricEvidence(
        total_metrics_evaluated=50,
        alerts=[
            MetricAlert(
                metric_name="container_memory_bytes",
                service="cartservice",
                current_value=200000000.0,
                baseline_mean=50000000.0,
                baseline_std=5000000.0,
                z_score=30.0,
                is_oom_risk=True,
                severity="CRITICAL",
                description="Memory spike",
            )
        ],
        formatted_prompt="### Metric Anomaly Alerts:\n- [CRITICAL] cartservice memory spike",
    )

    trace_ev = TraceEvidence(
        total_traces=10,
        total_spans=50,
        error_spans_count=3,
        culprit_service="paymentservice",
        formatted_prompt="### Distributed Trace Analysis:\n- Leaf Culprit: paymentservice",
    )

    prompt = build_multimodal_prompt(
        run_id="run_stress_test_001",
        log_evidences=log_ev,
        metric_evidence=metric_ev,
        trace_evidence=trace_ev,
        mode="fusion",
        max_total_tokens=2400,
    )

    # STRICT BUDGET ASSERTION
    actual_tokens = count_bpe_tokens(prompt)
    assert actual_tokens <= 2400
    # ZERO SCENARIO LEAKAGE ASSERTION
    assert "scenario" not in prompt.lower()
    assert "cartservice_mem" not in prompt


def test_ablation_prompt_modes():
    log_ev = {"frontend": LogEvidence(service="frontend", formatted_prompt="LOG_FRONTEND_DATA")}
    metric_ev = MetricEvidence(formatted_prompt="METRIC_DATA")
    trace_ev = TraceEvidence(formatted_prompt="TRACE_DATA")

    # Mode 1: logs-only
    p_logs = build_multimodal_prompt("id1", log_ev, metric_ev, trace_ev, mode="logs-only")
    assert "LOG_FRONTEND_DATA" in p_logs
    assert "METRIC_DATA" not in p_logs
    assert "TRACE_DATA" not in p_logs

    # Mode 2: logs-metrics
    p_lm = build_multimodal_prompt("id1", log_ev, metric_ev, trace_ev, mode="logs-metrics")
    assert "LOG_FRONTEND_DATA" in p_lm
    assert "METRIC_DATA" in p_lm
    assert "TRACE_DATA" not in p_lm

    # Mode 3: logs-traces
    p_lt = build_multimodal_prompt("id1", log_ev, metric_ev, trace_ev, mode="logs-traces")
    assert "LOG_FRONTEND_DATA" in p_lt
    assert "METRIC_DATA" not in p_lt
    assert "TRACE_DATA" in p_lt

    # Mode 4: fusion
    p_fus = build_multimodal_prompt("id1", log_ev, metric_ev, trace_ev, mode="fusion")
    assert "LOG_FRONTEND_DATA" in p_fus
    assert "METRIC_DATA" in p_fus
    assert "TRACE_DATA" in p_fus


def test_fusion_engine_leaf_culprit_priority():
    engine = FusionEngine()

    # Superficial error in frontend logs (HTTP 500 caller symptom)
    log_ev = {
        "frontend": LogEvidence(
            service="frontend",
            total_raw_lines=100,
            snippets=[LogSnippet(service="frontend", line_number=10, target_line="500 Internal Server Error")],
        )
    }

    # Trace identifies paymentservice as leaf culprit
    trace_ev = TraceEvidence(
        total_traces=1,
        total_spans=3,
        error_spans_count=2,
        root_service="frontend",
        culprit_service="paymentservice",
        culprit_span=TraceSpanEvidence(
            trace_id="t1",
            span_id="s_pay",
            service_name="paymentservice",
            operation_name="/Charge",
            duration_ms=3000.0,
            status_code=14,
            is_leaf_culprit=True,
            depth=2,
        ),
    )

    ranked, failure_cat, triangulation, confidence = engine.correlate(
        log_evidences=log_ev,
        metric_evidence=None,
        trace_evidence=trace_ev,
    )

    # CRITICAL: Trace leaf 'paymentservice' prioritized over superficial 'frontend' log
    assert ranked[0] == "paymentservice"
    assert failure_cat == FailureCategory.EC_3_CASCADE
    assert triangulation.primary_signal in ["TRACES", "FUSION"]
    assert "paymentservice" in triangulation.triangulation_reasoning
    assert confidence >= 0.70


def test_topological_causal_graph_propagation():
    """
    Verifies that when caller (frontend) and callee (cartservice) both log errors,
    the topological dependency graph dampens the caller and attributes causality to callee.
    """
    engine = FusionEngine()

    # Both log errors
    log_ev = {
        "frontend": LogEvidence(
            service="frontend",
            total_raw_lines=50,
            snippets=[LogSnippet(service="frontend", line_number=5, target_line="Failed to connect to cartservice")],
        ),
        "cartservice": LogEvidence(
            service="cartservice",
            total_raw_lines=50,
            snippets=[LogSnippet(service="cartservice", line_number=12, target_line="Out of memory crash")],
        ),
    }

    # Dependency graph shows frontend -> cartservice
    trace_ev = TraceEvidence(
        total_traces=1,
        total_spans=2,
        error_spans_count=1,
        culprit_service="cartservice",
        service_dependency_graph={"frontend": ["cartservice"]},
    )

    ranked, _, _, _ = engine.correlate(log_evidences=log_ev, trace_evidence=trace_ev)
    assert ranked[0] == "cartservice"


def test_dynamic_bayesian_confidence_and_hypothesis_scoring():
    """
    Verifies that multi-signal consensus produces mathematically continuous
    Bayesian confidence (not static constants), and evaluates competing hypotheses dynamically.
    """
    engine = FusionEngine()

    # Weak single-signal evidence
    log_ev_weak = {
        "currencyservice": LogEvidence(
            service="currencyservice",
            snippets=[LogSnippet(service="currencyservice", line_number=1, target_line="error reading rate")],
        )
    }
    _, cat_weak, _, conf_weak = engine.correlate(log_evidences=log_ev_weak)
    assert 0.50 <= conf_weak <= 0.75

    # Strong tri-modal corroboration
    from src.schemas.evidence import MetricAlert
    metric_ev_strong = MetricEvidence(
        alerts=[
            MetricAlert(
                metric_name="currencyservice/mem",
                service="currencyservice",
                current_value=150.0,
                baseline_mean=50.0,
                baseline_std=5.0,
                z_score=20.0,
                is_oom_risk=True,
                time_to_oom_seconds=45.0,
                description="OOM crash",
            )
        ],
        has_oom_alert=True,
    )
    trace_ev_strong = TraceEvidence(
        total_traces=1,
        total_spans=3,
        error_spans_count=2,
        culprit_service="currencyservice",
        culprit_span=TraceSpanEvidence(
            trace_id="t1",
            span_id="s1",
            service_name="currencyservice",
            operation_name="/convert",
            duration_ms=1000.0,
            depth=2,
            is_leaf_culprit=True,
        ),
    )

    ranked, cat_strong, _, conf_strong = engine.correlate(
        log_evidences=log_ev_weak,
        metric_evidence=metric_ev_strong,
        trace_evidence=trace_ev_strong,
    )

    # Multi-signal Bayesian agreement should dynamically yield high confidence (> 0.90)
    assert conf_strong >= 0.90
    assert cat_strong == FailureCategory.EC_1_OOM
    assert ranked[0] == "currencyservice"


