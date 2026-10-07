"""
Distributed trace processing module for ObservaSage.
"""

from src.trace_processor.processor import TraceProcessor, extract_service_from_span

__all__ = ["TraceProcessor", "extract_service_from_span"]
