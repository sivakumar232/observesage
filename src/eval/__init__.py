"""
Evaluation package for ObservaSage benchmark accuracy scoring.
"""

from src.eval.metrics import (
    CaseScore,
    aggregate_scores,
    format_ablation_table,
    score_single_case,
)
from src.eval.taxonomy import (
    RCAEVAL_TO_OBSERVASAGE,
    is_fault_category_match,
    map_rcaeval_to_failure_category,
)

__all__ = [
    "RCAEVAL_TO_OBSERVASAGE",
    "CaseScore",
    "aggregate_scores",
    "format_ablation_table",
    "is_fault_category_match",
    "map_rcaeval_to_failure_category",
    "score_single_case",
]
