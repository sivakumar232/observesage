"""
Multi-Signal Fusion Engine for ObservaSage.
Correlates evidence across logs, metrics, and traces to perform cross-modal consensus scoring,
ranked culprit service localization, and structured evidence triangulation.
"""

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from src.schemas.evidence import (
    LogEvidence,
    MetricEvidence,
    TraceEvidence,
)
from src.schemas.rca_report import (
    EvidenceTriangulation,
    FailureCategory,
)


class FusionEngine:
    """
    Correlates tri-modal telemetry evidence into a unified diagnostic synthesis.
    """

    def __init__(
        self,
        trace_leaf_weight: float = 5.0,
        metric_oom_weight: float = 6.0,
        metric_alert_weight: float = 3.0,
        log_novel_weight: float = 2.5,
        log_snippet_weight: float = 1.0,
    ):
        self.trace_leaf_weight = trace_leaf_weight
        self.metric_oom_weight = metric_oom_weight
        self.metric_alert_weight = metric_alert_weight
        self.log_novel_weight = log_novel_weight
        self.log_snippet_weight = log_snippet_weight

    def correlate(
        self,
        log_evidences: Dict[str, LogEvidence],
        metric_evidence: Optional[MetricEvidence] = None,
        trace_evidence: Optional[TraceEvidence] = None,
    ) -> Tuple[List[str], FailureCategory, EvidenceTriangulation, float]:
        """
        Synthesizes multi-signal evidence using topological call graph propagation
        and cross-modal consensus scoring.
        Returns:
            (ranked_culprits, failure_category, evidence_triangulation, initial_confidence)
        """
        service_scores: Dict[str, float] = defaultdict(float)
        signal_votes: Dict[str, List[str]] = {"LOGS": [], "METRICS": [], "TRACES": []}

        # 1. Base Score from Logs
        for svc, log_ev in log_evidences.items():
            if not log_ev.snippets:
                continue
            score = (
                len(log_ev.snippets) * self.log_snippet_weight
                + log_ev.novel_templates_count * self.log_novel_weight
            )
            service_scores[svc] += score
            signal_votes["LOGS"].append(svc)

        # 2. Base Score from Metrics
        if metric_evidence and metric_evidence.alerts:
            for alert in metric_evidence.alerts:
                svc = alert.service
                if alert.is_oom_risk:
                    service_scores[svc] += self.metric_oom_weight
                else:
                    sev_mult = {"CRITICAL": 1.5, "HIGH": 1.2, "MEDIUM": 1.0, "LOW": 0.5}.get(alert.severity, 1.0)
                    service_scores[svc] += self.metric_alert_weight * sev_mult
                signal_votes["METRICS"].append(svc)

        # 3. Base Score from Traces
        if trace_evidence:
            if trace_evidence.culprit_service:
                svc = trace_evidence.culprit_service
                boost = self.trace_leaf_weight
                if trace_evidence.culprit_span and trace_evidence.culprit_span.depth > 0:
                    boost += 2.0  # Extra confidence for leaf depth > 0
                service_scores[svc] += boost
                signal_votes["TRACES"].append(svc)

        # 4. Topological Causal Graph Propagation (Dampen upstream callers, attribute to callee)
        if trace_evidence and trace_evidence.service_dependency_graph:
            dep_graph = trace_evidence.service_dependency_graph
            for caller, callees in dep_graph.items():
                if caller not in service_scores:
                    continue
                # If any downstream callee has error signals, caller is likely a secondary symptom
                failing_callees = [
                    c for c in callees
                    if c in service_scores and (c in signal_votes["TRACES"] or c in signal_votes["METRICS"] or c in signal_votes["LOGS"])
                ]
                if failing_callees:
                    # Dampen caller's score because its errors are topologically explained by downstream callees
                    service_scores[caller] *= 0.60
                    for c in failing_callees:
                        # Topological attribution boost to callee
                        service_scores[c] += 2.0

        # 5. Cross-Modal Consensus Multiplier (Reward multi-signal agreement)
        for svc in list(service_scores.keys()):
            modalities_count = sum(1 for modal, svcs in signal_votes.items() if svc in svcs)
            if modalities_count == 2:
                service_scores[svc] *= 1.35
            elif modalities_count >= 3:
                service_scores[svc] *= 1.75

        # Rank candidates
        ranked_candidates = sorted(service_scores.keys(), key=lambda s: service_scores[s], reverse=True)
        top_service = ranked_candidates[0] if ranked_candidates else "unknown"

        # 6. Dynamic Multi-Hypothesis Likelihood Formulation
        likelihoods: Dict[FailureCategory, float] = {
            FailureCategory.EC_1_OOM: 0.1,
            FailureCategory.EC_2_LATENCY: 0.1,
            FailureCategory.EC_3_CASCADE: 0.1,
            FailureCategory.EC_4_SILENT: 0.05,
            FailureCategory.EC_5_INFRA: 0.1,
            FailureCategory.CODE_BUG: 0.1,
        }

        # Metrics evidence likelihood contribution
        max_metric_div = 0.0
        if metric_evidence and metric_evidence.alerts:
            top_alert = metric_evidence.alerts[0]
            max_metric_div = max(abs(top_alert.z_score), abs(top_alert.robust_z_score or 0.0))
            if metric_evidence.has_oom_alert:
                likelihoods[FailureCategory.EC_1_OOM] += 8.0
                if any(a.time_to_oom_seconds and a.time_to_oom_seconds < 180.0 for a in metric_evidence.alerts):
                    likelihoods[FailureCategory.EC_1_OOM] += 4.0
            if metric_evidence.has_latency_anomaly:
                likelihoods[FailureCategory.EC_2_LATENCY] += 6.5

        # Trace evidence likelihood contribution
        leaf_depth = 0
        if trace_evidence:
            if trace_evidence.culprit_span:
                leaf_depth = trace_evidence.culprit_span.depth
            if trace_evidence.error_spans_count >= 2:
                likelihoods[FailureCategory.EC_3_CASCADE] += 7.0 + min(4.0, leaf_depth * 1.5)
            elif trace_evidence.error_spans_count == 1:
                likelihoods[FailureCategory.EC_3_CASCADE] += 3.0
            elif trace_evidence.total_traces > 0 and not trace_evidence.error_spans_count:
                likelihoods[FailureCategory.EC_2_LATENCY] += 4.0

        # Log evidence likelihood contribution
        total_novel_templates = sum(ev.novel_templates_count for ev in log_evidences.values())
        total_snippets = sum(len(ev.snippets) for ev in log_evidences.values())
        if total_snippets > 0:
            likelihoods[FailureCategory.CODE_BUG] += 3.5 + min(4.5, total_novel_templates * 1.5)
            for ev in log_evidences.values():
                for snip in ev.snippets:
                    snip_low = snip.target_line.lower()
                    if any(w in snip_low for w in ["connection refused", "network unreachable", "dns lookup failed", "host down"]):
                        likelihoods[FailureCategory.EC_5_INFRA] += 3.0

        # Dynamically deduce highest-probability failure category
        failure_cat = max(likelihoods.keys(), key=lambda k: likelihoods[k])
        if max(likelihoods.values()) <= 0.2:
            failure_cat = FailureCategory.UNKNOWN

        # 7. Dynamic Bayesian Consensus Confidence Calculation
        # Signal-specific normalized certainty factors c_m in [0, 1]
        c_metrics = min(0.95, max(0.40, max_metric_div / 8.0)) if (metric_evidence and metric_evidence.alerts) else 0.0
        c_traces = min(0.95, max(0.50, 0.45 + 0.15 * leaf_depth)) if (trace_evidence and trace_evidence.error_spans_count > 0) else 0.0
        c_logs = min(0.90, max(0.35, 0.35 + 0.10 * min(5, total_snippets))) if total_snippets > 0 else 0.0

        # Active agreeing signals for top candidate
        active_certainties = []
        agreeing_signals = 0
        if top_service in signal_votes["METRICS"] and c_metrics > 0:
            active_certainties.append(c_metrics)
            agreeing_signals += 1
        if top_service in signal_votes["TRACES"] and c_traces > 0:
            active_certainties.append(c_traces)
            agreeing_signals += 1
        if top_service in signal_votes["LOGS"] and c_logs > 0:
            active_certainties.append(c_logs)
            agreeing_signals += 1

        # Bayesian independent probability combination: 1 - product(1 - c_m)
        if active_certainties:
            prod_uncertainty = 1.0
            for c in active_certainties:
                prod_uncertainty *= (1.0 - c)
            raw_confidence = 1.0 - prod_uncertainty
            confidence = max(0.55, min(0.98, round(raw_confidence, 2)))
        else:
            confidence = 0.50

        # Determine Primary Signal dynamically
        if agreeing_signals >= 2:
            primary_signal = "FUSION"
        elif top_service in signal_votes["TRACES"]:
            primary_signal = "TRACES"
        elif top_service in signal_votes["METRICS"]:
            primary_signal = "METRICS"
        elif top_service in signal_votes["LOGS"]:
            primary_signal = "LOGS"
        else:
            primary_signal = "FUSION"

        # Evidence Insights
        logs_insight = self._summarize_logs_insight(log_evidences, top_service)
        metrics_insight = self._summarize_metrics_insight(metric_evidence, top_service)
        traces_insight = self._summarize_traces_insight(trace_evidence, top_service)

        # Build narrative reasoning
        triangulation_reasoning = self._build_reasoning(
            top_service, failure_cat, primary_signal, agreeing_signals, logs_insight, metrics_insight, traces_insight
        )

        triangulation = EvidenceTriangulation(
            primary_signal=primary_signal,  # type: ignore
            logs_insight=logs_insight,
            metrics_insight=metrics_insight,
            traces_insight=traces_insight,
            triangulation_reasoning=triangulation_reasoning,
        )

        return ranked_candidates, failure_cat, triangulation, confidence

    def _summarize_logs_insight(self, log_evidences: Dict[str, LogEvidence], target_svc: str) -> Optional[str]:
        if not log_evidences:
            return "No log evidence evaluated."
        target_ev = log_evidences.get(target_svc)
        if target_ev and target_ev.snippets:
            return (
                f"Isolated {len(target_ev.snippets)} error snippet(s) in '{target_svc}' "
                f"with {target_ev.novel_templates_count} novel Drain3 template(s)."
            )
        total_snips = sum(len(ev.snippets) for ev in log_evidences.values())
        return f"Identified {total_snips} error snippet(s) across {len(log_evidences)} microservices."

    def _summarize_metrics_insight(self, metric_ev: Optional[MetricEvidence], target_svc: str) -> Optional[str]:
        if not metric_ev or not metric_ev.alerts:
            return "Metrics evaluated within normal bounds (no Z-score anomalies)."
        svc_alerts = [a for a in metric_ev.alerts if a.service == target_svc]
        if svc_alerts:
            top_alert = svc_alerts[0]
            oom_txt = " [Steep memory slope indicates OOM risk]" if top_alert.is_oom_risk else ""
            return f"Detected {len(svc_alerts)} anomaly alert(s) for '{target_svc}': {top_alert.metric_name} (z={top_alert.z_score:+.2f}){oom_txt}."
        top_alert = metric_ev.alerts[0]
        return f"Peak anomaly detected at '{top_alert.service}': {top_alert.metric_name} (z={top_alert.z_score:+.2f})."

    def _summarize_traces_insight(self, trace_ev: Optional[TraceEvidence], target_svc: str) -> Optional[str]:
        if not trace_ev or trace_ev.error_spans_count == 0:
            return "Distributed traces show no failing spans or HTTP/gRPC error tags."
        if trace_ev.culprit_span:
            sp = trace_ev.culprit_span
            return (
                f"DFS DAG traversal isolated deepest leaf culprit at '{sp.service_name}:{sp.operation_name}' "
                f"(depth={sp.depth}, status={sp.status_code or 'ERROR'})."
            )
        return f"Encountered {trace_ev.error_spans_count} failing span(s) across call graphs."

    def _build_reasoning(
        self,
        culprit: str,
        category: FailureCategory,
        primary_signal: str,
        agreeing_count: int,
        logs_in: Optional[str],
        metrics_in: Optional[str],
        traces_in: Optional[str],
    ) -> str:
        parts = [
            f"Multi-signal correlation localized root cause to '{culprit}' under category {category.value}.",
            f"Cross-modal consensus verified across {agreeing_count} telemetry modality/modalities (Primary: {primary_signal}).",
        ]
        if traces_in and "deepest leaf" in traces_in:
            parts.append("Trace call hierarchy confirmed this service is the downstream origin rather than an innocent caller.")
        if metrics_in and "OOM risk" in metrics_in:
            parts.append("Metric time series confirmed memory consumption climbing at an unsustainable slope.")
        return " ".join(parts)
