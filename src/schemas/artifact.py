"""
Versioned Pydantic schemas for persisted per-case ObservaSage diagnostic artifacts.

One `CaseAnalysisArtifact` JSON file captures everything needed to inspect and
re-render a single `(case_id, mode)` evaluation without recomputing telemetry:
- Case metadata & ground truth
- Per-stage execution timings and statuses
- Serialized localized evidence across Logs, Metrics, and Traces
- Fusion ranking scores, signal votes, and category likelihoods
- Prompt token breakdown & prompt preview
- Final RCAReport and CaseScore evaluation record
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from src.schemas.evidence import LogEvidence, MetricEvidence, TraceEvidence
from src.schemas.rca_report import RCAReport

ErrorBucket = Literal[
    "hit",
    "ranking_miss",
    "localization_miss",
    "category_miss",
    "localization_and_category_miss",
]


def classify_error_bucket(top1_hit: bool, top3_hit: bool, fault_match: bool) -> ErrorBucket:
    """Classifies a case evaluation outcome into a granular error bucket."""
    if top1_hit and fault_match:
        return "hit"
    if top1_hit and not fault_match:
        return "category_miss"
    if not top1_hit and top3_hit:
        return "ranking_miss" if fault_match else "localization_and_category_miss"
    return "localization_miss" if fault_match else "localization_and_category_miss"


class StageTiming(BaseModel):
    stage: str
    status: Literal["ok", "skipped", "fallback", "error"] = "ok"
    duration_ms: float = 0.0
    detail: str | None = None


class EvidenceBundle(BaseModel):
    log_evidences: dict[str, LogEvidence] = Field(default_factory=dict)
    metric_evidence: MetricEvidence | None = None
    trace_evidence: TraceEvidence | None = None
    summary_counts: dict[str, int] = Field(default_factory=dict)


class ServiceFusionDetail(BaseModel):
    service: str
    score: float
    votes: list[str] = Field(default_factory=list)


class FusionDebugInfo(BaseModel):
    ranked_services: list[ServiceFusionDetail] = Field(default_factory=list)
    signal_votes: dict[str, list[str]] = Field(default_factory=dict)
    category_likelihoods: dict[str, float] = Field(default_factory=dict)
    hypothesized_category: str = ""
    initial_confidence: float = 0.0


class PromptAuditInfo(BaseModel):
    max_tokens: int = 2400
    total_tokens: int = 0
    logs_tokens: int = 0
    metrics_tokens: int = 0
    traces_tokens: int = 0
    prompt_preview: str = ""


class EvaluationRecord(BaseModel):
    case_id: str
    ground_truth_service: str
    predicted_service: str
    ranked_candidates: list[str] = Field(default_factory=list)
    ground_truth_fault: str
    predicted_category: str
    top1_hit: bool
    top3_hit: bool
    mrr: float
    fault_match: bool
    error_bucket: ErrorBucket = "hit"
    confidence: float
    prompt_tokens: int
    duration_ms: float = 0.0
    engine_type: str = "TELEMETRY_RAG_LOCAL"


class CaseAnalysisArtifact(BaseModel):
    """Canonical persisted per-case, per-mode diagnostic artifact."""

    schema_version: str = "1.0.0"
    case_id: str
    mode: str
    engine_type: str
    created_at: str
    source: Literal["live", "cache"] = "live"
    case_metadata: dict[str, Any] = Field(default_factory=dict)
    stage_timings: list[StageTiming] = Field(default_factory=list)
    evidence: EvidenceBundle
    fusion: FusionDebugInfo
    prompt: PromptAuditInfo
    report: RCAReport
    evaluation: EvaluationRecord
