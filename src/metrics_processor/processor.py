"""
Metrics Processor for ObservaSage.
Performs statistical anomaly detection (Z-scores) and rate-of-change slope analysis
across time-series telemetry with in-sample baseline calibration.
"""

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from src.schemas.evidence import MetricAlert, MetricEvidence
from src.schemas.telemetry import MetricSeries


def extract_service_from_metric(metric_name: str, query: str = "") -> str:
    """
    Extracts the microservice name dynamically from metric naming or query labels.
    Examples:
        'cartservice/container_memory_bytes' -> 'cartservice'
        'container_memory_usage_bytes{name="paymentservice"}' -> 'paymentservice'
        'http_requests_total{app="frontend"}' -> 'frontend'
    """
    # 1. Check slash delimiter: service/metric
    if "/" in metric_name:
        parts = metric_name.split("/")
        if parts[0].strip():
            return parts[0].strip()

    # 2. Check PromQL label selectors: {name="..."} or {app="..."} or {service="..."}
    label_patterns = [
        r'(?:name|app|service|pod|container|service_name)="([^"]+)"',
        r"(?:name|app|service|pod|container|service_name)='([^']+)'",
    ]
    target_str = f"{metric_name} {query}"
    for pat in label_patterns:
        match = re.search(pat, target_str)
        if match:
            return match.group(1).strip()

    # 3. Check prefix if followed by underscore
    if "_" in metric_name:
        prefix = metric_name.split("_")[0]
        # Avoid generic prefixes
        if prefix not in ["container", "http", "grpc", "node", "process", "system", "jvm", "go"]:
            return prefix

    return "system"


def compute_linear_slope(points: List[Dict[str, Any]]) -> float:
    """
    Computes linear regression rate of change (dy/dt) in units per second.
    """
    if len(points) < 2:
        return 0.0

    valid_pts = []
    for p in points:
        try:
            t = float(p.get("timestamp", 0.0))
            v = float(p.get("value", 0.0))
            valid_pts.append((t, v))
        except (ValueError, TypeError):
            continue

    if len(valid_pts) < 2:
        return 0.0

    # Sort chronologically
    valid_pts.sort(key=lambda pt: pt[0])
    dt_total = valid_pts[-1][0] - valid_pts[0][0]
    if dt_total <= 0:
        return 0.0

    t_mean = sum(t for t, _ in valid_pts) / len(valid_pts)
    v_mean = sum(v for _, v in valid_pts) / len(valid_pts)

    numerator = sum((t - t_mean) * (v - v_mean) for t, v in valid_pts)
    denominator = sum((t - t_mean) ** 2 for t, _ in valid_pts)

    if denominator == 0:
        return 0.0

    return numerator / denominator


def compute_mean_and_std(values: List[float]) -> Tuple[float, float]:
    """Computes sample mean and standard deviation."""
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, math.sqrt(variance)


