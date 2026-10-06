"""
Academic evaluation metrics for Root Cause Analysis (RCA).
Computes Top@1 Accuracy, Top@3 Accuracy, Mean Reciprocal Rank (MRR),
and fault category accuracy across benchmark test cases.
"""

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from src.eval.taxonomy import is_fault_category_match
from src.schemas.rca_report import RCAReport


@dataclass
class CaseScore:
    case_id: str
    ground_truth_service: str
    predicted_service: str
    ranked_candidates: List[str]
    ground_truth_fault: str
    predicted_category: str
    top1_hit: bool
    top3_hit: bool
    mrr: float
    fault_match: bool
    confidence: float
    prompt_tokens: int
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ranked_candidates"] = ";".join(self.ranked_candidates)
        return d


def score_single_case(
    case_id: str,
    ground_truth_service: str,
    ground_truth_fault: str,
    report: RCAReport,
    duration_ms: float = 0.0,
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
    )


def aggregate_scores(scores: List[CaseScore]) -> Dict[str, Any]:
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
            "by_fault_type": {},
        }

    total = len(scores)
    top1_acc = sum(1 for s in scores if s.top1_hit) / total
    top3_acc = sum(1 for s in scores if s.top3_hit) / total
    mean_mrr = sum(s.mrr for s in scores) / total
    fault_acc = sum(1 for s in scores if s.fault_match) / total

    # Group by fault type
    by_fault: Dict[str, List[CaseScore]] = defaultdict(list)
    for s in scores:
        ft = s.ground_truth_fault.upper() if s.ground_truth_fault else "UNKNOWN"
        by_fault[ft].append(s)

    fault_summary: Dict[str, Dict[str, float]] = {}
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
        "by_fault_type": fault_summary,
    }


def format_ablation_table(scores: List[CaseScore], mode_label: str) -> str:
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

    return "\n".join(lines)
