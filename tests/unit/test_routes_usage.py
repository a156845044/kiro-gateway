# -*- coding: utf-8 -*-

"""
Unit tests for usage routes.

Tests for:
- /token-usage
- /token-usage/daily
- /token-usage/events
- /usage-viewer
"""

from unittest.mock import patch


class TestUsageSummaryRoutes:
    """Tests for usage summary endpoints."""

    def test_token_usage_returns_summary_without_authentication(self, test_client):
        """
        What it does: Returns summary data without requiring auth.
        Purpose: Verify the public usage summary endpoint works from the main app.
        """
        expected_summary = {
            "period": "week",
            "range": {
                "start_ms": 1,
                "end_ms": 2,
                "start_utc": "2026-01-01T00:00:00+00:00",
                "end_utc": "2026-01-08T00:00:00+00:00",
            },
            "totals": {
                "request_count": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "credits_used": 0.25,
            },
            "byModel": [],
        }

        with patch("kiro.routes_usage.usage_tracker.get_summary", return_value=expected_summary):
            response = test_client.get("/token-usage?period=week")

        assert response.status_code == 200
        assert response.json() == expected_summary

    def test_token_usage_daily_returns_daily_summary_without_authentication(self, test_client):
        """
        What it does: Returns daily summary data without requiring auth.
        Purpose: Verify the daily usage endpoint is publicly accessible.
        """
        expected_summary = {
            "period": "day",
            "range": {
                "start_ms": 1,
                "end_ms": 2,
                "start_utc": "2026-01-01T00:00:00+00:00",
                "end_utc": "2026-01-02T00:00:00+00:00",
            },
            "totals": {
                "request_count": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "credits_used": 0.25,
            },
            "byModel": [],
            "days": [],
        }

        with patch("kiro.routes_usage.usage_tracker.get_daily_summary", return_value=expected_summary):
            response = test_client.get("/token-usage/daily")

        assert response.status_code == 200
        assert response.json() == expected_summary

    def test_token_usage_events_returns_paginated_events_without_authentication(self, test_client):
        """
        What it does: Returns paginated event data without requiring auth.
        Purpose: Verify the usage events endpoint is publicly accessible.
        """
        expected_page = {
            "items": [
                {
                    "id": 1,
                    "created_at_ms": 123,
                    "created_at_utc": "2026-01-01T00:00:00+00:00",
                    "model": "claude-sonnet-4.5",
                    "endpoint": "chat_completions",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "credits_used": 0.25,
                    "session_id": "session-1",
                }
            ],
            "page": 2,
            "page_size": 1,
            "period": "month",
            "range": {
                "start_ms": 1,
                "end_ms": 2,
                "start_utc": "2025-12-02T00:00:00+00:00",
                "end_utc": "2026-01-01T00:00:00+00:00",
            },
            "total": 2,
            "total_pages": 2,
        }

        with patch("kiro.routes_usage.usage_tracker.get_events_page", return_value=expected_page):
            response = test_client.get("/token-usage/events?period=month&page=2&page_size=1")

        assert response.status_code == 200
        assert response.json() == expected_page

    def test_invalid_period_returns_400(self, test_client):
        """
        What it does: Returns 400 for invalid period values.
        Purpose: Verify invalid query parameters are reported clearly.
        """
        with patch("kiro.routes_usage.usage_tracker.get_summary", side_effect=ValueError("Invalid period: year")):
            response = test_client.get("/token-usage?period=year")

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid period: year"


class TestUsageViewerRoute:
    """Tests for the usage viewer HTML endpoint."""

    def test_usage_viewer_returns_html_without_authentication(self, test_client):
        """
        What it does: Returns the usage viewer HTML page without auth.
        Purpose: Verify the embedded dashboard is served correctly.
        """
        response = test_client.get("/usage-viewer")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Token Usage Viewer" in response.text
