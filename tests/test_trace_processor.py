"""
Unit tests for Checkpoint 2: TraceProcessor.
Verifies:
1. Dynamic service name extraction from span tags.
2. Call DAG reconstruction from parent references.
3. DFS leaf culprit localization (isolating true root cause from cascading 500 callers).
4. Clean trace handling with zero false positives.
"""

from src.schemas.telemetry import Trace, TraceSpan
from src.trace_processor import TraceProcessor, extract_service_from_span


def test_extract_service_from_span():
    span1 = TraceSpan(
        traceID="t1",
        spanID="s1",
        operationName="op1",
        startTime=1000,
        duration=100,
        tags=[{"key": "service.name", "value": "emailservice"}],
    )
    assert extract_service_from_span(span1) == "emailservice"

    span2 = TraceSpan(
        traceID="t2",
        spanID="s2",
        operationName="op2",
        startTime=1000,
        duration=100,
        tags=[{"key": "app", "value": "cartservice"}],
    )
    assert extract_service_from_span(span2) == "cartservice"


def test_dfs_leaf_culprit_extraction_cascading_failure():
    """
    Simulates a cascading microservice failure:
    frontend (HTTP 500) -> checkoutservice (HTTP 500) -> paymentservice (DEAD/UNAVAILABLE)
    Verifies that paymentservice (leaf) is selected as culprit, NOT frontend.
    """
    processor = TraceProcessor()

    # Create 3-tier trace DAG
    spans = [
        # Root span: frontend
        TraceSpan(
            traceID="tr_cascade_1",
            spanID="sp_frontend",
            operationName="/checkout",
            startTime=1000000,
            duration=3200000,  # 3200ms
            tags=[
                {"key": "service.name", "value": "frontend"},
                {"key": "http.status_code", "value": 500},
                {"key": "error", "value": True},
            ],
            references=[],
        ),
        # Middle span: checkoutservice (child of frontend)
        TraceSpan(
            traceID="tr_cascade_1",
            spanID="sp_checkout",
            operationName="/PlaceOrder",
            startTime=1050000,
            duration=3100000,  # 3100ms
            tags=[
                {"key": "service.name", "value": "checkoutservice"},
                {"key": "http.status_code", "value": 500},
                {"key": "error", "value": True},
            ],
            references=[{"refType": "CHILD_OF", "traceID": "tr_cascade_1", "spanID": "sp_frontend"}],
        ),
        # Leaf span: paymentservice (child of checkoutservice, actual error origin)
        TraceSpan(
            traceID="tr_cascade_1",
            spanID="sp_payment",
            operationName="/Charge",
            startTime=1100000,
            duration=3000000,  # 3000ms
            tags=[
                {"key": "service.name", "value": "paymentservice"},
                {"key": "grpc.status_code", "value": 14},  # UNAVAILABLE
                {"key": "error", "value": True},
                {"key": "error.message", "value": "connection refused on port 50051"},
            ],
            references=[{"refType": "CHILD_OF", "traceID": "tr_cascade_1", "spanID": "sp_checkout"}],
        ),
    ]

    trace = Trace(traceID="tr_cascade_1", spans=spans)
    evidence = processor.process([trace])

    assert evidence.total_traces == 1
    assert evidence.error_spans_count == 3
    assert evidence.root_service == "frontend"

    # CRITICAL CHECK: Culprit is the leaf paymentservice, NOT frontend
    assert evidence.culprit_service == "paymentservice"
    assert evidence.culprit_span is not None
    assert evidence.culprit_span.service_name == "paymentservice"
    assert evidence.culprit_span.operation_name == "/Charge"
    assert evidence.culprit_span.depth == 2
    assert evidence.culprit_span.is_leaf_culprit is True
    assert "paymentservice" in evidence.call_hierarchy_summary
    assert "LEAF CULPRIT" in evidence.call_hierarchy_summary


def test_clean_traces_no_errors():
    processor = TraceProcessor()

    spans = [
        TraceSpan(
            traceID="tr_clean",
            spanID="sp_1",
            operationName="/home",
            startTime=1000,
            duration=25000,  # 25ms
            tags=[{"key": "service.name", "value": "frontend"}, {"key": "http.status_code", "value": 200}],
        ),
        TraceSpan(
            traceID="tr_clean",
            spanID="sp_2",
            operationName="/getProducts",
            startTime=1010,
            duration=15000,
            tags=[{"key": "service.name", "value": "productcatalogservice"}],
            references=[{"refType": "CHILD_OF", "traceID": "tr_clean", "spanID": "sp_1"}],
        ),
    ]

    trace = Trace(traceID="tr_clean", spans=spans)
    evidence = processor.process([trace])

    assert evidence.total_traces == 1
    assert evidence.error_spans_count == 0
    assert evidence.culprit_service is None
    assert evidence.culprit_span is None
