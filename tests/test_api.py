"""
Unit & Integration tests for ObservaSage FastAPI Dashboard, Artifact Inspection & WebSocket API.
"""

from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from src.api import create_app
from tests.test_pipeline_orchestrator import _create_synthetic_case


def _build_test_client(tmp_path: Path) -> TestClient:
    cases_root = tmp_path / "rcaeval"
    _create_synthetic_case(cases_root / "re2ob_checkoutservice_mem_1")

    registry_path = cases_root / "cases.parquet"
    pd.DataFrame(
        {
            "case": ["re2ob_checkoutservice_mem_1"],
            "dataset": ["RE2-OB"],
            "root_cause_service": ["checkoutservice"],
            "fault": ["mem"],
            "has_logs": [True],
            "has_traces": [True],
        }
    ).to_parquet(registry_path, index=False)

    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
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
    ).to_csv(results_dir / "ablation_fusion.csv", index=False)

    app = create_app(cases_root=cases_root, results_dir=results_dir)
    return TestClient(app)


def test_api_health_cases_and_index(tmp_path: Path):
    client = _build_test_client(tmp_path)

    r_idx = client.get("/")
    assert r_idx.status_code == 200
    assert "ObservaSage" in r_idx.text

    r_fav = client.get("/favicon.ico")
    assert r_fav.status_code == 204

    r_health = client.get("/api/health")
    assert r_health.status_code == 200
    assert r_health.json()["cases_available"] == 1

    r_cases = client.get("/api/cases?suite=RE2-OB&fault_type=mem")
    assert r_cases.status_code == 200
    assert r_cases.json()["total"] == 1
    case_item = r_cases.json()["cases"][0]
    assert case_item["case_id"] == "re2ob_checkoutservice_mem_1"
    assert case_item["scored"] is True
    assert case_item["has_artifact"] is False

    r_detail = client.get("/api/case/re2ob_checkoutservice_mem_1")
    assert r_detail.status_code == 200
    assert r_detail.json()["ground_truth_service"] == "checkoutservice"
    assert r_detail.json()["telemetry_files"]["logs.parquet"]["exists"] is True


def test_api_analyze_artifact_compare_and_security(tmp_path: Path):
    client = _build_test_client(tmp_path)

    # Path traversal blocked
    r_bad = client.post("/api/analyze", json={"case_id": "../etc", "mode": "fusion"})
    assert r_bad.status_code == 400

    # Missing case
    r_missing = client.post("/api/analyze", json={"case_id": "nonexistent_case", "mode": "fusion"})
    assert r_missing.status_code == 404

    # Before artifact exists, /analysis falls back cleanly to CSV summary
    r_pre = client.get("/api/case/re2ob_checkoutservice_mem_1/analysis?mode=fusion")
    assert r_pre.status_code == 200
    assert r_pre.json()["found"] is True
    assert r_pre.json()["has_artifact"] is False
    assert r_pre.json()["score"]["predicted_service"] == "checkoutservice"

    # POST /api/analyze runs live once, persists artifact, and returns full schema
    r_live = client.post(
        "/api/analyze",
        json={"case_id": "re2ob_checkoutservice_mem_1", "mode": "fusion", "use_llm": False},
    )
    assert r_live.status_code == 200
    live_body = r_live.json()
    assert live_body["cached"] is False
    assert live_body["has_artifact"] is True
    assert live_body["report"]["root_cause_service"] == "checkoutservice"
    assert live_body["artifact"]["evidence"]["summary_counts"]["log_snippets"] >= 1
    assert live_body["artifact"]["fusion"]["ranked_services"]
    assert live_body["prompt_tokens"] < 2500

    # Second POST /api/analyze hits persisted artifact cache and still returns full report + artifact
    r_cached = client.post(
        "/api/analyze",
        json={"case_id": "re2ob_checkoutservice_mem_1", "mode": "fusion", "use_llm": False},
    )
    assert r_cached.status_code == 200
    cached_body = r_cached.json()
    assert cached_body["cached"] is True
    assert cached_body["report"]["root_cause_service"] == "checkoutservice"
    assert cached_body["artifact"]["prompt"]["prompt_preview"]

    # Cross-mode comparison for single case
    r_cmp_case = client.get("/api/case/re2ob_checkoutservice_mem_1/compare")
    assert r_cmp_case.status_code == 200
    assert len(r_cmp_case.json()["modes"]) == 4
    fusion_row = next(m for m in r_cmp_case.json()["modes"] if m["mode"] == "fusion")
    assert fusion_row["scored"] is True
    assert fusion_row["has_artifact"] is True

    # Evaluate & compare aggregates (includes error_bucket and has_artifact)
    r_eval = client.get("/api/evaluate?mode=fusion")
    assert r_eval.status_code == 200
    assert r_eval.json()["summary"]["overall_top1"] == 1.0
    assert r_eval.json()["cases"][0]["error_bucket"] == "hit"
    assert r_eval.json()["cases"][0]["has_artifact"] is True

    # GET /analysis?recompute=true executes live and returns full artifact
    r_recomp = client.get("/api/case/re2ob_checkoutservice_mem_1/analysis?mode=fusion&recompute=true")
    assert r_recomp.status_code == 200
    assert r_recomp.json()["cached"] is False
    assert r_recomp.json()["has_artifact"] is True
    assert len(r_recomp.json()["artifact"]["evidence"]["trace_evidence"]["error_spans"]) >= 1

    r_cmp = client.get("/api/compare?modes=fusion")
    assert r_cmp.status_code == 200
    assert len(r_cmp.json()["modes"]) == 1


def test_websocket_diagnose(tmp_path: Path):
    client = _build_test_client(tmp_path)

    # First run: no artifact yet -> streams ingesting, retrieving, fusing, complete
    with client.websocket_connect("/ws/diagnose") as ws:
        ws.send_json({"case_id": "re2ob_checkoutservice_mem_1", "mode": "fusion", "use_llm": False})
        stages = []
        while True:
            msg = ws.receive_json()
            stages.append(msg["stage"])
            if msg["stage"] == "complete":
                assert msg["cached"] is False
                assert msg["report"]["root_cause_service"] == "checkoutservice"
                assert msg["artifact"]["evidence"]["summary_counts"]["log_snippets"] >= 1
                break
        assert stages == ["ingesting", "retrieving", "fusing", "complete"]

    # Second run: artifact persisted -> immediate complete with full artifact
    with client.websocket_connect("/ws/diagnose") as ws:
        ws.send_json({"case_id": "re2ob_checkoutservice_mem_1", "mode": "fusion", "use_llm": False})
        msg = ws.receive_json()
        assert msg["stage"] == "complete"
        assert msg["cached"] is True
        assert msg["report"]["root_cause_service"] == "checkoutservice"
        assert msg["artifact"]["fusion"]["ranked_services"]
