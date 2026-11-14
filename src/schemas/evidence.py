"""
Pydantic schemas for extracted telemetry evidence across logs, metrics, and traces.
"""

from typing import Literal

from pydantic import BaseModel, Field


class LogSnippet(BaseModel):
    service: str
    line_number: int
    matched_keyword: str | None = None
    template_id: str | None = None
    is_novel: bool = True
    context_before: list[str] = Field(default_factory=list, description="m=3 lines before error")
    target_line: str
    context_after: list[str] = Field(default_factory=list, description="n=7 lines after error")


class LogEvidence(BaseModel):
    service: str
    total_raw_lines: int = 0
    novel_templates_count: int = 0
    snippets: list[LogSnippet] = Field(default_factory=list)
    formatted_prompt: str = ""
    estimated_tokens: int = 0


class MetricAlert(BaseModel):
    metric_name: str
    service: str
    current_value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    robust_z_score: float | None = Field(default=None, description="Non-parametric MAD-based robust z-score")
    adaptive_threshold: float | None = Field(default=None, description="Dynamically calibrated baseline anomaly threshold")
    is_threshold_exceeded: bool = False
    is_oom_risk: bool = False
    slope_dM_dt: float | None = Field(default=None, description="Memory consumption rate of change (bytes/sec)")
    time_to_oom_seconds: float | None = Field(default=None, description="Estimated seconds until cgroup memory limit breach")
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    description: str


class MetricEvidence(BaseModel):
    total_metrics_evaluated: int = 0
    alerts: list[MetricAlert] = Field(default_factory=list)
    has_oom_alert: bool = False
    has_latency_anomaly: bool = False
    formatted_prompt: str = ""
    estimated_tokens: int = 0


class TraceSpanEvidence(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    service_name: str
    operation_name: str
    duration_ms: float
    self_duration_ms: float | None = None
    status_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    is_leaf_culprit: bool = False
    depth: int = 0


class TraceEvidence(BaseModel):
    total_traces: int = 0
    total_spans: int = 0
    error_spans_count: int = 0
    latency_bottlenecks_count: int = 0
    root_service: str | None = None
    culprit_service: str | None = None
    culprit_span: TraceSpanEvidence | None = None
    error_spans: list[TraceSpanEvidence] = Field(default_factory=list)
    latency_bottlenecks: list[TraceSpanEvidence] = Field(default_factory=list)
    service_dependency_graph: dict[str, list[str]] = Field(default_factory=dict, description="Adjacency list of caller -> callee dependencies")
    call_hierarchy_summary: str = ""
    formatted_prompt: str = ""
    estimated_tokens: int = 0

