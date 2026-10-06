#!/usr/bin/env python3
"""
ObservaSage Automated Benchmark Evaluation & Ablation Harness.
Evaluates RCAEval test cases across 4 system configurations:
  - logs-only (LogSage baseline)
  - logs-metrics
  - logs-traces
  - fusion (ObservaSage)

Calculates Top@1 Accuracy, Top@3 Accuracy, Mean Reciprocal Rank (MRR),
and fault classification accuracy, generating publication-ready Markdown tables and CSVs.

Usage:
    uv run python scripts/evaluate_rcaeval.py --mode fusion --output-csv results/ablation_fusion.csv
    uv run python scripts/evaluate_rcaeval.py --mode logs-only --output-csv results/ablation_logs.csv
"""

import argparse
import csv
import glob
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from scripts.rcaeval_adapter import RCAEvalAdapter
from src.eval import (
    CaseScore,
    aggregate_scores,
    format_ablation_table,
    score_single_case,
)
from src.fusion import FusionEngine
from src.llm import GeminiRCAClient, build_multimodal_prompt
from src.log_processor import LogSageProcessor
from src.metrics_processor import MetricsProcessor
from src.schemas import TelemetrySnapshot
from src.trace_processor import TraceProcessor

console = Console()


def run_diagnosis_pipeline(
    snapshot: TelemetrySnapshot,
    mode: str,
    client: GeminiRCAClient,
    strict_live: bool = False,
) -> Any:
    """Runs the tri-modal processing and prompt inference pipeline for a single snapshot."""
    # 1. Log processing
    log_processor = LogSageProcessor()
    if snapshot.baseline and snapshot.baseline.logs:
        log_processor.miner.train_baseline_from_runs([snapshot.baseline.logs])
        log_processor.is_baseline_ready = True
    else:
        log_processor.load_or_train_baselines()
    log_evidences = log_processor.process_all_logs(snapshot.logs)

    # 2. Metric processing
    metric_evidence = None
    if mode in ["logs-metrics", "fusion"]:
        metric_proc = MetricsProcessor()
        metric_evidence = metric_proc.process(
            incident_metrics=snapshot.metrics,
            baseline_metrics=snapshot.baseline_metrics,
        )

    # 3. Trace processing
    trace_evidence = None
    if mode in ["logs-traces", "fusion"]:
        trace_proc = TraceProcessor()
        trace_evidence = trace_proc.process(
            incident_traces=snapshot.traces,
            baseline_traces=snapshot.baseline_traces,
        )

    # 4. Fusion Engine
    fusion_engine = FusionEngine()
    ranked_candidates, failure_cat, triangulation, conf = fusion_engine.correlate(
        log_evidences=log_evidences,
        metric_evidence=metric_evidence,
        trace_evidence=trace_evidence,
    )

    # 5. Multimodal Prompt
    prompt = build_multimodal_prompt(
        run_id=snapshot.run_id,
        log_evidences=log_evidences,
        metric_evidence=metric_evidence,
        trace_evidence=trace_evidence,
        mode=mode,  # type: ignore
    )

    # 6. LLM Inference
    scenario = snapshot.metadata.scenario if snapshot.metadata else "rcaeval"
    allow_heur = not strict_live and not client.is_configured
    report = client.diagnose(
        run_id=snapshot.run_id,
        scenario=scenario,
        user_prompt=prompt,
        candidate_services=ranked_candidates,
        hypothesized_category=failure_cat,
        triangulation=triangulation,
        allow_heuristic=allow_heur,
    )

    if not report.culprit_services and ranked_candidates:
        report.culprit_services = ranked_candidates[:3]

    return report


