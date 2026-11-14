"""
Distributed Trace Processor for ObservaSage.
Reconstructs span Directed Acyclic Graphs (DAGs), calculates self-durations,
and pinpoints root cause culprit services using DFS leaf traversal and bottleneck analysis.
"""

from collections import defaultdict

from src.schemas.evidence import TraceEvidence, TraceSpanEvidence
from src.schemas.telemetry import Trace, TraceSpan


def extract_service_from_span(span: TraceSpan) -> str:
    """Extracts service name from tags or processID."""
    for tag in span.tags:
        if tag.get("key") in ["service.name", "service", "app", "component"]:
            return str(tag.get("value", "")).strip()
    return "unknown_service"


def extract_error_info(span: TraceSpan) -> tuple[bool, int | None, str | None, str | None]:
    """
    Checks if a span has error status and extracts status code, error code, and error message.
    """
    is_error = False
    status_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None

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
        incident_traces: list[Trace],
        baseline_traces: list[Trace] | None = None,
    ) -> TraceEvidence:
        """
        Reconstructs call trees, identifies leaf error culprits, and formats trace evidence.
        """
        if not incident_traces:
            return TraceEvidence(total_traces=0, total_spans=0)

        total_traces = len(incident_traces)
        total_spans = sum(len(t.spans) for t in incident_traces)

        # Baseline durations by (service, operation)
        baseline_durations: dict[tuple[str, str], list[float]] = defaultdict(list)
        if baseline_traces:
            for trace in baseline_traces:
                for span in trace.spans:
                    svc = extract_service_from_span(span)
                    dur_ms = span.duration / 1000.0 if span.duration > 0 else 0.0
                    baseline_durations[(svc, span.operationName)].append(dur_ms)

        # Evaluate incident traces
        error_spans_count = 0
        latency_bottlenecks_count = 0
        culprit_candidates: dict[str, int] = defaultdict(int)
        best_culprit_span: TraceSpanEvidence | None = None
        error_span_evidences: list[TraceSpanEvidence] = []
        latency_bottleneck_evidences: list[TraceSpanEvidence] = []
        root_services: set[str] = set()
        service_dep_graph: dict[str, set[str]] = defaultdict(set)

        failing_trace_hierarchies: list[str] = []

        for trace in incident_traces:
            if not trace.spans:
                continue

            # Build adjacency and span map
            span_map: dict[str, TraceSpan] = {s.spanID: s for s in trace.spans}
            children_map: dict[str, list[str]] = defaultdict(list)
            parents_map: dict[str, str | None] = {}

            for span in trace.spans:
                parent_id = None
                for ref in span.references:
                    if ref.get("refType") in ["CHILD_OF", "FOLLOWS_FROM"]:
                        parent_id = ref.get("spanID")
                        break
                parents_map[span.spanID] = parent_id
                if parent_id and parent_id in span_map:
                    children_map[parent_id].append(span.spanID)
                    # Track caller -> callee service dependency edge
                    p_svc = extract_service_from_span(span_map[parent_id])
                    c_svc = extract_service_from_span(span)
                    if p_svc and c_svc and p_svc != c_svc and p_svc != "unknown_service" and c_svc != "unknown_service":
                        service_dep_graph[p_svc].add(c_svc)

            # Find root span(s)
            root_spans = [s for s in trace.spans if not parents_map.get(s.spanID) or parents_map[s.spanID] not in span_map]
            if not root_spans:
                root_spans = [trace.spans[0]]

            for r in root_spans:
                root_services.add(extract_service_from_span(r))

            # Compute depths and self-durations via DFS
            depths: dict[str, int] = {}
            self_durations_ms: dict[str, float] = {}

            for r in root_spans:
                self._traverse_depth(r.spanID, 0, children_map, depths)

            for s_id, span in span_map.items():
                dur_ms = span.duration / 1000.0 if span.duration > 0 else 0.0
                children_dur_ms = sum(
                    (span_map[c].duration / 1000.0)
                    for c in children_map.get(s_id, [])
                    if c in span_map and span_map[c].duration > 0
                )
                self_durations_ms[s_id] = max(0.0, dur_ms - children_dur_ms)

            # Find error spans in this trace
            trace_error_spans: list[TraceSpan] = []
            for s in trace.spans:
                is_err, _, _, _ = extract_error_info(s)
                if is_err:
                    trace_error_spans.append(s)
                    error_spans_count += 1

            # Check for timeout / deadline inversion:
            # If a parent failed due to timeout/deadline exceeded, identify bottleneck child
            bottleneck_child_spans: list[TraceSpan] = []
            for s in trace_error_spans:
                is_err, s_code, e_code, e_msg = extract_error_info(s)
                err_text = f"{e_code or ''} {e_msg or ''}".lower()
                is_timeout = s_code in [504, 4] or any(
                    w in err_text for w in ["deadline", "timeout", "timed out", "context deadline exceeded"]
                )
                if is_timeout and children_map.get(s.spanID):
                    # Check if a child consumed majority of the parent's duration
                    p_dur = s.duration if s.duration > 0 else 1.0
                    for c_id in children_map[s.spanID]:
                        c_span = span_map[c_id]
                        if c_span.duration >= 0.6 * p_dur or self_durations_ms.get(c_id, 0.0) >= 0.5 * (p_dur / 1000.0):
                            bottleneck_child_spans.append(c_span)

            # Detect latency bottleneck spans vs baseline p99 self-duration (catches CPU/delay faults where statusCode=0)
            latency_bottleneck_spans: list[TraceSpan] = []
            if baseline_durations:
                for s_id, s_dur_ms in self_durations_ms.items():
                    span = span_map[s_id]
                    svc = extract_service_from_span(span)
                    bl_durs = baseline_durations.get((svc, span.operationName), [])
                    if len(bl_durs) >= 3:
                        bl_sorted = sorted(bl_durs)
                        p99_idx = max(0, int(len(bl_sorted) * 0.99) - 1)
                        p99_ms = bl_sorted[p99_idx]
                        if s_dur_ms > max(50.0, p99_ms * 2.0):
                            latency_bottleneck_spans.append(span)
                            latency_bottlenecks_count += 1

            # Combine explicit error spans, timeout bottleneck spans, and latency bottleneck spans
            candidate_spans_for_leaf = trace_error_spans + bottleneck_child_spans + latency_bottleneck_spans

            for err_sp in trace_error_spans:
                if len(error_span_evidences) < 50:
                    error_span_evidences.append(
                        self._to_span_evidence(trace.traceID, err_sp, parents_map, self_durations_ms, depths, is_leaf=False)
                    )
            for lat_sp in latency_bottleneck_spans:
                if len(latency_bottleneck_evidences) < 50:
                    latency_bottleneck_evidences.append(
                        self._to_span_evidence(trace.traceID, lat_sp, parents_map, self_durations_ms, depths, is_leaf=False)
                    )

            # Leaf culprit extraction:
            # If multiple anomalous spans, find the one with MAXIMUM depth (deepest leaf)
            trace_culprit_id: str | None = None
            if candidate_spans_for_leaf:
                candidate_spans_for_leaf.sort(key=lambda s: depths.get(s.spanID, 0), reverse=True)
                leaf_span = candidate_spans_for_leaf[0]
                trace_culprit_id = leaf_span.spanID
                leaf_svc = extract_service_from_span(leaf_span)
                culprit_candidates[leaf_svc] += 3  # Higher weight for leaf culprit

                span_ev = self._to_span_evidence(
                    trace.traceID, leaf_span, parents_map, self_durations_ms, depths, is_leaf=True
                )
                if best_culprit_span is None or span_ev.depth > best_culprit_span.depth:
                    best_culprit_span = span_ev

            # Format tree hierarchy for the first few failing or bottleneck traces
            if (trace_error_spans or latency_bottleneck_spans) and len(failing_trace_hierarchies) < self.max_traces_to_format:
                hierarchy_lines = [f"Trace {trace.traceID}:"]
                highlight_id = best_culprit_span.span_id if best_culprit_span else trace_culprit_id
                bottleneck_ids = {s.spanID for s in latency_bottleneck_spans}
                for r in root_spans:
                    self._print_subtree(
                        r.spanID, 0, span_map, children_map, hierarchy_lines, highlight_id, bottleneck_ids
                    )
                failing_trace_hierarchies.append("\n".join(hierarchy_lines))

        # Select overall culprit service
        culprit_service = None
        if culprit_candidates:
            culprit_service = max(culprit_candidates.items(), key=lambda item: item[1])[0]
        elif best_culprit_span:
            culprit_service = best_culprit_span.service_name

        root_service = next(iter(root_services)) if root_services else None
        call_hierarchy_summary = "\n\n".join(failing_trace_hierarchies)
        formatted_prompt = self._format_prompt(
            total_traces,
            error_spans_count,
            latency_bottlenecks_count,
            culprit_service,
            best_culprit_span,
            call_hierarchy_summary,
        )
        est_tokens = max(1, len(formatted_prompt) // 4)

        return TraceEvidence(
            total_traces=total_traces,
            total_spans=total_spans,
            error_spans_count=error_spans_count,
            latency_bottlenecks_count=latency_bottlenecks_count,
            root_service=root_service,
            culprit_service=culprit_service,
            culprit_span=best_culprit_span,
            error_spans=error_span_evidences,
            latency_bottlenecks=latency_bottleneck_evidences,
            service_dependency_graph={k: sorted(v) for k, v in service_dep_graph.items()},
            call_hierarchy_summary=call_hierarchy_summary,
            formatted_prompt=formatted_prompt,
            estimated_tokens=est_tokens,
        )

    @staticmethod
    def _to_span_evidence(
        trace_id: str,
        span: TraceSpan,
        parents_map: dict[str, str | None],
        self_durations_ms: dict[str, float],
        depths: dict[str, int],
        is_leaf: bool = False,
    ) -> TraceSpanEvidence:
        _is_err, s_code, e_code, e_msg = extract_error_info(span)
        return TraceSpanEvidence(
            trace_id=trace_id,
            span_id=span.spanID,
            parent_span_id=parents_map.get(span.spanID),
            service_name=extract_service_from_span(span),
            operation_name=span.operationName,
            duration_ms=span.duration / 1000.0 if span.duration > 0 else 0.0,
            self_duration_ms=self_durations_ms.get(span.spanID, 0.0),
            status_code=s_code,
            error_code=e_code,
            error_message=e_msg,
            is_leaf_culprit=is_leaf,
            depth=depths.get(span.spanID, 0),
        )

    @staticmethod
    def _traverse_depth(
        span_id: str,
        current_depth: int,
        children_map: dict[str, list[str]],
        depths: dict[str, int],
    ) -> None:
        if span_id in depths:
            return
        depths[span_id] = current_depth
        for child_id in children_map.get(span_id, []):
            TraceProcessor._traverse_depth(child_id, current_depth + 1, children_map, depths)

    @staticmethod
    def _print_subtree(
        s_id: str,
        indent: int,
        span_map: dict[str, TraceSpan],
        children_map: dict[str, list[str]],
        hierarchy_lines: list[str],
        best_culprit_span_id: str | None,
        bottleneck_ids: set[str],
    ) -> None:
        span = span_map[s_id]
        svc = extract_service_from_span(span)
        dur = span.duration / 1000.0 if span.duration > 0 else 0.0
        is_err, sc, _, _ = extract_error_info(span)
        err_label = f" [ERROR {sc or ''}]" if is_err else (" [LATENCY BOTTLENECK]" if s_id in bottleneck_ids else "")
        leaf_mark = " <--- [LEAF CULPRIT]" if best_culprit_span_id == s_id else ""
        indent_str = "  " * indent + ("↳ " if indent > 0 else "")
        hierarchy_lines.append(f"{indent_str}{svc}:{span.operationName} ({dur:.1f}ms){err_label}{leaf_mark}")
        for c_id in children_map.get(s_id, []):
            TraceProcessor._print_subtree(
                c_id, indent + 1, span_map, children_map, hierarchy_lines, best_culprit_span_id, bottleneck_ids
            )

    def _format_prompt(
        self,
        total_traces: int,
        error_spans_count: int,
        latency_bottlenecks_count: int,
        culprit_service: str | None,
        culprit_span: TraceSpanEvidence | None,
        call_hierarchy: str,
    ) -> str:
        if error_spans_count == 0 and latency_bottlenecks_count == 0:
            return f"Evaluated {total_traces} distributed traces. No span error codes or anomalies detected."

        lines = [
            "### Distributed Trace Analysis:",
            f"- Total Traces: {total_traces}, Failing Spans: {error_spans_count}, Latency Bottlenecks: {latency_bottlenecks_count}",
        ]
        if culprit_service:
            lines.append(f"- Isolated Culprit Service: '{culprit_service}'")
        if culprit_span:
            status_label = culprit_span.status_code or ("ERROR" if error_spans_count > 0 else "LATENCY_ANOMALY")
            lines.append(
                f"- Deepest Leaf Culprit Span: {culprit_span.service_name}:{culprit_span.operation_name} "
                f"(status={status_label}, duration={culprit_span.duration_ms:.1f}ms, depth={culprit_span.depth})"
            )
        if call_hierarchy:
            lines.append("\nCall Hierarchy DAG:")
            lines.append(call_hierarchy)

        return "\n".join(lines)
