#!/usr/bin/env python3
"""
RCAEval Tri-Modal Dataset Downloader for ObservaSage.

Downloads all tri-modal benchmark cases (Logs + Metrics + Traces) from Hugging Face
(`phamquiluan/RCAEval`) into `data/ground_truth/rcaeval/`.

By default, running with no arguments downloads all 239 tri-modal cases across:
  - RE2-OB (90 cases)
  - RE2-TT (89 cases)
  - RE3-OB (30 cases)
  - RE3-TT (30 cases)

Usage:
    .venv/bin/python scripts/download_rcaeval.py
    .venv/bin/python scripts/download_rcaeval.py --suite RE2-OB
    .venv/bin/python scripts/download_rcaeval.py --limit 6
    .venv/bin/python scripts/download_rcaeval.py --dry-run
"""

import argparse
from pathlib import Path

import pandas as pd
from huggingface_hub import hf_hub_download, snapshot_download
from rich.console import Console
from rich.table import Table

REPO_ID = "phamquiluan/RCAEval"
DEFAULT_OUTPUT_DIR = Path("data/ground_truth/rcaeval")
REQUIRED_FILES = ("logs.parquet", "metrics.parquet", "traces.parquet", "inject_time.txt")

console = Console()


def ensure_registry(output_dir: Path) -> Path:
    """Ensures `cases.parquet` exists locally, downloading it if needed."""
    registry_path = output_dir / "cases.parquet"
    if not registry_path.exists():
        console.print(f"[cyan]Downloading cases.parquet from {REPO_ID}...[/cyan]")
        hf_hub_download(
            repo_id=REPO_ID,
            repo_type="dataset",
            filename="cases.parquet",
            local_dir=str(output_dir),
        )
    return registry_path


def get_trimodal_cases(registry_path: Path, suites: list[str] | None = None) -> pd.DataFrame:
    """Filters `cases.parquet` to cases that have both logs and traces (plus metrics)."""
    df = pd.read_parquet(registry_path)
    tri = df[df["has_logs"].astype(bool) & df["has_traces"].astype(bool)].copy()
    if suites:
        normalized = {s.upper() for s in suites}
        tri = tri[tri["dataset"].str.upper().isin(normalized)]
    return tri.reset_index(drop=True)


def is_case_complete(case_dir: Path) -> bool:
    """Returns True if the case directory already contains all required telemetry files."""
    if not case_dir.is_dir():
        return False
    return all((case_dir / fname).exists() for fname in REQUIRED_FILES)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download all tri-modal RCAEval cases (Logs + Metrics + Traces)."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Target directory for RCAEval cases (default: data/ground_truth/rcaeval).",
    )
    parser.add_argument(
        "--suite",
        nargs="+",
        default=None,
        help="Optional suite filter (e.g. RE2-OB RE2-TT RE3-OB RE3-TT). Defaults to all tri-modal.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of missing cases to download.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Parallel download workers for huggingface_hub (default: 4).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without downloading any files.",
    )
    args = parser.parse_args()

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    registry_path = ensure_registry(output_dir)
    tri_df = get_trimodal_cases(registry_path, suites=args.suite)

    if tri_df.empty:
        console.print("[red]No matching tri-modal cases found in cases.parquet.[/red]")
        return

    all_cases = tri_df["case"].astype(str).tolist()
    already_present = [c for c in all_cases if is_case_complete(output_dir / c)]
    missing_cases = [c for c in all_cases if not is_case_complete(output_dir / c)]

    if args.limit is not None:
        missing_cases = missing_cases[: max(0, args.limit)]

    table = Table(title="RCAEval Tri-Modal Dataset Status")
    table.add_column("Metric", style="bold cyan")
    table.add_column("Count", justify="right")
    table.add_row("Total tri-modal cases selected", str(len(all_cases)))
    table.add_row("Already downloaded (complete)", str(len(already_present)))
    table.add_row("To download now", str(len(missing_cases)))
    console.print(table)

    if not missing_cases:
        console.print("[green]All selected tri-modal cases are already downloaded![/green]")
        return

    if args.dry_run:
        console.print("[yellow]Dry run enabled. First 10 cases queued:[/yellow]")
        for c in missing_cases[:10]:
            console.print(f"  - {c}")
        if len(missing_cases) > 10:
            console.print(f"  ... and {len(missing_cases) - 10} more.")
        return

    patterns = [f"{case_id}/*" for case_id in missing_cases]
    console.print(
        f"[cyan]Downloading {len(missing_cases)} tri-modal cases from {REPO_ID} into {output_dir}...[/cyan]"
    )

    snapshot_download(
        repo_id=REPO_ID,
        repo_type="dataset",
        allow_patterns=patterns,
        local_dir=str(output_dir),
        max_workers=args.max_workers,
    )

    completed_now = sum(1 for c in all_cases if is_case_complete(output_dir / c))
    console.print(
        f"[bold green]Done![/bold green] {completed_now}/{len(all_cases)} tri-modal cases are ready in {output_dir}."
    )


if __name__ == "__main__":
    main()
