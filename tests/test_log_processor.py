"""
Unit tests for LogSage log preprocessing pipeline.
"""

from src.log_processor.filter import (
    LOGSAGE_KEYWORDS,
    matches_keyword,
    expand_asymmetric_context,
)
from src.log_processor.miner import DrainBaselineMiner
from src.log_processor.processor import LogSageProcessor, estimate_tokens


def test_keyword_matching():
    assert matches_keyword("CRITICAL: fatal error occurred") == "fatal"
    assert matches_keyword("Panic in goroutine 12") == "panic"
    assert matches_keyword("Connection failed on port 50051") == "fail"
    assert matches_keyword("System.Exception: Null reference") == "exception"
    assert matches_keyword("All systems normal and healthy") is None


def test_asymmetric_context_expansion():
    # Construct a log of 20 lines
    log_lines = [f"line_{i}" for i in range(20)]
    target_idx = 10  # "line_10"

    ctx_before, target, ctx_after = expand_asymmetric_context(
        log_lines, target_idx, m=3, n=7
    )

    assert target == "line_10"
    # Exactly m=3 lines before: line_7, line_8, line_9
    assert ctx_before == ["line_7", "line_8", "line_9"]
    # Exactly n=7 lines after: line_11 through line_17
    assert ctx_after == [f"line_{i}" for i in range(11, 18)]
    assert len(ctx_before) == 3
    assert len(ctx_after) == 7


def test_drain_baseline_mining_and_diffing():
    miner = DrainBaselineMiner()
    baseline_logs = [
        "2026-10-04T10:00:00 INFO User user123 logged in from 192.168.1.1",
        "2026-10-04T10:00:01 INFO User user456 logged in from 192.168.1.2",
        "2026-10-04T10:00:02 INFO Checkout completed for order 999",
    ]
    miner.train_baseline_from_runs([{"test_svc": baseline_logs}])
    assert len(miner.baseline_templates) > 0

    # Normal log line matching baseline
    template, is_novel = miner.classify_line(
        "2026-10-04T10:00:05 INFO User user789 logged in from 192.168.1.3"
    )
    assert is_novel is False

    # Novel error line NOT in baseline
    template, is_novel = miner.classify_line(
        "2026-10-04T10:00:06 FATAL OutOfMemory killed container immediately"
    )
    assert is_novel is True


def test_logsage_processor_end_to_end():
    processor = LogSageProcessor(m_before=3, n_after=7, max_log_tokens=1500)
    # Load baselines from disk
    ok = processor.load_or_train_baselines()
    assert ok is True
    assert processor.is_baseline_ready is True

    test_logs = {
        "cartservice": [
            "info: normal startup",
            "info: connected to redis",
            "info: get cart",
            "fatal: connection to redis lost unexpectedly",
            "info: attempting retry 1",
            "info: attempting retry 2",
            "info: retry failed",
            "info: shutting down",
        ]
    }

    evidence_dict = processor.process_all_logs(test_logs)
    assert "cartservice" in evidence_dict
    ev = evidence_dict["cartservice"]
    assert len(ev.snippets) >= 1
    assert ev.snippets[0].matched_keyword == "fatal"
    assert "fatal: connection to redis lost unexpectedly" in ev.snippets[0].target_line
    assert ev.estimated_tokens > 0


def test_probe_filtering_in_asymmetric_context():
    """
    Verifies that interleaved health checks (/healthz, kube-probe) do not displace
    relevant context lines around an error.
    """
    logs = [
        "2026-10-06 10:00:00 [req-1] Calling database pool",
        "2026-10-06 10:00:01 kube-probe GET /healthz 200 OK",
        "2026-10-06 10:00:02 [req-1] Connection pool exhausted",
        "2026-10-06 10:00:03 [req-1] ERROR: Fatal database timeout",
        "2026-10-06 10:00:04 kube-probe GET /healthz 200 OK",
        "2026-10-06 10:00:05 [req-1] Stack trace: at DB.connect() line 42",
    ]
    target_idx = 3  # "ERROR: Fatal database timeout"

    ctx_before, target, ctx_after = expand_asymmetric_context(
        logs, target_idx, m=2, n=2, filter_probes=True
    )

    assert "ERROR: Fatal database timeout" in target
    # Probe line at index 1 is skipped, preserving earlier context
    assert not any("/healthz" in l for l in ctx_before)
    assert not any("/healthz" in l for l in ctx_after)
    assert any("Calling database pool" in l for l in ctx_before)
    assert any("Stack trace" in l for l in ctx_after)

