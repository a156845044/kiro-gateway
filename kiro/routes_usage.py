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
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
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


def _resolve_profile_arn(auth_manager: Any) -> Optional[str]:
    """
    Resolve the correct CodeWhisperer profileArn for API calls.

    Priority: .env PROFILE_ARN (read by absolute path, populated by
    get_profile_arn.py) → config.PROFILE_ARN → auth_manager.profile_arn.
    Only values starting with 'arn:aws:' are accepted, to avoid sending a
    clientId UUID that would fail server-side ARN validation.

    Args:
        auth_manager: The active KiroAuthManager

    Returns:
        A valid profileArn string, or None
    """
    from kiro.config import _get_raw_env_value, PROFILE_ARN
    env_path = Path(__file__).resolve().parent.parent / ".env"
    candidates = [
        _get_raw_env_value("PROFILE_ARN", str(env_path)),
        PROFILE_ARN,
        getattr(auth_manager, "profile_arn", None),
    ]
    for c in candidates:
        if c and c.startswith("arn:aws:"):
            return c
    return next((c for c in candidates if c), None)


def _build_kiro_api_context(auth_manager: Any, token: str) -> tuple:
    """
    Build the base URL host, headers, and profileArn for a q.amazonaws.com call.

    The q.{region}.amazonaws.com data plane hosts getUsageLimits and
    ListAvailableModels. External IdP (Microsoft SSO) accounts require the
    'TokenType: EXTERNAL_IDP' header AND a full AWS-SDK-style User-Agent set;
    without them the server returns 400 'Invalid ARN' or 403 'subscription
    does not support this application'.

    Args:
        auth_manager: The active KiroAuthManager
        token: A valid access token

    Returns:
        Tuple of (region, headers dict, profile_arn)
    """
    import re as _re
    import uuid as _uuid

    api_host = getattr(auth_manager, "api_host", "") or ""
    _m = _re.search(r"(?:runtime|q)\.([^.]+)\.(kiro\.dev|amazonaws\.com)", api_host)
    region = _m.group(1) if _m else "us-east-1"

    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        # Full AWS-SDK-style headers are REQUIRED — a bare request gets 403.
        "User-Agent": (
            "aws-sdk-js/1.0.0 ua/2.1 os/windows lang/js "
            "api/codewhispererruntime#1.0.0 m/N,E KiroIDE"
        ),
        "x-amz-user-agent": "aws-sdk-js/1.0.0 KiroIDE",
        "amz-sdk-invocation-id": str(_uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=3",
    }
    try:
        from kiro.auth import AuthType
        if getattr(auth_manager, "auth_type", None) == AuthType.EXTERNAL_IDP:
            headers["TokenType"] = "EXTERNAL_IDP"
    except Exception as _e:
        logger.debug(f"[KiroAPI] Could not determine auth_type: {_e}")

    profile_arn = _resolve_profile_arn(auth_manager)
    return region, headers, profile_arn


def _get_auth_manager(request: Request) -> Any:
    """
    Fetch the first available account's auth manager from app state.

    Args:
        request: The FastAPI request

    Returns:
        The KiroAuthManager

    Raises:
        HTTPException: If no auth manager is available
    """
    account_manager = getattr(request.app.state, "account_manager", None)
    if account_manager is None:
        raise HTTPException(status_code=503, detail="Account manager not available")
    for _account_id, account in account_manager._accounts.items():
        if account.auth_manager is not None:
            return account.auth_manager
    raise HTTPException(status_code=503, detail="No auth manager available")


def _compute_quota(breakdown: Dict[str, Any]) -> Dict[str, float]:
    """
    Aggregate base usage + active bonuses + active free-trial into totals.

    Mirrors the logic from wjsoj/cc-core:kiroapi/credits.go UsageTotal/LimitTotal.

    Args:
        breakdown: One entry from usageBreakdownList

    Returns:
        Dict with keys: used, limit, remaining, next_reset_ms
    """
    used  = float(breakdown.get("currentUsageWithPrecision") or breakdown.get("currentUsage") or 0)
    limit = float(breakdown.get("usageLimitWithPrecision")  or breakdown.get("usageLimit")  or 0)

    # Add active free-trial
    trial = breakdown.get("freeTrialInfo") or {}
    if trial.get("freeTrialStatus") == "ACTIVE":
        used  += float(trial.get("currentUsageWithPrecision") or trial.get("currentUsage") or 0)
        limit += float(trial.get("usageLimitWithPrecision")  or trial.get("usageLimit")  or 0)

    # Add active bonuses
    for bonus in breakdown.get("bonuses") or []:
        if bonus.get("status") == "ACTIVE":
            used  += float(bonus.get("currentUsage") or 0)
            limit += float(bonus.get("usageLimit")   or 0)

    remaining = max(0.0, limit - used)
    return {
        "used":          round(used,  4),
        "limit":         round(limit, 4),
        "remaining":     round(remaining, 4),
        "next_reset_ms": breakdown.get("nextDateReset"),
    }


