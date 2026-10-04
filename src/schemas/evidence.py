"""
Pydantic schemas for extracted telemetry evidence across logs, metrics, and traces.
"""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class LogSnippet(BaseModel):
    service: str
    line_number: int
    matched_keyword: Optional[str] = None
    template_id: Optional[str] = None
    is_novel: bool = True
    context_before: List[str] = Field(default_factory=list, description="m=3 lines before error")
    target_line: str
    context_after: List[str] = Field(default_factory=list, description="n=7 lines after error")


class LogEvidence(BaseModel):
    service: str
    total_raw_lines: int = 0
    novel_templates_count: int = 0
    snippets: List[LogSnippet] = Field(default_factory=list)
    formatted_prompt: str = ""
    estimated_tokens: int = 0


class MetricAlert(BaseModel):
    metric_name: str
    service: str
    current_value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    is_threshold_exceeded: bool = False
    is_oom_risk: bool = False
    slope_dM_dt: Optional[float] = Field(default=None, description="Memory consumption rate of change (bytes/sec)")
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    description: str


class MetricEvidence(BaseModel):
    total_metrics_evaluated: int = 0
    alerts: List[MetricAlert] = Field(default_factory=list)
    has_oom_alert: bool = False
    has_latency_anomaly: bool = False
    formatted_prompt: str = ""
    estimated_tokens: int = 0


class TraceSpanEvidence(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    service_name: str
    operation_name: str
    duration_ms: float
    status_code: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    is_leaf_culprit: bool = False
    depth: int = 0


class TraceEvidence(BaseModel):
    total_traces: int = 0
    total_spans: int = 0
    error_spans_count: int = 0
    root_service: Optional[str] = None
    culprit_service: Optional[str] = None
    culprit_span: Optional[TraceSpanEvidence] = None
    call_hierarchy_summary: str = ""
    formatted_prompt: str = ""
    estimated_tokens: int = 0
