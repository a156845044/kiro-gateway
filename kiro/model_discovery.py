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
Live model discovery for Kiro Gateway.

The newer `runtime.{region}.kiro.dev` host (used for chat/completions by most
current Kiro Desktop / IDE accounts) does not expose a `/ListAvailableModels`
API (see `account_manager._is_runtime_endpoint`). However, the legacy
`q.{region}.amazonaws.com` data plane still serves this endpoint for ALL
account types, including accounts whose inference traffic goes through
`runtime.kiro.dev`. Calling it directly gives an accurate, real-time view of
the models actually entitled to the account, instead of the static
`FALLBACK_MODELS` list bundled with the gateway.

This module centralizes that "always try the legacy discovery host" logic so
it is not duplicated between the usage dashboard (`routes_usage.py`) and the
OpenAI-compatible `/v1/models` endpoint (`routes_openai.py` / `account_manager.py`).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
from loguru import logger

from kiro.auth import AuthType, KiroAuthManager


def resolve_profile_arn(auth_manager: KiroAuthManager) -> Optional[str]:
    """
    Resolve the correct CodeWhisperer profileArn for API calls.

    Priority: .env PROFILE_ARN (read by absolute path, populated by
    get_profile_arn.py) -> config.PROFILE_ARN -> auth_manager.profile_arn.
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
    for candidate in candidates:
        if candidate and candidate.startswith("arn:aws:"):
            return candidate
    return next((candidate for candidate in candidates if candidate), None)


def build_kiro_api_context(auth_manager: KiroAuthManager, token: str) -> Tuple[str, Dict[str, str], Optional[str]]:
    """
    Build the region, headers, and profileArn for a q.amazonaws.com call.

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
    api_host = getattr(auth_manager, "api_host", "") or ""
    match = re.search(r"(?:runtime|q)\.([^.]+)\.(kiro\.dev|amazonaws\.com)", api_host)
    region = match.group(1) if match else "us-east-1"

    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        # Full AWS-SDK-style headers are REQUIRED — a bare request gets 403.
        "User-Agent": (
            "aws-sdk-js/1.0.0 ua/2.1 os/windows lang/js "
            "api/codewhispererruntime#1.0.0 m/N,E KiroIDE"
        ),
        "x-amz-user-agent": "aws-sdk-js/1.0.0 KiroIDE",
        "amz-sdk-invocation-id": str(uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=3",
    }
    if getattr(auth_manager, "auth_type", None) == AuthType.EXTERNAL_IDP:
        headers["TokenType"] = "EXTERNAL_IDP"

    profile_arn = resolve_profile_arn(auth_manager)
    return region, headers, profile_arn


async def fetch_live_models(auth_manager: KiroAuthManager) -> Optional[Dict[str, Any]]:
    """
    Live-fetch the current model catalog from q.{region}.amazonaws.com/ListAvailableModels.

    This works regardless of whether the account's inference traffic normally
    goes through runtime.{region}.kiro.dev or q.{region}.amazonaws.com — the
    discovery endpoint is hosted separately and reflects real-time account
    entitlements. Never raises; any failure (auth error, network error,
    non-200, empty payload) results in None so callers can fall back to their
    existing cached/static model list.

    Args:
        auth_manager: The active KiroAuthManager for the account to query

    Returns:
        Parsed JSON body from Kiro (with "models" and optionally
        "defaultModel" keys) on success, or None if the live call failed or
        returned no models.
    """
    try:
        token = await auth_manager.get_access_token()
        region, headers, profile_arn = build_kiro_api_context(auth_manager, token)
        url = f"https://q.{region}.amazonaws.com/ListAvailableModels"
        params: Dict[str, str] = {"origin": "AI_EDITOR", "maxResults": "50"}
        if profile_arn:
            params["profileArn"] = profile_arn

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params, headers=headers)

        if response.status_code != 200:
            logger.warning(
                f"[ModelDiscovery] Live ListAvailableModels returned {response.status_code}: "
                f"{response.text[:150]}"
            )
            return None

        body = response.json()
        if not body.get("models"):
            logger.debug("[ModelDiscovery] Live fetch returned no models")
            return None

        logger.debug(f"[ModelDiscovery] Live fetch returned {len(body['models'])} models")
        return body

    except Exception as exc:
        logger.warning(f"[ModelDiscovery] Live fetch failed: {exc}")
        return None
