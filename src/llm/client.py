"""
Gemini LLM Client for ObservaSage Root Cause Analysis.
Supports Google GenAI SDK with structured Pydantic schema validation.
Includes schema validation recovery retry loop and strict evaluation gating (zero heuristic fallback).
"""

import json
import logging
import os
import time
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError

from src.llm.prompt import SYSTEM_PROMPT
from src.schemas.rca_report import (
    RCAReport,
)

load_dotenv()

_logger = logging.getLogger(__name__)


class GeminiRCAClient:
    """
    Client interface for querying Gemini models with structured JSON output and schema retry loop.
    """

    def __init__(
        self,
        model_name: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        max_api_retries: int = 3,
    ):
        self.model_name = model_name or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.timeout_seconds = timeout_seconds
        self.max_api_retries = max_api_retries
        self._genai_client = None

        if self.api_key and self.api_key != "your_gemini_api_key_here":
            try:
                from google import genai
                from google.genai import types

                http_opts = types.HttpOptions(timeout=int(self.timeout_seconds * 1000))
                self._genai_client = genai.Client(api_key=self.api_key, http_options=http_opts)
            except (ImportError, ValueError, RuntimeError) as exc:
                _logger.warning("Failed to initialize Google GenAI client: %s", exc)
                self._genai_client = None

    @property
    def is_configured(self) -> bool:
        """Returns True if a live Gemini API key is configured."""
        return self._genai_client is not None

    def _generate_with_backoff(self, contents: list[str], config: Any) -> Any:
        """Calls Gemini generate_content with exponential backoff on transient/rate-limit errors."""
        from google.genai.errors import APIError

        assert self._genai_client is not None
        last_exc: Exception | None = None
        for attempt in range(self.max_api_retries + 1):
            try:
                return self._genai_client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
            except (APIError, TimeoutError, OSError, RuntimeError) as exc:
                last_exc = exc
                err_str = str(exc).lower()
                is_transient = any(
                    k in err_str for k in ("429", "rate", "quota", "503", "timeout", "unavailable", "overloaded")
                )
                if not is_transient or attempt >= self.max_api_retries:
                    raise
                delay = 2.0 * (2.0**attempt)
                _logger.warning("Gemini API transient error (%s); retrying in %.1fs...", exc, delay)
                time.sleep(delay)
        raise RuntimeError(f"Gemini API call failed: {last_exc}")

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

        response = self._generate_with_backoff(contents=[rag_prompt], config=config)

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
                    retry_resp = self._generate_with_backoff(
                        contents=[rag_prompt, raw_text, retry_msg],
                        config=config,
                    )
                    raw_text = retry_resp.text or "{}"

        raise RuntimeError(f"Structured RCAReport validation failed after {max_retries} retry/retries: {last_error}")