def main():
    parser = argparse.ArgumentParser(description="ObservaSage Benchmark Evaluation & Ablation Runner")
    parser.add_argument(
        "--mode",
        type=str,
        default="fusion",
        choices=["logs-only", "logs-metrics", "logs-traces", "fusion"],
        help="Ablation configuration mode",
    )
    parser.add_argument("--snapshots-dir", type=str, default="data/runs/failed", help="Path to converted telemetry snapshots")
    parser.add_argument("--raw-cases-dir", type=str, help="Path to raw RCAEval cases root folder (converts on the fly)")
    parser.add_argument("--output-csv", type=str, help="Path to save detailed evaluation CSV")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of cases to evaluate (for dry-runs)")
    parser.add_argument("--strict-live", action="store_true", help="Disallow offline heuristic fallback (requires API key)")

    args = parser.parse_args()

    client = GeminiRCAClient()
    mode_tag = "LIVE GEMINI 1.5 PRO" if client.is_configured else "SIMULATED / HEURISTIC"

    console.print(Panel(
        f"[bold white]ObservaSage RCAEval Evaluation Harness[/bold white]\n"
        f"Mode: [bold cyan]{args.mode}[/bold cyan] | Engine: [bold green]{mode_tag}[/bold green]",
        box=ROUNDED,
        border_style="cyan",
    ))

    # 1. Discover target cases
    target_snapshots: List[Path] = []
    if args.raw_cases_dir and os.path.exists(args.raw_cases_dir):
        adapter = RCAEvalAdapter()
        console.print(f"[dim]Scanning raw cases in: {args.raw_cases_dir}...[/dim]")
        raw_root = Path(args.raw_cases_dir)
        for c_dir in sorted(raw_root.iterdir()):
            if c_dir.is_dir() and (c_dir / "inject_time.txt").exists():
                s_file, _ = adapter.save_case(c_dir, output_dir=args.snapshots_dir)
                target_snapshots.append(s_file)
    else:
        pattern = os.path.join(args.snapshots_dir, "*_telemetry.json")
        target_snapshots = [Path(p) for p in sorted(glob.glob(pattern))]

    if not target_snapshots:
        console.print(f"[bold red]Error:[/bold red] No telemetry snapshots found in {args.snapshots_dir}.")
        console.print("[yellow]Tip: Run scripts/rcaeval_adapter.py on RCAEval cases first, or specify --raw-cases-dir.[/yellow]")
        sys.exit(1)

    if args.limit:
        target_snapshots = target_snapshots[: args.limit]

    console.print(f"Loaded [bold green]{len(target_snapshots)}[/bold green] benchmark cases to evaluate in mode: [cyan]{args.mode}[/cyan]\n")

    # 2. Evaluation Loop
    scores: List[CaseScore] = []
    for idx, snap_path in enumerate(target_snapshots, start=1):
        with open(snap_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            snapshot = TelemetrySnapshot(**data)

        # Retrieve ground truth
        gt_file = snap_path.parent / f"{snapshot.run_id}_ground_truth.json"
        gt_service = snapshot.ground_truth_service or "unknown"
        gt_fault = snapshot.ground_truth_fault_type or "unknown"

        if gt_file.exists():
            with open(gt_file, "r", encoding="utf-8") as gf:
                gt_meta = json.load(gf)
                gt_service = gt_meta.get("ground_truth_service", gt_service)
                gt_fault = gt_meta.get("ground_truth_fault_type", gt_fault)

        start_t = time.time()
        try:
            report = run_diagnosis_pipeline(
                snapshot=snapshot,
                mode=args.mode,
                client=client,
                strict_live=args.strict_live,
            )
            duration_ms = (time.time() - start_t) * 1000.0

            score = score_single_case(
                case_id=snapshot.run_id,
                ground_truth_service=gt_service,
                ground_truth_fault=gt_fault,
                report=report,
                duration_ms=duration_ms,
            )
            scores.append(score)

            status_glyph = "[bold green]✔ HIT[/bold green]" if score.top1_hit else "[bold red]✘ MISS[/bold red]"
            console.print(
                f"[{idx}/{len(target_snapshots)}] {snapshot.run_id[:35]:35s} | "
                f"GT: [cyan]{gt_service:14s}[/cyan] | Pred: [yellow]{score.predicted_service:14s}[/yellow] | "
                f"MRR: {score.mrr:.2f} | {status_glyph}"
            )
        except Exception as e:
            console.print(f"[{idx}/{len(target_snapshots)}] [bold red]ERROR {snapshot.run_id}:[/bold red] {e}")

    # 3. Aggregate and Display Results
    if not scores:
        console.print("[red]No cases scored successfully.[/red]")
        return

    summary = aggregate_scores(scores)
    console.print("\n")
    summary_table = Table(title=f"Evaluation Summary ({args.mode})", box=ROUNDED, expand=True)
    summary_table.add_column("Metric", style="bold cyan")
    summary_table.add_column("Score", style="bold green")

    summary_table.add_row("Total Evaluated Cases", str(summary["total_cases"]))
    summary_table.add_row("Top@1 Accuracy", f"{summary['overall_top1']:.2%}")
    summary_table.add_row("Top@3 Accuracy", f"{summary['overall_top3']:.2%}")
    summary_table.add_row("Mean Reciprocal Rank (MRR)", f"{summary['overall_mrr']:.4f}")
    summary_table.add_row("Fault Classification Accuracy", f"{summary['overall_fault_acc']:.2%}")
    console.print(summary_table)

    # 4. Print Markdown Table (For Paper)
    md_table = format_ablation_table(scores, mode_label=args.mode)
    console.print(Panel(md_table, title="[bold white]Publication Table Format (Markdown)[/bold white]", box=ROUNDED))

    # 5. Save detailed CSV
    if args.output_csv:
        out_csv_path = Path(args.output_csv).resolve()
        out_csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_csv_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(scores[0].to_dict().keys()))
            writer.writeheader()
            for s in scores:
                writer.writerow(s.to_dict())
        console.print(f"[dim]Detailed per-case results saved to: {out_csv_path}[/dim]\n")


if __name__ == "__main__":
    main()
