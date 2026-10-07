"""
Unit tests for Checkpoint 1: RCAEval Benchmark Adapter & Quantization Layer.
Verifies:
1. Flexible timestamp parsing across Unix float/int/ms/us/ns and ISO-8601 strings.
2. In-sample baseline vs incident temporal slicing with ZERO data leakage.
3. 5-second time-bucket quantization across logs, metrics, and trace spans.
4. Call-tree DAG assembly from raw traces.csv rows.
5. Ground truth metadata extraction and round-trip TelemetrySnapshot serialization.
"""

import json
from pathlib import Path
import pytest

from scripts.rcaeval_adapter import (
    RCAEvalAdapter,
    detect_column,
    parse_timestamp_value,
)
from src.schemas.telemetry import TelemetrySnapshot


def test_timestamp_parsing_fidelity():
    # Unix seconds
    assert parse_timestamp_value(1680000000) == 1680000000.0
    assert parse_timestamp_value("1680000000.5") == 1680000000.5

    # Milliseconds
    assert parse_timestamp_value(1680000000000) == 1680000000.0

    # Microseconds
    assert parse_timestamp_value(1680000000000000) == 1680000000.0

    # Nanoseconds
    assert parse_timestamp_value(1680000000000000000) == 1680000000.0

    # ISO-8601 strings
    ts_iso = parse_timestamp_value("2023-03-28T12:00:00Z")
    assert isinstance(ts_iso, float)
    assert ts_iso > 1600000000.0

    ts_iso_frac = parse_timestamp_value("2023-03-28T12:00:00.123456Z")
    assert isinstance(ts_iso_frac, float)


def test_detect_column():
    fields = ["Timestamp", "Service_Name", "Log_Message", "Extra"]
    assert detect_column(fields, ["time", "timestamp"]) == "Timestamp"
    assert detect_column(fields, ["service", "service_name"]) == "Service_Name"
    assert detect_column(fields, ["message", "log_message"]) == "Log_Message"
    assert detect_column(fields, ["absent"]) is None


@pytest.fixture
def mock_rcaeval_case(tmp_path: Path) -> Path:
    """Creates a realistic synthetic RCAEval case directory with in-sample baseline & incident."""
    case_dir = tmp_path / "RE2_online-boutique_cartservice_mem_1"
    case_dir.mkdir(parents=True, exist_ok=True)

    t_inj = 1680000100.0  # Injection at t=100s

    # 1. inject_time.txt
    (case_dir / "inject_time.txt").write_text(f"{int(t_inj)}\n", encoding="utf-8")

    # 2. logs.csv (3 baseline lines, 3 incident lines)
    logs_content = (
        "time,service,message\n"
        f"{t_inj - 50.0},cartservice,Normal startup initialized\n"
        f"{t_inj - 30.0},frontend,User browsing catalog\n"
        f"{t_inj - 10.0},cartservice,Cache warm up complete\n"
        f"{t_inj + 5.0},cartservice,Allocating 500MB memory buffer\n"
        f"{t_inj + 12.0},cartservice,KILLED: Out of memory\n"
        f"{t_inj + 15.0},frontend,Failed to connect to cartservice:50051\n"
    )
    (case_dir / "logs.csv").write_text(logs_content, encoding="utf-8")

    # 3. metrics.json (container memory rising across baseline and incident)
    metrics_data = {
        "cartservice/container_memory_bytes": [
            [t_inj - 60.0, 50000000],
            [t_inj - 30.0, 52000000],
            [t_inj, 55000000],
            [t_inj + 10.0, 150000000],  # Spike!
            [t_inj + 20.0, 200000000],
        ],
        "frontend/http_requests_total": [
            [t_inj - 30.0, 100],
            [t_inj + 10.0, 105],
        ],
    }
    (case_dir / "metrics.json").write_text(json.dumps(metrics_data), encoding="utf-8")

    # 4. traces.csv (Call tree: frontend -> cartservice)
    traces_content = (
        "time,trace_id,span_id,parent_id,service,operation,duration_ms,status\n"
        # Baseline trace (t = t_inj - 20)
        f"{t_inj - 20.0},tr_base_1,sp_1,,frontend,/cart,50,OK\n"
        f"{t_inj - 20.0},tr_base_1,sp_2,sp_1,cartservice,GetCart,40,OK\n"
        # Incident trace (t = t_inj + 10)
        f"{t_inj + 10.0},tr_inc_1,sp_3,,frontend,/cart,3200,500\n"
        f"{t_inj + 10.0},tr_inc_1,sp_4,sp_3,cartservice,GetCart,3100,500\n"
    )
    (case_dir / "traces.csv").write_text(traces_content, encoding="utf-8")

    return case_dir


