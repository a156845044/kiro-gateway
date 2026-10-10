# -*- coding: utf-8 -*-

"""
Native effort support for Kiro API.

Kiro accepts a model effort level in the request root under
`additionalModelRequestFields.output_config.effort` (Claude family).
This module normalizes the effort requested by the client (Anthropic
`output_config.effort` or OpenAI `reasoning_effort`), clamps it to the levels
the target model supports, and builds the payload fragment.

The field is only sent when the client explicitly requested an effort, so
requests without it are unchanged.
"""

from typing import Any, Dict, Optional, Tuple

from loguru import logger

# Effort levels ordered from lowest to highest.
EFFORT_ORDER: Tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

_FULL_LEVELS: Tuple[str, ...] = EFFORT_ORDER
_NO_XHIGH_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "max")

# Models known to support native effort (Kiro model IDs after normalization).
# shortcut: static table, models outside it omit the field; extend when new models ship.
MODEL_EFFORT_LEVELS: Dict[str, Tuple[str, ...]] = {
    "claude-opus-5.5": _FULL_LEVELS,
    "claude-sonnet-5.5": _FULL_LEVELS,
    "claude-opus-5": _FULL_LEVELS,
    "claude-sonnet-5": _FULL_LEVELS,
    "claude-fable-5.1": _FULL_LEVELS,
    "claude-opus-4.8": _FULL_LEVELS,
    "claude-opus-4.7": _FULL_LEVELS,
    "claude-opus-4.6": _NO_XHIGH_LEVELS,
    "claude-sonnet-4.6": _NO_XHIGH_LEVELS,
}

# OpenAI reasoning_effort values that differ from Kiro effort names.
_OPENAI_EFFORT_ALIASES: Dict[str, str] = {"minimal": "low"}


def normalize_effort(value: Any) -> Optional[str]:
    """
    Normalize a client-supplied effort value.

    Args:
        value: Raw value (any type) from the client request

    Returns:
        Lowercase effort in EFFORT_ORDER, or None if missing/unrecognized
    """
    if not isinstance(value, str):
        return None
    effort = value.strip().lower()
    effort = _OPENAI_EFFORT_ALIASES.get(effort, effort)
    return effort if effort in EFFORT_ORDER else None


def clamp_effort(model_id: str, effort: str) -> Optional[str]:
    """
    Clamp an effort level to the levels supported by the model.

    An unsupported level is replaced by the nearest supported level at or
    above it, else the highest supported one (e.g. xhigh -> max).

    Args:
        model_id: Normalized Kiro model ID (an optional "-1m" suffix is ignored)
        effort: Normalized effort from normalize_effort()

    Returns:
        Supported effort, or None if the model has no native effort support
    """
    levels = MODEL_EFFORT_LEVELS.get(model_id.removesuffix("-1m"))
    if not levels:
        return None
    if effort in levels:
        return effort
    for candidate in EFFORT_ORDER[EFFORT_ORDER.index(effort):]:
        if candidate in levels:
            return candidate
    return levels[-1]


def build_additional_model_fields(model_id: str, effort: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Build the `additionalModelRequestFields` payload fragment.

    Args:
        model_id: Normalized Kiro model ID
        effort: Normalized effort requested by the client (or None)

    Returns:
        Fragment for the payload root, or None when nothing should be sent
    """
    if not effort:
        return None
    resolved = clamp_effort(model_id, effort)
    if not resolved:
        logger.debug(f"Model {model_id} has no native effort support, ignoring effort='{effort}'")
        return None
    if resolved != effort:
        logger.info(f"Effort '{effort}' clamped to '{resolved}' for model {model_id}")
    return {"output_config": {"effort": resolved}}
