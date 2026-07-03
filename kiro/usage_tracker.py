# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
SQLite-backed usage tracking for token consumption events.

This module stores per-request token usage and exposes summary helpers for
dashboard and API endpoints.
"""

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from loguru import logger


CreditsValue = Optional[Union[int, float, Dict[str, Any]]]


class UsageTracker:
    """
    Track token usage events in a SQLite database.

    The tracker uses a process-local lock and opens short-lived SQLite
    connections for each operation to keep access thread-safe.

    Args:
        db_path: Optional override for the SQLite database path
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = Path(db_path or os.getenv("USAGE_DB_PATH", "usage.sqlite"))
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _initialize_database(self) -> None:
        """Create required tables and indexes if they do not exist."""
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ms INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            endpoint TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            credits_used REAL
        )
        """
        create_index_sql = """
        CREATE INDEX IF NOT EXISTS idx_usage_events_created_at_ms
        ON usage_events (created_at_ms)
        """

        with self._lock:
            with self._connect() as connection:
                connection.execute(create_table_sql)
                connection.execute(create_index_sql)
                connection.commit()

    def _connect(self) -> sqlite3.Connection:
        """
        Create a SQLite connection with row access by column name.

        Returns:
            Configured SQLite connection
        """
        connection = sqlite3.connect(str(self._db_path))
        connection.row_factory = sqlite3.Row
        return connection

    def _get_period_range(self, period: str) -> Tuple[int, int, str, str]:
        """
        Resolve the UTC time range for a named period.

        Args:
            period: Supported values are day, week, and month

        Returns:
            Tuple of start_ms, end_ms, start_utc, end_utc

        Raises:
            ValueError: If the period is unsupported
        """
        now = datetime.fromtimestamp(time.time(), tz=timezone.utc)
        if period == "day":
            start = now - timedelta(days=1)
        elif period == "week":
            start = now - timedelta(days=7)
        elif period == "month":
            start = now - timedelta(days=30)
        else:
            raise ValueError(f"Invalid period: {period}")

        start_ms = int(start.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        return start_ms, end_ms, start.isoformat(), now.isoformat()

    def _normalize_credits_used(self, credits_used: CreditsValue) -> Optional[float]:
        """
        Normalize Kiro metering payloads into a numeric credits value.

        Args:
            credits_used: Raw usage payload from the Kiro stream

        Returns:
            Parsed credits value or None when unavailable
        """
        if isinstance(credits_used, (int, float)):
            return float(credits_used)

        if not isinstance(credits_used, dict):
            return None

        for key in ("credits_used", "credits", "usage"):
            value = credits_used.get(key)
            if isinstance(value, (int, float)):
                return float(value)

        return None

    def _query_model_summary(self, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        """
        Query aggregated usage grouped by model.

        Args:
            start_ms: Inclusive range start in Unix milliseconds
            end_ms: Exclusive range end in Unix milliseconds

        Returns:
            List of model summary dictionaries
        """
        summary_sql = """
        SELECT
            model,
            COUNT(*) AS request_count,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(credits_used), 0) AS credits_used
        FROM usage_events
        WHERE created_at_ms >= ? AND created_at_ms < ?
        GROUP BY model
        ORDER BY total_tokens DESC, model ASC
        """

        with self._lock:
            with self._connect() as connection:
                rows = connection.execute(summary_sql, (start_ms, end_ms)).fetchall()

        return [
            {
                "model": row["model"],
                "request_count": int(row["request_count"]),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
                "total_tokens": int(row["total_tokens"]),
                "credits_used": float(row["credits_used"] or 0.0),
            }
            for row in rows
        ]

    def _build_totals(self, by_model: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Build overall totals from model summary rows.

        Args:
            by_model: Aggregated per-model rows

        Returns:
            Totals dictionary
        """
        return {
            "request_count": sum(item["request_count"] for item in by_model),
            "input_tokens": sum(item["input_tokens"] for item in by_model),
            "output_tokens": sum(item["output_tokens"] for item in by_model),
            "total_tokens": sum(item["total_tokens"] for item in by_model),
            "credits_used": float(sum(item["credits_used"] for item in by_model)),
        }

    def record_event(
        self,
        model: str,
        endpoint: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        credits_used: CreditsValue,
        session_id: str,
    ) -> None:
        """
        Persist a single usage event.

        Args:
            model: Model name used for the request
            endpoint: API surface name (messages or chat_completions)
            input_tokens: Prompt/input token count
            output_tokens: Completion/output token count
            total_tokens: Combined token count
            credits_used: Raw or normalized credits payload
            session_id: Conversation or session identifier
        """
        created_at = datetime.fromtimestamp(time.time(), tz=timezone.utc)
        created_at_ms = int(created_at.timestamp() * 1000)
        normalized_credits = self._normalize_credits_used(credits_used)

        insert_sql = """
        INSERT INTO usage_events (
            created_at_ms,
            created_at_utc,
            session_id,
            endpoint,
            model,
            input_tokens,
            output_tokens,
            total_tokens,
            credits_used
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        try:
            with self._lock:
                with self._connect() as connection:
                    connection.execute(
                        insert_sql,
                        (
                            created_at_ms,
                            created_at.isoformat(),
                            session_id,
                            endpoint,
                            model,
                            int(input_tokens),
                            int(output_tokens),
                            int(total_tokens),
                            normalized_credits,
                        ),
                    )
                    connection.commit()
        except sqlite3.Error as error:
            logger.error(f"Failed to record usage event: {error}")

    def get_summary(self, period: str) -> Dict[str, Any]:
        """
        Return aggregate usage totals for a time period.

        Args:
            period: Supported values are day, week, and month

        Returns:
            TokenUsageSummary-compatible dictionary
        """
        start_ms, end_ms, start_utc, end_utc = self._get_period_range(period)
        by_model = self._query_model_summary(start_ms, end_ms)
        totals = self._build_totals(by_model)
        return {
            "byModel": by_model,
            "period": period,
            "range": {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_utc": start_utc,
                "end_utc": end_utc,
            },
            "totals": totals,
        }

    def get_daily_summary(self, period: str) -> Dict[str, Any]:
        """
        Return daily usage totals and per-model breakdowns for a period.

        Args:
            period: Supported values are day, week, and month

        Returns:
            TokenUsageDailySummary-compatible dictionary
        """
        start_ms, end_ms, start_utc, end_utc = self._get_period_range(period)
        by_model = self._query_model_summary(start_ms, end_ms)
        totals = self._build_totals(by_model)

        daily_sql = """
        SELECT
            substr(created_at_utc, 1, 10) AS event_date,
            model,
            COUNT(*) AS request_count,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(credits_used), 0) AS credits_used
        FROM usage_events
        WHERE created_at_ms >= ? AND created_at_ms < ?
        GROUP BY event_date, model
        ORDER BY event_date ASC, total_tokens DESC, model ASC
        """

        with self._lock:
            with self._connect() as connection:
                rows = connection.execute(daily_sql, (start_ms, end_ms)).fetchall()

        days_map: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            event_date = str(row["event_date"])
            if event_date not in days_map:
                day_start = datetime.fromisoformat(f"{event_date}T00:00:00+00:00")
                day_end = day_start + timedelta(days=1)
                days_map[event_date] = {
                    "date": event_date,
                    "start_ms": int(day_start.timestamp() * 1000),
                    "end_ms": int(day_end.timestamp() * 1000),
                    "totals": {
                        "request_count": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "credits_used": 0.0,
                    },
                    "byModel": [],
                }

            model_summary = {
                "model": row["model"],
                "request_count": int(row["request_count"]),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
                "total_tokens": int(row["total_tokens"]),
                "credits_used": float(row["credits_used"] or 0.0),
            }
            day_entry = days_map[event_date]
            day_entry["byModel"].append(model_summary)
            day_entry["totals"]["request_count"] += model_summary["request_count"]
            day_entry["totals"]["input_tokens"] += model_summary["input_tokens"]
            day_entry["totals"]["output_tokens"] += model_summary["output_tokens"]
            day_entry["totals"]["total_tokens"] += model_summary["total_tokens"]
            day_entry["totals"]["credits_used"] += model_summary["credits_used"]

        return {
            "days": list(days_map.values()),
            "byModel": by_model,
            "period": period,
            "range": {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_utc": start_utc,
                "end_utc": end_utc,
            },
            "totals": totals,
        }

    def get_events_page(self, period: str, page: int, page_size: int) -> Dict[str, Any]:
        """
        Return a paginated list of raw usage events.

        Args:
            period: Supported values are day, week, and month
            page: 1-based page number
            page_size: Number of events per page

        Returns:
            TokenUsageEventsPage-compatible dictionary

        Raises:
            ValueError: If pagination inputs are invalid
        """
        if page < 1:
            raise ValueError("Page must be greater than or equal to 1")
        if page_size < 1:
            raise ValueError("Page size must be greater than or equal to 1")

        start_ms, end_ms, start_utc, end_utc = self._get_period_range(period)
        offset = (page - 1) * page_size

        count_sql = """
        SELECT COUNT(*) AS total
        FROM usage_events
        WHERE created_at_ms >= ? AND created_at_ms < ?
        """
        events_sql = """
        SELECT
            id,
            created_at_ms,
            created_at_utc,
            model,
            endpoint,
            input_tokens,
            output_tokens,
            total_tokens,
            credits_used,
            session_id
        FROM usage_events
        WHERE created_at_ms >= ? AND created_at_ms < ?
        ORDER BY created_at_ms DESC, id DESC
        LIMIT ? OFFSET ?
        """

        with self._lock:
            with self._connect() as connection:
                total_row = connection.execute(count_sql, (start_ms, end_ms)).fetchone()
                event_rows = connection.execute(
                    events_sql,
                    (start_ms, end_ms, page_size, offset),
                ).fetchall()

        total = int(total_row["total"] if total_row is not None else 0)
        total_pages = (total + page_size - 1) // page_size if total else 0

        items = [
            {
                "id": int(row["id"]),
                "created_at_ms": int(row["created_at_ms"]),
                "created_at_utc": row["created_at_utc"],
                "model": row["model"],
                "endpoint": row["endpoint"],
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
                "total_tokens": int(row["total_tokens"]),
                "credits_used": None if row["credits_used"] is None else float(row["credits_used"]),
                "session_id": row["session_id"],
            }
            for row in event_rows
        ]

        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "period": period,
            "range": {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_utc": start_utc,
                "end_utc": end_utc,
            },
            "total": total,
            "total_pages": total_pages,
        }


usage_tracker = UsageTracker()