@router.get("/token-usage/quota")
async def get_kiro_quota(request: Request) -> Dict[str, Any]:
    """
    Fetch live subscription quota from the Kiro API.

    Calls GET https://q.{region}.amazonaws.com/getUsageLimits and returns
    computed totals for Premium Interactions (AGENTIC_REQUEST resource type).

    Returns:
        Quota dict: used, limit, remaining, next_reset_ms, subscription_title
    """
    auth_manager = _get_auth_manager(request)

    try:
        token = await auth_manager.get_access_token()
    except Exception as exc:
        logger.error(f"[Quota] Failed to get auth token: {exc}")
        raise HTTPException(status_code=503, detail=f"Auth error: {exc}") from exc

    # Build region, headers (incl. TokenType for External IdP), and profileArn.
    region, headers, profile_arn = _build_kiro_api_context(auth_manager, token)
    url = f"https://q.{region}.amazonaws.com/getUsageLimits"
    logger.debug(f"[Quota] Calling {url} (region={region}, profileArn={profile_arn})")

    params = {"origin": "AI_EDITOR", "resourceType": "AGENTIC_REQUEST"}
    if profile_arn:
        params["profileArn"] = profile_arn

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.RequestError as exc:
        logger.error(f"[Quota] Request failed: {exc}")
        raise HTTPException(status_code=502, detail=f"Kiro API unreachable: {exc}") from exc

    if resp.status_code != 200:
        logger.warning(f"[Quota] Kiro returned {resp.status_code}: {resp.text[:200]}")
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])

    body = resp.json()
    logger.debug(f"[Quota] Raw response keys: {list(body.keys())}")

    breakdowns = body.get("usageBreakdownList") or []
    # The breakdown's resourceType is "CREDIT" (unit=INVOCATIONS) even though the
    # request asks for AGENTIC_REQUEST. Prefer CREDIT/AGENTIC_REQUEST, else first.
    primary = next(
        (b for b in breakdowns if b.get("resourceType") in ("CREDIT", "AGENTIC_REQUEST")),
        breakdowns[0] if breakdowns else {}
    )

    quota = _compute_quota(primary)
    quota["subscription_title"] = (
        (body.get("subscriptionInfo") or {}).get("subscriptionTitle") or "Unknown"
    )
    quota["subscription_type"] = (
        (body.get("subscriptionInfo") or {}).get("type")
        or (body.get("subscriptionInfo") or {}).get("subscriptionType")
        or ""
    )
    quota["unit"] = primary.get("displayName") or primary.get("unit", "Credits")
    return quota


@router.get("/token-usage/models")
async def get_available_models(request: Request) -> Dict[str, Any]:
    """
    Return the list of models available for the current account.

    Fetches live from the Kiro ListAvailableModels API (q.{region}.amazonaws.com)
    using the same auth pattern as the quota endpoint. Falls back to the
    in-memory model cache if the live call fails.

    Returns:
        Dict with models list, count, and default_model
    """
    auth_manager = _get_auth_manager(request)

    # Try the live API first
    try:
        token = await auth_manager.get_access_token()
        region, headers, profile_arn = _build_kiro_api_context(auth_manager, token)
        url = f"https://q.{region}.amazonaws.com/ListAvailableModels"
        params: Dict[str, str] = {"origin": "AI_EDITOR", "maxResults": "50"}
        if profile_arn:
            params["profileArn"] = profile_arn

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params, headers=headers)

        if resp.status_code == 200:
            body = resp.json()
            raw_models = body.get("models", []) or []
            default_model = (body.get("defaultModel") or {}).get("modelId")
            models = [
                {"id": m.get("modelId"), "name": m.get("modelName") or m.get("modelId")}
                for m in raw_models if m.get("modelId")
            ]
            if models:
                logger.debug(f"[Models] Fetched {len(models)} models from live API")
                return {"models": models, "count": len(models), "default_model": default_model}
        else:
            logger.warning(
                f"[Models] Live ListAvailableModels returned {resp.status_code}: "
                f"{resp.text[:150]} — falling back to cache"
            )
    except Exception as exc:
        logger.warning(f"[Models] Live fetch failed ({exc}); falling back to cache")

    # Fallback: in-memory cache (static FALLBACK_MODELS for runtime endpoints)
    account_manager = getattr(request.app.state, "account_manager", None)
    models = []
    if account_manager:
        for account_id, account in account_manager._accounts.items():
            if account.model_cache:
                cached = account.model_cache.get_all_model_ids()
                models = [{"id": m, "name": m} for m in cached]
                break

    seen: set = set()
    unique = []
    for m in models:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)

    return {"models": unique, "count": len(unique), "default_model": None}


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
