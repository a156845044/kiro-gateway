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

"""FastAPI routes for usage tracking and the embedded usage dashboard."""

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from loguru import logger

from kiro.usage_tracker import usage_tracker


router = APIRouter(tags=["Usage"])


def _usage_viewer_path() -> Path:
    """
    Resolve the filesystem path to the embedded usage viewer.

    Returns:
        Path to the HTML dashboard file
    """
    return Path(__file__).resolve().parent / "static" / "usage_viewer.html"


@router.get("/token-usage")
async def get_token_usage_summary(period: str = Query("day")) -> Dict[str, Any]:
    """
    Return aggregated usage totals for the requested period.

    Args:
        period: Summary period (day, week, or month)

    Returns:
        Token usage summary data
    """
    try:
        return usage_tracker.get_summary(period)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/token-usage/daily")
async def get_token_usage_daily_summary(period: str = Query("day")) -> Dict[str, Any]:
    """
    Return daily usage aggregates for the requested period.

    Args:
        period: Summary period (day, week, or month)

    Returns:
        Daily token usage summary data
    """
    try:
        return usage_tracker.get_daily_summary(period)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/token-usage/events")
async def get_token_usage_events_page(
    period: str = Query("day"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    """
    Return a paginated list of usage events.

    Args:
        period: Summary period (day, week, or month)
        page: 1-based page number
        page_size: Number of events per page

    Returns:
        Paginated usage event data
    """
    try:
        return usage_tracker.get_events_page(period, page, page_size)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/usage-viewer", response_class=HTMLResponse)
async def get_usage_viewer() -> HTMLResponse:
    """
    Serve the embedded HTML usage dashboard.

    Returns:
        HTML response containing the dashboard page
    """
    viewer_path = _usage_viewer_path()
    try:
        content = viewer_path.read_text(encoding="utf-8")
        return HTMLResponse(
            content,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )
    except FileNotFoundError as error:
        logger.error(f"Usage viewer file not found: {viewer_path}")
        raise HTTPException(status_code=404, detail="Usage viewer not found") from error
    except OSError as error:
        logger.error(f"Failed to read usage viewer file: {error}")
        raise HTTPException(status_code=500, detail="Failed to load usage viewer") from error