class MetricsProcessor:
    """
    Analyzes metrics timeseries using dynamic Z-score filtering and memory slope detection.
    """

    def __init__(
        self,
        z_threshold: float = 3.0,
        oom_slope_threshold_bytes_per_sec: float = 50000.0,  # 50 KB/s
        max_alerts: int = 10,
        max_metric_tokens: int = 500,
    ):
        self.z_threshold = z_threshold
        self.oom_slope_threshold_bytes_per_sec = oom_slope_threshold_bytes_per_sec
        self.max_alerts = max_alerts
        self.max_metric_tokens = max_metric_tokens

    def process(
        self,
        incident_metrics: List[MetricSeries],
        baseline_metrics: Optional[List[MetricSeries]] = None,
    ) -> MetricEvidence:
        """
        Evaluates incident metrics against baseline metrics (or self-derived baseline).
        """
        if not incident_metrics:
            return MetricEvidence(total_metrics_evaluated=0)

        # 1. Build baseline reference map: { metric_name: (mean, std) }
        baseline_stats: Dict[str, Tuple[float, float]] = {}
        if baseline_metrics:
            for b_series in baseline_metrics:
                vals = [float(p.get("value", 0.0)) for p in b_series.data if "value" in p]
                if vals:
                    baseline_stats[b_series.metric_name] = compute_mean_and_std(vals)

        alerts: List[MetricAlert] = []
        has_oom_alert = False
        has_latency_anomaly = False

        # 2. Evaluate each incident series
        for series in incident_metrics:
            if not series.data:
                continue

            vals = []
            for p in series.data:
                try:
                    vals.append(float(p.get("value", 0.0)))
                except (ValueError, TypeError):
                    continue

            if not vals:
                continue

            service = extract_service_from_metric(series.metric_name, series.query)

            # Retrieve or calculate baseline
            if series.metric_name in baseline_stats:
                b_mean, b_std = baseline_stats[series.metric_name]
            else:
                # If no baseline provided, compute from earliest 30% of incident series
                split_idx = max(2, int(len(vals) * 0.3))
                b_mean, b_std = compute_mean_and_std(vals[:split_idx])

            # Peak divergence from baseline
            # Pick the point with maximum absolute difference from mean
            peak_val = max(vals, key=lambda v: abs(v - b_mean))

            # Effective standard deviation (avoid division by zero)
            eps = max(1e-6, abs(b_mean) * 1e-4) if b_mean != 0 else 1e-4
            eff_std = max(b_std, eps)

            z_score = (peak_val - b_mean) / eff_std
            abs_z = abs(z_score)

            # Check if this metric is memory-related
            name_lower = series.metric_name.lower()
            is_memory = any(k in name_lower for k in ["memory", "mem", "rss", "bytes_used", "heap"])
            is_latency = any(k in name_lower for k in ["latency", "duration", "time", "delay", "seconds"])

            slope = None
            is_oom_risk = False
            if is_memory:
                slope = compute_linear_slope(series.data)
                # OOM risk if memory is growing fast and Z-score is elevated
                if slope > self.oom_slope_threshold_bytes_per_sec and z_score > 1.5:
                    is_oom_risk = True
                    has_oom_alert = True

            is_threshold_exceeded = abs_z >= self.z_threshold or is_oom_risk

            if is_threshold_exceeded:
                # Determine severity
                if is_oom_risk or abs_z >= 8.0:
                    sev = "CRITICAL"
                elif abs_z >= 5.0:
                    sev = "HIGH"
                elif abs_z >= 3.0:
                    sev = "MEDIUM"
                else:
                    sev = "LOW"

                if is_latency and z_score > self.z_threshold:
                    has_latency_anomaly = True

                desc_parts = [f"Spike to {peak_val:.2f} (baseline μ={b_mean:.2f}, σ={b_std:.2f}, z={z_score:+.2f})"]
                if is_oom_risk:
                    desc_parts.append(f"Steep memory slope +{slope/1e6:.2f}MB/s indicates OOM risk")
                elif slope is not None and slope > 0:
                    desc_parts.append(f"Slope +{slope/1e6:.2f}MB/s")

                alert = MetricAlert(
                    metric_name=series.metric_name,
                    service=service,
                    current_value=peak_val,
                    baseline_mean=b_mean,
                    baseline_std=b_std,
                    z_score=z_score,
                    is_threshold_exceeded=True,
                    is_oom_risk=is_oom_risk,
                    slope_dM_dt=slope,
                    severity=sev,
                    description="; ".join(desc_parts),
                )
                alerts.append(alert)

        # 3. Sort alerts by severity & absolute Z-score
        severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        alerts.sort(key=lambda a: (severity_order.get(a.severity, 4), -abs(a.z_score)))
        alerts = alerts[: self.max_alerts]

        # 4. Format prompt
        formatted_prompt = self._format_prompt(alerts)
        est_tokens = max(1, len(formatted_prompt) // 4)

        return MetricEvidence(
            total_metrics_evaluated=len(incident_metrics),
            alerts=alerts,
            has_oom_alert=has_oom_alert,
            has_latency_anomaly=has_latency_anomaly,
            formatted_prompt=formatted_prompt,
            estimated_tokens=est_tokens,
        )

    def _format_prompt(self, alerts: List[MetricAlert]) -> str:
        if not alerts:
            return "No metric anomalies detected (Z-scores within normal distribution)."

        lines = ["### Metric Anomaly Alerts:"]
        for a in alerts:
            oom_str = " [CRITICAL OOM RISK]" if a.is_oom_risk else ""
            slope_str = f", slope={a.slope_dM_dt/1e6:+.2f}MB/s" if a.slope_dM_dt else ""
            lines.append(
                f"- [{a.severity}] Service '{a.service}' - {a.metric_name}: "
                f"value={a.current_value:.2f} (baseline μ={a.baseline_mean:.2f}, z={a.z_score:+.2f}{slope_str}){oom_str}"
            )
        return "\n".join(lines)
