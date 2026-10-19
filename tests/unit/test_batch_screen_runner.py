"""批量筛选共享核心单测：初筛分流纯函数 + summary 落盘。"""
from __future__ import annotations

import json

from pa_agent.batch.screen_runner import (
    STATUS_MARKS,
    should_deep_analyze,
    summarize_results,
    write_screen_summary,
)


class TestShouldDeepAnalyze:
    def test_light_none_fail_open(self):
        ok, why = should_deep_analyze(None, long_only=True, min_conviction=55)
        assert ok is True and why == ""

    def test_not_pass_rejected_with_reason(self):
        ok, why = should_deep_analyze(
            {"pass": False, "direction": "neutral", "conviction": 30, "reason": "漂移行情"},
            long_only=True, min_conviction=0,
        )
        assert ok is False and why == "漂移行情"

    def test_bearish_rejected_in_long_only(self):
        ok, why = should_deep_analyze(
            {"pass": True, "direction": "bearish", "conviction": 70, "reason": ""},
            long_only=True, min_conviction=0,
        )
        assert ok is False and "空头" in why

    def test_bearish_allowed_when_short_enabled(self):
        ok, why = should_deep_analyze(
            {"pass": True, "direction": "bearish", "conviction": 70, "reason": ""},
            long_only=False, min_conviction=0,
        )
        assert ok is True and why == ""

    def test_conviction_below_threshold_rejected(self):
        ok, why = should_deep_analyze(
            {"pass": True, "direction": "bullish", "conviction": 42, "reason": ""},
            long_only=True, min_conviction=55,
        )
        assert ok is False and "42" in why and "55" in why

    def test_conviction_at_threshold_passes(self):
        ok, _ = should_deep_analyze(
            {"pass": True, "direction": "bullish", "conviction": 55, "reason": ""},
            long_only=True, min_conviction=55,
        )
        assert ok is True

    def test_conviction_missing_counts_as_zero(self):
        ok, why = should_deep_analyze(
            {"pass": True, "direction": "neutral", "conviction": None, "reason": ""},
            long_only=True, min_conviction=10,
        )
        assert ok is False

    def test_not_pass_takes_priority_over_bearish(self):
        """初筛不通过的原因应优先于空头/门槛原因（原样透传初筛理由）。"""
        ok, why = should_deep_analyze(
            {"pass": False, "direction": "bearish", "conviction": 5, "reason": "铁丝网"},
            long_only=True, min_conviction=50,
        )
        assert ok is False and why == "铁丝网"


class TestWriteScreenSummary:
    def _results(self):
        return [
            {"symbol": "600001", "status": "opportunity", "light": None,
             "tokens_light": 100, "tokens_full": 1000},
            {"symbol": "600002", "status": "screened_out", "light": None,
             "tokens_light": 100, "tokens_full": 0},
            {"symbol": "600003", "status": "failed", "light": None,
             "tokens_light": 0, "tokens_full": 0},
        ]

    def test_summary_counts_and_file(self, tmp_path):
        out_dir = write_screen_summary(
            tmp_path, self._results(),
            {"timeframe": "1h", "bar_count": 100, "mode": "light",
             "light_only": False, "min_conviction": 55,
             "duration_seconds": 3.2},
        )
        assert out_dir.exists()
        assert out_dir.parent == tmp_path
        assert out_dir.name.startswith("screen_")
        data = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
        assert data["total"] == 3
        assert data["opportunities"] == 1
        assert data["screened_out"] == 1
        assert data["failed"] == 1
        assert data["min_conviction"] == 55
        assert data["tokens_light"] == 200
        assert data["tokens_full"] == 1000
        assert len(data["results"]) == 3

    def test_summarize_results_empty(self):
        assert summarize_results([]) == {
            "total": 0, "opportunities": 0, "no_order": 0, "screened_out": 0,
            "light_pass": 0, "limit_skip": 0, "failed": 0,
            "tokens_light": 0, "tokens_full": 0,
        }

    def test_status_marks_cover_all_states(self):
        assert set(STATUS_MARKS) == {
            "opportunity", "no_order", "screened_out",
            "light_pass", "limit_skip", "failed",
        }
