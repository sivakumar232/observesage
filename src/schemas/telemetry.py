"""
Pydantic schemas for raw telemetry and pipeline run metadata.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PipelineStep(BaseModel):
    step: str
    status_code: int | None = None
    duration_ms: float = 0.0


class PipelineRunMeta(BaseModel):
    run_id: str
    scenario: str = "normal"
    status: Literal["SUCCESS", "FAILED"]
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    error_message: str | None = None
    steps: list[PipelineStep] = Field(default_factory=list)


class MetricSeries(BaseModel):
    metric_name: str
    query: str
    data: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class TraceSpan(BaseModel):
    traceID: str
    spanID: str
    operationName: str
    references: list[dict[str, Any]] = Field(default_factory=list)
    startTime: int
    duration: int
    tags: list[dict[str, Any]] = Field(default_factory=list)
    logs: list[dict[str, Any]] = Field(default_factory=list)
    processID: str | None = None
    warnings: list[str] | None = None


class Trace(BaseModel):
    traceID: str
    spans: list[TraceSpan] = Field(default_factory=list)
    processes: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] | None = None


class TimeBucketSummary(BaseModel):
    bucket_index: int
    start_timestamp: float
    end_timestamp: float
    log_count: int = 0
    metric_points_count: int = 0
    span_count: int = 0
    services_involved: list[str] = Field(default_factory=list)


class TelemetryData(BaseModel):
    logs: dict[str, list[str]] = Field(default_factory=dict)
    metrics: list[MetricSeries] = Field(default_factory=list)
    traces: list[Trace] = Field(default_factory=list)


class TelemetrySnapshot(BaseModel):
    run_id: str
    metadata: PipelineRunMeta | None = None
    captured_at: datetime | None = None
    telemetry: TelemetryData
    baseline: TelemetryData | None = None
    inject_time: float | None = None
    time_buckets: list[TimeBucketSummary] | None = None
    ground_truth_service: str | None = None
    ground_truth_fault_type: str | None = None

    @model_validator(mode="before")
    @classmethod
    def handle_nested_or_flat(cls, data: Any) -> Any:
        if isinstance(data, dict) and (
            "telemetry" not in data and ("logs" in data or "metrics" in data or "traces" in data)
        ):
            # If telemetry is at root level, bundle it into telemetry field
            return {
                    "run_id": data.get("run_id", "unknown"),
                    "metadata": data.get("metadata"),
                    "captured_at": data.get("captured_at"),
                    "inject_time": data.get("inject_time"),
                    "time_buckets": data.get("time_buckets"),
                    "ground_truth_service": data.get("ground_truth_service"),
                    "ground_truth_fault_type": data.get("ground_truth_fault_type"),
                    "baseline": data.get("baseline"),
                    "telemetry": {
                        "logs": data.get("logs", {}),
                        "metrics": data.get("metrics", []),
                        "traces": data.get("traces", []),
                    },
                }
        return data

    @property
    def logs(self) -> dict[str, list[str]]:
        return self.telemetry.logs

    @property
    def metrics(self) -> list[MetricSeries]:
        return self.telemetry.metrics

    @property
    def traces(self) -> list[Trace]:
        return self.telemetry.traces

    @property
    def baseline_logs(self) -> dict[str, list[str]]:
        return self.baseline.logs if self.baseline else {}

    @property
    def baseline_metrics(self) -> list[MetricSeries]:
        return self.baseline.metrics if self.baseline else []

    @property
    def baseline_traces(self) -> list[Trace]:
        return self.baseline.traces if self.baseline else []
