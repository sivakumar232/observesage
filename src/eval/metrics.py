"""
Academic evaluation metrics for Root Cause Analysis (RCA).
Computes Top@1 Accuracy, Top@3 Accuracy, Mean Reciprocal Rank (MRR),
and fault category accuracy across benchmark test cases.
"""

import csv
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.eval.taxonomy import is_fault_category_match
from src.schemas.artifact import classify_error_bucket
from src.schemas.rca_report import RCAReport


@dataclass
class CaseScore:
    case_id: str
    ground_truth_service: str
    predicted_service: str
    ranked_candidates: list[str]
    ground_truth_fault: str
    predicted_category: str
    top1_hit: bool
    top3_hit: bool
    mrr: float
    fault_match: bool
    confidence: float
    prompt_tokens: int
    duration_ms: float = 0.0
    engine_type: str = "LIVE_LLM"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ranked_candidates"] = ";".join(self.ranked_candidates)
        return d


def score_single_case(
    case_id: str,
    ground_truth_service: str,
    ground_truth_fault: str,
    report: RCAReport,
    duration_ms: float = 0.0,
    engine_type: str = "LIVE_LLM",
) -> CaseScore:
    """
    Evaluates an RCAReport against ground-truth labels.
    Calculates Top@1 hit, Top@3 hit, and Mean Reciprocal Rank (MRR).
    """
    gt_svc_clean = (ground_truth_service or "").strip().lower()

    # Ranked culprit candidates
    candidates = [s.strip().lower() for s in (report.culprit_services or [report.root_cause_service])]
    pred_top1 = candidates[0] if candidates else (report.root_cause_service or "").strip().lower()

    # 1. Top@1 Hit
    top1_hit = pred_top1 == gt_svc_clean

    # 2. Top@3 Hit
    top3_hit = gt_svc_clean in candidates[:3]

    # 3. Reciprocal Rank (MRR)
    mrr = 0.0
    for rank_idx, cand in enumerate(candidates, start=1):
        if cand == gt_svc_clean:
            mrr = 1.0 / rank_idx
            break

    # 4. Fault Type Match
    fault_match = is_fault_category_match(report.failure_category, ground_truth_fault)

    return CaseScore(
        case_id=case_id,
        ground_truth_service=ground_truth_service,
        predicted_service=report.root_cause_service,
        ranked_candidates=report.culprit_services or [report.root_cause_service],
        ground_truth_fault=ground_truth_fault,
        predicted_category=report.failure_category.value,
        top1_hit=top1_hit,
        top3_hit=top3_hit,
        mrr=mrr,
        fault_match=fault_match,
        confidence=report.confidence_score,
        prompt_tokens=report.prompt_tokens_used,
        duration_ms=duration_ms,
        engine_type=engine_type,
    )


def aggregate_scores(scores: list[CaseScore]) -> dict[str, Any]:
    """
    Computes overall summary statistics and breakdown by ground-truth fault type.
    """
    if not scores:
        return {
            "total_cases": 0,
            "overall_top1": 0.0,
            "overall_top3": 0.0,
            "overall_mrr": 0.0,
            "overall_fault_acc": 0.0,
            "error_buckets": {},
            "by_fault_type": {},
        }

    total = len(scores)
    top1_acc = sum(1 for s in scores if s.top1_hit) / total
    top3_acc = sum(1 for s in scores if s.top3_hit) / total
    mean_mrr = sum(s.mrr for s in scores) / total
    fault_acc = sum(1 for s in scores if s.fault_match) / total

    buckets: dict[str, int] = defaultdict(int)
    for s in scores:
        buckets[classify_error_bucket(s.top1_hit, s.top3_hit, s.fault_match)] += 1

    # Group by fault type
    by_fault: dict[str, list[CaseScore]] = defaultdict(list)
    for s in scores:
        ft = s.ground_truth_fault.upper() if s.ground_truth_fault else "UNKNOWN"
        by_fault[ft].append(s)

    fault_summary: dict[str, dict[str, float]] = {}
    for ft, group in sorted(by_fault.items()):
        n = len(group)
        fault_summary[ft] = {
            "count": n,
            "top1": sum(1 for s in group if s.top1_hit) / n,
            "top3": sum(1 for s in group if s.top3_hit) / n,
            "mrr": sum(s.mrr for s in group) / n,
            "fault_acc": sum(1 for s in group if s.fault_match) / n,
        }

    return {
        "total_cases": total,
        "overall_top1": top1_acc,
        "overall_top3": top3_acc,
        "overall_mrr": mean_mrr,
        "overall_fault_acc": fault_acc,
        "error_buckets": dict(buckets),
        "by_fault_type": fault_summary,
    }


