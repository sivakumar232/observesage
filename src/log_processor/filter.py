import re
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


def is_benign_probe(line: str) -> bool:
    """
    Detects benign background health check or metric scrape probes that
    commonly interleave into container stdout and pollute stack traces.
    """
    low = line.lower()
    return any(p in low for p in [
        "/healthz", "/ready", "/live", "kube-probe", "health check 200", "prometheus scraper", "/metrics"
    ])


def extract_correlation_tag(line: str) -> Optional[str]:
    """
    Extracts trace_id, request_id, or thread identifier from structured or semi-structured log lines.
    """
    match = re.search(r'(?:trace_?id|req(?:uest)?_?id|thread|worker)[=:\s]+([a-zA-Z0-9_\-]+)', line, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    bracket_match = re.search(r'\[([a-zA-Z0-9_\-]{6,})\]', line)
    if bracket_match:
        return bracket_match.group(1).lower()
    return None


def expand_asymmetric_context(
    lines: List[str],
    target_idx: int,
    m: int = 3,
    n: int = 7,
    filter_probes: bool = False,
) -> Tuple[List[str], str, List[str]]:
    """
    Applies asymmetric context expansion around a target log line:
      - m lines before (default: 3)
      - n lines after (default: 7, where n > m as per LogSage paper)
    
    If filter_probes is True, skips interleaved health checks to prevent
    background probes from displacing relevant stack traces.
    """
    if not (0 <= target_idx < len(lines)):
        raise IndexError(f"Target index {target_idx} out of range [0, {len(lines)})")

    target_line = lines[target_idx]

    if not filter_probes:
        start_idx = max(0, target_idx - m)
        end_idx = min(len(lines), target_idx + n + 1)
        context_before = lines[start_idx:target_idx]
        context_after = lines[target_idx + 1:end_idx]
        return context_before, target_line, context_after

    # Context before: collect up to m non-probe lines
    context_before: List[str] = []
    curr = target_idx - 1
    while curr >= 0 and len(context_before) < m:
        if not is_benign_probe(lines[curr]):
            context_before.insert(0, lines[curr])
        curr -= 1

    # Context after: collect up to n non-probe lines
    context_after: List[str] = []
    curr = target_idx + 1
    while curr < len(lines) and len(context_after) < n:
        if not is_benign_probe(lines[curr]):
            context_after.append(lines[curr])
        curr += 1

    return context_before, target_line, context_after

