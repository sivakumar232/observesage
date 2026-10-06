#!/usr/bin/env python3
"""
ObservaSage Root Cause Analysis (RCA) CLI.
Analyzes incident telemetry snapshots using Multi-Signal Triangulation (Logs, Metrics, Traces).
Supports all 4 ablation modes: logs-only, logs-metrics, logs-traces, fusion.

Usage:
    uv run python scripts/analyze.py --telemetry-file data/runs/failed/run_..._telemetry.json --mode fusion
    uv run python scripts/analyze.py --run-id run_... --mode logs-only
"""

import argparse
import glob
import json
import os
import sys
from typing import Optional
from pathlib import Path
from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.rag import TelemetryRAGRetriever
from src.llm import GeminiRCAClient
from src.schemas import RCAReport, TelemetrySnapshot, RemediationStep

console = Console()


def find_telemetry_file(run_id_or_path: str) -> Optional[str]:
    """Finds telemetry file by exact path or run_id."""
    if os.path.isfile(run_id_or_path):
        return run_id_or_path

    # Check data/runs/failed/ and data/runs/success/
    for sub in ["failed", "success"]:
        candidate = os.path.join("data", "runs", sub, f"{run_id_or_path}_telemetry.json")
        if os.path.isfile(candidate):
            return candidate

    return None


def print_rca_report(report: RCAReport, is_live_llm: bool):
    """Renders a beautiful Rich RCA report card to the terminal."""
    console.print("\n")
    mode_tag = "[bold green]LIVE GEMINI 1.5 PRO[/bold green]" if is_live_llm else "[bold yellow]OFFLINE SIMULATED[/bold yellow]"
    console.print(Panel(
        f"[bold white]Root Cause Analysis Report[/bold white] — Run: [cyan]{report.run_id}[/cyan] ({mode_tag})",
        box=ROUNDED,
        border_style="cyan"
    ))

    # Summary Table
    table = Table(box=ROUNDED, show_header=False, expand=True)
    table.add_column("Field", style="bold cyan", width=24)
    table.add_column("Value", style="white")

    table.add_row("Root Cause Service", f"[bold red]{report.root_cause_service}[/bold red]")
    if report.culprit_services:
        cand_str = " -> ".join([f"[yellow]{s}[/yellow]" for s in report.culprit_services[:3]])
        table.add_row("Ranked Culprits (Top-k)", cand_str)
    table.add_row("Failure Category", f"[bold magenta]{report.failure_category.value}[/bold magenta]")
    table.add_row("Confidence Score", f"[bold green]{report.confidence_score * 100:.1f}%[/bold green]")
    table.add_row("Primary Signal", f"[yellow]{report.evidence_triangulation.primary_signal}[/yellow]")
    table.add_row("Diagnostic Summary", report.root_cause_summary)
    table.add_row("Triangulation Logic", report.evidence_triangulation.triangulation_reasoning)
    table.add_row("Prompt Tokens Used", f"{report.prompt_tokens_used} tokens")

    console.print(table)

    # Remediation Table
    if report.remediation_steps:
        rem_table = Table(title="Recommended Remediation Actions", box=ROUNDED, expand=True)
        rem_table.add_column("#", width=4, style="dim")
        rem_table.add_column("Action", style="bold yellow")
        rem_table.add_column("Target Service", style="cyan")
        rem_table.add_column("Command / Config", style="green")
        rem_table.add_column("Expected Impact", style="dim white")

        for idx, rem in enumerate(report.remediation_steps, 1):
            rem_table.add_row(
                str(idx),
                rem.action,
                rem.target_service,
                rem.command_or_config or "N/A",
                rem.expected_impact
            )
        console.print(rem_table)
    console.print("\n")


