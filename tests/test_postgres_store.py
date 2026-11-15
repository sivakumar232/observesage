"""
Tests for PostgreSQL Storage Layer (Connection, Schema, Upsert, Artifact JSONB, Summary Aggregation).
"""

import pytest

from src.db.postgres_store import PostgresStore
from src.schemas.artifact import (
    CaseAnalysisArtifact,
    EvaluationRecord,
    EvidenceBundle,
    FusionDebugInfo,
    PromptAuditInfo,
    StageTiming,
)
from src.schemas.rca_report import EvidenceTriangulation, FailureCategory, RCAReport


def test_postgres_store_roundtrip():
    store = PostgresStore()
    try:
        store.init_db()
    except Exception as exc:
        pytest.skip(f"PostgreSQL container not reachable: {exc}")

    # Build synthetic artifact
    test_case_id = "test_unit_checkoutservice_cpu_1"
    art = CaseAnalysisArtifact(
        schema_version="1.0.0",
        case_id=test_case_id,
        mode="fusion",
        engine_type="TELEMETRY_RAG_LOCAL",
        created_at="2026-11-14T10:00:00Z",
        source="live",
        case_metadata={"dataset": "TEST"},
        stage_timings=[StageTiming(stage="fusion", duration_ms=10.0)],
        evidence=EvidenceBundle(),
        fusion=FusionDebugInfo(initial_confidence=0.92),
        prompt=PromptAuditInfo(total_tokens=150, prompt_preview="test prompt"),
        report=RCAReport(
            run_id=test_case_id,
            scenario="test_cpu",
            root_cause_service="checkoutservice",
            culprit_services=["checkoutservice", "frontend"],
            failure_category=FailureCategory.EC_2_LATENCY,
            confidence_score=0.92,
            root_cause_summary="Unit test root cause summary",
            evidence_triangulation=EvidenceTriangulation(
                primary_signal="FUSION",
                triangulation_reasoning="Unit test triangulation reasoning",
            ),
        ),
        evaluation=EvaluationRecord(
            case_id=test_case_id,
            ground_truth_service="checkoutservice",
            predicted_service="checkoutservice",
            ranked_candidates=["checkoutservice", "frontend"],
            ground_truth_fault="cpu",
            predicted_category=FailureCategory.EC_2_LATENCY.value,
            top1_hit=True,
            top3_hit=True,
            mrr=1.0,
            fault_match=True,
            error_bucket="hit",
            confidence=0.92,
            prompt_tokens=150,
            duration_ms=100.0,
            engine_type="TELEMETRY_RAG_LOCAL",
        ),
    )

    # Save to PostgreSQL
    store.save_artifact(art)

    # Load from PostgreSQL
    loaded = store.load_artifact(test_case_id, "fusion")
    assert loaded is not None
    assert loaded.case_id == test_case_id
    assert loaded.report.root_cause_service == "checkoutservice"
    assert loaded.evaluation.top1_hit is True
    assert loaded.evaluation.mrr == 1.0

    # Query mode summary
    summary = store.get_mode_summary("fusion")
    assert summary is not None
    assert summary["total_cases"] >= 1
    assert "cpu" in summary["by_fault_type"]

    # Cleanup test row
    with store.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM evaluation_results WHERE case_id = %s;", (test_case_id,))
