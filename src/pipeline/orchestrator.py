"""
Dataset-Only Batch Pipeline Orchestrator for ObservaSage.

Drives end-to-end RCA evaluation across downloaded RCAEval benchmark cases
with per-case state isolation, crash-safe incremental CSV persistence,
resume support, and 4-way ablation summary generation.

Usage:
    .venv/bin/python -m src.pipeline.orchestrator --modes fusion --limit 10
    .venv/bin/python -m src.pipeline.orchestrator --suite RE2-OB --modes logs-only logs-metrics logs-traces fusion
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from scripts.rcaeval_adapter import RCAEvalAdapter
from src.eval.metrics import (
    CaseScore,
    aggregate_scores,
    export_case_comparison_csv,
    format_ablation_table,
    score_single_case,
)
from src.llm import GeminiRCAClient
from src.rag import RetrievedRAGContext, TelemetryRAGRetriever
from src.schemas import (
    CaseAnalysisArtifact,
    EvaluationRecord,
    EvidenceBundle,
    FusionDebugInfo,
    PromptAuditInfo,
    RCAReport,
    ServiceFusionDetail,
    StageTiming,
    TelemetrySnapshot,
    classify_error_bucket,
)

AblationMode = Literal["logs-only", "logs-metrics", "logs-traces", "fusion"]
ALL_MODES: tuple[AblationMode, ...] = ("logs-only", "logs-metrics", "logs-traces", "fusion")

_logger = logging.getLogger(__name__)
console = Console()


class ObservaSagePipeline:
    """
    Orchestrates single-case and batch multi-signal RCA over RCAEval parquet cases.
    Guarantees per-case isolation (fresh retriever/Drain3 state per case) and
    crash-safe incremental CSV output.
    """

    def __init__(
        self,
        cases_root: Path | str = Path("data/ground_truth/rcaeval"),
        registry_path: Path | str | None = None,
        llm_client: GeminiRCAClient | None = None,
        strict_live: bool = False,
        use_llm: bool = True,
    ):
        self.cases_root = Path(cases_root)
        self.registry_path = Path(registry_path) if registry_path else self.cases_root / "cases.parquet"
        self.use_llm = use_llm
        self.llm_client = llm_client if llm_client is not None else (GeminiRCAClient() if use_llm else None)
        self.strict_live = strict_live
        cases_metadata = (
            RCAEvalAdapter.load_metadata_registry(self.registry_path)
            if self.registry_path.exists()
            else None
        )
        self.adapter = RCAEvalAdapter(cases_metadata=cases_metadata)

    def discover_cases(
        self,
        suite_filter: list[str] | None = None,
        fault_filter: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, str]]:
        """
        Discovers locally available, complete tri-modal cases ordered by registry metadata.
        """
        required = ("logs.parquet", "metrics.parquet", "traces.parquet", "inject_time.txt")

        if self.registry_path.exists():
            df = pd.read_parquet(self.registry_path)
            df = df[df["has_logs"].astype(bool) & df["has_traces"].astype(bool)].copy()
            if suite_filter:
                suites_upper = {s.upper() for s in suite_filter}
                df = df[df["dataset"].str.upper().isin(suites_upper)]
            if fault_filter:
                faults_lower = {f.lower() for f in fault_filter}
                df = df[df["fault"].str.lower().isin(faults_lower)]

            discovered: list[dict[str, str]] = []
            for _, row in df.iterrows():
                case_id = str(row["case"])
                case_dir = self.cases_root / case_id
                if case_dir.is_dir() and all((case_dir / f).exists() for f in required):
                    discovered.append(
                        {
                            "case_id": case_id,
                            "case_dir": str(case_dir),
                            "dataset": str(row.get("dataset", "")),
                            "fault_service": str(row.get("root_cause_service", "unknown")),
                            "fault_type": str(row.get("fault", "unknown")),
                        }
                    )
        else:
            discovered = []
            for c_dir in sorted(self.cases_root.iterdir()):
                if c_dir.is_dir() and all((c_dir / f).exists() for f in required):
                    gt = self.adapter.extract_ground_truth(c_dir)
                    fault = gt.get("ground_truth_fault_type", "unknown")
                    if fault_filter and fault.lower() not in {f.lower() for f in fault_filter}:
                        continue
                    discovered.append(
                        {
                            "case_id": c_dir.name,
                            "case_dir": str(c_dir),
                            "dataset": "",
                            "fault_service": gt.get("ground_truth_service", "unknown"),
                            "fault_type": fault,
                        }
                    )

        if limit is not None:
            discovered = discovered[: max(0, limit)]
        return discovered

    def load_case(self, case_dir: Path | str) -> tuple[TelemetrySnapshot, dict[str, Any]]:
        """Stage 1: Converts a raw RCAEval parquet case directory into a TelemetrySnapshot."""
        return self.adapter.convert_case(Path(case_dir))

    @staticmethod
    def retrieve_context(snapshot: TelemetrySnapshot, mode: AblationMode = "fusion") -> RetrievedRAGContext:
        """Stage 2: Runs fresh TelemetryRAGRetriever (logs, metrics, traces, fusion, prompt)."""
        return TelemetryRAGRetriever().retrieve(snapshot, mode=mode)

    def diagnose_context(
        self,
        snapshot: TelemetrySnapshot,
        rag_ctx: RetrievedRAGContext,
    ) -> tuple[RCAReport, str, str | None]:
        """
        Stage 3: Produces an RCAReport from retrieved context using live LLM or local RAG fallback.
        Returns (report, engine_type, fallback_reason).
        """
        if self.use_llm and self.llm_client is not None and self.llm_client.is_configured:
            report = self.llm_client.diagnose(
                run_id=snapshot.run_id,
                scenario=rag_ctx.scenario,
                rag_prompt=rag_ctx.rag_prompt,
            )
            if not report.prompt_tokens_used:
                report.prompt_tokens_used = rag_ctx.token_count
            return report, "LIVE_LLM", None

        if self.strict_live:
            raise RuntimeError("Strict live evaluation requested, but GEMINI_API_KEY is not configured.")

        top_svc = rag_ctx.candidate_services[0] if rag_ctx.candidate_services else "unknown"
        report = RCAReport(
            run_id=snapshot.run_id,
            scenario=rag_ctx.scenario,
            root_cause_service=top_svc,
            culprit_services=rag_ctx.candidate_services[:3] if rag_ctx.candidate_services else [top_svc],
            failure_category=rag_ctx.hypothesized_category,
            confidence_score=rag_ctx.confidence,
            root_cause_summary=(
                f"Telemetry-RAG localized root cause to '{top_svc}' "
                f"failing under {rag_ctx.hypothesized_category.value}."
            ),
            evidence_triangulation=rag_ctx.triangulation,
            prompt_tokens_used=rag_ctx.token_count,
        )
        reason = "LLM disabled (--no-llm)" if not self.use_llm else "GEMINI_API_KEY not configured"
        return report, "TELEMETRY_RAG_LOCAL", reason

    @staticmethod
    def build_artifact(
        case_id: str,
        mode: str,
        snapshot: TelemetrySnapshot,
        rag_ctx: RetrievedRAGContext,
        report: RCAReport,
        score: CaseScore,
        stage_timings: list[StageTiming],
        source: Literal["live", "cache"] = "live",
    ) -> CaseAnalysisArtifact:
        """Builds a canonical, self-contained CaseAnalysisArtifact from pipeline outputs."""
        log_snippets_count = sum(len(ev.snippets) for ev in rag_ctx.log_evidences.values())
        novel_templates_count = sum(ev.novel_templates_count for ev in rag_ctx.log_evidences.values())
        logs_tokens = sum(ev.estimated_tokens for ev in rag_ctx.log_evidences.values())

        metric_ev = rag_ctx.metric_evidence
        trace_ev = rag_ctx.trace_evidence

        summary_counts = {
            "log_services": len(rag_ctx.log_evidences),
            "log_snippets": log_snippets_count,
            "novel_templates": novel_templates_count,
            "metric_alerts": len(metric_ev.alerts) if metric_ev else 0,
            "oom_alerts": sum(1 for a in metric_ev.alerts if a.is_oom_risk) if metric_ev else 0,
            "trace_total": trace_ev.total_traces if trace_ev else 0,
            "trace_spans": trace_ev.total_spans if trace_ev else 0,
            "trace_errors": trace_ev.error_spans_count if trace_ev else 0,
            "trace_bottlenecks": trace_ev.latency_bottlenecks_count if trace_ev else 0,
        }

        evidence_bundle = EvidenceBundle(
            log_evidences=rag_ctx.log_evidences,
            metric_evidence=metric_ev,
            trace_evidence=trace_ev,
            summary_counts=summary_counts,
        )

        # Build FusionDebugInfo ordered strictly by rag_ctx.candidate_services
        debug = rag_ctx.fusion_debug or {}
        service_scores: dict[str, float] = debug.get("service_scores", {})
        signal_votes: dict[str, list[str]] = debug.get("signal_votes", {})
        ordered_services = list(rag_ctx.candidate_services) + [
            s for s in service_scores if s not in rag_ctx.candidate_services
        ]
        ranked_details = [
            ServiceFusionDetail(
                service=svc,
                score=service_scores.get(svc, 0.0),
                votes=[modal for modal, svcs in signal_votes.items() if svc in svcs],
            )
            for svc in ordered_services
            if svc in service_scores
        ]
        fusion_info = FusionDebugInfo(
            ranked_services=ranked_details,
            signal_votes=signal_votes,
            category_likelihoods=debug.get("category_likelihoods", {}),
            hypothesized_category=rag_ctx.hypothesized_category.value,
            initial_confidence=rag_ctx.confidence,
        )

        sec_tokens = rag_ctx.prompt_section_tokens or {}
        prompt_info = PromptAuditInfo(
            max_tokens=2400,
            total_tokens=sec_tokens.get("total", rag_ctx.token_count),
            logs_tokens=sec_tokens.get("logs", logs_tokens),
            metrics_tokens=sec_tokens.get("metrics", metric_ev.estimated_tokens if metric_ev else 0),
            traces_tokens=sec_tokens.get("traces", trace_ev.estimated_tokens if trace_ev else 0),
            prompt_preview=rag_ctx.rag_prompt,
        )

        eval_record = EvaluationRecord(
            case_id=score.case_id,
            ground_truth_service=score.ground_truth_service,
            predicted_service=score.predicted_service,
            ranked_candidates=list(score.ranked_candidates),
            ground_truth_fault=score.ground_truth_fault,
            predicted_category=score.predicted_category,
            top1_hit=score.top1_hit,
            top3_hit=score.top3_hit,
            mrr=score.mrr,
            fault_match=score.fault_match,
            error_bucket=classify_error_bucket(score.top1_hit, score.top3_hit, score.fault_match),
            confidence=score.confidence,
            prompt_tokens=score.prompt_tokens,
            duration_ms=score.duration_ms,
            engine_type=score.engine_type,
        )

        case_metadata = {
            "run_id": snapshot.run_id,
            "scenario": snapshot.metadata.scenario if snapshot.metadata else "unknown",
            "log_services_raw": len(snapshot.logs),
            "metric_series_raw": len(snapshot.metrics),
            "traces_raw": len(snapshot.traces),
        }

        return CaseAnalysisArtifact(
            case_id=case_id,
            mode=mode,
            engine_type=score.engine_type,
            created_at=datetime.now(UTC).isoformat(),
            source=source,
            case_metadata=case_metadata,
            stage_timings=stage_timings,
            evidence=evidence_bundle,
            fusion=fusion_info,
            prompt=prompt_info,
            report=report,
            evaluation=eval_record,
        )

    @staticmethod
    def artifact_path(output_dir: Path | str, case_id: str, mode: str) -> Path:
        """Canonical disk path for a persisted case+mode artifact."""
        return Path(output_dir) / "artifacts" / case_id / f"{mode.replace('-', '_')}.json"

    @classmethod
    def save_artifact(cls, artifact: CaseAnalysisArtifact, output_dir: Path | str) -> Path:
        """Persists a CaseAnalysisArtifact atomically via temp file + os.replace, and syncs to PostgreSQL."""
        path = cls.artifact_path(output_dir, artifact.case_id, artifact.mode)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = artifact.model_dump_json(indent=2)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".tmp_", suffix=".json", delete=False
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)

        # Sync to PostgreSQL if available
        try:
            from src.db.postgres_store import PostgresStore
            PostgresStore().save_artifact(artifact)
        except Exception as e:
            _logger.debug("PostgreSQL sync bypassed: %s", e)

        return path

    @classmethod
    def load_artifact(cls, output_dir: Path | str, case_id: str, mode: str) -> CaseAnalysisArtifact | None:
        """Loads a persisted CaseAnalysisArtifact from disk if present and valid."""
        path = cls.artifact_path(output_dir, case_id, mode)
        if not path.exists():
            return None
        try:
            artifact = CaseAnalysisArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            artifact.source = "cache"
            return artifact
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Failed to load artifact %s: %s", path, exc)
            return None

    def run_case_full(
        self,
        case_dir: Path | str,
        mode: AblationMode = "fusion",
        ground_truth_service: str | None = None,
        fault_type: str | None = None,
        snapshot: TelemetrySnapshot | None = None,
        gt_info: dict[str, Any] | None = None,
        rag_ctx: RetrievedRAGContext | None = None,
    ) -> tuple[RCAReport, CaseScore, CaseAnalysisArtifact]:
        """
        Runs the canonical ObservaSage pipeline on a single case and returns
        (RCAReport, CaseScore, CaseAnalysisArtifact).
        Accepts optional precomputed `snapshot`/`gt_info`/`rag_ctx` to prevent duplicate work.
        """
        t_total = time.perf_counter()
        case_path = Path(case_dir)
        timings: list[StageTiming] = []

        # Stage 1: Ingest
        t0 = time.perf_counter()
        if snapshot is None or gt_info is None:
            snapshot, gt_info = self.load_case(case_path)
            timings.append(StageTiming(stage="ingesting", status="ok", duration_ms=round((time.perf_counter() - t0) * 1000.0, 2)))
        else:
            timings.append(StageTiming(stage="ingesting", status="skipped", duration_ms=0.0, detail="Reused precomputed snapshot"))

        gt_service = ground_truth_service or gt_info.get("ground_truth_service") or "unknown"
        gt_fault = fault_type or gt_info.get("ground_truth_fault_type") or "unknown"

        # Stage 2: Retrieve + Fuse + Prompt
        t1 = time.perf_counter()
        if rag_ctx is None:
            rag_ctx = self.retrieve_context(snapshot, mode=mode)
            timings.append(StageTiming(stage="retrieving", status="ok", duration_ms=round((time.perf_counter() - t1) * 1000.0, 2)))
        else:
            timings.append(StageTiming(stage="retrieving", status="skipped", duration_ms=0.0, detail="Reused precomputed RAG context"))

        # Stage 3: Diagnose
        t2 = time.perf_counter()
        report, engine_type, fallback_reason = self.diagnose_context(snapshot, rag_ctx)
        timings.append(
            StageTiming(
                stage="diagnosing",
                status="fallback" if fallback_reason else "ok",
                duration_ms=round((time.perf_counter() - t2) * 1000.0, 2),
                detail=fallback_reason,
            )
        )

        # Stage 4: Score
        t3 = time.perf_counter()
        elapsed_ms = (time.perf_counter() - t_total) * 1000.0
        score = score_single_case(
            case_id=case_path.name,
            ground_truth_service=gt_service,
            ground_truth_fault=gt_fault,
            report=report,
            duration_ms=round(elapsed_ms, 2),
            engine_type=engine_type,
        )
        timings.append(StageTiming(stage="scoring", status="ok", duration_ms=round((time.perf_counter() - t3) * 1000.0, 2)))

        artifact = self.build_artifact(
            case_id=case_path.name,
            mode=mode,
            snapshot=snapshot,
            rag_ctx=rag_ctx,
            report=report,
            score=score,
            stage_timings=timings,
            source="live",
        )
        return report, score, artifact

    def run_single_case(
        self,
        case_dir: Path | str,
        mode: AblationMode = "fusion",
        ground_truth_service: str | None = None,
        fault_type: str | None = None,
    ) -> tuple[RCAReport, CaseScore]:
        """
        Backwards-compatible wrapper returning (RCAReport, CaseScore) for a single case.
        """
        report, score, _ = self.run_case_full(
            case_dir=case_dir,
            mode=mode,
            ground_truth_service=ground_truth_service,
            fault_type=fault_type,
        )
        return report, score

    @staticmethod
    def _load_existing_scores(csv_path: Path) -> dict[str, CaseScore]:
        """Loads previously saved CaseScore rows from CSV for crash-safe resume."""
        existing: dict[str, CaseScore] = {}
        if not csv_path.exists():
            return existing
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cid = row.get("case_id", "").strip()
                if not cid:
                    continue
                existing[cid] = CaseScore(
                    case_id=cid,
                    ground_truth_service=row.get("ground_truth_service", ""),
                    predicted_service=row.get("predicted_service", ""),
                    ranked_candidates=[s for s in row.get("ranked_candidates", "").split(";") if s],
                    ground_truth_fault=row.get("ground_truth_fault", ""),
                    predicted_category=row.get("predicted_category", ""),
                    top1_hit=row.get("top1_hit", "").lower() == "true",
                    top3_hit=row.get("top3_hit", "").lower() == "true",
                    mrr=float(row.get("mrr", 0.0) or 0.0),
                    fault_match=row.get("fault_match", "").lower() == "true",
                    confidence=float(row.get("confidence", 0.0) or 0.0),
                    prompt_tokens=int(row.get("prompt_tokens", 0) or 0),
                    duration_ms=float(row.get("duration_ms", 0.0) or 0.0),
                    engine_type=row.get("engine_type", "TELEMETRY_RAG_LOCAL"),
                )
        return existing

    @staticmethod
    def _append_score_csv(csv_path: Path, score: CaseScore) -> None:
        """Appends a single CaseScore row immediately so progress survives crashes."""
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        row_dict = score.to_dict()
        file_exists = csv_path.exists() and csv_path.stat().st_size > 0
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row_dict.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(row_dict)

    def run_batch(
        self,
        modes: list[AblationMode] | tuple[AblationMode, ...] = ("fusion",),
        suite_filter: list[str] | None = None,
        fault_filter: list[str] | None = None,
        limit: int | None = None,
        output_dir: Path | str = Path("results"),
        resume: bool = True,
        backfill_artifacts: bool = False,
    ) -> dict[str, list[CaseScore]]:
        """
        Iterates over discovered RCAEval cases across requested ablation modes,
        persisting each case incrementally to `results/ablation_{mode}.csv`.
        """
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        cases = self.discover_cases(suite_filter=suite_filter, fault_filter=fault_filter, limit=limit)
        if not cases:
            console.print("[yellow]No complete tri-modal cases found matching filters.[/yellow]")
            return {m: [] for m in modes}

        results_by_mode: dict[str, list[CaseScore]] = {}
        ablation_summaries: dict[str, dict] = {}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            for mode in modes:
                csv_name = f"ablation_{mode.replace('-', '_')}.csv"
                csv_path = out_path / csv_name

                if resume:
                    existing_map = self._load_existing_scores(csv_path)
                else:
                    if csv_path.exists():
                        csv_path.unlink()
                    existing_map = {}

                mode_scores: list[CaseScore] = []
                task_id = progress.add_task(f"[cyan]{mode}[/cyan]", total=len(cases))

                for case_meta in cases:
                    cid = case_meta["case_id"]
                    art_exists = self.artifact_path(out_path, cid, mode).exists()
                    if cid in existing_map and (not backfill_artifacts or art_exists):
                        mode_scores.append(existing_map[cid])
                        progress.advance(task_id)
                        continue

                    try:
                        _, score, artifact = self.run_case_full(
                            case_dir=case_meta["case_dir"],
                            mode=mode,
                            ground_truth_service=case_meta["fault_service"],
                            fault_type=case_meta["fault_type"],
                        )
                        if cid in existing_map:
                            # Backfill mode: preserve existing CSV score row, only write missing artifact
                            mode_scores.append(existing_map[cid])
                        else:
                            mode_scores.append(score)
                            self._append_score_csv(csv_path, score)
                        self.save_artifact(artifact, out_path)
                    except Exception as exc:  # noqa: BLE001
                        _logger.error("Case %s (%s) failed: %s", cid, mode, exc)
                    progress.advance(task_id)

                results_by_mode[mode] = mode_scores
                ablation_summaries[mode] = aggregate_scores(mode_scores)

        if results_by_mode:
            suite_desc = ", ".join(suite_filter) if suite_filter else "All discovered suites"
            engine_desc = "LIVE_LLM" if (self.use_llm and self.llm_client and self.llm_client.is_configured) else "TELEMETRY_RAG_LOCAL"
            comparison_lines = [
                "### ObservaSage 4-Way Ablation Comparison",
                "",
                f"- **Dataset Scope:** {suite_desc}",
                f"- **Engine Mode:** `{engine_desc}`",
                "",
                "| Mode | Cases | Top@1 | Top@3 | MRR | Category Acc |",
                "|---|:---:|:---:|:---:|:---:|:---:|",
            ]
            per_mode_tables: list[str] = []
            for mode, m_scores in results_by_mode.items():
                st = ablation_summaries[mode]
                comparison_lines.append(
                    f"| `{mode}` | {st['total_cases']} | {st['overall_top1']:.2%} | "
                    f"{st['overall_top3']:.2%} | {st['overall_mrr']:.4f} | {st['overall_fault_acc']:.2%} |"
                )
                per_mode_tables.append(format_ablation_table(m_scores, mode))

            full_md = "\n".join(comparison_lines) + "\n\n" + "\n\n".join(per_mode_tables) + "\n"
            (out_path / "paper_table.md").write_text(full_md, encoding="utf-8")
            export_case_comparison_csv(results_by_mode, out_path / "case_comparison.csv", artifacts_root=out_path)
            console.print("\n" + "\n".join(comparison_lines))

        return results_by_mode


def main() -> None:
    parser = argparse.ArgumentParser(description="ObservaSage Dataset-Only Batch Pipeline Orchestrator")
    parser.add_argument("--cases-root", type=Path, default=Path("data/ground_truth/rcaeval"))
    parser.add_argument("--registry-file", type=Path, default=None)
    parser.add_argument("--suite", nargs="+", default=None, help="Filter suites (e.g. RE2-OB RE2-TT)")
    parser.add_argument("--fault-filter", nargs="+", default=None, help="Filter faults (e.g. cpu mem delay)")
    parser.add_argument("--modes", nargs="+", choices=list(ALL_MODES), default=["fusion"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    parser.add_argument("--no-resume", action="store_true", help="Overwrite existing mode CSVs instead of resuming")
    parser.add_argument("--backfill-artifacts", action="store_true", help="Generate missing JSON artifacts for cases already in CSVs")
    parser.add_argument("--strict-live", action="store_true", help="Require live Gemini API key")
    parser.add_argument("--no-llm", action="store_true", help="Run local Telemetry-RAG + Fusion without external LLM calls")
    args = parser.parse_args()

    pipeline = ObservaSagePipeline(
        cases_root=args.cases_root,
        registry_path=args.registry_file,
        strict_live=args.strict_live,
        use_llm=not args.no_llm,
    )
    pipeline.run_batch(
        modes=args.modes,
        suite_filter=args.suite,
        fault_filter=args.fault_filter,
        limit=args.limit,
        output_dir=args.output_dir,
        resume=not args.no_resume,
        backfill_artifacts=args.backfill_artifacts,
    )


if __name__ == "__main__":
    main()
