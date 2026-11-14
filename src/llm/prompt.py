"""
Prompt construction and token budgeting for ObservaSage Gemini Root Cause Analysis.
Strictly budgets prompt tokens under 2,500 BPE tokens using exact tokenizer.
Supports 4-way ablation modes: logs-only, logs-metrics, logs-traces, fusion.
"""

import logging
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"
if not os.getenv("TIKTOKEN_CACHE_DIR") and _DEFAULT_CACHE_DIR.is_dir():
    os.environ["TIKTOKEN_CACHE_DIR"] = str(_DEFAULT_CACHE_DIR)

_logger = logging.getLogger(__name__)

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except (ImportError, OSError, ValueError) as exc:
    _logger.warning("tiktoken unavailable (%s); falling back to character-based token estimate.", exc)
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
    """Truncates text to ensure it stays strictly within max_tokens."""
    if count_bpe_tokens(text) <= max_tokens:
        return text

    suffix = "\n...[truncated for token budget]"
    suffix_tokens = count_bpe_tokens(suffix)
    safe_max = max(1, max_tokens - suffix_tokens - 2)

    if _ENC is not None:
        tokens = _ENC.encode(text)
        truncated_tokens = tokens[:safe_max]
        return _ENC.decode(truncated_tokens) + suffix

    # Fallback character truncation accounting for suffix length
    char_limit = max(20, safe_max * 3)
    return text[:char_limit] + suffix


def build_multimodal_prompt_with_breakdown(
    run_id: str,
    log_evidences: dict[str, LogEvidence],
    metric_evidence: MetricEvidence | None = None,
    trace_evidence: TraceEvidence | None = None,
    mode: Literal["logs-only", "logs-metrics", "logs-traces", "fusion"] = "fusion",
    max_total_tokens: int = 2400,
    system_prompt_overhead: int = 300,
) -> tuple[str, dict[str, int]]:
    """
    Constructs an elastic, budgeted prompt for LLM diagnosis and returns
    `(full_prompt, section_token_breakdown)` where `section_token_breakdown`
    records actual final BPE token allocations for `traces`, `metrics`, `logs`, and `total`.
    """
    effective_budget = max(400, max_total_tokens - system_prompt_overhead)
    sections: list[str] = [f"### Incident Reference ID: {run_id}"]
    breakdown: dict[str, int] = {"traces": 0, "metrics": 0, "logs": 0, "total": 0}

    include_traces = mode in ["logs-traces", "fusion"] and trace_evidence is not None
    include_metrics = mode in ["logs-metrics", "fusion"] and metric_evidence is not None

    # Base budget envelope calculation
    fixed_overhead = 120  # headers, instructions
    elastic_pool = max(300, effective_budget - fixed_overhead)

    # 1. Traces Section (includes service call topology if available)
    if include_traces and trace_evidence:
        trace_parts: list[str] = []
        if trace_evidence.service_dependency_graph:
            topo_lines = ["### Service Dependency Graph (Caller -> Callees):"]
            for caller, callees in sorted(trace_evidence.service_dependency_graph.items()):
                topo_lines.append(f"- {caller} ➔ [{', '.join(callees)}]")
            trace_parts.append("\n".join(topo_lines))

        if trace_evidence.formatted_prompt:
            trace_parts.append(trace_evidence.formatted_prompt)

        if trace_parts:
            combined_trace = "\n\n".join(trace_parts)
            needed_trace_tokens = count_bpe_tokens(combined_trace)
            dynamic_trace_cap = min(needed_trace_tokens, int(elastic_pool * 0.45))
            trace_section = truncate_to_tokens(combined_trace, max(250, dynamic_trace_cap))
            breakdown["traces"] = count_bpe_tokens(trace_section)
            sections.append(trace_section)

    # 2. Metrics Section (takes actual needed tokens or up to dynamic cap)
    if include_metrics and metric_evidence and metric_evidence.formatted_prompt:
        needed_metric_tokens = count_bpe_tokens(metric_evidence.formatted_prompt)
        dynamic_metric_cap = min(needed_metric_tokens, int(elastic_pool * 0.35))
        metric_section = truncate_to_tokens(metric_evidence.formatted_prompt, max(200, dynamic_metric_cap))
        breakdown["metrics"] = count_bpe_tokens(metric_section)
        sections.append(metric_section)

    # 3. Logs Section (dynamically absorbs all remaining budget)
    log_blocks: list[str] = []
    if log_evidences:
        for _svc, ev in sorted(log_evidences.items(), key=lambda item: len(item[1].snippets), reverse=True):
            if ev.formatted_prompt:
                log_blocks.append(ev.formatted_prompt)

    if log_blocks:
        combined_logs = "\n\n".join(log_blocks)
        current_used = count_bpe_tokens("\n\n".join(sections))
        remaining_budget = max(200, effective_budget - current_used - 80)
        budgeted_logs = truncate_to_tokens(combined_logs, remaining_budget)
        log_section = "### Extracted Log Evidence (Drain3 Novel Templates & Asymmetric Context):\n" + budgeted_logs
        breakdown["logs"] = count_bpe_tokens(log_section)
        sections.append(log_section)

    sections.append(
        "\nAnalyze the telemetry evidence above, triangulate signals across modalities using the dependency topology, "
        "and produce the structured Root Cause Analysis report."
    )

    full_prompt = "\n\n".join(sections)
    # Final safeguard check
    if count_bpe_tokens(full_prompt) > effective_budget:
        full_prompt = truncate_to_tokens(full_prompt, effective_budget)

    breakdown["total"] = count_bpe_tokens(full_prompt)
    return full_prompt, breakdown


def build_multimodal_prompt(
    run_id: str,
    log_evidences: dict[str, LogEvidence],
    metric_evidence: MetricEvidence | None = None,
    trace_evidence: TraceEvidence | None = None,
    mode: Literal["logs-only", "logs-metrics", "logs-traces", "fusion"] = "fusion",
    max_total_tokens: int = 2400,
    system_prompt_overhead: int = 300,
) -> str:
    """Backwards-compatible wrapper returning only the formatted prompt string."""
    prompt, _ = build_multimodal_prompt_with_breakdown(
        run_id=run_id,
        log_evidences=log_evidences,
        metric_evidence=metric_evidence,
        trace_evidence=trace_evidence,
        mode=mode,
        max_total_tokens=max_total_tokens,
        system_prompt_overhead=system_prompt_overhead,
    )
    return prompt


def build_log_only_prompt(run_id: str, scenario: str, log_evidences: dict[str, LogEvidence]) -> str:
    """Backward-compatible wrapper for log-only prompt."""
    return build_multimodal_prompt(run_id=run_id, log_evidences=log_evidences, mode="logs-only")
