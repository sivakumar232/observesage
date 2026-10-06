"""
Unit tests for Checkpoint 2: MetricsProcessor.
Verifies:
1. Dynamic service extraction from metric names and PromQL queries.
2. Z-score deviation calculation against baseline.
3. Memory rate-of-change (dy/dt) slope and OOM risk detection.
4. Latency anomaly flagging and structured MetricEvidence generation.
"""

from src.metrics_processor import MetricsProcessor, extract_service_from_metric
from src.schemas.telemetry import MetricSeries


def test_extract_service_from_metric():
    assert extract_service_from_metric("cartservice/container_memory_bytes") == "cartservice"
    assert (
        extract_service_from_metric("container_memory_usage_bytes", 'container_memory_usage_bytes{name="paymentservice"}')
        == "paymentservice"
    )
    assert extract_service_from_metric("frontend_http_requests_total") == "frontend"
    assert extract_service_from_metric("node_cpu_utilization") == "system"


def test_z_score_spike_detection():
    processor = MetricsProcessor(z_threshold=3.0)

    baseline = [
        MetricSeries(
            metric_name="currencyservice/cpu_seconds_total",
            query="rcaeval",
            data=[{"timestamp": 100.0 + i, "value": 10.0 + (i % 2)} for i in range(20)],
        )
    ]

    # Incident with huge spike
    incident = [
        MetricSeries(
            metric_name="currencyservice/cpu_seconds_total",
            query="rcaeval",
            data=[
                {"timestamp": 200.0, "value": 10.5},
                {"timestamp": 205.0, "value": 95.0},  # Massive spike
            ],
        )
    ]

    evidence = processor.process(incident, baseline)
    assert evidence.total_metrics_evaluated == 1
    assert len(evidence.alerts) == 1

    alert = evidence.alerts[0]
    assert alert.service == "currencyservice"
    assert alert.z_score > 10.0
    assert alert.severity == "CRITICAL"
    assert alert.is_threshold_exceeded is True


def test_oom_memory_slope_detection():
    processor = MetricsProcessor(oom_slope_threshold_bytes_per_sec=50000.0)

    # Baseline memory ~ 50MB
    baseline = [
        MetricSeries(
            metric_name="cartservice/container_memory_bytes",
            query="rcaeval",
            data=[{"timestamp": 100.0 + i, "value": 50000000.0 + (i * 100)} for i in range(10)],
        )
    ]

    # Rapid climb: 50MB -> 150MB in 10 seconds (+10MB/s)
    incident = [
        MetricSeries(
            metric_name="cartservice/container_memory_bytes",
            query="rcaeval",
            data=[
                {"timestamp": 200.0, "value": 50000000.0},
                {"timestamp": 205.0, "value": 100000000.0},
                {"timestamp": 210.0, "value": 150000000.0},
            ],
        )
    ]

    evidence = processor.process(incident, baseline)
    assert evidence.has_oom_alert is True
    assert len(evidence.alerts) == 1

    alert = evidence.alerts[0]
    assert alert.is_oom_risk is True
    assert alert.severity == "CRITICAL"
    assert alert.slope_dM_dt is not None
    assert alert.slope_dM_dt > 5000000.0  # > 5 MB/s
    assert "OOM risk" in alert.description


def test_latency_anomaly_detection():
    processor = MetricsProcessor(z_threshold=3.0)

    baseline = [
        MetricSeries(
            metric_name="frontend/http_request_duration_seconds",
            query="rcaeval",
            data=[{"timestamp": 100.0 + i, "value": 0.05} for i in range(10)],
        )
    ]

    incident = [
        MetricSeries(
            metric_name="frontend/http_request_duration_seconds",
            query="rcaeval",
            data=[
                {"timestamp": 200.0, "value": 0.05},
                {"timestamp": 205.0, "value": 3.5},  # 3.5 seconds latency
            ],
        )
    ]

    evidence = processor.process(incident, baseline)
    assert evidence.has_latency_anomaly is True
    assert len(evidence.alerts) == 1
    assert evidence.alerts[0].service == "frontend"


def test_robust_mad_z_score_detection():
    processor = MetricsProcessor(z_threshold=3.0)

    # Baseline with skewed latency data: mostly 0.05, a couple 0.08
    baseline = [
        MetricSeries(
            metric_name="checkoutservice/http_latency",
            query="rcaeval",
            data=[{"timestamp": 100.0 + i, "value": 0.05 if i % 4 != 0 else 0.06} for i in range(20)],
        )
    ]

    # Incident with sudden latency jump
    incident = [
        MetricSeries(
            metric_name="checkoutservice/http_latency",
            query="rcaeval",
            data=[
                {"timestamp": 200.0, "value": 0.05},
                {"timestamp": 205.0, "value": 2.5},
            ],
        )
    ]

    evidence = processor.process(incident, baseline)
    assert len(evidence.alerts) == 1
    alert = evidence.alerts[0]
    assert alert.robust_z_score is not None
    assert abs(alert.robust_z_score) > 3.0


def test_cgroup_memory_limit_time_to_oom():
    processor = MetricsProcessor()

    # Memory limit is 200MB, current usage climbing from 150MB to 180MB at 3MB/s
    # Remaining = 20MB -> TimeToOOM = ~6.6 seconds (< 300s)
    incident = [
        MetricSeries(
            metric_name="cartservice/spec_memory_limit_bytes",
            query="rcaeval",
            data=[{"timestamp": 200.0, "value": 200000000.0}],
        ),
        MetricSeries(
            metric_name="cartservice/container_memory_bytes",
            query="rcaeval",
            data=[
                {"timestamp": 200.0, "value": 150000000.0},
                {"timestamp": 210.0, "value": 180000000.0},
            ],
        ),
    ]

    evidence = processor.process(incident)
    assert evidence.has_oom_alert is True
    mem_alert = [a for a in evidence.alerts if a.metric_name == "cartservice/container_memory_bytes"][0]
    assert mem_alert.is_oom_risk is True
    assert mem_alert.time_to_oom_seconds is not None
    assert mem_alert.time_to_oom_seconds < 300.0

