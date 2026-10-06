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


def compute_median_and_mad(values: List[float]) -> Tuple[float, float]:
    """
    Computes sample median and Median Absolute Deviation (MAD) for robust,
    non-parametric anomaly detection on skewed metrics (e.g. latency, error rates).
    """
    if not values:
        return 0.0, 0.0
    s_vals = sorted(values)
    n = len(s_vals)
    if n % 2 == 1:
        med = s_vals[n // 2]
    else:
        med = (s_vals[n // 2 - 1] + s_vals[n // 2]) / 2.0
    devs = sorted(abs(x - med) for x in values)
    if n % 2 == 1:
        mad = devs[n // 2]
    else:
        mad = (devs[n // 2 - 1] + devs[n // 2]) / 2.0
    return med, mad


class MetricsProcessor:
    """
    Analyzes metrics timeseries using dynamic Z-score filtering, non-parametric MAD scoring,
    cgroup memory limit correlation, and TimeToOOM slope estimation.
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

        # 1. Build baseline reference map: { metric_name: (mean, std, median, mad) }
        baseline_stats: Dict[str, Tuple[float, float, float, float]] = {}
        if baseline_metrics:
            for b_series in baseline_metrics:
                vals = [float(p.get("value", 0.0)) for p in b_series.data if "value" in p]
                if vals:
                    mean_val, std_val = compute_mean_and_std(vals)
                    med_val, mad_val = compute_median_and_mad(vals)
                    baseline_stats[b_series.metric_name] = (mean_val, std_val, med_val, mad_val)

        # 2. Extract service-level cgroup memory limits if present in telemetry
        service_memory_limits: Dict[str, float] = {}
        all_series = (baseline_metrics or []) + incident_metrics
        for s in all_series:
            name_low = s.metric_name.lower()
            if any(k in name_low for k in ["spec_memory_limit", "memory_limit", "mem_limit"]) and s.data:
                svc = extract_service_from_metric(s.metric_name, s.query)
                try:
                    last_limit = float(s.data[-1].get("value", 0.0))
                    if last_limit > 0:
                        service_memory_limits[svc] = last_limit
                except (ValueError, TypeError):
                    pass

        alerts: List[MetricAlert] = []
        has_oom_alert = False
        has_latency_anomaly = False

        # 3. Evaluate each incident series
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
                b_mean, b_std, b_med, b_mad = baseline_stats[series.metric_name]
            else:
                # If no baseline provided, compute from earliest 30% of incident series
                split_idx = max(2, int(len(vals) * 0.3))
                b_mean, b_std = compute_mean_and_std(vals[:split_idx])
                b_med, b_mad = compute_median_and_mad(vals[:split_idx])

            # Peak divergence from baseline
            peak_val = max(vals, key=lambda v: abs(v - b_mean))

            # Effective standard deviation and MAD (avoid division by zero)
            eps = max(1e-6, abs(b_mean) * 1e-4) if b_mean != 0 else 1e-4
            eff_std = max(b_std, eps)
            eff_mad = max(b_mad, eps)

            z_score = (peak_val - b_mean) / eff_std
            # Non-parametric robust z-score: 0.6745 * (x - median) / MAD
            robust_z = 0.6745 * (peak_val - b_med) / eff_mad
            abs_z = abs(z_score)
            abs_robust_z = abs(robust_z)

            # Check if this metric is memory-related
            name_lower = series.metric_name.lower()
            is_memory = any(k in name_lower for k in ["memory", "mem", "rss", "bytes_used", "heap"])
            is_latency = any(k in name_lower for k in ["latency", "duration", "time", "delay", "seconds"])

            slope = None
            is_oom_risk = False
            time_to_oom_seconds = None

            if is_memory:
                slope = compute_linear_slope(series.data)
                mem_limit = service_memory_limits.get(service)
                if mem_limit and slope > 0:
                    remaining_bytes = max(0.0, mem_limit - peak_val)
                    time_to_oom_seconds = remaining_bytes / slope if slope > 0 else None
                    if time_to_oom_seconds is not None and time_to_oom_seconds < 300.0:
                        is_oom_risk = True
                        has_oom_alert = True

                # Fallback to slope threshold and elevated z-score
                if not is_oom_risk and slope > self.oom_slope_threshold_bytes_per_sec and z_score > 1.5:
                    is_oom_risk = True
                    has_oom_alert = True

            effective_divergence = max(abs_z, abs_robust_z)
            is_threshold_exceeded = effective_divergence >= self.z_threshold or is_oom_risk

            if is_threshold_exceeded:
                # Determine severity
                if is_oom_risk or effective_divergence >= 8.0:
                    sev = "CRITICAL"
                elif effective_divergence >= 5.0:
                    sev = "HIGH"
                elif effective_divergence >= 3.0:
                    sev = "MEDIUM"
                else:
                    sev = "LOW"

                if is_latency and (z_score > self.z_threshold or robust_z > self.z_threshold):
                    has_latency_anomaly = True

                desc_parts = [
                    f"Spike to {peak_val:.2f} (baseline μ={b_mean:.2f}, σ={b_std:.2f}, z={z_score:+.2f}, robust_z={robust_z:+.2f})"
                ]
                if is_oom_risk:
                    oom_desc = f"Steep memory slope +{slope/1e6:.2f}MB/s indicates OOM risk"
                    if time_to_oom_seconds is not None:
                        oom_desc += f" (estimated TimeToOOM: {time_to_oom_seconds:.0f}s)"
                    desc_parts.append(oom_desc)
                elif slope is not None and slope > 0:
                    desc_parts.append(f"Slope +{slope/1e6:.2f}MB/s")

                alert = MetricAlert(
                    metric_name=series.metric_name,
                    service=service,
                    current_value=peak_val,
                    baseline_mean=b_mean,
                    baseline_std=b_std,
                    z_score=z_score,
                    robust_z_score=robust_z,
                    is_threshold_exceeded=True,
                    is_oom_risk=is_oom_risk,
                    slope_dM_dt=slope,
                    time_to_oom_seconds=time_to_oom_seconds,
                    severity=sev,
                    description="; ".join(desc_parts),
                )
                alerts.append(alert)

        # 4. Sort alerts by severity & maximum divergence
        severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        alerts.sort(
            key=lambda a: (
                severity_order.get(a.severity, 4),
                -max(abs(a.z_score), abs(a.robust_z_score or 0.0)),
            )
        )
        alerts = alerts[: self.max_alerts]

        # 5. Format prompt
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
            if a.time_to_oom_seconds is not None:
                oom_str += f" (TimeToOOM ~{a.time_to_oom_seconds:.0f}s)"
            slope_str = f", slope={a.slope_dM_dt/1e6:+.2f}MB/s" if a.slope_dM_dt else ""
            lines.append(
                f"- [{a.severity}] Service '{a.service}' - {a.metric_name}: "
                f"value={a.current_value:.2f} (baseline μ={a.baseline_mean:.2f}, z={a.z_score:+.2f}{slope_str}){oom_str}"
            )
        return "\n".join(lines)