def test_rcaeval_adapter_temporal_slicing(mock_rcaeval_case: Path):
    adapter = RCAEvalAdapter(
        baseline_duration_sec=300.0,
        incident_duration_sec=120.0,
        bucket_size_sec=5.0,
    )

    snapshot, gt_info = adapter.convert_case(mock_rcaeval_case)

    # 1. Ground Truth verification
    assert gt_info["case_id"] == "RE2_online-boutique_cartservice_mem_1"
    assert gt_info["ground_truth_service"] == "cartservice"
    assert gt_info["ground_truth_fault_type"] == "MEM"
    assert snapshot.ground_truth_service == "cartservice"
    assert snapshot.ground_truth_fault_type == "MEM"

    # 2. In-sample Baseline vs Incident Slicing (ZERO data leakage)
    assert snapshot.baseline is not None
    # Baseline logs
    assert "cartservice" in snapshot.baseline_logs
    assert len(snapshot.baseline_logs["cartservice"]) == 2
    assert "Normal startup initialized" in snapshot.baseline_logs["cartservice"][0]
    # Incident logs
    assert "cartservice" in snapshot.logs
    assert len(snapshot.logs["cartservice"]) == 2
    assert "KILLED: Out of memory" in snapshot.logs["cartservice"][1]
    # Verify no incident error in baseline
    for lines in snapshot.baseline_logs.values():
        for line in lines:
            assert "Out of memory" not in line

    # 3. Metric series slicing
    assert len(snapshot.baseline_metrics) == 2
    assert len(snapshot.metrics) == 2
    mem_base = next(m for m in snapshot.baseline_metrics if "container_memory_bytes" in m.metric_name)
    mem_inc = next(m for m in snapshot.metrics if "container_memory_bytes" in m.metric_name)
    # Baseline has 2 points (< t_inj)
    assert len(mem_base.data) == 2
    # Incident has 3 points (>= t_inj)
    assert len(mem_inc.data) == 3
    assert mem_inc.data[-1]["value"] == 200000000

    # 4. Traces slicing & DAG assembly
    assert len(snapshot.baseline_traces) == 1
    assert snapshot.baseline_traces[0].traceID == "tr_base_1"
    assert len(snapshot.baseline_traces[0].spans) == 2

    assert len(snapshot.traces) == 1
    assert snapshot.traces[0].traceID == "tr_inc_1"
    assert len(snapshot.traces[0].spans) == 2

    # Check child span reference
    child_span = next(s for s in snapshot.traces[0].spans if s.spanID == "sp_4")
    assert len(child_span.references) == 1
    assert child_span.references[0]["spanID"] == "sp_3"
    assert child_span.references[0]["refType"] == "CHILD_OF"

    # 5. Time Bucket Quantization
    assert snapshot.time_buckets is not None
    assert len(snapshot.time_buckets) > 0
    # Buckets before t_inj should have bucket_index < 0
    base_buckets = [b for b in snapshot.time_buckets if b.bucket_index < 0]
    assert len(base_buckets) > 0
    # Buckets at or after t_inj should have bucket_index >= 0
    inc_buckets = [b for b in snapshot.time_buckets if b.bucket_index >= 0]
    assert len(inc_buckets) > 0
    assert any("cartservice" in b.services_involved for b in inc_buckets)


def test_rcaeval_adapter_save_and_reload(mock_rcaeval_case: Path, tmp_path: Path):
    output_dir = tmp_path / "converted_runs"
    adapter = RCAEvalAdapter()

    snap_file, gt_file = adapter.save_case(mock_rcaeval_case, output_dir=output_dir)

    assert snap_file.exists()
    assert gt_file.exists()

    with open(snap_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Validates against TelemetrySnapshot Pydantic schema
    loaded = TelemetrySnapshot(**data)
    assert loaded.run_id == "RE2_online-boutique_cartservice_mem_1"
    assert loaded.inject_time == 1680000100.0
    assert len(loaded.logs) > 0
    assert len(loaded.baseline_logs) > 0
    assert len(loaded.time_buckets) > 0
