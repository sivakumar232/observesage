"""
ObservaSage Core Schemas Package
"""

from src.schemas.artifact import (
    CaseAnalysisArtifact,
    ErrorBucket,
    EvaluationRecord,
    EvidenceBundle,
    FusionDebugInfo,
    PromptAuditInfo,
    ServiceFusionDetail,
    StageTiming,
    classify_error_bucket,
)
from src.schemas.evidence import (
    LogEvidence,
    LogSnippet,
    MetricAlert,
    MetricEvidence,
    TraceEvidence,
    TraceSpanEvidence,
)
from src.schemas.rca_report import (
    EvidenceTriangulation,
    FailureCategory,
    RCAReport,
    RemediationStep,
)
from src.schemas.telemetry import (
    MetricSeries,
    PipelineRunMeta,
    PipelineStep,
    TelemetrySnapshot,
    Trace,
    TraceSpan,
)

__all__ = [
    "CaseAnalysisArtifact",
    "ErrorBucket",
    "EvaluationRecord",
    "EvidenceBundle",
    "EvidenceTriangulation",
    "FailureCategory",
    "FusionDebugInfo",
    "LogEvidence",
    "LogSnippet",
    "MetricAlert",
    "MetricEvidence",
    "MetricSeries",
    "PipelineRunMeta",
    "PipelineStep",
    "PromptAuditInfo",
    "RCAReport",
    "RemediationStep",
    "ServiceFusionDetail",
    "StageTiming",
    "TelemetrySnapshot",
    "Trace",
    "TraceEvidence",
    "TraceSpan",
    "TraceSpanEvidence",
    "classify_error_bucket",
]
