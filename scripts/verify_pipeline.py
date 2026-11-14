#!/usr/bin/env python3
"""
End-to-end integrity verifier for ObservaSage.

Runs a set of independent, falsifiable checks over one live case conversion and
over every persisted CaseAnalysisArtifact. It does not re-run the diagnosis; it
verifies that the data, the evidence, the prompt, the persisted evaluation and
the CSV summaries are mutually consistent.

Usage:
    python scripts/verify_pipeline.py
    python scripts/verify_pipeline.py --case re2ob_checkoutservice_cpu_1
    python scripts/verify_pipeline.py --skip-artifacts
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from scripts.rcaeval_adapter import RCAEvalAdapter
from src.llm.prompt import count_bpe_tokens
from src.metrics_processor.processor import extract_service_from_metric
from src.pipeline.orchestrator import ObservaSagePipeline
from src.rag import TelemetryRAGRetriever

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def parse_log_ts(line: str) -> float | None:
    m = re.match(r"\[([^\]]+)\]", line)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def verify_case(case_id: str, cases_root: Path) -> None:
    case_dir = cases_root / case_id
    if not case_dir.is_dir():
        check("case exists", False, str(case_dir))
        return

    adapter = RCAEvalAdapter()
    snapshot, gt = adapter.convert_case(case_dir)
    inject = snapshot.inject_time
    assert inject is not None

    print(f"\n=== Case {case_id} ===")
    print(
        f"GT: service={gt.get('ground_truth_service')} fault={gt.get('ground_truth_fault_type')} "
        f"inject={inject}"
    )

    # ---------------------------------------------------------------- temporal
    base_metric_ts = [p["timestamp"] for s in snapshot.baseline_metrics for p in s.data]
    inc_metric_ts = [p["timestamp"] for s in snapshot.metrics for p in s.data]
    if base_metric_ts and inc_metric_ts:
        check(
            "metrics baseline strictly before injection",
            max(base_metric_ts) < inject <= min(inc_metric_ts),
            f"max_base={max(base_metric_ts):.3f} min_inc={min(inc_metric_ts):.3f} inject={inject:.3f}",
        )

    base_trace_ts = [s.startTime / 1e6 for t in snapshot.baseline.traces for s in t.spans]
    inc_trace_ts = [s.startTime / 1e6 for t in snapshot.traces for s in t.spans]
    if base_trace_ts and inc_trace_ts:
        check(
            "traces baseline strictly before injection",
            max(base_trace_ts) < inject <= min(inc_trace_ts),
            f"max_base={max(base_trace_ts):.3f} min_inc={min(inc_trace_ts):.3f}",
        )

    base_log_ts = [
        ts for lines in snapshot.baseline.logs.values() for line in lines if (ts := parse_log_ts(line))
    ]
    inc_log_ts = [
        ts for lines in snapshot.logs.values() for line in lines if (ts := parse_log_ts(line))
    ]
    if base_log_ts and inc_log_ts:
        check(
            "logs baseline strictly before injection",
            max(base_log_ts) < inject <= min(inc_log_ts),
            f"max_base={max(base_log_ts):.3f} min_inc={min(inc_log_ts):.3f}",
        )

    # ---------------------------------------------------------------- traces
    orphan = 0
    total = 0
    complete = 0
    for trace in snapshot.traces:
        ids = {s.spanID for s in trace.spans}
        roots = 0
        for span in trace.spans:
            total += 1
            parent = None
            for ref in span.references:
                if ref.get("refType") in ("CHILD_OF", "FOLLOWS_FROM"):
                    parent = ref.get("spanID")
                    break
            if parent and parent not in ids:
                orphan += 1
            if not parent:
                roots += 1
        if roots == 1:
            complete += 1
    rate = (orphan / total * 100.0) if total else 0.0
    check(
        "trace DAG has no orphaned spans",
        orphan == 0,
        f"{orphan}/{total} orphaned ({rate:.1f}%), {complete}/{len(snapshot.traces)} traces have exactly one root",
    )
    check(
        "at least one incident trace",
        len(snapshot.traces) > 0,
        f"{len(snapshot.traces)} traces / {total} spans",
    )

    # service-name alignment across modalities
    trace_services = {
        str(tag.get("value"))
        for t in snapshot.traces
        for s in t.spans
        for tag in s.tags
        if tag.get("key") == "service.name"
    }
    metric_services = {extract_service_from_metric(m.metric_name, m.query) for m in snapshot.metrics}
    log_services = set(snapshot.logs)
    overlap = trace_services & (metric_services | log_services)
    check(
        "trace services overlap metric/log service names",
        len(overlap) > 0,
        f"trace_only={sorted(trace_services - metric_services - log_services)}",
    )

    # ---------------------------------------------------------------- retrieval
    ctx = TelemetryRAGRetriever().retrieve(snapshot, mode="fusion")
    prompt = ctx.rag_prompt

    first_line = prompt.splitlines()[0] if prompt else ""
    check(
        "prompt incident id is opaque",
        case_id not in first_line and "INC-" in first_line,
        first_line,
    )
    check(
        "run/case id does not appear anywhere in prompt",
        case_id not in prompt,
        "ground-truth-bearing case id leaked into prompt" if case_id in prompt else "clean",
    )
    check(
        "prompt token accounting matches tokenizer",
        count_bpe_tokens(prompt) == ctx.token_count,
        f"stored={ctx.token_count} recounted={count_bpe_tokens(prompt)}",
    )
    check(
        "prompt under 2500 BPE tokens",
        ctx.token_count < 2500,
        f"{ctx.token_count} tokens",
    )
    check(
        "ranked candidates are non-empty",
        bool(ctx.candidate_services),
        f"{ctx.candidate_services[:5]}",
    )
    check(
        "confidence within [0, 1]",
        0.0 <= ctx.confidence <= 1.0,
        f"{ctx.confidence}",
    )

    if ctx.metric_evidence and ctx.metric_evidence.alerts:
        bad = []
        for a in ctx.metric_evidence.alerts:
            if a.baseline_std:
                z = (a.current_value - a.baseline_mean) / a.baseline_std
                if abs(z - a.z_score) > 1e-6:
                    bad.append((a.service, a.metric_name, a.z_score, z))
        check(
            "metric z-scores reproducible from stored stats",
            not bad,
            f"{len(bad)} mismatches" if bad else f"{len(ctx.metric_evidence.alerts)} alerts verified",
        )

    print(
        "Evidence: "
        f"logs={len(ctx.log_evidences)}svc/{sum(len(e.snippets) for e in ctx.log_evidences.values())}snips, "
        f"alerts={len(ctx.metric_evidence.alerts) if ctx.metric_evidence else 0}, "
        f"traces={ctx.trace_evidence.total_traces if ctx.trace_evidence else 0}/"
        f"{ctx.trace_evidence.error_spans_count if ctx.trace_evidence else 0}err/"
        f"{ctx.trace_evidence.latency_bottlenecks_count if ctx.trace_evidence else 0}bn, "
        f"top={ctx.candidate_services[0] if ctx.candidate_services else None}"
    )


def verify_artifacts(results_dir: Path) -> None:
    print("\n=== Persisted artifacts vs CSV ===")
    artifacts = sorted((results_dir / "artifacts").rglob("*.json"))
    check("artifacts exist", bool(artifacts), f"{len(artifacts)} files")

    by_mode: dict[str, dict[str, dict]] = {}
    for mode in ("logs-only", "logs-metrics", "logs-traces", "fusion"):
        path = results_dir / f"ablation_{mode.replace('-', '_')}.csv"
        if not path.exists():
            continue
        with open(path, newline="", encoding="utf-8") as f:
            by_mode[mode] = {row["case_id"]: row for row in csv.DictReader(f)}

    mismatched: list[str] = []
    missing_csv: list[str] = []
    leaked: list[str] = []
    old_engine: Counter[str] = Counter()
    confidence_delta: list[float] = []

    for path in artifacts:
        try:
            art = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            mismatched.append(f"{path.name}: unreadable ({exc})")
            continue

        case_id = art.get("case_id", "")
        mode = art.get("mode", path.stem.replace("_", "-"))
        ev = art.get("evaluation", {})
        old_engine[art.get("engine_type", "?")] += 1

        row = by_mode.get(mode, {}).get(case_id)
        if row is None:
            missing_csv.append(f"{mode}/{case_id}")
        else:
            for key, cast in (("top1_hit", lambda v: v == "True"), ("top3_hit", lambda v: v == "True")):
                if bool(ev.get(key)) != cast(row.get(key, "")):
                    mismatched.append(f"{mode}/{case_id}:{key}")
            try:
                if abs(float(ev.get("mrr", 0)) - float(row.get("mrr", 0))) > 1e-6:
                    mismatched.append(f"{mode}/{case_id}:mrr")
            except ValueError:
                mismatched.append(f"{mode}/{case_id}:mrr")

        prompt = art.get("prompt", {}).get("prompt_preview", "")
        if case_id and case_id in prompt:
            leaked.append(f"{mode}/{case_id}")

    check(
        "artifact evaluation matches CSV row",
        not mismatched,
        f"{len(mismatched)} mismatches" + (f" e.g. {mismatched[:3]}" if mismatched else ""),
    )
    check(
        "every artifact has a CSV row",
        not missing_csv,
        f"{len(missing_csv)} artifacts without CSV row" + (f" e.g. {missing_csv[:3]}" if missing_csv else ""),
    )
    check(
        "no artifact prompt leaks the case id",
        not leaked,
        f"{len(leaked)}/{len(artifacts)} artifacts leak" + (f" e.g. {leaked[:2]}" if leaked else ""),
    )
    print(f"Artifact engine types: {dict(old_engine)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="ObservaSage end-to-end integrity verifier")
    parser.add_argument("--case", default="re2ob_checkoutservice_cpu_1")
    parser.add_argument("--cases-root", type=Path, default=Path("data/ground_truth/rcaeval"))
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--skip-artifacts", action="store_true")
    args = parser.parse_args()

    try:
        verify_case(args.case, args.cases_root)
    except Exception as exc:  # noqa: BLE001
        check("case verification completed", False, repr(exc))

    if not args.skip_artifacts:
        try:
            verify_artifacts(args.results)
        except Exception as exc:  # noqa: BLE001
            check("artifact verification completed", False, repr(exc))

    failures = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED:")
        for name, _, detail in failures:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