def main():
    parser = argparse.ArgumentParser(description="ObservaSage Automated Root Cause Analysis")
    parser.add_argument("--telemetry-file", type=str, help="Path to telemetry JSON file")
    parser.add_argument("--run-id", type=str, help="Run ID of the failed run")
    parser.add_argument(
        "--mode",
        type=str,
        default="fusion",
        choices=["logs-only", "logs-metrics", "logs-traces", "fusion"],
        help="Ablation diagnostic mode",
    )
    parser.add_argument("--save-report", action="store_true", default=True, help="Save report to JSON alongside telemetry")
    args = parser.parse_args()

    target_ref = args.telemetry_file or args.run_id
    if not target_ref:
        candidates = sorted(glob.glob("data/runs/failed/*_telemetry.json"), reverse=True)
        if candidates:
            target_ref = candidates[0]
            console.print(f"[dim]No run specified. Analyzing most recent failed run: {target_ref}[/dim]")
        else:
            console.print("[bold red]Error:[/bold red] No failed telemetry runs found in data/runs/failed/.")
            sys.exit(1)

    filepath = find_telemetry_file(target_ref)
    if not filepath:
        console.print(f"[bold red]Error:[/bold red] Telemetry file not found for: {target_ref}")
        sys.exit(1)

    console.print(f"[bold cyan]▶ Loading Telemetry Snapshot:[/bold cyan] {filepath} (Mode: [bold green]{args.mode}[/bold green])")
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
        snapshot = TelemetrySnapshot(**data)

    scenario = snapshot.metadata.scenario if snapshot.metadata else "unknown"

    # Step 1: Multi-Modal Telemetry-RAG Retrieval
    console.print("[dim]1. Running Telemetry-RAG multi-modal retrieval across Logs, Metrics, and Traces...[/dim]")
    retriever = TelemetryRAGRetriever()
    rag_ctx = retriever.retrieve(snapshot, mode=args.mode)
    console.print(f"✔ Retrieved RAG context ({rag_ctx.token_count} BPE tokens).")
    if rag_ctx.candidate_services:
        console.print(f"✔ Top retrieved culprit candidate: [bold cyan]{rag_ctx.candidate_services[0]}[/bold cyan] (Category: {rag_ctx.hypothesized_category.value})")

    # Step 2: Generative LLM Diagnosis
    client = GeminiRCAClient()
    if client.is_configured:
        console.print("[dim]2. Sending grounded Telemetry-RAG prompt to Gemini 1.5 Pro...[/dim]")
        report = client.diagnose(
            run_id=snapshot.run_id,
            scenario=rag_ctx.scenario,
            rag_prompt=rag_ctx.rag_prompt,
        )
    else:
        console.print("[yellow]Notice: GEMINI_API_KEY not configured. Generating report directly from Telemetry-RAG retrieved consensus.[/yellow]")
        top_svc = rag_ctx.candidate_services[0] if rag_ctx.candidate_services else "unknown"
        report = RCAReport(
            run_id=snapshot.run_id,
            scenario=rag_ctx.scenario,
            root_cause_service=top_svc,
            culprit_services=rag_ctx.candidate_services[:3] if rag_ctx.candidate_services else [top_svc],
            failure_category=rag_ctx.hypothesized_category,
            confidence_score=0.92,
            root_cause_summary=f"Telemetry-RAG localized root cause to '{top_svc}' failing under {rag_ctx.hypothesized_category.value}.",
            evidence_triangulation=rag_ctx.triangulation,
            remediation_steps=[
                RemediationStep(
                    action=f"Inspect and scale compute / network resources for {top_svc}",
                    target_service=top_svc,
                    command_or_config=f"docker update --cpus=2.0 {top_svc}",
                    expected_impact=f"Relieves resource pressure on {top_svc}",
                )
            ],
            prompt_tokens_used=rag_ctx.token_count,
            total_tokens_used=rag_ctx.token_count + 120,
        )

    # Step 3: Display Report
    print_rca_report(report, is_live_llm=client.is_configured)

    # Step 8: Save Report
    if args.save_report:
        out_path = filepath.replace("_telemetry.json", "_rca.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report.model_dump_json(indent=2))
        console.print(f"[dim]RCA Report saved to: {out_path}[/dim]\n")


if __name__ == "__main__":
    main()
