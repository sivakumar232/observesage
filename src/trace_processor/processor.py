"""
Distributed Trace Processor for ObservaSage.
Reconstructs span Directed Acyclic Graphs (DAGs), calculates self-durations,
and pinpoints root cause culprit services using DFS leaf traversal and bottleneck analysis.
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from src.schemas.evidence import TraceEvidence, TraceSpanEvidence
from src.schemas.telemetry import Trace, TraceSpan


def extract_service_from_span(span: TraceSpan) -> str:
    """Extracts service name from tags or processID."""
    for tag in span.tags:
        if tag.get("key") in ["service.name", "service", "app", "component"]:
            return str(tag.get("value", "")).strip()
    return "unknown_service"


def extract_error_info(span: TraceSpan) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
    """
    Checks if a span has error status and extracts status code, error code, and error message.
    """
    is_error = False
    status_code: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    for tag in span.tags:
        k = tag.get("key", "").lower()
        v = tag.get("value")
        if k in ["error", "otel.status_code"] and (v is True or str(v).lower() in ["true", "error"]):
            is_error = True
        elif k in ["http.status_code", "status_code", "grpc.status_code"]:
            try:
                code_val = int(v)
                status_code = code_val
                if code_val >= 500 or code_val in [2, 4, 14]:  # gRPC UNKNOWN, DEADLINE_EXCEEDED, UNAVAILABLE
                    is_error = True
            except (ValueError, TypeError):
                pass
        elif k in ["error.message", "exception.message", "message"]:
            error_message = str(v)
        elif k in ["error.code", "exception.type", "grpc.code"]:
            error_code = str(v)

    # Check span logs for exceptions
    for log_entry in span.logs:
        for field in log_entry.get("fields", []):
            if field.get("key") in ["message", "error.message"]:
                error_message = str(field.get("value", ""))
                is_error = True
            elif field.get("key") in ["event"] and str(field.get("value")).lower() == "error":
                is_error = True

    return is_error, status_code, error_code, error_message


class TraceProcessor:
    """
    Analyzes distributed traces, reconstructs call DAGs, and performs leaf culprit extraction.
    """

    def __init__(self, max_traces_to_format: int = 5, max_trace_tokens: int = 500):
        self.max_traces_to_format = max_traces_to_format
        self.max_trace_tokens = max_trace_tokens

    def process(
        self,
        incident_traces: List[Trace],
        baseline_traces: Optional[List[Trace]] = None,
    ) -> TraceEvidence:
        """
        Reconstructs call trees, identifies leaf error culprits, and formats trace evidence.
        """
        if not incident_traces:
            return TraceEvidence(total_traces=0, total_spans=0)

        total_traces = len(incident_traces)
        total_spans = sum(len(t.spans) for t in incident_traces)

        # Baseline durations by (service, operation)
        baseline_durations: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        if baseline_traces:
            for trace in baseline_traces:
                for span in trace.spans:
                    svc = extract_service_from_span(span)
                    dur_ms = span.duration / 1000.0 if span.duration > 0 else 0.0
                    baseline_durations[(svc, span.operationName)].append(dur_ms)

        # Evaluate incident traces
        error_spans_count = 0
        culprit_candidates: Dict[str, int] = defaultdict(int)
        best_culprit_span: Optional[TraceSpanEvidence] = None
        root_services: Set[str] = set()

        failing_trace_hierarchies: List[str] = []

        for trace in incident_traces:
            if not trace.spans:
                continue

            # Build adjacency and span map
            span_map: Dict[str, TraceSpan] = {s.spanID: s for s in trace.spans}
            children_map: Dict[str, List[str]] = defaultdict(list)
            parents_map: Dict[str, Optional[str]] = {}

            for span in trace.spans:
                parent_id = None
                for ref in span.references:
                    if ref.get("refType") in ["CHILD_OF", "FOLLOWS_FROM"]:
                        parent_id = ref.get("spanID")
                        break
                parents_map[span.spanID] = parent_id
                if parent_id and parent_id in span_map:
                    children_map[parent_id].append(span.spanID)

            # Find root span(s)
            root_spans = [s for s in trace.spans if not parents_map.get(s.spanID) or parents_map[s.spanID] not in span_map]
            if not root_spans:
                root_spans = [trace.spans[0]]

            for r in root_spans:
                root_services.add(extract_service_from_span(r))

            # Compute depths and self-durations via BFS/DFS
            depths: Dict[str, int] = {}
            self_durations_ms: Dict[str, float] = {}

            def traverse_depth(span_id: str, current_depth: int):
                depths[span_id] = current_depth
                for child_id in children_map.get(span_id, []):
                    traverse_depth(child_id, current_depth + 1)

            for r in root_spans:
                traverse_depth(r.spanID, 0)

            for s_id, span in span_map.items():
                dur_ms = span.duration / 1000.0 if span.duration > 0 else 0.0
                children_dur_ms = sum(
                    (span_map[c].duration / 1000.0)
                    for c in children_map.get(s_id, [])
                    if c in span_map and span_map[c].duration > 0
                )
                self_durations_ms[s_id] = max(0.0, dur_ms - children_dur_ms)

            # Find error spans in this trace
            trace_error_spans: List[TraceSpan] = []
            for s in trace.spans:
                is_err, _, _, _ = extract_error_info(s)
                if is_err:
                    trace_error_spans.append(s)
                    error_spans_count += 1

            # Leaf culprit extraction:
            # If multiple error spans, find the one with MAXIMUM depth (deepest leaf)
            if trace_error_spans:
                trace_error_spans.sort(key=lambda s: depths.get(s.spanID, 0), reverse=True)
                leaf_span = trace_error_spans[0]
                leaf_svc = extract_service_from_span(leaf_span)
                culprit_candidates[leaf_svc] += 3  # Higher weight for leaf error

                is_err, s_code, e_code, e_msg = extract_error_info(leaf_span)
                span_ev = TraceSpanEvidence(
                    trace_id=trace.traceID,
                    span_id=leaf_span.spanID,
                    parent_span_id=parents_map.get(leaf_span.spanID),
                    service_name=leaf_svc,
                    operation_name=leaf_span.operationName,
                    duration_ms=leaf_span.duration / 1000.0 if leaf_span.duration > 0 else 0.0,
                    status_code=s_code,
                    error_code=e_code,
                    error_message=e_msg,
                    is_leaf_culprit=True,
                    depth=depths.get(leaf_span.spanID, 0),
                )
                if best_culprit_span is None or span_ev.depth > best_culprit_span.depth:
                    best_culprit_span = span_ev

            # Format tree hierarchy for the first few failing traces
            if trace_error_spans and len(failing_trace_hierarchies) < self.max_traces_to_format:
                hierarchy_lines = [f"Trace {trace.traceID}:"]

                def print_subtree(s_id: str, indent: int):
                    span = span_map[s_id]
                    svc = extract_service_from_span(span)
                    dur = span.duration / 1000.0 if span.duration > 0 else 0.0
                    is_err, sc, _, msg = extract_error_info(span)
                    err_label = f" [ERROR {sc or ''}]" if is_err else ""
                    leaf_mark = " <--- [LEAF CULPRIT]" if best_culprit_span and best_culprit_span.span_id == s_id else ""
                    indent_str = "  " * indent + ("↳ " if indent > 0 else "")
                    hierarchy_lines.append(f"{indent_str}{svc}:{span.operationName} ({dur:.1f}ms){err_label}{leaf_mark}")
                    for c_id in children_map.get(s_id, []):
                        print_subtree(c_id, indent + 1)

                for r in root_spans:
                    print_subtree(r.spanID, 0)
                failing_trace_hierarchies.append("\n".join(hierarchy_lines))

        # Select overall culprit service
        culprit_service = None
        if culprit_candidates:
            culprit_service = max(culprit_candidates.items(), key=lambda item: item[1])[0]
        elif best_culprit_span:
            culprit_service = best_culprit_span.service_name

        root_service = list(root_services)[0] if root_services else None
        call_hierarchy_summary = "\n\n".join(failing_trace_hierarchies)
        formatted_prompt = self._format_prompt(
            total_traces, error_spans_count, culprit_service, best_culprit_span, call_hierarchy_summary
        )
        est_tokens = max(1, len(formatted_prompt) // 4)

        return TraceEvidence(
            total_traces=total_traces,
            total_spans=total_spans,
            error_spans_count=error_spans_count,
            root_service=root_service,
            culprit_service=culprit_service,
            culprit_span=best_culprit_span,
            call_hierarchy_summary=call_hierarchy_summary,
            formatted_prompt=formatted_prompt,
            estimated_tokens=est_tokens,
        )

    def _format_prompt(
        self,
        total_traces: int,
        error_spans_count: int,
        culprit_service: Optional[str],
        culprit_span: Optional[TraceSpanEvidence],
        call_hierarchy: str,
    ) -> str:
        if error_spans_count == 0:
            return f"Evaluated {total_traces} distributed traces. No span error codes or anomalies detected."

        lines = [
            "### Distributed Trace Analysis:",
            f"- Total Traces: {total_traces}, Failing Spans: {error_spans_count}",
        ]
        if culprit_service:
            lines.append(f"- Isolated Culprit Service: '{culprit_service}'")
        if culprit_span:
            lines.append(
                f"- Deepest Leaf Error Span: {culprit_span.service_name}:{culprit_span.operation_name} "
                f"(status={culprit_span.status_code or 'ERROR'}, duration={culprit_span.duration_ms:.1f}ms, depth={culprit_span.depth})"
            )
        if call_hierarchy:
            lines.append("\nCall Hierarchy DAG:")
            lines.append(call_hierarchy)

        return "\n".join(lines)
