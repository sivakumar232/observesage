"""
Pydantic schemas for raw telemetry and pipeline run metadata.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator


class PipelineStep(BaseModel):
    step: str
    status_code: Optional[int] = None
    duration_ms: float = 0.0


class PipelineRunMeta(BaseModel):
    run_id: str
    scenario: str = "normal"
    status: Literal["SUCCESS", "FAILED"]
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    error_message: Optional[str] = None
    steps: List[PipelineStep] = Field(default_factory=list)


class MetricSeries(BaseModel):
    metric_name: str
    query: str
    data: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None


class TraceSpan(BaseModel):
    traceID: str
    spanID: str
    operationName: str
    references: List[Dict[str, Any]] = Field(default_factory=list)
    startTime: int
    duration: int
    tags: List[Dict[str, Any]] = Field(default_factory=list)
    logs: List[Dict[str, Any]] = Field(default_factory=list)
    processID: Optional[str] = None
    warnings: Optional[List[str]] = None


class Trace(BaseModel):
    traceID: str
    spans: List[TraceSpan] = Field(default_factory=list)
    processes: Dict[str, Any] = Field(default_factory=dict)
    warnings: Optional[List[str]] = None


class TelemetryData(BaseModel):
    logs: Dict[str, List[str]] = Field(default_factory=dict)
    metrics: List[MetricSeries] = Field(default_factory=list)
    traces: List[Trace] = Field(default_factory=list)


class TelemetrySnapshot(BaseModel):
    run_id: str
    metadata: Optional[PipelineRunMeta] = None
    captured_at: Optional[datetime] = None
    telemetry: TelemetryData

    @model_validator(mode="before")
    @classmethod
    def handle_nested_or_flat(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # If telemetry is at root level, bundle it into telemetry field
            if "telemetry" not in data and ("logs" in data or "metrics" in data or "traces" in data):
                return {
                    "run_id": data.get("run_id", "unknown"),
                    "metadata": data.get("metadata"),
                    "captured_at": data.get("captured_at"),
                    "telemetry": {
                        "logs": data.get("logs", {}),
                        "metrics": data.get("metrics", []),
                        "traces": data.get("traces", []),
                    },
                }
        return data

    @property
    def logs(self) -> Dict[str, List[str]]:
        return self.telemetry.logs

    @property
    def metrics(self) -> List[MetricSeries]:
        return self.telemetry.metrics

    @property
    def traces(self) -> List[Trace]:
        return self.telemetry.traces
