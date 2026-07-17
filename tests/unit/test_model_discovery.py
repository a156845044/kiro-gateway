# -*- coding: utf-8 -*-

"""
Unit tests for kiro/model_discovery.py - Live model discovery.

Tests for:
- resolve_profile_arn(): profileArn resolution priority chain
- build_kiro_api_context(): region extraction, headers, EXTERNAL_IDP handling
- fetch_live_models(): live ListAvailableModels call with graceful fallback
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from kiro.auth import AuthType
from kiro.model_discovery import (
    build_kiro_api_context,
    fetch_live_models,
    resolve_profile_arn,
)


class TestResolveProfileArn:
    """Tests for resolve_profile_arn()."""

    def test_prefers_env_profile_arn_over_auth_manager(self):
        """
        What it does: Verifies .env PROFILE_ARN wins over auth_manager.profile_arn.
        Purpose: Ensure the documented priority order (.env > config > auth_manager)
        is honored so users can always override via .env.
        """
        print("\n=== Test: .env PROFILE_ARN takes priority ===")
        auth_manager = Mock()
        auth_manager.profile_arn = "arn:aws:codewhisperer:us-east-1:999:profile/from-auth-manager"

        with patch("kiro.config._get_raw_env_value", return_value="arn:aws:codewhisperer:us-east-1:111:profile/from-env"), \
             patch("kiro.config.PROFILE_ARN", None):
            result = resolve_profile_arn(auth_manager)

        print(f"Resolved: {result}")
        assert result == "arn:aws:codewhisperer:us-east-1:111:profile/from-env"

    def test_falls_back_to_auth_manager_profile_arn(self):
        """
        What it does: Verifies auth_manager.profile_arn is used when no env/config ARN exists.
        Purpose: Ensure accounts without an explicit PROFILE_ARN override still resolve one.
        """
        print("\n=== Test: fallback to auth_manager.profile_arn ===")
        auth_manager = Mock()
        auth_manager.profile_arn = "arn:aws:codewhisperer:us-east-1:999:profile/from-auth-manager"

        with patch("kiro.config._get_raw_env_value", return_value=None), \
             patch("kiro.config.PROFILE_ARN", None):
            result = resolve_profile_arn(auth_manager)

        print(f"Resolved: {result}")
        assert result == "arn:aws:codewhisperer:us-east-1:999:profile/from-auth-manager"

    def test_returns_first_truthy_candidate_when_none_look_like_an_arn(self):
        """
        What it does: Verifies a non-ARN candidate (e.g. a raw clientId UUID) is still
        returned as a last resort when it's the only candidate available.
        Purpose: Document existing behavior - the function prefers a real 'arn:aws:'
        value but degrades to "best guess" rather than silently returning None.
        """
        print("\n=== Test: non-ARN fallback candidate ===")
        auth_manager = Mock()
        auth_manager.profile_arn = None

        with patch("kiro.config._get_raw_env_value", return_value="not-an-arn-uuid"), \
             patch("kiro.config.PROFILE_ARN", None):
            result = resolve_profile_arn(auth_manager)

        print(f"Resolved: {result}")
        assert result == "not-an-arn-uuid"

    def test_returns_none_when_no_candidates_exist(self):
        """
        What it does: Verifies None is returned when no ARN candidate exists anywhere.
        Purpose: Ensure callers can safely omit profileArn from request params.
        """
        print("\n=== Test: no candidates at all ===")
        auth_manager = Mock()
        auth_manager.profile_arn = None

        with patch("kiro.config._get_raw_env_value", return_value=None), \
             patch("kiro.config.PROFILE_ARN", None):
            result = resolve_profile_arn(auth_manager)

        print(f"Resolved: {result}")
        assert result is None


class TestBuildKiroApiContext:
    """Tests for build_kiro_api_context()."""

    def test_extracts_region_from_runtime_host(self):
        """
        What it does: Verifies region is extracted from a runtime.kiro.dev host.
        Purpose: Ensure accounts using the newer inference host still resolve the
        correct AWS region for the q.amazonaws.com discovery call.
        """
        print("\n=== Test: region extraction from runtime.kiro.dev ===")
        auth_manager = Mock()
        auth_manager.api_host = "https://runtime.eu-central-1.kiro.dev"
        auth_manager.auth_type = AuthType.KIRO_DESKTOP

        with patch("kiro.model_discovery.resolve_profile_arn", return_value=None):
            region, headers, _ = build_kiro_api_context(auth_manager, "test-token")

        print(f"Region: {region}, headers: {headers}")
        assert region == "eu-central-1"
        assert headers["Authorization"] == "Bearer test-token"

    def test_extracts_region_from_legacy_q_host(self):
        """
        What it does: Verifies region is extracted from a legacy q.amazonaws.com host.
        Purpose: Ensure backward compatibility with pre-runtime accounts.
        """
        print("\n=== Test: region extraction from q.amazonaws.com ===")
        auth_manager = Mock()
        auth_manager.api_host = "https://q.us-west-2.amazonaws.com"
        auth_manager.auth_type = AuthType.KIRO_DESKTOP

        with patch("kiro.model_discovery.resolve_profile_arn", return_value=None):
            region, _, _ = build_kiro_api_context(auth_manager, "test-token")

        print(f"Region: {region}")
        assert region == "us-west-2"

    def test_defaults_to_us_east_1_when_host_unrecognized(self):
        """
        What it does: Verifies a fallback region is used for an unparseable host.
        Purpose: Ensure the function never raises on unexpected/blank api_host values.
        """
        print("\n=== Test: default region fallback ===")
        auth_manager = Mock()
        auth_manager.api_host = ""
        auth_manager.auth_type = AuthType.KIRO_DESKTOP

        with patch("kiro.model_discovery.resolve_profile_arn", return_value=None):
            region, _, _ = build_kiro_api_context(auth_manager, "test-token")

        print(f"Region: {region}")
        assert region == "us-east-1"

    def test_adds_token_type_header_for_external_idp(self):
        """
        What it does: Verifies the TokenType header is added for EXTERNAL_IDP accounts.
        Purpose: External IdP (Microsoft SSO) accounts return 400/403 without this header.
        """
        print("\n=== Test: TokenType header for EXTERNAL_IDP ===")
        auth_manager = Mock()
        auth_manager.api_host = "https://runtime.us-east-1.kiro.dev"
        auth_manager.auth_type = AuthType.EXTERNAL_IDP

        with patch("kiro.model_discovery.resolve_profile_arn", return_value=None):
            _, headers, _ = build_kiro_api_context(auth_manager, "test-token")

        print(f"Headers: {headers}")
        assert headers["TokenType"] == "EXTERNAL_IDP"

    def test_omits_token_type_header_for_kiro_desktop(self):
        """
        What it does: Verifies the TokenType header is absent for KIRO_DESKTOP accounts.
        Purpose: Ensure the header is only added when actually required.
        """
        print("\n=== Test: no TokenType header for KIRO_DESKTOP ===")
        auth_manager = Mock()
        auth_manager.api_host = "https://runtime.us-east-1.kiro.dev"
        auth_manager.auth_type = AuthType.KIRO_DESKTOP

        with patch("kiro.model_discovery.resolve_profile_arn", return_value=None):
            _, headers, _ = build_kiro_api_context(auth_manager, "test-token")

        print(f"Headers: {headers}")
        assert "TokenType" not in headers

    def test_includes_resolved_profile_arn(self):
        """
        What it does: Verifies the resolved profileArn is returned alongside region/headers.
        Purpose: Ensure callers can attach profileArn to request params when present.
        """
        print("\n=== Test: profile_arn passthrough ===")
        auth_manager = Mock()
        auth_manager.api_host = "https://runtime.us-east-1.kiro.dev"
        auth_manager.auth_type = AuthType.KIRO_DESKTOP

        with patch("kiro.model_discovery.resolve_profile_arn", return_value="arn:aws:codewhisperer:us-east-1:123:profile/x"):
            _, _, profile_arn = build_kiro_api_context(auth_manager, "test-token")

        print(f"Profile ARN: {profile_arn}")
        assert profile_arn == "arn:aws:codewhisperer:us-east-1:123:profile/x"


class TestFetchLiveModels:
    """Tests for fetch_live_models()."""

    @staticmethod
    def _make_auth_manager(token="test-token", api_host="https://runtime.us-east-1.kiro.dev"):
        auth_manager = Mock()
        auth_manager.get_access_token = AsyncMock(return_value=token)
        auth_manager.api_host = api_host
        auth_manager.auth_type = AuthType.KIRO_DESKTOP
        auth_manager.profile_arn = None
        return auth_manager

    @staticmethod
    def _make_http_client(response):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    @pytest.mark.asyncio
    async def test_returns_body_on_success(self):
        """
        What it does: Verifies the parsed JSON body is returned on a 200 response with models.
        Purpose: Ensure the happy path surfaces real-time model data (e.g. brand new
        models like gpt-5.6-sol) to callers instead of the static fallback list.
        """
        print("\n=== Test: fetch_live_models() success ===")
        auth_manager = self._make_auth_manager()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "models": [{"modelId": "gpt-5.6-sol", "modelName": "GPT 5.6 Sol"}],
            "defaultModel": {"modelId": "auto"},
        }
        mock_client = self._make_http_client(mock_response)

        with patch("kiro.model_discovery.httpx.AsyncClient", return_value=mock_client):
            body = await fetch_live_models(auth_manager)

        print(f"Body: {body}")
        assert body is not None
        assert body["models"][0]["modelId"] == "gpt-5.6-sol"
        assert body["defaultModel"]["modelId"] == "auto"

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200_status(self):
        """
        What it does: Verifies None is returned when Kiro responds with a non-200 status.
        Purpose: Ensure callers fall back to their cached/static model list on API errors
        (e.g. transient 403/500 from the discovery endpoint) instead of crashing.
        """
        print("\n=== Test: fetch_live_models() non-200 ===")
        auth_manager = self._make_auth_manager()

        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.text = "subscription does not support this application"
        mock_client = self._make_http_client(mock_response)

        with patch("kiro.model_discovery.httpx.AsyncClient", return_value=mock_client):
            body = await fetch_live_models(auth_manager)

        print(f"Body: {body}")
        assert body is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_models_list(self):
        """
        What it does: Verifies None is returned when the response has an empty/missing models list.
        Purpose: Ensure an empty catalog doesn't get treated as "success" and wipe out an
        account's previously cached models.
        """
        print("\n=== Test: fetch_live_models() empty models ===")
        auth_manager = self._make_auth_manager()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"models": []}
        mock_client = self._make_http_client(mock_response)

        with patch("kiro.model_discovery.httpx.AsyncClient", return_value=mock_client):
            body = await fetch_live_models(auth_manager)

        print(f"Body: {body}")
        assert body is None

    @pytest.mark.asyncio
    async def test_returns_none_when_token_fetch_raises(self):
        """
        What it does: Verifies None is returned (not an exception) when getting the
        access token fails.
        Purpose: fetch_live_models() must never raise - callers rely on graceful
        fallback to the existing cache.
        """
        print("\n=== Test: fetch_live_models() token error ===")
        auth_manager = Mock()
        auth_manager.get_access_token = AsyncMock(side_effect=RuntimeError("token refresh failed"))

        body = await fetch_live_models(auth_manager)

        print(f"Body: {body}")
        assert body is None

    @pytest.mark.asyncio
    async def test_returns_none_when_http_call_raises(self):
        """
        What it does: Verifies None is returned when the underlying HTTP call raises
        (e.g. connection error/timeout).
        Purpose: Ensure network failures degrade gracefully instead of propagating.
        """
        print("\n=== Test: fetch_live_models() network error ===")
        auth_manager = self._make_auth_manager()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("network unreachable"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("kiro.model_discovery.httpx.AsyncClient", return_value=mock_client):
            body = await fetch_live_models(auth_manager)

        print(f"Body: {body}")
        assert body is None

    @pytest.mark.asyncio
    async def test_includes_profile_arn_in_request_params_when_present(self):
        """
        What it does: Verifies profileArn is included in query params when resolvable.
        Purpose: Ensure Kiro Desktop accounts with a profileArn send it, avoiding the
        400 'Invalid ARN' errors this endpoint returns without it.
        """
        print("\n=== Test: fetch_live_models() includes profileArn ===")
        auth_manager = self._make_auth_manager()
        auth_manager.profile_arn = "arn:aws:codewhisperer:us-east-1:123:profile/x"

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"models": [{"modelId": "auto"}]}
        mock_client = self._make_http_client(mock_response)

        with patch("kiro.model_discovery.httpx.AsyncClient", return_value=mock_client), \
             patch("kiro.config._get_raw_env_value", return_value=None), \
             patch("kiro.config.PROFILE_ARN", None):
            await fetch_live_models(auth_manager)

        call_kwargs = mock_client.get.call_args.kwargs
        print(f"Call params: {call_kwargs.get('params')}")
        assert call_kwargs["params"]["profileArn"] == "arn:aws:codewhisperer:us-east-1:123:profile/x"

    @pytest.mark.asyncio
    async def test_requests_legacy_q_host_even_for_runtime_accounts(self):
        """
        What it does: Verifies the request URL always targets q.{region}.amazonaws.com,
        even when the account's own api_host is runtime.{region}.kiro.dev.
        Purpose: This is the whole point of the module - runtime.kiro.dev accounts don't
        expose /ListAvailableModels, but the legacy q.amazonaws.com discovery host does.
        """
        print("\n=== Test: fetch_live_models() targets legacy q host ===")
        auth_manager = self._make_auth_manager(api_host="https://runtime.us-east-1.kiro.dev")

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"models": [{"modelId": "auto"}]}
        mock_client = self._make_http_client(mock_response)

        with patch("kiro.model_discovery.httpx.AsyncClient", return_value=mock_client):
            await fetch_live_models(auth_manager)

        call_args = mock_client.get.call_args
        print(f"Call args: {call_args}")
        requested_url = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
        assert requested_url == "https://q.us-east-1.amazonaws.com/ListAvailableModels"
