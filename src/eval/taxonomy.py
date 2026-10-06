"""
Bidirectional fault taxonomy mapping between RCAEval benchmark labels
and ObservaSage FailureCategory enum.
"""

from typing import Dict, Optional
from src.schemas.rca_report import FailureCategory

# Standard academic taxonomy mapping
RCAEVAL_TO_OBSERVASAGE: Dict[str, FailureCategory] = {
    "MEM": FailureCategory.EC_1_OOM,
    "MEMORY": FailureCategory.EC_1_OOM,
    "OOM": FailureCategory.EC_1_OOM,
    "DELAY": FailureCategory.EC_2_LATENCY,
    "NET_DELAY": FailureCategory.EC_2_LATENCY,
    "LOSS": FailureCategory.EC_2_LATENCY,
    "NET_LOSS": FailureCategory.EC_2_LATENCY,
    "CORRUPT": FailureCategory.EC_4_SILENT,
    "CPU": FailureCategory.EC_2_LATENCY,
    "DISK": FailureCategory.EC_1_OOM,
    "SOCKET": FailureCategory.EC_5_INFRA,
    "PARTITION": FailureCategory.EC_5_INFRA,
    "CRASH": FailureCategory.EC_3_CASCADE,
}


def map_rcaeval_to_failure_category(rcaeval_fault: str) -> FailureCategory:
    """
    Maps an RCAEval fault label (e.g. 'MEM', 'DELAY') to an ObservaSage FailureCategory.
    """
    cleaned = (rcaeval_fault or "").strip().upper()
    return RCAEVAL_TO_OBSERVASAGE.get(cleaned, FailureCategory.UNKNOWN)


def is_fault_category_match(predicted: FailureCategory, ground_truth_raw: str) -> bool:
    """
    Determines whether a predicted FailureCategory matches an RCAEval fault label.
    """
    mapped_gt = map_rcaeval_to_failure_category(ground_truth_raw)
    if mapped_gt == FailureCategory.UNKNOWN:
        return False
    return predicted == mapped_gt
