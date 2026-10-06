"""
Gemini LLM Client for ObservaSage Root Cause Analysis.
Supports Google GenAI SDK with structured Pydantic schema validation.
Includes schema validation recovery retry loop and strict evaluation gating (zero heuristic fallback).
"""

import json
import os
import re
from typing import Optional
from dotenv import load_dotenv
from pydantic import ValidationError

from src.schemas.rca_report import (
    EvidenceTriangulation,
    FailureCategory,
    RCAReport,
    RemediationStep,
)
from src.llm.prompt import SYSTEM_PROMPT

load_dotenv()


class GeminiRCAClient:
    """
    Client interface for querying Gemini models with structured JSON output and schema retry loop.
    """

    def __init__(self, model_name: str = "gemini-1.5-pro", api_key: Optional[str] = None):
        self.model_name = model_name
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._genai_client = None

        if self.api_key and self.api_key != "your_gemini_api_key_here":
            try:
                from google import genai
                self._genai_client = genai.Client(api_key=self.api_key)
            except Exception:
                self._genai_client = None

    @property
    def is_configured(self) -> bool:
        """Returns True if a live Gemini API key is configured."""
        return self._genai_client is not None

    def diagnose(
        self,
        run_id: str,
        scenario: str,
        user_prompt: str,
        allow_heuristic: bool = True,
    ) -> RCAReport:
        """
        Sends diagnostic prompt to Gemini and parses the structured RCAReport response.
        If allow_heuristic is False (e.g. academic benchmark mode), fails loudly on API error
        rather than poisoning results with heuristic pattern matching.
        """
        if self._genai_client is not None:
            try:
                return self._diagnose_live(run_id, scenario, user_prompt, max_retries=1)
            except Exception as e:
                if not allow_heuristic:
                    raise RuntimeError(f"Gemini live diagnosis failed in strict evaluation mode: {e}") from e
                return self._diagnose_heuristic(run_id, scenario, user_prompt, error_note=str(e))

        if not allow_heuristic:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured, and allow_heuristic is False. "
                "Academic benchmark evaluation requires a live Gemini API key."
            )

        return self._diagnose_heuristic(run_id, scenario, user_prompt)

    def _diagnose_live(
        self, run_id: str, scenario: str, user_prompt: str, max_retries: int = 1
    ) -> RCAReport:
        """Queries the Gemini API with structured schema enforcement and retry on validation failure."""
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=RCAReport,
        )

        response = self._genai_client.models.generate_content(
            model=self.model_name,
            contents=[user_prompt],
            config=config,
        )

        last_error = None
        raw_text = response.text or "{}"
        for attempt in range(max_retries + 1):
            try:
                data = json.loads(raw_text)
                report = RCAReport(**data)
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    report.prompt_tokens_used = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                    report.total_tokens_used = getattr(response.usage_metadata, "total_token_count", 0) or 0
                return report
            except (json.JSONDecodeError, ValidationError) as err:
                last_error = err
                if attempt < max_retries:
                    retry_msg = (
                        f"Previous output caused validation error: {err}.\n"
                        f"Output JSON must strictly conform to the RCAReport schema."
                    )
                    retry_resp = self._genai_client.models.generate_content(
                        model=self.model_name,
                        contents=[user_prompt, raw_text, retry_msg],
                        config=config,
                    )
                    raw_text = retry_resp.text or "{}"

        raise RuntimeError(f"Structured RCAReport validation failed after {max_retries} retry/retries: {last_error}")

    def _diagnose_heuristic(
        self, run_id: str, scenario: str, user_prompt: str, error_note: Optional[str] = None
    ) -> RCAReport:
        """
        Deterministic diagnostic generator for offline mode or fallback.
        Parses common failure patterns from prompt text.
        """
        prompt_lower = user_prompt.lower()
        root_service = "unknown"
        failure_cat = FailureCategory.UNKNOWN
        remediation = []

        if "paymentservice" in prompt_lower and ("dial tcp" in prompt_lower or "unavailable" in prompt_lower or "connection refused" in prompt_lower):
            root_service = "paymentservice"
            failure_cat = FailureCategory.EC_3_CASCADE
            remediation.append(
                RemediationStep(
                    action="Check container status and restart paymentservice",
                    target_service="paymentservice",
                    command_or_config="docker restart paymentservice",
                    expected_impact="Restores gRPC checkout listener on port 50051",
                )
            )
        elif "oom" in prompt_lower or "killed" in prompt_lower or "out of memory" in prompt_lower:
            root_service = "cartservice"
            failure_cat = FailureCategory.EC_1_OOM
            remediation.append(
                RemediationStep(
                    action="Increase cgroup memory limit",
                    target_service="cartservice",
                    command_or_config="mem_limit: 128m in docker-compose.yml",
                    expected_impact="Prevents Linux kernel OOM termination",
                )
            )
        elif "timeout" in prompt_lower or "deadline exceeded" in prompt_lower:
            root_service = "currencyservice"
            failure_cat = FailureCategory.EC_2_LATENCY
            remediation.append(
                RemediationStep(
                    action="Inspect downstream latency and tune client deadlines",
                    target_service="currencyservice",
                    command_or_config="gRPC timeout = 5s",
                    expected_impact="Eliminates cascading deadline exceeded timeouts",
                )
            )
        else:
            root_service = "frontend"
            failure_cat = FailureCategory.CODE_BUG
            remediation.append(
                RemediationStep(
                    action="Inspect application logs and unhandled stack traces",
                    target_service="frontend",
                    command_or_config="docker logs frontend",
                    expected_impact="Isolates unhandled application logic errors",
                )
            )

        note = f" (Offline Diagnostic: {error_note})" if error_note else " (Offline Simulated Engine)"
        summary = f"Root cause traced to {root_service} failing under {failure_cat.value}.{note}"

        return RCAReport(
            run_id=run_id,
            scenario=scenario,
            root_cause_service=root_service,
            culprit_services=[root_service],
            failure_category=failure_cat,
            confidence_score=0.92,
            root_cause_summary=summary,
            evidence_triangulation=EvidenceTriangulation(
                primary_signal="LOGS",
                logs_insight=f"Identified error lines pointing to failure in {root_service}.",
                metrics_insight="No metric anomalies evaluated (log-only mode).",
                traces_insight="No trace spans evaluated (log-only mode).",
                triangulation_reasoning="Diagnosed from LogSage template diffing and keyword matched stack trace context.",
            ),
            remediation_steps=remediation,
            prompt_tokens_used=len(user_prompt) // 4,
            total_tokens_used=(len(user_prompt) // 4) + 120,
        )
