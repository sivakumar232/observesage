"""
Gemini LLM Client for ObservaSage Root Cause Analysis.
Supports Google GenAI SDK with structured Pydantic schema validation.
Includes schema validation recovery retry loop and strict evaluation gating (zero heuristic fallback).
"""

import json
import os
import re
from typing import Optional, List
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
        rag_prompt: str,
        max_retries: int = 1,
    ) -> RCAReport:
        """
        Executes generative Root Cause Analysis using the retrieved Telemetry-RAG prompt context.
        Strictly requires an active Gemini API client; zero deterministic heuristic fallback.
        """
        if self._genai_client is None:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured. Pure Telemetry-RAG diagnosis requires an active "
                "Gemini API key to perform multi-modal root cause analysis. "
                "Please configure GEMINI_API_KEY in your .env file or environment variables."
            )

        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=RCAReport,
        )

        response = self._genai_client.models.generate_content(
            model=self.model_name,
            contents=[rag_prompt],
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
                        contents=[rag_prompt, raw_text, retry_msg],
                        config=config,
                    )
                    raw_text = retry_resp.text or "{}"

        raise RuntimeError(f"Structured RCAReport validation failed after {max_retries} retry/retries: {last_error}")
