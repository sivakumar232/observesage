#!/usr/bin/env python3
"""
Migrates existing CSV evaluation results and JSON artifacts into PostgreSQL.
"""

import csv
import json
from pathlib import Path
from rich.console import Console

from src.db.postgres_store import PostgresStore
from src.schemas.artifact import CaseAnalysisArtifact

console = Console()


def migrate_csv_and_artifacts():
    results_dir = Path("results")
    artifacts_dir = results_dir / "artifacts"
    store = PostgresStore()
    store.init_db()

    modes = ["logs-only", "logs-metrics", "logs-traces", "fusion"]
    migrated_csv = 0
    migrated_artifacts = 0

    # 1. Migrate JSON Artifacts first (richest information)
    if artifacts_dir.is_dir():
        for case_folder in artifacts_dir.iterdir():
            if not case_folder.is_dir():
                continue
            case_id = case_folder.name
            for art_file in case_folder.glob("*.json"):
                mode_slug = art_file.stem
                mode = mode_slug.replace("_", "-")
                try:
                    with open(art_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    art = CaseAnalysisArtifact.model_validate(data)
                    store.save_artifact(art)
                    migrated_artifacts += 1
                except Exception as e:
                    console.print(f"[yellow]Skipping artifact {art_file}: {e}[/yellow]")

    console.print(f"[green]✓ Migrated {migrated_artifacts} full diagnostic JSON artifacts into PostgreSQL.[/green]")

    # 2. Check each mode CSV to populate any cases not in artifacts
    upsert_csv_sql = """
    INSERT INTO evaluation_results (
        case_id, suite, mode,
        ground_truth_service, predicted_service, ranked_candidates,
        ground_truth_fault, predicted_category,
        top1_hit, top3_hit, mrr, fault_match, error_bucket,
        confidence, prompt_tokens, duration_ms, engine_type,
        has_artifact, created_at
    ) VALUES (
        %(case_id)s, %(suite)s, %(mode)s,
        %(ground_truth_service)s, %(predicted_service)s, %(ranked_candidates)s,
        %(ground_truth_fault)s, %(predicted_category)s,
        %(top1_hit)s, %(top3_hit)s, %(mrr)s, %(fault_match)s, %(error_bucket)s,
        %(confidence)s, %(prompt_tokens)s, %(duration_ms)s, %(engine_type)s,
        FALSE, NOW()
    )
    ON CONFLICT (case_id, mode) DO NOTHING;
    """

    with store.get_connection() as conn:
        with conn.cursor() as cur:
            for mode in modes:
                csv_path = results_dir / f"ablation_{mode.replace('-', '_')}.csv"
                if not csv_path.exists():
                    continue
                with open(csv_path, newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        cid = row.get("case_id", "").strip()
                        suite = cid.split("_")[0].upper() if "_" in cid else "UNKNOWN"
                        cands = [s.strip() for s in row.get("ranked_candidates", "").split(";") if s.strip()]
                        top1 = row.get("top1_hit", "").lower() == "true"
                        top3 = row.get("top3_hit", "").lower() == "true"
                        fmatch = row.get("fault_match", "").lower() == "true"
                        params = {
                            "case_id": cid,
                            "suite": suite,
                            "mode": mode,
                            "ground_truth_service": row.get("ground_truth_service", ""),
                            "predicted_service": row.get("predicted_service", ""),
                            "ranked_candidates": cands,
                            "ground_truth_fault": row.get("ground_truth_fault", ""),
                            "predicted_category": row.get("predicted_category", ""),
                            "top1_hit": top1,
                            "top3_hit": top3,
                            "mrr": float(row.get("mrr", 0.0) or 0.0),
                            "fault_match": fmatch,
                            "error_bucket": "hit" if top1 and fmatch else "category_miss" if top1 else "ranking_miss" if top3 else "localization_miss",
                            "confidence": float(row.get("confidence", 0.0) or 0.0),
                            "prompt_tokens": int(row.get("prompt_tokens", 0) or 0),
                            "duration_ms": float(row.get("duration_ms", 0.0) or 0.0),
                            "engine_type": row.get("engine_type", "TELEMETRY_RAG_LOCAL"),
                        }
                        cur.execute(upsert_csv_sql, params)
                        migrated_csv += 1

    console.print(f"[green]✓ Checked CSV records ({migrated_csv} rows checked).[/green]")


if __name__ == "__main__":
    migrate_csv_and_artifacts()