def format_ablation_table(scores: list[CaseScore], mode_label: str) -> str:
    """Renders a GitHub-flavored Markdown table of results for research paper inclusion."""
    summary = aggregate_scores(scores)
    lines = [
        f"### ObservaSage Benchmark Results ({mode_label})",
        f"- Total Evaluated Cases: {summary['total_cases']}",
        f"- Overall Top@1 Accuracy: {summary['overall_top1']:.2%}",
        f"- Overall Top@3 Accuracy: {summary['overall_top3']:.2%}",
        f"- Overall Mean Reciprocal Rank (MRR): {summary['overall_mrr']:.4f}\n",
        "| Fault Type | Cases | Top@1 Accuracy | Top@3 Accuracy | MRR | Fault Class Acc |",
        "|---|:---:|:---:|:---:|:---:|:---:|",
    ]

    for ft, st in summary["by_fault_type"].items():
        lines.append(
            f"| **{ft}** | {st['count']} | {st['top1']:.2%} | {st['top3']:.2%} | {st['mrr']:.3f} | {st['fault_acc']:.2%} |"
        )

    lines.append(
        f"| **Overall** | **{summary['total_cases']}** | **{summary['overall_top1']:.2%}** | "
        f"**{summary['overall_top3']:.2%}** | **{summary['overall_mrr']:.3f}** | **{summary['overall_fault_acc']:.2%}** |"
    )
    if summary.get("error_buckets"):
        bucket_str = ", ".join(f"`{k}`={v}" for k, v in sorted(summary["error_buckets"].items()))
        lines.append(f"\n- Error Buckets: {bucket_str}")

    return "\n".join(lines)


def export_case_comparison_csv(
    results_by_mode: dict[str, list[CaseScore]],
    csv_path: Path,
    artifacts_root: Path | None = None,
) -> Path:
    """
    Exports a unified per-case cross-mode comparison CSV linking each case across
    all evaluated ablation modes and their persisted artifact paths.
    """
    modes = list(results_by_mode.keys())
    by_case: dict[str, dict[str, CaseScore]] = defaultdict(dict)
    for mode, scores in results_by_mode.items():
        for sc in scores:
            by_case[sc.case_id][mode] = sc

    fieldnames = ["case_id", "ground_truth_service", "ground_truth_fault"]
    for m in modes:
        slug = m.replace("-", "_")
        fieldnames.extend([f"{slug}_pred", f"{slug}_top1", f"{slug}_mrr", f"{slug}_bucket"])
        if artifacts_root is not None:
            fieldnames.append(f"{slug}_artifact")

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for cid in sorted(by_case.keys()):
            mode_map = by_case[cid]
            first = next(iter(mode_map.values()))
            row: dict[str, Any] = {
                "case_id": cid,
                "ground_truth_service": first.ground_truth_service,
                "ground_truth_fault": first.ground_truth_fault,
            }
            for m in modes:
                slug = m.replace("-", "_")
                sc = mode_map.get(m)
                if sc is not None:
                    row[f"{slug}_pred"] = sc.predicted_service
                    row[f"{slug}_top1"] = sc.top1_hit
                    row[f"{slug}_mrr"] = sc.mrr
                    row[f"{slug}_bucket"] = classify_error_bucket(sc.top1_hit, sc.top3_hit, sc.fault_match)
                    if artifacts_root is not None:
                        art = artifacts_root / "artifacts" / cid / f"{slug}.json"
                        row[f"{slug}_artifact"] = str(art) if art.exists() else ""
            writer.writerow(row)
    return csv_path
