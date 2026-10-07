#!/usr/bin/env python3
"""
RCAEval Benchmark Ingestion and Quantized Adapter.
Converts raw RCAEval case directories (logs.csv, metrics.json, traces.csv, inject_time.txt)
into unified, validated ObservaSage TelemetrySnapshot instances.

Key Architectural Guarantees:
1. Zero Hardcoding: Dynamically extracts microservices, metrics, and trace call trees.
2. In-Sample Baseline Slicing: Derives normal baselines directly from pre-injection window
   [T_inj - baseline_sec, T_inj] to eliminate workload drift and data leakage.
3. 5-Second Time-Bucket Quantization: Synchronizes heterogeneous clock scrape intervals
   across logs, metrics, and distributed trace spans into aligned timeline slices.
"""

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple, Union

try:
    import pandas as pd
except ImportError:
    pd = None

from src.schemas.telemetry import (
    MetricSeries,
    PipelineRunMeta,
    PipelineStep,
    TelemetryData,
    TelemetrySnapshot,
    TimeBucketSummary,
    Trace,
    TraceSpan,
)


def parse_timestamp_value(raw: Any) -> float:
    """
    Parses various timestamp representations (Unix seconds, ms, us, ns, or ISO-8601 strings)
    into standard Unix epoch float seconds.
    """
    if raw is None:
        return 0.0

    if isinstance(raw, (int, float)):
        val = float(raw)
        # Nanoseconds (> 1e17)
        if val > 1e16:
            return val / 1e9
        # Microseconds (> 1e14)
        if val > 1e13:
            return val / 1e6
        # Milliseconds (> 1e11)
        if val > 1e10:
            return val / 1e3
        return val

    raw_str = str(raw).strip()
    # Try parsing numeric string
    try:
        val = float(raw_str)
        return parse_timestamp_value(val)
    except ValueError:
        pass

    # Parse ISO-8601 or common datetime formats
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ]:
        try:
            dt = datetime.strptime(raw_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue

    # Fallback to datetime.fromisoformat if supported
    try:
        dt = datetime.fromisoformat(raw_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        raise ValueError(f"Unable to parse timestamp value: {raw}")


def detect_column(fieldnames: List[str], candidates: List[str]) -> Optional[str]:
    """Finds the first matching candidate in fieldnames (case-insensitive)."""
    norm_map = {fn.strip().lower(): fn for fn in fieldnames}
    for cand in candidates:
        if cand.lower() in norm_map:
            return norm_map[cand.lower()]
    return None


class RCAEvalAdapter:
    """
    Translates raw RCAEval benchmark case folders into validated TelemetrySnapshot models.
    """

    def __init__(
        self,
        baseline_duration_sec: float = 600.0,
        incident_duration_sec: float = 300.0,
        bucket_size_sec: float = 5.0,
        cases_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.baseline_duration_sec = baseline_duration_sec
        self.incident_duration_sec = incident_duration_sec
        self.bucket_size_sec = bucket_size_sec
        self.cases_metadata = cases_metadata or {}

    @classmethod
    def load_metadata_registry(cls, registry_path: Union[str, Path]) -> Dict[str, Dict[str, Any]]:
        """Loads cases.parquet or cases.csv if available."""
        path = Path(registry_path)
        if not path.exists():
            return {}

        registry: Dict[str, Dict[str, Any]] = {}
        if path.suffix == ".parquet" and pd is not None:
            df = pd.read_parquet(path)
            for _, row in df.iterrows():
                row_dict = row.to_dict()
                case_id = str(row_dict.get("case", row_dict.get("case_id", row_dict.get("id", ""))))
                if case_id:
                    registry[case_id] = row_dict
        elif path.suffix == ".csv":
            with open(path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    case_id = str(row.get("case", row.get("case_id", row.get("id", ""))))
                    if case_id:
                        registry[case_id] = row
        return registry

    def parse_inject_time(self, case_dir: Path) -> float:
        """Reads inject_time.txt from case directory."""
        inject_file = case_dir / "inject_time.txt"
        if not inject_file.exists():
            raise FileNotFoundError(f"Missing inject_time.txt in {case_dir}")

        with open(inject_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return parse_timestamp_value(content)

    def parse_logs(
        self, case_dir: Path, inject_time: float
    ) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], List[Tuple[float, str]]]:
        """
        Parses logs (logs.parquet or logs.csv) into (baseline_logs, incident_logs, raw_timestamped_logs).
        Returns logs segmented by service and bucketable timestamp pairs.
        """
        baseline_start = inject_time - self.baseline_duration_sec
        incident_end = inject_time + self.incident_duration_sec

        baseline_logs: Dict[str, List[str]] = {}
        incident_logs: Dict[str, List[str]] = {}
        all_timestamped_logs: List[Tuple[float, str]] = []

        logs_parquet = case_dir / "logs.parquet"
        logs_csv = case_dir / "logs.csv"

        if logs_parquet.exists() and pd is not None:
            df = pd.read_parquet(logs_parquet)
            time_col = detect_column(df.columns.tolist(), ["timestamp", "time", "datetime", "ts"])
            service_col = detect_column(df.columns.tolist(), ["container_name", "service", "service_name", "pod", "app"])
            msg_col = detect_column(df.columns.tolist(), ["message", "content", "log", "text", "msg", "body"])

            if time_col and msg_col:
                for _, row in df.iterrows():
                    try:
                        ts = parse_timestamp_value(row[time_col])
                    except Exception:
                        continue

                    service = str(row[service_col]).strip() if service_col and pd.notna(row[service_col]) else "unknown_service"
                    msg = str(row[msg_col]).strip()
                    iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    formatted_line = f"[{iso_ts}] {msg}"

                    all_timestamped_logs.append((ts, service))
                    if baseline_start <= ts < inject_time:
                        baseline_logs.setdefault(service, []).append(formatted_line)
                    elif inject_time <= ts <= incident_end:
                        incident_logs.setdefault(service, []).append(formatted_line)
            return baseline_logs, incident_logs, all_timestamped_logs

        if not logs_csv.exists():
            return {}, {}, []

        with open(logs_csv, mode="r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return {}, {}, []

            time_col = detect_column(reader.fieldnames, ["time", "timestamp", "datetime", "date", "ts"])
            service_col = detect_column(reader.fieldnames, ["service", "service_name", "pod", "app", "component", "container_name"])
            msg_col = detect_column(reader.fieldnames, ["message", "content", "log", "text", "msg", "body"])

            if not time_col or not msg_col:
                return {}, {}, []

            for row in reader:
                try:
                    ts = parse_timestamp_value(row[time_col])
                except Exception:
                    continue

                service = (row[service_col].strip() if service_col and row.get(service_col) else "unknown_service")
                msg = row[msg_col].strip()
                iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                formatted_line = f"[{iso_ts}] {msg}"

                all_timestamped_logs.append((ts, service))

                if baseline_start <= ts < inject_time:
                    baseline_logs.setdefault(service, []).append(formatted_line)
                elif inject_time <= ts <= incident_end:
                    incident_logs.setdefault(service, []).append(formatted_line)

        return baseline_logs, incident_logs, all_timestamped_logs

    def parse_metrics(
        self, case_dir: Path, inject_time: float
    ) -> Tuple[List[MetricSeries], List[MetricSeries], List[Tuple[float, str]]]:
        """
        Parses metrics (metrics.parquet or metrics.json) into (baseline_metrics, incident_metrics, raw_timestamped_points).
        Handles wide-format Parquet tables and nested JSON dictionaries.
        """
        baseline_start = inject_time - self.baseline_duration_sec
        incident_end = inject_time + self.incident_duration_sec

        flat_series: Dict[str, List[List[Any]]] = {}

        metrics_parquet = case_dir / "metrics.parquet"
        metrics_json = case_dir / "metrics.json"

        if metrics_parquet.exists() and pd is not None:
            df = pd.read_parquet(metrics_parquet)
            time_col = "time" if "time" in df.columns else ("timestamp" if "timestamp" in df.columns else None)
            if time_col:
                times = df[time_col].values
                for col in df.columns:
                    if col == time_col:
                        continue
                    vals = df[col].values
                    pts = [[times[i], vals[i]] for i in range(len(times)) if pd.notna(vals[i])]
                    flat_series[col] = pts

        elif metrics_json.exists():
            with open(metrics_json, "r", encoding="utf-8") as f:
                raw_metrics = json.load(f)

            if isinstance(raw_metrics, dict):
                for k1, v1 in raw_metrics.items():
                    if isinstance(v1, dict):
                        for k2, v2 in v1.items():
                            if isinstance(v2, list):
                                flat_series[f"{k1}/{k2}"] = v2
                    elif isinstance(v1, list):
                        flat_series[k1] = v1
            elif isinstance(raw_metrics, list):
                for item in raw_metrics:
                    if isinstance(item, dict):
                        name = item.get("metric", item.get("metric_name", "unknown"))
                        vals = item.get("values", item.get("data", []))
                        if isinstance(vals, list):
                            flat_series[name] = vals

        baseline_metric_series: List[MetricSeries] = []
        incident_metric_series: List[MetricSeries] = []
        all_timestamped_points: List[Tuple[float, str]] = []

        for metric_name, points in flat_series.items():
            base_pts: List[Dict[str, Any]] = []
            inc_pts: List[Dict[str, Any]] = []

            for pt in points:
                if not (isinstance(pt, (list, tuple)) and len(pt) >= 2):
                    continue
                try:
                    ts = parse_timestamp_value(pt[0])
                    val = float(pt[1])
                except (ValueError, TypeError):
                    continue

                service_name = metric_name.split("/")[0] if "/" in metric_name else "system"
                all_timestamped_points.append((ts, service_name))

                pt_dict = {"timestamp": ts, "value": val}
                if baseline_start <= ts < inject_time:
                    base_pts.append(pt_dict)
                elif inject_time <= ts <= incident_end:
                    inc_pts.append(pt_dict)

            if base_pts:
                baseline_metric_series.append(
                    MetricSeries(
                        metric_name=metric_name,
                        query=f"rcaeval:{metric_name}",
                        data=base_pts,
                    )
                )
            if inc_pts:
                incident_metric_series.append(
                    MetricSeries(
                        metric_name=metric_name,
                        query=f"rcaeval:{metric_name}",
                        data=inc_pts,
                    )
                )

        return baseline_metric_series, incident_metric_series, all_timestamped_points

    def parse_traces(
        self, case_dir: Path, inject_time: float
    ) -> Tuple[List[Trace], List[Trace], List[Tuple[float, str]]]:
        """
        Parses traces (traces.parquet or traces.csv) into (baseline_traces, incident_traces, raw_timestamped_spans).
        Assembles individual span rows into parent-child Trace DAG objects.
        """
        baseline_start = inject_time - self.baseline_duration_sec
        incident_end = inject_time + self.incident_duration_sec

        raw_traces_map: Dict[str, List[TraceSpan]] = {}
        all_timestamped_spans: List[Tuple[float, str]] = []

        traces_parquet = case_dir / "traces.parquet"
        traces_csv = case_dir / "traces.csv"

        if traces_parquet.exists() and pd is not None:
            df = pd.read_parquet(traces_parquet)
            time_col = detect_column(df.columns.tolist(), ["startTime", "startTimeMillis", "time", "timestamp", "ts"])
            if time_col and len(df) > 0:
                raw_times = df[time_col].values
                # Standardize to seconds
                if float(raw_times[0]) > 1e12:
                    ts_seconds = raw_times / 1e6
                else:
                    ts_seconds = raw_times
                
                mask = (ts_seconds >= baseline_start) & (ts_seconds <= incident_end)
                df = df[mask]
                
                # Sample up to 10,000 spans from within the window if still large
                if len(df) > 10000:
                    df = df.iloc[:10000]

            trace_id_col = detect_column(df.columns.tolist(), ["traceID", "trace_id", "id"])
            span_id_col = detect_column(df.columns.tolist(), ["spanID", "span_id"])
            parent_id_col = detect_column(df.columns.tolist(), ["parentSpanID", "parent_id", "parent_span_id"])
            service_col = detect_column(df.columns.tolist(), ["serviceName", "service", "service_name", "app"])
            op_col = detect_column(df.columns.tolist(), ["operationName", "methodName", "operation", "rpc"])
            duration_col = detect_column(df.columns.tolist(), ["duration", "duration_ms", "duration_us"])
            status_col = detect_column(df.columns.tolist(), ["statusCode", "status", "status_code", "error"])

            for _, row in df.iterrows():
                try:
                    ts = parse_timestamp_value(row[time_col])
                except Exception:
                    continue

                trace_id = str(row[trace_id_col]).strip() if trace_id_col and pd.notna(row[trace_id_col]) else "tr_unknown"
                span_id = str(row[span_id_col]).strip() if span_id_col and pd.notna(row[span_id_col]) else f"{trace_id}_{len(all_timestamped_spans)}"
                parent_id = str(row[parent_id_col]).strip() if parent_id_col and pd.notna(row[parent_id_col]) else None
                if parent_id in ("", "none", "null", "None", "0"):
                    parent_id = None

                service = str(row[service_col]).strip() if service_col and pd.notna(row[service_col]) else "unknown_service"
                operation = str(row[op_col]).strip() if op_col and pd.notna(row[op_col]) else "unknown_op"

                dur_us = 0
                if duration_col and pd.notna(row[duration_col]):
                    try:
                        dur_us = int(float(row[duration_col]))
                    except (ValueError, TypeError):
                        dur_us = 0

                tags: List[Dict[str, Any]] = [
                    {"key": "service.name", "value": service},
                    {"key": "operation.name", "value": operation},
                ]
                if status_col and pd.notna(row[status_col]):
                    st = str(row[status_col]).strip().lower()
                    if st in ["error", "500", "502", "503", "504", "true", "1", "fail"]:
                        tags.append({"key": "error", "value": True})
                    if st.isdigit():
                        tags.append({"key": "http.status_code", "value": int(st)})

                references = []
                if parent_id:
                    references.append({"refType": "CHILD_OF", "traceID": trace_id, "spanID": parent_id})

                start_time_us = int(ts * 1e6)
                span = TraceSpan(
                    traceID=trace_id,
                    spanID=span_id,
                    operationName=operation,
                    references=references,
                    startTime=start_time_us,
                    duration=dur_us,
                    tags=tags,
                )
                raw_traces_map.setdefault(trace_id, []).append(span)
                all_timestamped_spans.append((ts, service))

        elif traces_csv.exists():
            with open(traces_csv, mode="r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    return [], [], []

                time_col = detect_column(reader.fieldnames, ["time", "timestamp", "start_time", "startTime", "ts"])
                trace_id_col = detect_column(reader.fieldnames, ["trace_id", "traceID", "traceId", "id"])
                span_id_col = detect_column(reader.fieldnames, ["span_id", "spanID", "spanId"])
                parent_id_col = detect_column(reader.fieldnames, ["parent_id", "parent_span_id", "parentSpanID", "parentId"])
                service_col = detect_column(reader.fieldnames, ["service", "service_name", "serviceName", "app"])
                op_col = detect_column(reader.fieldnames, ["operation", "operation_name", "operationName", "rpc", "method"])
                duration_col = detect_column(reader.fieldnames, ["duration", "duration_ms", "duration_us", "latency"])
                status_col = detect_column(reader.fieldnames, ["status", "status_code", "statusCode", "error", "has_error"])

                if not time_col or not trace_id_col:
                    return [], [], []

                row_idx = 0
                for row in reader:
                    row_idx += 1
                    try:
                        ts = parse_timestamp_value(row[time_col])
                    except Exception:
                        continue

                    trace_id = row[trace_id_col].strip()
                    span_id = (
                        row[span_id_col].strip()
                        if span_id_col and row.get(span_id_col)
                        else f"{trace_id}_{row_idx}"
                    )
                    parent_id = (
                        row[parent_id_col].strip()
                        if parent_id_col and row.get(parent_id_col)
                        else None
                    )
                    if parent_id in ("", "none", "null", "None", "0"):
                        parent_id = None

                    service = (
                        row[service_col].strip()
                        if service_col and row.get(service_col)
                        else "unknown_service"
                    )
                    operation = (
                        row[op_col].strip()
                        if op_col and row.get(op_col)
                        else "unknown_op"
                    )

                    duration_us = 0
                    if duration_col and row.get(duration_col):
                        try:
                            dur_val = float(row[duration_col])
                            if dur_val < 100000 and "ms" in duration_col.lower():
                                duration_us = int(dur_val * 1000)
                            else:
                                duration_us = int(dur_val)
                        except ValueError:
                            duration_us = 0

                    tags: List[Dict[str, Any]] = [
                        {"key": "service.name", "value": service},
                        {"key": "operation.name", "value": operation},
                    ]
                    if status_col and row.get(status_col):
                        st = str(row[status_col]).strip().lower()
                        if st in ["error", "500", "502", "503", "504", "true", "1", "fail", "failed"]:
                            tags.append({"key": "error", "value": True})
                        if st.isdigit():
                            tags.append({"key": "http.status_code", "value": int(st)})

                    references = []
                    if parent_id:
                        references.append({"refType": "CHILD_OF", "traceID": trace_id, "spanID": parent_id})

                    start_time_us = int(ts * 1e6)
                    span = TraceSpan(
                        traceID=trace_id,
                        spanID=span_id,
                        operationName=operation,
                        references=references,
                        startTime=start_time_us,
                        duration=duration_us,
                        tags=tags,
                    )

                    raw_traces_map.setdefault(trace_id, []).append(span)
                    all_timestamped_spans.append((ts, service))

        baseline_traces: List[Trace] = []
        incident_traces: List[Trace] = []

        for trace_id, spans in raw_traces_map.items():
            if not spans:
                continue
            min_ts_sec = min(s.startTime for s in spans) / 1e6
            trace_obj = Trace(traceID=trace_id, spans=spans)

            if baseline_start <= min_ts_sec < inject_time:
                baseline_traces.append(trace_obj)
            elif inject_time <= min_ts_sec <= incident_end:
                incident_traces.append(trace_obj)

        return baseline_traces, incident_traces, all_timestamped_spans

    def compute_time_buckets(
        self,
        inject_time: float,
        timestamped_logs: List[Tuple[float, str]],
        timestamped_metrics: List[Tuple[float, str]],
        timestamped_spans: List[Tuple[float, str]],
    ) -> List[TimeBucketSummary]:
        """
        Quantizes events across all signals into discrete time buckets
        aligned to inject_time. Negative indices represent pre-injection baseline buckets.
        """
        min_time = inject_time - self.baseline_duration_sec
        max_time = inject_time + self.incident_duration_sec

        bucket_data: Dict[int, Dict[str, Any]] = {}

        def add_event(ts: float, svc: str, kind: str):
            if ts < min_time or ts > max_time:
                return
            b_idx = int((ts - inject_time) // self.bucket_size_sec)
            if b_idx not in bucket_data:
                b_start = inject_time + (b_idx * self.bucket_size_sec)
                b_end = b_start + self.bucket_size_sec
                bucket_data[b_idx] = {
                    "bucket_index": b_idx,
                    "start_timestamp": b_start,
                    "end_timestamp": b_end,
                    "log_count": 0,
                    "metric_points_count": 0,
                    "span_count": 0,
                    "services": set(),
                }
            entry = bucket_data[b_idx]
            if kind == "log":
                entry["log_count"] += 1
            elif kind == "metric":
                entry["metric_points_count"] += 1
            elif kind == "span":
                entry["span_count"] += 1
            if svc and svc != "unknown_service":
                entry["services"].add(svc)

        for ts, svc in timestamped_logs:
            add_event(ts, svc, "log")
        for ts, svc in timestamped_metrics:
            add_event(ts, svc, "metric")
        for ts, svc in timestamped_spans:
            add_event(ts, svc, "span")

        summaries: List[TimeBucketSummary] = []
        for b_idx in sorted(bucket_data.keys()):
            data = bucket_data[b_idx]
            summaries.append(
                TimeBucketSummary(
                    bucket_index=b_idx,
                    start_timestamp=data["start_timestamp"],
                    end_timestamp=data["end_timestamp"],
                    log_count=data["log_count"],
                    metric_points_count=data["metric_points_count"],
                    span_count=data["span_count"],
                    services_involved=sorted(list(data["services"])),
                )
            )

        return summaries

    def extract_ground_truth(self, case_dir: Path) -> Dict[str, Any]:
        """
        Retrieves ground truth labels for the case directory:
        1. Checks cases.parquet / cases.csv metadata registry.
        2. Fallbacks to folder naming convention: {dataset}_{service}_{fault}_{instance}
        """
        case_id = case_dir.name

        # Auto-load registry from parent directory if not already loaded
        if not self.cases_metadata:
            parent_reg = case_dir.parent / "cases.parquet"
            if parent_reg.exists():
                self.cases_metadata = self.load_metadata_registry(parent_reg)

        # 1. Check loaded registry
        if case_id in self.cases_metadata:
            meta = self.cases_metadata[case_id]
            return {
                "case_id": case_id,
                "ground_truth_service": meta.get("root_cause_service", meta.get("service", "")),
                "ground_truth_fault_type": meta.get("fault", meta.get("fault_type", "")),
                "benchmark": meta.get("system", meta.get("benchmark", "")),
                "dataset": meta.get("dataset", ""),
            }

        # 2. Parse from folder name: e.g. re2ob_checkoutservice_cpu_1 or RE2_online-boutique_cartservice_mem_1
        parts = case_id.split("_")
        gt_service = "unknown"
        gt_fault = "unknown"
        dataset = parts[0] if parts else "unknown"
        benchmark = "unknown"

        if len(parts) >= 4:
            if parts[-1].isdigit() and len(parts) == 4:
                # e.g., re2ob_checkoutservice_cpu_1
                dataset = parts[0]
                gt_service = parts[1]
                gt_fault = parts[2].upper()
            else:
                # e.g., RE2_online-boutique_cartservice_mem_1
                dataset = parts[0]
                benchmark = parts[1]
                gt_service = parts[2]
                gt_fault = parts[3].upper()
        elif len(parts) == 3:
            benchmark = parts[0]
            gt_service = parts[1]
            gt_fault = parts[2].upper()

        return {
            "case_id": case_id,
            "ground_truth_service": gt_service,
            "ground_truth_fault_type": gt_fault,
            "benchmark": benchmark,
            "dataset": dataset,
        }

    def convert_case(self, case_dir: Union[str, Path]) -> Tuple[TelemetrySnapshot, Dict[str, Any]]:
        """
        Converts a single RCAEval case folder into a validated TelemetrySnapshot
        and companion ground truth dictionary.
        """
        case_path = Path(case_dir).resolve()
        case_id = case_path.name

        # 1. Inject Time
        inject_time = self.parse_inject_time(case_path)

        # 2. Signal Extraction
        base_logs, inc_logs, ts_logs = self.parse_logs(case_path, inject_time)
        base_metrics, inc_metrics, ts_metrics = self.parse_metrics(case_path, inject_time)
        base_traces, inc_traces, ts_traces = self.parse_traces(case_path, inject_time)

        # 3. Time Bucket Quantization
        time_buckets = self.compute_time_buckets(inject_time, ts_logs, ts_metrics, ts_traces)

        # 4. Ground Truth
        gt_info = self.extract_ground_truth(case_path)
        gt_info["inject_time_unix"] = inject_time

        # 5. Pipeline Run Metadata
        meta = PipelineRunMeta(
            run_id=case_id,
            scenario=f"rcaeval_{gt_info.get('ground_truth_fault_type', 'incident').lower()}",
            status="FAILED",
            start_time=datetime.fromtimestamp(inject_time - self.baseline_duration_sec, tz=timezone.utc),
            end_time=datetime.fromtimestamp(inject_time + self.incident_duration_sec, tz=timezone.utc),
            duration_seconds=self.baseline_duration_sec + self.incident_duration_sec,
            error_message=f"RCAEval injected fault: {gt_info.get('ground_truth_fault_type')}",
            steps=[
                PipelineStep(step="baseline_workload", status_code=200, duration_ms=self.baseline_duration_sec * 1000),
                PipelineStep(step="fault_injection", status_code=500, duration_ms=self.incident_duration_sec * 1000),
            ],
        )

        snapshot = TelemetrySnapshot(
            run_id=case_id,
            metadata=meta,
            captured_at=datetime.fromtimestamp(inject_time, tz=timezone.utc),
            inject_time=inject_time,
            time_buckets=time_buckets,
            ground_truth_service=gt_info.get("ground_truth_service"),
            ground_truth_fault_type=gt_info.get("ground_truth_fault_type"),
            telemetry=TelemetryData(
                logs=inc_logs,
                metrics=inc_metrics,
                traces=inc_traces,
            ),
            baseline=TelemetryData(
                logs=base_logs,
                metrics=base_metrics,
                traces=base_traces,
            ),
        )

        return snapshot, gt_info

    def save_case(
        self,
        case_dir: Union[str, Path],
        output_dir: Union[str, Path] = "data/runs/failed",
    ) -> Tuple[Path, Path]:
        """Converts case and saves snapshot & ground_truth JSON files."""
        snapshot, gt_info = self.convert_case(case_dir)
        out_path = Path(output_dir).resolve()
        out_path.mkdir(parents=True, exist_ok=True)

        snapshot_file = out_path / f"{snapshot.run_id}_telemetry.json"
        gt_file = out_path / f"{snapshot.run_id}_ground_truth.json"

        with open(snapshot_file, "w", encoding="utf-8") as f:
            f.write(snapshot.model_dump_json(indent=2))

        with open(gt_file, "w", encoding="utf-8") as f:
            json.dump(gt_info, f, indent=2)

        return snapshot_file, gt_file


def main():
    parser = argparse.ArgumentParser(description="ObservaSage RCAEval Benchmark Ingestion Adapter")
    parser.add_argument("--case-dir", type=str, help="Path to single RCAEval case folder")
    parser.add_argument("--cases-root", type=str, help="Directory containing multiple RCAEval case folders")
    parser.add_argument("--registry-file", type=str, default="data/ground_truth/rcaeval/cases.parquet", help="Path to cases.parquet/csv")
    parser.add_argument("--output-dir", type=str, default="data/runs/failed", help="Output directory for JSON snapshots")
    parser.add_argument("--baseline-sec", type=float, default=600.0, help="Pre-injection baseline window seconds (default: 600)")
    parser.add_argument("--incident-sec", type=float, default=300.0, help="Post-injection incident window seconds (default: 300)")
    parser.add_argument("--bucket-sec", type=float, default=5.0, help="Time bucket quantization seconds (default: 5.0)")

    args = parser.parse_args()

    registry = {}
    if os.path.exists(args.registry_file):
        registry = RCAEvalAdapter.load_metadata_registry(args.registry_file)

    adapter = RCAEvalAdapter(
        baseline_duration_sec=args.baseline_sec,
        incident_duration_sec=args.incident_sec,
        bucket_size_sec=args.bucket_sec,
        cases_metadata=registry,
    )

    targets: List[Path] = []
    if args.case_dir:
        targets.append(Path(args.case_dir))
    elif args.cases_root:
        root = Path(args.cases_root)
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "inject_time.txt").exists():
                targets.append(child)

    if not targets:
        print("No valid RCAEval case directories found. Provide --case-dir or --cases-root.")
        return

    print(f"Found {len(targets)} RCAEval case(s) to process. Converting to {args.output_dir}...")
    for idx, case_path in enumerate(targets, 1):
        try:
            s_file, g_file = adapter.save_case(case_path, output_dir=args.output_dir)
            print(f"[{idx}/{len(targets)}] Converted: {case_path.name} -> {s_file.name}")
        except Exception as e:
            print(f"[{idx}/{len(targets)}] ERROR converting {case_path.name}: {e}")


if __name__ == "__main__":
    main()
