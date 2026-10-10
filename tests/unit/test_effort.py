# -*- coding: utf-8 -*-

"""
Unit tests for kiro.effort and native effort wiring in the converters.
"""

import pytest

from kiro import converters_core
from kiro.converters_anthropic import anthropic_to_kiro, extract_thinking_config_from_anthropic
from kiro.converters_core import ThinkingConfig, UnifiedMessage, build_kiro_payload
from kiro.converters_openai import build_kiro_payload as openai_to_kiro
from kiro.converters_openai import extract_thinking_config_from_openai
from kiro.effort import build_additional_model_fields, clamp_effort, normalize_effort
from kiro.models_anthropic import AnthropicMessage, AnthropicMessagesRequest
from kiro.models_openai import ChatCompletionRequest, ChatMessage


class TestNormalizeEffort:
    """normalize_effort edge cases."""

    @pytest.mark.parametrize("value,expected", [
        ("low", "low"), ("HIGH", "high"), (" Medium ", "medium"),
        ("xhigh", "xhigh"), ("max", "max"), ("minimal", "low"),
    ])
    def test_valid_values(self, value, expected):
        """
        What it does: Normalizes valid and aliased effort strings.
        Purpose: Clients differ in casing and OpenAI uses "minimal".
        """
        assert normalize_effort(value) == expected

    @pytest.mark.parametrize("value", [None, "", "none", "extreme", 5, ["high"], {"effort": "high"}])
    def test_invalid_values_return_none(self, value):
        """
        What it does: Rejects unknown, empty and non-string values.
        Purpose: Never forward garbage to Kiro (would cause "Improperly formed request").
        """
        assert normalize_effort(value) is None


class TestClampEffort:
    """clamp_effort behavior per model."""

    def test_supported_level_is_kept(self):
        assert clamp_effort("claude-opus-4.7", "xhigh") == "xhigh"

    def test_xhigh_clamps_to_max_on_46(self):
        """
        What it does: xhigh on a model without xhigh moves up to max.
        Purpose: Match the model's supported enum.
        """
        assert clamp_effort("claude-opus-4.6", "xhigh") == "max"

    def test_1m_suffix_is_ignored(self):
        assert clamp_effort("claude-sonnet-4.6-1m", "xhigh") == "max"

    def test_unknown_model_returns_none(self):
        assert clamp_effort("deepseek-3.2", "high") is None
        assert clamp_effort("", "high") is None


class TestBuildAdditionalModelFields:
    """Payload fragment construction."""

    def test_none_effort_returns_none(self):
        assert build_additional_model_fields("claude-opus-4.7", None) is None

    def test_unsupported_model_returns_none(self):
        assert build_additional_model_fields("glm-5", "high") is None

    def test_supported_model_returns_fragment(self):
        assert build_additional_model_fields("claude-opus-4.7", "high") == {
            "output_config": {"effort": "high"}
        }


def _anthropic_request(**kwargs) -> AnthropicMessagesRequest:
    return AnthropicMessagesRequest(
        model=kwargs.pop("model", "claude-opus-4.7"),
        messages=[AnthropicMessage(role="user", content="hi")],
        max_tokens=4096,
        **kwargs,
    )


def _openai_request(**kwargs) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=kwargs.pop("model", "claude-opus-4.7"),
        messages=[ChatMessage(role="user", content="hi")],
        **kwargs,
    )


class TestAdapterExtraction:
    """Effort extraction from both API surfaces."""

    def test_anthropic_output_config_effort(self):
        config = extract_thinking_config_from_anthropic(_anthropic_request(output_config={"effort": "high"}))
        assert config.effort == "high"

    def test_anthropic_effort_kept_with_thinking_variants(self):
        for thinking in ({"type": "enabled", "budget_tokens": 2000}, {"type": "disabled"}, {"type": "x"}):
            config = extract_thinking_config_from_anthropic(
                _anthropic_request(thinking=thinking, output_config={"effort": "low"})
            )
            assert config.effort == "low"

    def test_anthropic_garbage_output_config(self):
        assert extract_thinking_config_from_anthropic(_anthropic_request(output_config={"effort": 3})).effort is None
        assert extract_thinking_config_from_anthropic(_anthropic_request()).effort is None

    def test_openai_reasoning_effort(self):
        assert extract_thinking_config_from_openai(_openai_request(reasoning_effort="xhigh")).effort == "xhigh"
        assert extract_thinking_config_from_openai(_openai_request(reasoning_effort="minimal")).effort == "low"

    def test_openai_none_and_missing_have_no_effort(self):
        assert extract_thinking_config_from_openai(_openai_request(reasoning_effort="none")).effort is None
        assert extract_thinking_config_from_openai(_openai_request()).effort is None


class TestPayloadIntegration:
    """additionalModelRequestFields in the final Kiro payload (shared by streaming and non-streaming)."""

    def test_anthropic_payload_contains_effort(self):
        payload = anthropic_to_kiro(_anthropic_request(output_config={"effort": "xhigh"}), "conv", "arn")
        assert payload["additionalModelRequestFields"] == {"output_config": {"effort": "xhigh"}}

    def test_openai_payload_contains_effort(self):
        payload = openai_to_kiro(_openai_request(reasoning_effort="high"), "conv", "arn")
        assert payload["additionalModelRequestFields"] == {"output_config": {"effort": "high"}}

    def test_no_effort_leaves_payload_unchanged(self):
        payload = anthropic_to_kiro(_anthropic_request(), "conv", "arn")
        assert "additionalModelRequestFields" not in payload

    def test_unsupported_model_omits_field(self):
        payload = anthropic_to_kiro(
            _anthropic_request(model="deepseek-3.2", output_config={"effort": "high"}), "conv", "arn"
        )
        assert "additionalModelRequestFields" not in payload

    def test_kill_switch_disables_field(self, monkeypatch):
        """
        What it does: NATIVE_EFFORT=false path leaves the payload without the field.
        Purpose: Users must be able to restore native pre-feature behavior.
        """
        monkeypatch.setattr(converters_core, "NATIVE_EFFORT_ENABLED", False)
        result = build_kiro_payload(
            messages=[UnifiedMessage(role="user", content="hi")],
            system_prompt="",
            model_id="claude-opus-4.7",
            tools=None,
            conversation_id="c",
            profile_arn="",
            thinking_config=ThinkingConfig(effort="high"),
        )
        assert "additionalModelRequestFields" not in result.payload
