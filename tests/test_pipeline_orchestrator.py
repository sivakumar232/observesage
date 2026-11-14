"""
Unit tests for Phase 3: ObservaSage Dataset-Only Pipeline Orchestrator.
Tests case discovery, single-case execution, batch ablation CSV persistence, and crash-safe resume.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from src.pipeline import ObservaSagePipeline


def _create_synthetic_case(case_dir: Path, inject_ts: float = 1700000600.0) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "inject_time.txt").write_text(str(int(inject_ts)), encoding="utf-8")

    # Minimal logs.parquet
    pd.DataFrame(
        {
            "timestamp": [inject_ts - 60, inject_ts + 10],
            "container_name": ["checkoutservice", "checkoutservice"],
            "message": [
                "INFO Health check passed",
                "ERROR OutOfMemoryError: Java heap space exceeded",
            ],
        }
    ).to_parquet(case_dir / "logs.parquet", index=False)

    # Minimal metrics.parquet
    pd.DataFrame(
        {
            "time": [inject_ts - 120, inject_ts - 60, inject_ts + 10, inject_ts + 30],
            "checkoutservice_mem": [100.0, 102.0, 450.0, 620.0],
            "cartservice_cpu": [10.0, 11.0, 10.5, 10.2],
        }
    ).to_parquet(case_dir / "metrics.parquet", index=False)

    # Minimal traces.parquet
    pd.DataFrame(
        {
            "startTime": [int((inject_ts + 10) * 1e6)],
            "traceID": ["tr_1"],
            "spanID": ["sp_1"],
            "parentSpanID": [""],
            "serviceName": ["checkoutservice"],
            "operationName": ["/PlaceOrder"],
            "duration": [250000],
            "statusCode": [500],
        }
    ).to_parquet(case_dir / "traces.parquet", index=False)


def test_pipeline_discover_and_run_batch(tmp_path: Path):
    cases_root = tmp_path / "rcaeval"
    case_a = cases_root / "re2ob_checkoutservice_mem_1"
    case_b = cases_root / "re2ob_cartservice_cpu_1"
    _create_synthetic_case(case_a)
    _create_synthetic_case(case_b)

    registry_path = cases_root / "cases.parquet"
    pd.DataFrame(
        {
            "case": ["re2ob_checkoutservice_mem_1", "re2ob_cartservice_cpu_1", "re1ob_missing_1"],
            "dataset": ["RE2-OB", "RE2-OB", "RE1-OB"],
            "root_cause_service": ["checkoutservice", "cartservice", "adservice"],
            "fault": ["mem", "cpu", "cpu"],
            "has_logs": [True, True, False],
            "has_traces": [True, True, False],
        }
    ).to_parquet(registry_path, index=False)

    mock_client = MagicMock()
    mock_client.is_configured = False

    pipeline = ObservaSagePipeline(
        cases_root=cases_root,
        registry_path=registry_path,
        llm_client=mock_client,
        strict_live=False,
    )

    discovered = pipeline.discover_cases(suite_filter=["RE2-OB"])
    assert len(discovered) == 2
    assert {c["case_id"] for c in discovered} == {"re2ob_checkoutservice_mem_1", "re2ob_cartservice_cpu_1"}

    out_dir = tmp_path / "results"
    results = pipeline.run_batch(modes=["logs-only", "fusion"], output_dir=out_dir, resume=False)

    assert len(results["logs-only"]) == 2
    assert len(results["fusion"]) == 2
    assert (out_dir / "ablation_logs_only.csv").exists()
    assert (out_dir / "ablation_fusion.csv").exists()
    assert (out_dir / "paper_table.md").exists()
    assert (out_dir / "case_comparison.csv").exists()
    cmp_text = (out_dir / "case_comparison.csv").read_text(encoding="utf-8")
    assert "fusion_artifact" in cmp_text
    assert "re2ob_checkoutservice_mem_1" in cmp_text

    # Verify per-case JSON artifacts were persisted for both modes
    art_path = ObservaSagePipeline.artifact_path(out_dir, "re2ob_checkoutservice_mem_1", "fusion")
    assert art_path.exists()
    loaded = ObservaSagePipeline.load_artifact(out_dir, "re2ob_checkoutservice_mem_1", "fusion")
    assert loaded is not None
    assert loaded.source == "cache"
    assert loaded.case_id == "re2ob_checkoutservice_mem_1"
    assert loaded.mode == "fusion"
    assert loaded.evidence.summary_counts["log_snippets"] >= 1
    assert loaded.fusion.ranked_services
    assert loaded.prompt.total_tokens < 2500

    # Verify resume skips already-completed cases without duplicating rows
    resumed = pipeline.run_batch(modes=["fusion"], output_dir=out_dir, resume=True)
    assert len(resumed["fusion"]) == 2
    csv_lines = (out_dir / "ablation_fusion.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(csv_lines) == 3  # 1 header + 2 rows


def test_staged_pipeline_and_artifact_roundtrip(tmp_path: Path):
    cases_root = tmp_path / "rcaeval"
    case_dir = cases_root / "re2ob_checkoutservice_mem_1"
    _create_synthetic_case(case_dir)

    pipeline = ObservaSagePipeline(cases_root=cases_root, use_llm=False)
    snapshot, gt_info = pipeline.load_case(case_dir)
    rag_ctx = pipeline.retrieve_context(snapshot, mode="fusion")

    report, score, artifact = pipeline.run_case_full(
        case_dir=case_dir,
        mode="fusion",
        ground_truth_service="checkoutservice",
        fault_type="mem",
        snapshot=snapshot,
        gt_info=gt_info,
        rag_ctx=rag_ctx,
    )

    assert report.root_cause_service == "checkoutservice"
    assert score.top1_hit is True
    assert artifact.evaluation.error_bucket == ("hit" if score.fault_match else "category_miss")
    assert any(st.stage == "ingesting" and st.status == "skipped" for st in artifact.stage_timings)
    assert any(st.stage == "retrieving" and st.status == "skipped" for st in artifact.stage_timings)

    out_dir = tmp_path / "results"
    saved_path = ObservaSagePipeline.save_artifact(artifact, out_dir)
    assert saved_path.exists()
    reloaded = ObservaSagePipeline.load_artifact(out_dir, "re2ob_checkoutservice_mem_1", "fusion")
    assert reloaded is not None
    assert reloaded.report.root_cause_service == "checkoutservice"
    assert reloaded.evaluation.top1_hit is True


def test_partial_telemetry_silent_fallback_artifact(tmp_path: Path):
    """Verifies empty logs/traces still produce a valid CaseAnalysisArtifact without crashing."""
    cases_root = tmp_path / "rcaeval"
    case_dir = cases_root / "re2ob_empty_case_1"
    case_dir.mkdir(parents=True, exist_ok=True)
    inject_ts = 1700000600.0
    (case_dir / "inject_time.txt").write_text(str(int(inject_ts)), encoding="utf-8")
    pd.DataFrame(columns=["timestamp", "container_name", "message"]).to_parquet(case_dir / "logs.parquet", index=False)
    pd.DataFrame({"time": [inject_ts - 60, inject_ts + 10], "svc_cpu": [10.0, 11.0]}).to_parquet(case_dir / "metrics.parquet", index=False)
    pd.DataFrame(columns=["startTime", "traceID", "spanID", "parentSpanID", "serviceName", "operationName", "duration", "statusCode"]).to_parquet(case_dir / "traces.parquet", index=False)

    pipeline = ObservaSagePipeline(cases_root=cases_root, use_llm=False)
    report, _score, artifact = pipeline.run_case_full(
        case_dir=case_dir,
        mode="fusion",
        ground_truth_service="svc",
        fault_type="cpu",
    )
    assert report.run_id
    assert artifact.evidence.summary_counts["log_snippets"] == 0
    assert artifact.evaluation.error_bucket in {
        "hit",
        "ranking_miss",
        "localization_miss",
        "category_miss",
        "localization_and_category_miss",
    }


def test_backfill_artifacts_preserves_csv_and_generates_json(tmp_path: Path):
    """Verifies --backfill-artifacts generates missing JSON artifacts without duplicating CSV rows."""
    cases_root = tmp_path / "rcaeval"
    case_a = cases_root / "re2ob_checkoutservice_mem_1"
    _create_synthetic_case(case_a)

    out_dir = tmp_path / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "case_id": "re2ob_checkoutservice_mem_1",
                "ground_truth_service": "checkoutservice",
                "predicted_service": "checkoutservice",
                "ranked_candidates": "checkoutservice",
                "ground_truth_fault": "mem",
                "predicted_category": "EC-1: OOM / Resource Exhaustion",
                "top1_hit": True,
                "top3_hit": True,
                "mrr": 1.0,
                "fault_match": True,
                "confidence": 0.92,
                "prompt_tokens": 350,
                "duration_ms": 120.0,
                "engine_type": "TELEMETRY_RAG_LOCAL",
            }
        ]
    ).to_csv(out_dir / "ablation_fusion.csv", index=False)

    assert not ObservaSagePipeline.artifact_path(out_dir, "re2ob_checkoutservice_mem_1", "fusion").exists()

    pipeline = ObservaSagePipeline(cases_root=cases_root, use_llm=False)
    pipeline.run_batch(modes=["fusion"], output_dir=out_dir, resume=True, backfill_artifacts=True)

    assert ObservaSagePipeline.artifact_path(out_dir, "re2ob_checkoutservice_mem_1", "fusion").exists()
    assert (out_dir / "case_comparison.csv").exists()
    assert (out_dir / "paper_table.md").exists()
    csv_lines = (out_dir / "ablation_fusion.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(csv_lines) == 2  # 1 header + 1 row (no duplicate)

    art = ObservaSagePipeline.load_artifact(out_dir, "re2ob_checkoutservice_mem_1", "fusion")
    assert art is not None
    # Verify ranked_services is sorted descending by score
    scores = [s.score for s in art.fusion.ranked_services]
    assert scores == sorted(scores, reverse=True)
    assert len(art.evidence.trace_evidence.error_spans) >= 1
