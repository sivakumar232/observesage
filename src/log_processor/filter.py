"""
LogSage keyword filtering and asymmetric context expansion.
Strict adherence to LogSage paper (arXiv:2506.03691).
"""

from typing import List, Optional, Tuple


# Exact keywords defined in LogSage paper
LOGSAGE_KEYWORDS: tuple[str, ...] = (
    "fatal",
    "fail",
    "panic",
    "error",
    "exit",
    "kill",
    "no such file",
    "err:",
    "err!",
    "failures:",
    "exception",
    "cannot",
)


def matches_keyword(line: str) -> Optional[str]:
    """
    Checks if a line contains any of the LogSage keywords (case-insensitive).
    Returns the first matched keyword, or None.
    """
    line_lower = line.lower()
    for kw in LOGSAGE_KEYWORDS:
        if kw in line_lower:
            return kw
    return None


def expand_asymmetric_context(
    lines: List[str],
    target_idx: int,
    m: int = 3,
    n: int = 7,
) -> Tuple[List[str], str, List[str]]:
    """
    Applies asymmetric context expansion around a target log line:
      - m lines before (default: 3)
      - n lines after (default: 7, where n > m as per LogSage paper)
    
    Returns (context_before, target_line, context_after).
    """
    if not (0 <= target_idx < len(lines)):
        raise IndexError(f"Target index {target_idx} out of range [0, {len(lines)})")

    start_idx = max(0, target_idx - m)
    end_idx = min(len(lines), target_idx + n + 1)

    context_before = lines[start_idx:target_idx]
    target_line = lines[target_idx]
    context_after = lines[target_idx + 1:end_idx]

    return context_before, target_line, context_after
