"""
Prompt construction and token budgeting for ObservaSage Gemini Root Cause Analysis.
Strictly budgets prompt tokens under 2,500 BPE tokens using exact tokenizer.
Supports 4-way ablation modes: logs-only, logs-metrics, logs-traces, fusion.
"""

from typing import Dict, List, Literal, Optional

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None

from src.schemas.evidence import LogEvidence, MetricEvidence, TraceEvidence


SYSTEM_PROMPT = """You are ObservaSage, an expert AI Root Cause Analysis (RCA) diagnostic engineer for microservices and distributed systems.
Your task is to analyze the provided multi-signal telemetry (Logs, Metrics, Traces) from an incident and determine the exact technical root cause.

Diagnostic Rules:
1. Identify the guilty microservice (the actual root cause, not upstream callers that failed as a consequence).
2. Rank the top suspected culprit services in `remediation_steps` or summary if ambiguous.
3. Classify the failure into exactly ONE of the following categories:
   - "EC-1: OOM / Resource Exhaustion" (Memory limits breached, high memory slope, process killed)
   - "EC-2: Flaky / Intermittent Latency" (Network packet loss, high latency, CPU throttling)
   - "EC-3: Cascading Service Crash" (A dependency died, causing callers to fail with connection refused or timeouts)
   - "EC-4: Silent Data Corruption" (No fatal error logs, but wrong data returned)
   - "EC-5: Infrastructure Drift / Network Partition" (Host unreachable, network disconnect, DNS failure)
   - "CODE_BUG: Application Exception / Logic Error" (Unhandled application crash, null pointer, syntax error)
   - "UNKNOWN: Unclassified Failure"
4. Assign a realistic confidence score between 0.0 and 1.0.
5. Triangulate the evidence: explain what each signal (Logs, Metrics, Traces) proved.
6. Provide actionable remediation steps to fix the issue.

You MUST respond strictly in the requested JSON schema.
"""


def count_bpe_tokens(text: str) -> int:
    """Exact BPE token count using cl100k_base encoding, with fallback."""
    if _ENC is not None:
        return len(_ENC.encode(text))
    # Fallback to conservative estimate for technical telemetry (~3 chars per token)
    return max(1, len(text) // 3)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncates text to ensure it stays within max_tokens."""
    if count_bpe_tokens(text) <= max_tokens:
        return text

    if _ENC is not None:
        tokens = _ENC.encode(text)
        truncated_tokens = tokens[: max_tokens - 10]
        return _ENC.decode(truncated_tokens) + "\n...[truncated for token budget]"

    # Fallback character truncation
    char_limit = max(50, max_tokens * 3)
    return text[:char_limit] + "\n...[truncated for token budget]"


def build_multimodal_prompt(
    run_id: str,
    log_evidences: Dict[str, LogEvidence],
    metric_evidence: Optional[MetricEvidence] = None,
    trace_evidence: Optional[TraceEvidence] = None,
    mode: Literal["logs-only", "logs-metrics", "logs-traces", "fusion"] = "fusion",
    max_total_tokens: int = 2400,
) -> str:
    """
    Constructs a budgeted, blind prompt for LLM diagnosis.
    Guarantees no scenario ground-truth leakage and strictly caps total tokens.
    """
    sections: List[str] = [f"### Incident Reference ID: {run_id}"]

    # 1. Traces Section (allocated ~400 tokens)
    include_traces = mode in ["logs-traces", "fusion"] and trace_evidence is not None
    if include_traces and trace_evidence and trace_evidence.formatted_prompt:
        trace_str = truncate_to_tokens(trace_evidence.formatted_prompt, 400)
        sections.append(trace_str)

    # 2. Metrics Section (allocated ~400 tokens)
    include_metrics = mode in ["logs-metrics", "fusion"] and metric_evidence is not None
    if include_metrics and metric_evidence and metric_evidence.formatted_prompt:
        metric_str = truncate_to_tokens(metric_evidence.formatted_prompt, 400)
        sections.append(metric_str)

    # 3. Logs Section (allocated remainder, up to 1300 tokens)
    log_blocks: List[str] = []
    if log_evidences:
        for svc, ev in sorted(log_evidences.items(), key=lambda item: len(item[1].snippets), reverse=True):
            if ev.formatted_prompt:
                log_blocks.append(ev.formatted_prompt)

    if log_blocks:
        combined_logs = "\n\n".join(log_blocks)
        current_len = count_bpe_tokens("\n\n".join(sections))
        remaining_budget = max(200, max_total_tokens - current_len - 100)
        budgeted_logs = truncate_to_tokens(combined_logs, remaining_budget)
        sections.append("### Extracted Log Evidence (Drain3 Novel Templates & Asymmetric Context):\n" + budgeted_logs)

    sections.append(
        "\nAnalyze the telemetry evidence above, triangulate signals across modalities, "
        "and produce the structured Root Cause Analysis report."
    )

    full_prompt = "\n\n".join(sections)
    # Final safeguard check
    if count_bpe_tokens(full_prompt) > max_total_tokens:
        full_prompt = truncate_to_tokens(full_prompt, max_total_tokens)

    return full_prompt


def build_log_only_prompt(run_id: str, scenario: str, log_evidences: Dict[str, LogEvidence]) -> str:
    """Backward-compatible wrapper for log-only prompt."""
    return build_multimodal_prompt(run_id=run_id, log_evidences=log_evidences, mode="logs-only")
