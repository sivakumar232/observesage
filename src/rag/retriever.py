"""
Multi-Modal Telemetry-RAG Retriever for ObservaSage.
Retrieves and indexes localized anomaly context across Logs, Metrics, and Traces:
- Log Retriever: Drain3 novel template diffing + keyword search + asymmetric context expansion (m=3, n=7)
- Metric Retriever: Baseline rolling Z-score deviations (|z| >= 3.0), slope dM/dt, and latency divergence
- Trace Retriever: Jaeger DAG DFS critical path traversal and bottleneck/error leaf extraction

Synthesizes retrieved signals into a bounded multimodal RAG prompt strictly under 2,500 BPE tokens.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from src.fusion import FusionEngine
from src.llm.prompt import build_multimodal_prompt_with_breakdown, count_bpe_tokens
from src.log_processor import LogSageProcessor
from src.metrics_processor import MetricsProcessor
from src.schemas.evidence import LogEvidence, MetricEvidence, TraceEvidence
from src.schemas.rca_report import EvidenceTriangulation, FailureCategory
from src.schemas.telemetry import TelemetrySnapshot
from src.trace_processor import TraceProcessor


@dataclass
class RetrievedRAGContext:
    """Retrieved multi-signal context ready for LLM diagnostic generation."""
    run_id: str
    scenario: str
    rag_prompt: str
    token_count: int
    candidate_services: list[str]
    hypothesized_category: FailureCategory
    triangulation: EvidenceTriangulation
    log_evidences: dict[str, LogEvidence]
    metric_evidence: MetricEvidence | None
    trace_evidence: TraceEvidence | None
    confidence: float = 0.70
    fusion_debug: dict[str, Any] = field(default_factory=dict)
    prompt_section_tokens: dict[str, int] = field(default_factory=dict)


class TelemetryRAGRetriever:
    """
    Retriever that extracts and ranks anomalies from raw telemetry,
    producing an augmented prompt context for generative LLM diagnosis.
    """

    def __init__(
        self,
        max_prompt_tokens: int = 2400,
        z_threshold: float = 3.0,
        oom_slope_threshold_bytes_per_sec: float = 50000.0,
    ):
        self.max_prompt_tokens = max_prompt_tokens
        # Note: LogSageProcessor is instantiated per retrieve() call to prevent Drain3 state bleed.
        self.metrics_processor = MetricsProcessor(
            z_threshold=z_threshold,
            oom_slope_threshold_bytes_per_sec=oom_slope_threshold_bytes_per_sec,
        )
        self.trace_processor = TraceProcessor()
        self.fusion_engine = FusionEngine()

    def retrieve(
        self,
        snapshot: TelemetrySnapshot,
        mode: Literal["logs-only", "logs-metrics", "logs-traces", "fusion"] = "fusion",
    ) -> RetrievedRAGContext:
        """
        Executes multi-modal retrieval across all telemetry signals in the snapshot.
        """
        # 1. Log Retrieval (LogSage Drain3 + Asymmetric Context Expansion)
        # Instantiate a fresh LogSageProcessor per case to prevent Drain3 cluster state bleeding across cases
        log_processor = LogSageProcessor()
        if snapshot.baseline and snapshot.baseline.logs:
            log_processor.miner.train_baseline_from_runs([snapshot.baseline.logs])
            log_processor.is_baseline_ready = True
        else:
            log_processor.load_or_train_baselines()

        log_evidences = log_processor.process_all_logs(snapshot.logs)

        # 2. Metric Retrieval (Z-score spike detection & memory slope)
        metric_evidence: MetricEvidence | None = None
        if mode in ["logs-metrics", "fusion"]:
            metric_evidence = self.metrics_processor.process(
                incident_metrics=snapshot.metrics,
                baseline_metrics=snapshot.baseline_metrics,
            )

        # 3. Trace Retrieval (Jaeger DAG & DFS leaf culprit)
        trace_evidence: TraceEvidence | None = None
        if mode in ["logs-traces", "fusion"]:
            trace_evidence = self.trace_processor.process(
                incident_traces=snapshot.traces,
                baseline_traces=snapshot.baseline_traces,
            )

        # 4. Multi-Signal Evidence Triangulation & Candidate Scoring
        ranked_candidates, failure_cat, triangulation, initial_conf, fusion_debug = (
            self.fusion_engine.correlate_with_debug(
                log_evidences=log_evidences,
                metric_evidence=metric_evidence,
                trace_evidence=trace_evidence,
            )
        )

        # 5. RAG Prompt Assembly with Strict Token Budget Guarantee
        rag_prompt, section_tokens = build_multimodal_prompt_with_breakdown(
            run_id=snapshot.run_id,
            log_evidences=log_evidences,
            metric_evidence=metric_evidence,
            trace_evidence=trace_evidence,
            mode=mode,
            max_total_tokens=self.max_prompt_tokens,
        )

        token_count = section_tokens.get("total") or count_bpe_tokens(rag_prompt)
        scenario = snapshot.metadata.scenario if snapshot.metadata else "unknown"

        return RetrievedRAGContext(
            run_id=snapshot.run_id,
            scenario=scenario,
            rag_prompt=rag_prompt,
            token_count=token_count,
            candidate_services=ranked_candidates,
            hypothesized_category=failure_cat,
            triangulation=triangulation,
            log_evidences=log_evidences,
            metric_evidence=metric_evidence,
            trace_evidence=trace_evidence,
            confidence=initial_conf,
            fusion_debug=fusion_debug,
            prompt_section_tokens=section_tokens,
        )
