"""
PostgreSQL Storage Layer for ObservaSage.
Replaces brittle scattered CSVs and hundreds of JSON artifact files
with indexed SQL tables and native JSONB columns.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, Literal

import psycopg
from psycopg.rows import dict_row

from src.schemas.artifact import CaseAnalysisArtifact
from src.schemas.rca_report import RCAReport

_logger = logging.getLogger(__name__)

DEFAULT_POSTGRES_URL = "postgresql://observasage:observasage_secret@127.0.0.1:5433/observasage"


def get_db_url() -> str:
    """Returns database URL from environment or local Docker default."""
    return os.getenv("DATABASE_URL", DEFAULT_POSTGRES_URL)


class PostgresStore:
    """
    Manages structured schema creation, storage, and fast querying of
    RCA evaluation metrics and full diagnostic artifacts in PostgreSQL.
    """

    def __init__(self, db_url: str | None = None) -> None:
        self.db_url = db_url or get_db_url()

    @contextmanager
    def get_connection(self) -> Generator[psycopg.Connection, None, None]:
        conn = psycopg.connect(self.db_url, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """Creates tables, JSONB indexes, and constraints if they do not exist."""
        ddl = """
        CREATE TABLE IF NOT EXISTS evaluation_results (
            id SERIAL PRIMARY KEY,
            case_id VARCHAR(128) NOT NULL,
            suite VARCHAR(64) NOT NULL,
            mode VARCHAR(32) NOT NULL,
            ground_truth_service VARCHAR(128) NOT NULL,
            predicted_service VARCHAR(128) NOT NULL,
            ranked_candidates TEXT[] NOT NULL DEFAULT '{}',
            ground_truth_fault VARCHAR(64) NOT NULL,
            predicted_category VARCHAR(128) NOT NULL,
            top1_hit BOOLEAN NOT NULL,
            top3_hit BOOLEAN NOT NULL,
            mrr DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            fault_match BOOLEAN NOT NULL DEFAULT FALSE,
            error_bucket VARCHAR(64) NOT NULL DEFAULT 'hit',
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            prompt_tokens INT NOT NULL DEFAULT 0,
            duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            engine_type VARCHAR(64) NOT NULL DEFAULT 'TELEMETRY_RAG_LOCAL',
            has_artifact BOOLEAN NOT NULL DEFAULT FALSE,
            artifact_json JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_case_mode UNIQUE (case_id, mode)
        );

        CREATE INDEX IF NOT EXISTS idx_eval_suite ON evaluation_results(suite);
        CREATE INDEX IF NOT EXISTS idx_eval_mode ON evaluation_results(mode);
        CREATE INDEX IF NOT EXISTS idx_eval_fault ON evaluation_results(ground_truth_fault);
        CREATE INDEX IF NOT EXISTS idx_eval_top1 ON evaluation_results(top1_hit);
        CREATE INDEX IF NOT EXISTS idx_eval_artifact_gin ON evaluation_results USING gin (artifact_json);
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)

    def save_artifact(self, artifact: CaseAnalysisArtifact) -> None:
        """Upserts a full diagnostic artifact into PostgreSQL."""
        eval_rec = artifact.evaluation
        case_id = artifact.case_id
        suite = case_id.split("_")[0].upper() if "_" in case_id else "UNKNOWN"
        mode = artifact.mode

        upsert_sql = """
        INSERT INTO evaluation_results (
            case_id, suite, mode,
            ground_truth_service, predicted_service, ranked_candidates,
            ground_truth_fault, predicted_category,
            top1_hit, top3_hit, mrr, fault_match, error_bucket,
            confidence, prompt_tokens, duration_ms, engine_type,
            has_artifact, artifact_json, created_at
        ) VALUES (
            %(case_id)s, %(suite)s, %(mode)s,
            %(ground_truth_service)s, %(predicted_service)s, %(ranked_candidates)s,
            %(ground_truth_fault)s, %(predicted_category)s,
            %(top1_hit)s, %(top3_hit)s, %(mrr)s, %(fault_match)s, %(error_bucket)s,
            %(confidence)s, %(prompt_tokens)s, %(duration_ms)s, %(engine_type)s,
            TRUE, %(artifact_json)s, NOW()
        )
        ON CONFLICT (case_id, mode) DO UPDATE SET
            ground_truth_service = EXCLUDED.ground_truth_service,
            predicted_service = EXCLUDED.predicted_service,
            ranked_candidates = EXCLUDED.ranked_candidates,
            ground_truth_fault = EXCLUDED.ground_truth_fault,
            predicted_category = EXCLUDED.predicted_category,
            top1_hit = EXCLUDED.top1_hit,
            top3_hit = EXCLUDED.top3_hit,
            mrr = EXCLUDED.mrr,
            fault_match = EXCLUDED.fault_match,
            error_bucket = EXCLUDED.error_bucket,
            confidence = EXCLUDED.confidence,
            prompt_tokens = EXCLUDED.prompt_tokens,
            duration_ms = EXCLUDED.duration_ms,
            engine_type = EXCLUDED.engine_type,
            has_artifact = TRUE,
            artifact_json = EXCLUDED.artifact_json,
            created_at = NOW();
        """
        artifact_dict = artifact.model_dump(mode="json")
        params = {
            "case_id": case_id,
            "suite": suite,
            "mode": mode,
            "ground_truth_service": eval_rec.ground_truth_service,
            "predicted_service": eval_rec.predicted_service,
            "ranked_candidates": eval_rec.ranked_candidates,
            "ground_truth_fault": eval_rec.ground_truth_fault,
            "predicted_category": eval_rec.predicted_category,
            "top1_hit": eval_rec.top1_hit,
            "top3_hit": eval_rec.top3_hit,
            "mrr": eval_rec.mrr,
            "fault_match": eval_rec.fault_match,
            "error_bucket": eval_rec.error_bucket,
            "confidence": eval_rec.confidence,
            "prompt_tokens": eval_rec.prompt_tokens,
            "duration_ms": eval_rec.duration_ms,
            "engine_type": artifact.engine_type,
            "artifact_json": json.dumps(artifact_dict),
        }
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(upsert_sql, params)

    def load_artifact(self, case_id: str, mode: str) -> CaseAnalysisArtifact | None:
        """Retrieves and deserializes a CaseAnalysisArtifact from PostgreSQL."""
        sql = """
        SELECT artifact_json
        FROM evaluation_results
        WHERE case_id = %s AND mode = %s AND has_artifact = TRUE
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (case_id, mode))
                row = cur.fetchone()
                if not row or not row.get("artifact_json"):
                    return None
                data = row["artifact_json"]
                if isinstance(data, str):
                    data = json.loads(data)
                return CaseAnalysisArtifact.model_validate(data)

    def get_mode_summary(self, mode: str) -> dict[str, Any] | None:
        """Runs fast in-database aggregation for overall performance and per-fault metrics."""
        sql_overall = """
        SELECT
            COUNT(*) as total_cases,
            COALESCE(AVG(CASE WHEN top1_hit THEN 1.0 ELSE 0.0 END), 0.0) as overall_top1,
            COALESCE(AVG(CASE WHEN top3_hit THEN 1.0 ELSE 0.0 END), 0.0) as overall_top3,
            COALESCE(AVG(mrr), 0.0) as overall_mrr,
            COALESCE(AVG(CASE WHEN fault_match THEN 1.0 ELSE 0.0 END), 0.0) as overall_fault_acc
        FROM evaluation_results
        WHERE mode = %s
        """

        sql_faults = """
        SELECT
            ground_truth_fault as fault,
            COUNT(*) as total,
            COALESCE(AVG(CASE WHEN top1_hit THEN 1.0 ELSE 0.0 END), 0.0) * 100 as top1_pct,
            COALESCE(AVG(CASE WHEN top3_hit THEN 1.0 ELSE 0.0 END), 0.0) * 100 as top3_pct,
            COALESCE(AVG(mrr), 0.0) as mrr
        FROM evaluation_results
        WHERE mode = %s
        GROUP BY ground_truth_fault
        ORDER BY ground_truth_fault
        """

        sql_buckets = """
        SELECT error_bucket, COUNT(*) as count
        FROM evaluation_results
        WHERE mode = %s
        GROUP BY error_bucket
        """

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql_overall, (mode,))
                ov = cur.fetchone()
                if not ov or ov["total_cases"] == 0:
                    return None

                cur.execute(sql_faults, (mode,))
                fault_rows = cur.fetchall()

                cur.execute(sql_buckets, (mode,))
                bucket_rows = cur.fetchall()

        by_fault: dict[str, Any] = {}
        for r in fault_rows:
            f = r["fault"]
            by_fault[f] = {
                "fault": f,
                "label": f.upper(),
                "total": r["total"],
                "top1_pct": round(float(r["top1_pct"]), 1),
                "top3_pct": round(float(r["top3_pct"]), 1),
                "mrr": round(float(r["mrr"]), 4),
            }

        buckets = {r["error_bucket"]: r["count"] for r in bucket_rows}

        return {
            "mode": mode,
            "total_cases": ov["total_cases"],
            "overall_top1": round(float(ov["overall_top1"]), 4),
            "overall_top3": round(float(ov["overall_top3"]), 4),
            "overall_mrr": round(float(ov["overall_mrr"]), 4),
            "overall_fault_acc": round(float(ov["overall_fault_acc"]), 4),
            "error_buckets": buckets,
            "by_fault_type": by_fault,
        }
