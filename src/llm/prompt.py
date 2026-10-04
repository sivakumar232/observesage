"""
Prompt construction for ObservaSage Gemini Root Cause Analysis.
Strictly budgets prompt tokens under 2,500 tokens.
"""

from typing import Dict, Optional
from src.schemas.evidence import LogEvidence, MetricEvidence, TraceEvidence


SYSTEM_PROMPT = """You are ObservaSage, an expert AI Root Cause Analysis (RCA) diagnostic engineer for microservices and CI/CD pipelines.
Your task is to analyze the provided multi-signal telemetry (Logs, Metrics, Traces) from a failed pipeline run and determine the exact technical root cause.

Diagnostic Rules:
1. Identify the guilty microservice (the actual root cause, not just upstream services that crashed as a consequence).
2. Classify the failure into exactly ONE of the following categories:
   - "EC-1: OOM / Resource Exhaustion" (Memory limits breached, high memory slope, process killed)
   - "EC-2: Flaky / Intermittent Latency" (Network packet loss, high latency, CPU throttling)
   - "EC-3: Cascading Service Crash" (A dependency died, causing callers to fail with connection refused or timeouts)
   - "EC-4: Silent Data Corruption" (No fatal error logs, but wrong data returned, e.g. empty cart or corrupted total)
   - "EC-5: Infrastructure Drift / Network Partition" (Host unreachable, network disconnect, DNS failure)
   - "CODE_BUG: Application Exception / Logic Error" (Unhandled application crash, null pointer, syntax error)
   - "UNKNOWN: Unclassified Failure"
3. Assign a realistic confidence score between 0.0 and 1.0.
4. Triangulate the evidence: explain what each signal (Logs, Metrics, Traces) proved.
5. Provide actionable remediation steps to fix the issue.

You MUST respond strictly in the requested JSON schema.
"""


def build_log_only_prompt(run_id: str, scenario: str, log_evidences: Dict[str, LogEvidence]) -> str:
    """
    Constructs a concise, formatted prompt for LogSage log-only baseline analysis.
    """
    sections = [
        f"### Target Run: {run_id}",
        f"### Scenario: {scenario}",
        "### Extracted Log Evidence (Preprocessed with Drain3 & LogSage Asymmetric Context):",
    ]

    for service, ev in log_evidences.items():
        if ev.formatted_prompt:
            sections.append(ev.formatted_prompt)

    sections.append("\nAnalyze the extracted logs above and produce the structured RCA diagnosis.")
    return "\n\n".join(sections)
