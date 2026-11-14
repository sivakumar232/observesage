"""
REST API routes for the ObservaSage Dashboard.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.eval.metrics import aggregate_scores
from src.pipeline.orchestrator import ALL_MODES, AblationMode, ObservaSagePipeline
from src.schemas import CaseAnalysisArtifact, classify_error_bucket

router = APIRouter(prefix="/api", tags=["ObservaSage"])

_FAULT_LABEL: dict[str, str] = {
    "cpu": "CPU Stress",
    "mem": "Memory Pressure",
    "delay": "Network Delay",
    "loss": "Packet Loss",
    "socket-connection": "Socket Congestion",
}


class AnalyzeRequest(BaseModel):
    case_id: str = Field(..., min_length=1, max_length=128)
    mode: AblationMode = "fusion"
    use_llm: bool = False
    force_recompute: bool = False


def _get_paths(request: Request) -> tuple[Path, Path]:
    cases_root: Path = request.app.state.cases_root
    results_dir: Path = request.app.state.results_dir
    return cases_root, results_dir


def _resolve_case_dir(cases_root: Path, case_id: str) -> Path:
    """Resolves a case directory safely, preventing path traversal."""
    if "/" in case_id or "\\" in case_id or ".." in case_id:
        raise HTTPException(status_code=400, detail="Invalid case_id")
    candidate = (cases_root / case_id).resolve()
    root_resolved = cases_root.resolve()
    if not candidate.is_relative_to(root_resolved) or not candidate.is_dir():
        raise HTTPException(status_code=404, detail=f"Case '{case_id}' not found")
    return candidate


def _validate_case_id(case_id: str) -> None:
    if "/" in case_id or "\\" in case_id or ".." in case_id:
        raise HTTPException(status_code=400, detail="Invalid case_id")


def _load_csv_as_rows(csv_path: Path) -> list[dict[str, str]]:
    """Returns raw CSV rows as a list of dicts. Empty if file absent."""
    if not csv_path.exists():
        return []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _csv_row_to_score_dict(row: dict[str, str]) -> dict[str, Any]:
    cands = [s for s in row.get("ranked_candidates", "").split(";") if s]
    top1 = row.get("top1_hit", "").lower() == "true"
    top3 = row.get("top3_hit", "").lower() == "true"
    fmatch = row.get("fault_match", "").lower() == "true"
    return {
        "case_id": row.get("case_id", "").strip(),
        "ground_truth_service": row.get("ground_truth_service", ""),
        "predicted_service": row.get("predicted_service", ""),
        "ranked_candidates": cands,
        "ground_truth_fault": row.get("ground_truth_fault", ""),
        "predicted_category": row.get("predicted_category", ""),
        "top1_hit": top1,
        "top3_hit": top3,
        "mrr": float(row.get("mrr", 0) or 0),
        "fault_match": fmatch,
        "error_bucket": classify_error_bucket(top1, top3, fmatch),
        "confidence": float(row.get("confidence", 0) or 0),
        "prompt_tokens": int(row.get("prompt_tokens", 0) or 0),
        "duration_ms": float(row.get("duration_ms", 0) or 0),
        "engine_type": row.get("engine_type", ""),
    }


def artifact_to_api_payload(artifact: CaseAnalysisArtifact) -> dict[str, Any]:
    """Canonical API response shape shared by REST and WebSocket endpoints."""
    dep_graph = (
        artifact.evidence.trace_evidence.service_dependency_graph
        if artifact.evidence.trace_evidence
        else {}
    )
    return {
        "case_id": artifact.case_id,
        "mode": artifact.mode,
        "cached": artifact.source == "cache",
        "source": artifact.source,
        "has_artifact": True,
        "artifact": artifact.model_dump(mode="json"),
        "report": artifact.report.model_dump(mode="json"),
        "score": artifact.evaluation.model_dump(mode="json"),
        "service_dependency_graph": dep_graph,
        "prompt_tokens": artifact.prompt.total_tokens,
    }


def _per_fault_breakdown(scores_map: dict) -> dict[str, dict[str, Any]]:
    """Groups CaseScore objects by ground_truth_fault and returns per-fault metrics."""
    groups: dict[str, list] = defaultdict(list)
    for s in scores_map.values():
        groups[s.ground_truth_fault].append(s)

    breakdown: dict[str, dict[str, Any]] = {}
    for fault, group in sorted(groups.items()):
        total = len(group)
        top1 = sum(1 for s in group if s.top1_hit)
        top3 = sum(1 for s in group if s.top3_hit)
        mrr = sum(s.mrr for s in group) / total if total else 0.0
        breakdown[fault] = {
            "fault": fault,
            "label": _FAULT_LABEL.get(fault, fault),
            "total": total,
            "top1_pct": round(top1 / total * 100, 1) if total else 0.0,
            "top3_pct": round(top3 / total * 100, 1) if total else 0.0,
            "mrr": round(mrr, 4),
        }
    return breakdown


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    cases_root, results_dir = _get_paths(request)
    pipeline = ObservaSagePipeline(cases_root=cases_root, use_llm=False)
    cases = pipeline.discover_cases()
    return {
        "status": "ok",
        "version": "1.0.0",
        "cases_available": len(cases),
        "results_dir": str(results_dir),
    }


@router.get("/cases")
def list_cases(
    request: Request,
    suite: str | None = Query(default=None, description="Filter by dataset suite, e.g. RE2-OB"),
    fault_type: str | None = Query(default=None, description="Filter by fault type, e.g. cpu, mem"),
    limit: int = Query(default=500, ge=1, le=1000),
    mode: str = Query(default="fusion", description="Ablation mode to merge scores from"),
) -> dict[str, Any]:
    """Returns discovered cases merged with pre-computed evaluation scores and artifact flags."""
    cases_root, results_dir = _get_paths(request)
    pipeline = ObservaSagePipeline(cases_root=cases_root, use_llm=False)
    cases = pipeline.discover_cases(
        suite_filter=[suite] if suite else None,
        fault_filter=[fault_type] if fault_type else None,
        limit=limit,
    )

    csv_path = results_dir / f"ablation_{mode.replace('-', '_')}.csv"
    score_rows = {row["case_id"]: row for row in _load_csv_as_rows(csv_path)}

    enriched: list[dict[str, Any]] = []
    for c in cases:
        cid = c["case_id"]
        sc = score_rows.get(cid)
        entry: dict[str, Any] = {**c}
        entry["has_artifact"] = ObservaSagePipeline.artifact_path(results_dir, cid, mode).exists()
        if sc:
            parsed = _csv_row_to_score_dict(sc)
            entry["scored"] = True
            entry["top1_hit"] = parsed["top1_hit"]
            entry["top3_hit"] = parsed["top3_hit"]
            entry["mrr"] = parsed["mrr"]
            entry["predicted_service"] = parsed["predicted_service"]
            entry["confidence"] = parsed["confidence"]
            entry["fault_match"] = parsed["fault_match"]
            entry["error_bucket"] = parsed["error_bucket"]
            entry["engine_type"] = parsed["engine_type"]
        else:
            entry["scored"] = False
        enriched.append(entry)

    return {"total": len(enriched), "cases": enriched, "mode": mode}


@router.get("/case/{case_id}")
def get_case_detail(request: Request, case_id: str) -> dict[str, Any]:
    """Returns canonical metadata, ground truth, and telemetry file availability for a case."""
    cases_root, results_dir = _get_paths(request)
    case_dir = _resolve_case_dir(cases_root, case_id)

    pipeline = ObservaSagePipeline(cases_root=cases_root, use_llm=False)
    gt_info = pipeline.adapter.extract_ground_truth(case_dir)

    files_info = {}
    for fname in ("logs.parquet", "metrics.parquet", "traces.parquet", "inject_time.txt"):
        fpath = case_dir / fname
        files_info[fname] = {"exists": fpath.exists(), "size_bytes": fpath.stat().st_size if fpath.exists() else 0}

    artifacts_available = [
        m for m in ALL_MODES if ObservaSagePipeline.artifact_path(results_dir, case_id, m).exists()
    ]
    return {
        "case_id": case_id,
        "ground_truth_service": gt_info.get("ground_truth_service", "unknown"),
        "ground_truth_fault": gt_info.get("ground_truth_fault_type", "unknown"),
        "inject_time": gt_info.get("inject_time"),
        "telemetry_files": files_info,
        "artifacts_available": artifacts_available,
    }


@router.get("/case/{case_id}/analysis")
def get_case_analysis(
    request: Request,
    case_id: str,
    mode: AblationMode = "fusion",
    recompute: bool = False,
    use_llm: bool = False,
) -> dict[str, Any]:
    """
    Returns the full CaseAnalysisArtifact for `(case_id, mode)` from disk if available,
    or falls back to CSV summary score. If `recompute=true`, executes the pipeline once
    and persists the artifact.
    """
    cases_root, results_dir = _get_paths(request)
    case_dir = _resolve_case_dir(cases_root, case_id)

    if not recompute:
        artifact = ObservaSagePipeline.load_artifact(results_dir, case_id, mode)
        if artifact is not None:
            return {"found": True, **artifact_to_api_payload(artifact)}

        # Fallback to CSV summary if artifact hasn't been generated yet
        csv_path = results_dir / f"ablation_{mode.replace('-', '_')}.csv"
        for row in _load_csv_as_rows(csv_path):
            if row.get("case_id", "").strip() == case_id:
                score_dict = _csv_row_to_score_dict(row)
                return {
                    "found": True,
                    "has_artifact": False,
                    "cached": True,
                    "source": "csv_summary",
                    "case_id": case_id,
                    "mode": mode,
                    "score": score_dict,
                    "artifact": None,
                    "report": None,
                    "service_dependency_graph": {},
                    "prompt_tokens": score_dict["prompt_tokens"],
                }
        return {"found": False, "has_artifact": False, "case_id": case_id, "mode": mode}

    pipeline = ObservaSagePipeline(cases_root=cases_root, use_llm=use_llm)
    _, _, artifact = pipeline.run_case_full(case_dir=case_dir, mode=mode)
    pipeline.save_artifact(artifact, results_dir)
    return {"found": True, **artifact_to_api_payload(artifact)}


@router.get("/case/{case_id}/compare")
def compare_case_across_modes(request: Request, case_id: str) -> dict[str, Any]:
    """Returns side-by-side evaluation outcomes for a single case across all 4 ablation modes."""
    _validate_case_id(case_id)
    _, results_dir = _get_paths(request)
    modes_out: list[dict[str, Any]] = []

    for mode in ALL_MODES:
        has_art = ObservaSagePipeline.artifact_path(results_dir, case_id, mode).exists()
        artifact = ObservaSagePipeline.load_artifact(results_dir, case_id, mode) if has_art else None
        if artifact is not None:
            ev = artifact.evaluation
            modes_out.append(
                {
                    "mode": mode,
                    "scored": True,
                    "has_artifact": True,
                    **ev.model_dump(mode="json"),
                }
            )
            continue

        csv_path = results_dir / f"ablation_{mode.replace('-', '_')}.csv"
        row_match = next(
            (r for r in _load_csv_as_rows(csv_path) if r.get("case_id", "").strip() == case_id),
            None,
        )
        if row_match:
            modes_out.append(
                {
                    "mode": mode,
                    "scored": True,
                    "has_artifact": False,
                    **_csv_row_to_score_dict(row_match),
                }
            )
        else:
            modes_out.append({"mode": mode, "scored": False, "has_artifact": False})

    return {"case_id": case_id, "modes": modes_out}


@router.get("/case/{case_id}/score")
def get_case_score(
    request: Request,
    case_id: str,
    mode: str = Query(default="fusion", description="Ablation mode"),
) -> dict[str, Any]:
    """Backwards-compatible score endpoint reading from the results CSV."""
    _validate_case_id(case_id)
    _, results_dir = _get_paths(request)
    csv_path = results_dir / f"ablation_{mode.replace('-', '_')}.csv"
    for row in _load_csv_as_rows(csv_path):
        if row.get("case_id", "").strip() == case_id:
            return {"mode": mode, "found": True, **_csv_row_to_score_dict(row)}
    return {"case_id": case_id, "mode": mode, "found": False}


@router.post("/analyze")
def analyze_case(request: Request, body: AnalyzeRequest) -> dict[str, Any]:
    """
    Runs or loads canonical RCA for a single case. Always returns the full
    CaseAnalysisArtifact payload (never `report: null`).
    """
    cases_root, results_dir = _get_paths(request)
    case_dir = _resolve_case_dir(cases_root, body.case_id)

    if not body.force_recompute and not body.use_llm:
        cached_art = ObservaSagePipeline.load_artifact(results_dir, body.case_id, body.mode)
        if cached_art is not None:
            return artifact_to_api_payload(cached_art)

    pipeline = ObservaSagePipeline(cases_root=cases_root, use_llm=body.use_llm)
    _, _, artifact = pipeline.run_case_full(case_dir=case_dir, mode=body.mode)
    pipeline.save_artifact(artifact, results_dir)
    return artifact_to_api_payload(artifact)


@router.get("/evaluate")
def evaluate_mode(
    request: Request,
    mode: Literal["logs-only", "logs-metrics", "logs-traces", "fusion"] = "fusion",
) -> dict[str, Any]:
    _, results_dir = _get_paths(request)
    csv_path = results_dir / f"ablation_{mode.replace('-', '_')}.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail=f"No evaluation CSV found for mode '{mode}'")

    scores_map = ObservaSagePipeline._load_existing_scores(csv_path)
    scores = list(scores_map.values())
    summary = aggregate_scores(scores)
    breakdown = _per_fault_breakdown(scores_map)
    enriched_cases = []
    for s in scores:
        d = s.to_dict()
        d["error_bucket"] = classify_error_bucket(s.top1_hit, s.top3_hit, s.fault_match)
        d["has_artifact"] = ObservaSagePipeline.artifact_path(results_dir, s.case_id, mode).exists()
        enriched_cases.append(d)
    return {
        "mode": mode,
        "summary": summary,
        "by_fault_type": breakdown,
        "cases": enriched_cases,
    }


@router.get("/compare")
def compare_modes(
    request: Request,
    modes: str = Query(default=",".join(ALL_MODES)),
) -> dict[str, Any]:
    _, results_dir = _get_paths(request)
    requested = [m.strip() for m in modes.split(",") if m.strip()]
    comparison: list[dict[str, Any]] = []

    for mode in requested:
        csv_path = results_dir / f"ablation_{mode.replace('-', '_')}.csv"
        if not csv_path.exists():
            continue
        scores_map = ObservaSagePipeline._load_existing_scores(csv_path)
        scores = list(scores_map.values())
        summary = aggregate_scores(scores)
        breakdown = _per_fault_breakdown(scores_map)
        comparison.append({"mode": mode, **summary, "by_fault_type": breakdown})

    return {"modes": comparison}
