"""
LogSage End-to-End Processor.
Faithfully implements the LogSage preprocessing pipeline (arXiv:2506.03691):
1. Ingests x=3 success runs to establish Drain3 baseline templates.
2. Identifies error candidates via keyword matching & novel template diffing.
3. Applies asymmetric context expansion (m=3 before, n=7 after).
4. Merges overlapping windows and respects token budget constraints.
5. Produces validated LogEvidence models ready for Gemini prompt assembly.
"""

import glob
import json
import os
from typing import Dict, List, Optional, Set, Tuple
from src.log_processor.filter import matches_keyword, expand_asymmetric_context
from src.log_processor.miner import DrainBaselineMiner
from src.schemas.evidence import LogEvidence, LogSnippet
from src.schemas.telemetry import TelemetrySnapshot


def estimate_tokens(text: str) -> int:
    """Standard token estimation (approx 4 characters per token)."""
    return max(1, len(text) // 4)


class LogSageProcessor:
    """
    ObservaSage implementation of the LogSage preprocessing pipeline.
    """

    def __init__(
        self,
        config_path: str = "config/drain3.ini",
        m_before: int = 3,
        n_after: int = 7,
        max_log_tokens: int = 1500,
    ):
        self.m_before = m_before
        self.n_after = n_after
        self.max_log_tokens = max_log_tokens
        self.miner = DrainBaselineMiner(config_path=config_path)
        self.is_baseline_ready = False

    def load_or_train_baselines(
        self,
        baseline_dir: str = "data/runs/success",
        cache_file: str = "data/baselines/drain3_baseline_templates.json",
        x_runs: int = 3,
    ) -> bool:
        """
        Attempts to load cached baseline templates, or trains on x=3 success runs.
        """
        if self.miner.load_baselines(cache_file) and len(self.miner.baseline_templates) > 0:
            self.is_baseline_ready = True
            return True

        # Collect success runs
        pattern = os.path.join(baseline_dir, "*_telemetry.json")
        run_files = sorted(glob.glob(pattern), reverse=True)

        if not run_files:
            return False

        # Take up to x=3 recent runs
        selected_files = run_files[:x_runs]
        runs_logs: List[Dict[str, List[str]]] = []

        for fpath in selected_files:
            try:
                with open(fpath, "r") as f:
                    data = json.load(f)
                    snapshot = TelemetrySnapshot(**data)
                    runs_logs.append(snapshot.logs)
            except Exception:
                continue

        if not runs_logs:
            return False

        self.miner.train_baseline_from_runs(runs_logs)
        self.miner.save_baselines(cache_file)
        self.is_baseline_ready = True
        return True

    def process_service_logs(self, service: str, raw_lines: List[str]) -> LogEvidence:
        """
        Processes logs for a single service:
        - Classifies templates with Drain3
        - Detects novel templates
        - Finds LogSage keyword matches
        - Performs asymmetric context expansion (m=3, n=7)
        - Merges overlapping windows
        """
        if not raw_lines:
            return LogEvidence(service=service)

        # Step 1: Drain3 classification and keyword scan
        error_indices: List[int] = []
        matched_keywords: Dict[int, Optional[str]] = {}
        templates: Dict[int, str] = {}
        novelty_map: Dict[int, bool] = {}

        for idx, line in enumerate(raw_lines):
            kw = matches_keyword(line)
            template, is_novel = self.miner.classify_line(line)

            matched_keywords[idx] = kw
            templates[idx] = template
            novelty_map[idx] = is_novel

            # LogSage retains lines that have error keywords OR are novel templates
            if kw is not None or is_novel:
                error_indices.append(idx)

        novel_count = sum(1 for is_novel in novelty_map.values() if is_novel)

        if not error_indices:
            return LogEvidence(
                service=service,
                total_raw_lines=len(raw_lines),
                novel_templates_count=novel_count,
            )

        # Step 2: Apply asymmetric context expansion (m=3 before, n=7 after)
        expanded_ranges: List[Tuple[int, int, int]] = []
        for target_idx in error_indices:
            start = max(0, target_idx - self.m_before)
            end = min(len(raw_lines) - 1, target_idx + self.n_after)
            expanded_ranges.append((start, target_idx, end))

        # Merge overlapping ranges
        merged_windows: List[Tuple[int, int, List[int]]] = []
        for start, target, end in expanded_ranges:
            if not merged_windows:
                merged_windows.append((start, end, [target]))
            else:
                last_start, last_end, targets = merged_windows[-1]
                if start <= last_end + 1:
                    merged_windows[-1] = (last_start, max(last_end, end), targets + [target])
                else:
                    merged_windows.append((start, end, [target]))

        # Step 3: Build LogSnippets
        snippets: List[LogSnippet] = []
        formatted_blocks: List[str] = []

        for win_start, win_end, targets in merged_windows:
            # Select primary target: prioritize target with explicit keyword match
            kw_targets = [t for t in targets if matched_keywords.get(t) is not None]
            primary_target = kw_targets[0] if kw_targets else targets[0]

            ctx_before, target_line, ctx_after = expand_asymmetric_context(
                raw_lines, primary_target, m=self.m_before, n=self.n_after, filter_probes=True
            )

            snippet = LogSnippet(
                service=service,
                line_number=primary_target + 1,
                matched_keyword=matched_keywords.get(primary_target),
                template_id=templates.get(primary_target),
                is_novel=novelty_map.get(primary_target, False),
                context_before=ctx_before,
                target_line=target_line,
                context_after=ctx_after,
            )
            snippets.append(snippet)

            block_lines = []
            for line in ctx_before:
                block_lines.append(f"  [ctx] {line}")
            block_lines.append(f"▶ [ERROR] {target_line}")
            for line in ctx_after:
                block_lines.append(f"  [ctx] {line}")

            formatted_blocks.append("\n".join(block_lines))

        formatted_prompt = f"--- Service: {service} ---\n" + "\n\n".join(formatted_blocks)
        est_tokens = estimate_tokens(formatted_prompt)

        return LogEvidence(
            service=service,
            total_raw_lines=len(raw_lines),
            novel_templates_count=novel_count,
            snippets=snippets,
            formatted_prompt=formatted_prompt,
            estimated_tokens=est_tokens,
        )

    def process_all_logs(self, logs: Dict[str, List[str]]) -> Dict[str, LogEvidence]:
        """
        Processes logs across all microservices and enforces token budget.
        """
        results: Dict[str, LogEvidence] = {}
        total_tokens = 0

        service_evidences: List[LogEvidence] = []
        for service, lines in logs.items():
            evidence = self.process_service_logs(service, lines)
            if evidence.snippets:
                service_evidences.append(evidence)

        # Prioritize services with errors
        service_evidences.sort(key=lambda ev: (len(ev.snippets), ev.novel_templates_count), reverse=True)

        for ev in service_evidences:
            if total_tokens + ev.estimated_tokens <= self.max_log_tokens:
                results[ev.service] = ev
                total_tokens += ev.estimated_tokens
            else:
                remaining_budget = max(0, self.max_log_tokens - total_tokens)
                if remaining_budget > 100:
                    truncated_prompt = ev.formatted_prompt[: remaining_budget * 4] + "\n...[truncated for token budget]"
                    ev.formatted_prompt = truncated_prompt
                    ev.estimated_tokens = remaining_budget
                    results[ev.service] = ev
                    total_tokens += remaining_budget
                break

        return results
