# -*- coding: utf-8 -*-

"""
Unit tests for usage_tracker module.

Tests for:
- UsageTracker event recording
- Summary aggregation
- Daily summary aggregation
- Event pagination
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kiro.usage_tracker import UsageTracker


def _create_tracker(tmp_path) -> UsageTracker:
    """
    Create an isolated usage tracker backed by a temporary SQLite file.

    Args:
        tmp_path: pytest temporary path fixture

    Returns:
        UsageTracker instance bound to a temporary database
    """
    return UsageTracker(str(tmp_path / "usage.sqlite"))


def _record_event_at(
    tracker: UsageTracker,
    when: datetime,
    model: str,
    endpoint: str,
    input_tokens: int,
    output_tokens: int,
    credits_used,
    session_id: str,
) -> None:
    """
    Record a usage event at a fixed timestamp.

    Args:
        tracker: Usage tracker under test
        when: UTC timestamp for the event
        model: Model name
        endpoint: Endpoint name
        input_tokens: Input token count
        output_tokens: Output token count
        credits_used: Raw credits payload
        session_id: Session identifier
    """
    with patch("kiro.usage_tracker.time.time", return_value=when.timestamp()):
        tracker.record_event(
            model=model,
            endpoint=endpoint,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            credits_used=credits_used,
            session_id=session_id,
        )


class TestUsageTrackerSummary:
    """Tests for summary aggregation."""

    def test_get_summary_aggregates_by_model_and_totals(self, tmp_path):
        """
        What it does: Aggregates usage totals across models.
        Purpose: Verify summary output matches expected structure and values.
        """
        tracker = _create_tracker(tmp_path)
        now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)

        _record_event_at(
            tracker,
            now - timedelta(hours=1),
            model="claude-sonnet-4.5",
            endpoint="chat_completions",
            input_tokens=10,
            output_tokens=5,
            credits_used={"credits": 0.5},
            session_id="session-1",
        )
        _record_event_at(
            tracker,
            now - timedelta(hours=2),
            model="claude-haiku-4.5",
            endpoint="messages",
            input_tokens=4,
            output_tokens=3,
            credits_used=1.25,
            session_id="session-2",
        )
        _record_event_at(
            tracker,
            now - timedelta(days=2),
            model="claude-sonnet-4.5",
            endpoint="chat_completions",
            input_tokens=999,
            output_tokens=999,
            credits_used=9.0,
            session_id="session-old",
        )

        with patch("kiro.usage_tracker.time.time", return_value=now.timestamp()):
            summary = tracker.get_summary("day")

        assert summary["period"] == "day"
        assert summary["totals"]["request_count"] == 2
        assert summary["totals"]["input_tokens"] == 14
        assert summary["totals"]["output_tokens"] == 8
        assert summary["totals"]["total_tokens"] == 22
        assert summary["totals"]["credits_used"] == pytest.approx(1.75)
        assert summary["range"]["end_utc"] == now.isoformat()
        assert len(summary["byModel"]) == 2
        assert summary["byModel"][0]["model"] == "claude-sonnet-4.5"
        assert summary["byModel"][0]["credits_used"] == pytest.approx(0.5)
        assert summary["byModel"][1]["model"] == "claude-haiku-4.5"

    def test_get_summary_rejects_invalid_period(self, tmp_path):
        """
        What it does: Rejects unsupported period values.
        Purpose: Verify invalid input is surfaced clearly.
        """
        tracker = _create_tracker(tmp_path)

        with pytest.raises(ValueError, match="Invalid period"):
            tracker.get_summary("year")


class TestUsageTrackerDailySummary:
    """Tests for daily summary aggregation."""

    def test_get_daily_summary_groups_events_by_day(self, tmp_path):
        """
        What it does: Groups usage into per-day buckets.
        Purpose: Verify daily charts can be built from aggregated data.
        """
        tracker = _create_tracker(tmp_path)
        now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)

        _record_event_at(
            tracker,
            now - timedelta(hours=1),
            model="claude-sonnet-4.5",
            endpoint="chat_completions",
            input_tokens=10,
            output_tokens=5,
            credits_used=0.25,
            session_id="session-1",
        )
        _record_event_at(
            tracker,
            now - timedelta(days=1, hours=2),
            model="claude-haiku-4.5",
            endpoint="messages",
            input_tokens=7,
            output_tokens=2,
            credits_used=0.75,
            session_id="session-2",
        )
        _record_event_at(
            tracker,
            now - timedelta(days=1, hours=4),
            model="claude-haiku-4.5",
            endpoint="messages",
            input_tokens=3,
            output_tokens=1,
            credits_used=None,
            session_id="session-3",
        )

        with patch("kiro.usage_tracker.time.time", return_value=now.timestamp()):
            summary = tracker.get_daily_summary("week")

        assert summary["period"] == "week"
        assert summary["totals"]["request_count"] == 3
        assert summary["totals"]["credits_used"] == pytest.approx(1.0)
        assert len(summary["days"]) == 2
        assert summary["days"][0]["date"] == "2026-01-09"
        assert summary["days"][0]["totals"]["request_count"] == 2
        assert summary["days"][0]["totals"]["total_tokens"] == 13
        assert summary["days"][1]["date"] == "2026-01-10"
        assert summary["days"][1]["totals"]["total_tokens"] == 15
        assert len(summary["byModel"]) == 2


class TestUsageTrackerEventsPage:
    """Tests for event pagination."""

    def test_get_events_page_returns_paginated_items(self, tmp_path):
        """
        What it does: Returns paginated event data ordered by newest first.
        Purpose: Verify the events table can page through stored usage events.
        """
        tracker = _create_tracker(tmp_path)
        now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)

        _record_event_at(
            tracker,
            now - timedelta(minutes=3),
            model="model-a",
            endpoint="chat_completions",
            input_tokens=1,
            output_tokens=1,
            credits_used=0.1,
            session_id="session-1",
        )
        _record_event_at(
            tracker,
            now - timedelta(minutes=2),
            model="model-b",
            endpoint="messages",
            input_tokens=2,
            output_tokens=2,
            credits_used=0.2,
            session_id="session-2",
        )
        _record_event_at(
            tracker,
            now - timedelta(minutes=1),
            model="model-c",
            endpoint="messages",
            input_tokens=3,
            output_tokens=3,
            credits_used=0.3,
            session_id="session-3",
        )

        with patch("kiro.usage_tracker.time.time", return_value=now.timestamp()):
            first_page = tracker.get_events_page("day", page=1, page_size=2)
            second_page = tracker.get_events_page("day", page=2, page_size=2)

        assert first_page["total"] == 3
        assert first_page["total_pages"] == 2
        assert len(first_page["items"]) == 2
        assert first_page["items"][0]["session_id"] == "session-3"
        assert first_page["items"][1]["session_id"] == "session-2"
        assert len(second_page["items"]) == 1
        assert second_page["items"][0]["session_id"] == "session-1"
