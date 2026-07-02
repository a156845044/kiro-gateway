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
Core streaming logic for parsing Kiro API responses.

This module contains shared logic used by both OpenAI and Anthropic streaming:
- KiroEvent dataclass for unified events
- Kiro SSE stream parsing
- Full response collection
- First token timeout handling

The core layer provides a unified interface that API-specific formatters use
to convert Kiro events to their respective SSE formats.
"""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Awaitable, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from kiro.parsers import AwsEventStreamParser, parse_bracket_tool_calls, deduplicate_tool_calls
from kiro.config import (
    FIRST_TOKEN_TIMEOUT,
    FIRST_TOKEN_MAX_RETRIES,
    HEARTBEAT_INTERVAL,
    FAKE_REASONING_ENABLED,
    FAKE_REASONING_HANDLING,
)
from kiro.thinking_parser import ThinkingParser

if TYPE_CHECKING:
    from kiro.cache import ModelInfoCache

# Import debug_logger for logging
try:
    from kiro.debug_logger import debug_logger
except ImportError:
    debug_logger = None


# ==================================================================================================
# Data Classes
# ==================================================================================================

@dataclass
class KiroEvent:
    """
    Unified event from Kiro API stream.
    
    This format is API-agnostic and can be converted to both OpenAI and Anthropic formats.
    
    Attributes:
        type: Event type (content, thinking, tool_use, usage, context_usage, error)
        content: Text content (for content events)
        thinking_content: Thinking/reasoning content (for thinking events)
        tool_use: Tool use data (for tool_use events)
        usage: Usage/metering data (for usage events)
        context_usage_percentage: Context usage percentage (for context_usage events)
        is_first_thinking_chunk: Whether this is the first thinking chunk
        is_last_thinking_chunk: Whether this is the last thinking chunk
    """
    type: str
    content: Optional[str] = None
    thinking_content: Optional[str] = None
    tool_use: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None
    context_usage_percentage: Optional[float] = None
    is_first_thinking_chunk: bool = False
    is_last_thinking_chunk: bool = False


@dataclass
class StreamResult:
    """
    Result of collecting a complete stream response.
    
    Attributes:
        content: Full text content
        thinking_content: Full thinking/reasoning content
        tool_calls: List of tool calls
        usage: Usage information
        context_usage_percentage: Context usage percentage from Kiro API
    """
    content: str = ""
    thinking_content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None
    context_usage_percentage: Optional[float] = None


class FirstTokenTimeoutError(Exception):
    """Exception raised when first token timeout occurs."""
    pass


# ==================================================================================================
# Kiro Stream Parsing
# ==================================================================================================

async def _iter_bytes_with_heartbeat(
    byte_iterator,
    heartbeat_interval: float,
) -> AsyncGenerator[Optional[bytes], None]:
    """
    Wraps an async bytes iterator and injects heartbeat signals (None) when no data
    arrives within heartbeat_interval seconds.

    This lets callers detect idle periods and send keepalive events to downstream
    clients without blocking or cancelling the underlying I/O task.

    Yields:
        bytes: An actual chunk of data from the iterator.
        None:  A heartbeat signal - no data received for heartbeat_interval seconds.
    """
    while True:
        task = asyncio.ensure_future(byte_iterator.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=heartbeat_interval)
                if done:
                    try:
                        result = task.result()
                    except StopAsyncIteration:
                        return  # Stream ended normally
                    yield result
                    break  # Create task for next chunk
                else:
                    yield None  # Heartbeat: no data yet
        except BaseException:
            if not task.done():
                task.cancel()
            raise


async def parse_kiro_stream(
    response: httpx.Response,
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
    enable_thinking_parser: bool = True
) -> AsyncGenerator[KiroEvent, None]:
    """
    Parses Kiro SSE stream and yields unified events.

    This is the core parsing function that converts Kiro's AWS binary Event Stream
    format into unified KiroEvent objects for any API format.

    Heartbeat strategy:
        Kiro always sends an ``initial-response`` frame first (containing only a
        ``conversationId``), which arrives almost immediately but carries no content.
        The model then "thinks" silently for several seconds before the first
        ``assistantResponseEvent`` arrives.  Without heartbeats the downstream client
        (Claude Code, Cursor, Codex, …) would see an idle SSE connection and
        time-out.

        We use ``_iter_bytes_with_heartbeat`` for **every** chunk read — not just
        the first — so keepalive events are forwarded to the client throughout the
        entire silent period, regardless of whether that silence comes before or
        after the ``initial-response`` frame.

    Args:
        response: HTTP response with data stream
        first_token_timeout: Maximum seconds to wait for the very first byte from
            Kiro before raising FirstTokenTimeoutError and triggering a retry.
        enable_thinking_parser: Whether to enable thinking block parsing.

    Yields:
        KiroEvent objects representing stream events.
        KiroEvent(type="heartbeat") is yielded during idle periods.

    Raises:
        FirstTokenTimeoutError: If no byte is received within first_token_timeout.
    """
    parser = AwsEventStreamParser()

    # Initialize thinking parser if fake reasoning is enabled
    thinking_parser: Optional[ThinkingParser] = None
    if FAKE_REASONING_ENABLED and enable_thinking_parser:
        thinking_parser = ThinkingParser(handling_mode=FAKE_REASONING_HANDLING)
        logger.debug(f"Thinking parser initialized with mode: {FAKE_REASONING_HANDLING}")

    heartbeat_interval = HEARTBEAT_INTERVAL if HEARTBEAT_INTERVAL > 0 else first_token_timeout
    logger.debug(
        f"Starting Kiro stream (first_token_timeout={first_token_timeout}s, "
        f"heartbeat_interval={heartbeat_interval}s)..."
    )

    byte_iterator = response.aiter_bytes()
    first_byte_received = False
    # Accumulates idle time before the first byte to enforce first_token_timeout.
    # After the first byte arrives this counter is no longer checked for timeout,
    # but continues to increment so debug logs show idle duration between chunks.
    time_without_data = 0.0

    try:
        async for chunk_or_none in _iter_bytes_with_heartbeat(byte_iterator, heartbeat_interval):
            if chunk_or_none is None:
                # No bytes from Kiro for heartbeat_interval seconds.
                time_without_data += heartbeat_interval
                if not first_byte_received and time_without_data >= first_token_timeout:
                    logger.warning(
                        f"[FirstTokenTimeout] Model did not respond within {first_token_timeout}s"
                    )
                    raise FirstTokenTimeoutError(
                        f"No response within {first_token_timeout} seconds"
                    )
                logger.debug(
                    f"No data for {time_without_data:.0f}s, sending heartbeat to client"
                )
                yield KiroEvent(type="heartbeat")
                continue

            # Received actual bytes from Kiro.
            chunk: bytes = chunk_or_none
            if not first_byte_received:
                first_byte_received = True
                logger.debug("First byte received from Kiro")
            time_without_data = 0.0  # Reset idle counter on every received chunk

            if debug_logger:
                debug_logger.log_raw_chunk(chunk)

            async for event in _process_chunk(parser, chunk, thinking_parser):
                yield event

        # Iterator exhausted — stream ended normally.
        if not first_byte_received:
            logger.debug("Empty response from Kiro API")
            return

        # Finalize thinking parser and yield any remaining content
        if thinking_parser:
            final_result = thinking_parser.finalize()

            if final_result.thinking_content:
                processed_thinking = thinking_parser.process_for_output(
                    final_result.thinking_content,
                    final_result.is_first_thinking_chunk,
                    final_result.is_last_thinking_chunk,
                )
                if processed_thinking:
                    yield KiroEvent(
                        type="thinking",
                        thinking_content=processed_thinking,
                        is_first_thinking_chunk=final_result.is_first_thinking_chunk,
                        is_last_thinking_chunk=final_result.is_last_thinking_chunk,
                    )

            if final_result.regular_content:
                yield KiroEvent(type="content", content=final_result.regular_content)

            if thinking_parser.found_thinking_block:
                logger.debug("Thinking block processing completed")

        # Yield structured tool calls accumulated by the parser
        for tc in parser.get_tool_calls():
            yield KiroEvent(type="tool_use", tool_use=tc)

    except FirstTokenTimeoutError:
        raise
    except GeneratorExit:
        logger.debug("Client disconnected (GeneratorExit)")
        raise
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e) if str(e) else "(empty message)"
        logger.error(f"Error during stream parsing: [{error_type}] {error_msg}", exc_info=True)
        raise


async def _process_chunk(
    parser: AwsEventStreamParser,
    chunk: bytes,
    thinking_parser: Optional[ThinkingParser]
) -> AsyncGenerator[KiroEvent, None]:
    """
    Process a single chunk from Kiro stream.
    
    Args:
        parser: AWS event stream parser
        chunk: Raw bytes chunk
        thinking_parser: Optional thinking parser for fake reasoning
    
    Yields:
        KiroEvent objects
    """
    events = parser.feed(chunk)
    
    for event in events:
        if event["type"] == "content":
            content = event["data"]
            
            # Process through thinking parser if enabled
            if thinking_parser:
                parse_result = thinking_parser.feed(content)
                
                # Yield thinking content if any
                if parse_result.thinking_content:
                    processed_thinking = thinking_parser.process_for_output(
                        parse_result.thinking_content,
                        parse_result.is_first_thinking_chunk,
                        parse_result.is_last_thinking_chunk,
                    )
                    if processed_thinking:
                        yield KiroEvent(
                            type="thinking",
                            thinking_content=processed_thinking,
                            is_first_thinking_chunk=parse_result.is_first_thinking_chunk,
                            is_last_thinking_chunk=parse_result.is_last_thinking_chunk,
                        )
                
                # Yield regular content if any
                if parse_result.regular_content:
                    yield KiroEvent(type="content", content=parse_result.regular_content)
            else:
                # No thinking parser - pass through as-is
                yield KiroEvent(type="content", content=content)
        
        elif event["type"] == "usage":
            yield KiroEvent(type="usage", usage=event["data"])
        
        elif event["type"] == "context_usage":
            yield KiroEvent(type="context_usage", context_usage_percentage=event["data"])


# ==================================================================================================
# Full Response Collection
# ==================================================================================================

async def collect_stream_to_result(
    response: httpx.Response,
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
    enable_thinking_parser: bool = True
) -> StreamResult:
    """
    Collects full response from Kiro stream.
    
    This function consumes the entire stream and returns a StreamResult
    with all accumulated data.
    
    Args:
        response: HTTP response with stream
        first_token_timeout: First token wait timeout
        enable_thinking_parser: Whether to enable thinking block parsing
    
    Returns:
        StreamResult with full content, thinking, tool calls, and usage
    """
    result = StreamResult()
    full_content_for_bracket_tools = ""
    
    async for event in parse_kiro_stream(response, first_token_timeout, enable_thinking_parser):
        if event.type == "heartbeat":
            # Non-streaming collection: no client to ping, just skip
            continue
        elif event.type == "content" and event.content:
            result.content += event.content
            full_content_for_bracket_tools += event.content
        elif event.type == "thinking" and event.thinking_content:
            result.thinking_content += event.thinking_content
            full_content_for_bracket_tools += event.thinking_content
        elif event.type == "tool_use" and event.tool_use:
            result.tool_calls.append(event.tool_use)
        elif event.type == "usage" and event.usage:
            result.usage = event.usage
        elif event.type == "context_usage" and event.context_usage_percentage is not None:
            result.context_usage_percentage = event.context_usage_percentage
    
    # Check for bracket-style tool calls in full content
    bracket_tool_calls = parse_bracket_tool_calls(full_content_for_bracket_tools)
    if bracket_tool_calls:
        result.tool_calls = deduplicate_tool_calls(result.tool_calls + bracket_tool_calls)
    
    return result


# ==================================================================================================
# Token Counting Utilities
# ==================================================================================================

def calculate_tokens_from_context_usage(
    context_usage_percentage: Optional[float],
    completion_tokens: int,
    model_cache: "ModelInfoCache",
    model: str
) -> Tuple[int, int, str, str]:
    """
    Calculate token counts from Kiro's context usage percentage.
    
    Args:
        context_usage_percentage: Context usage percentage from Kiro API
        completion_tokens: Number of completion tokens (counted via tiktoken)
        model_cache: Model cache for getting max input tokens
        model: Model name
    
    Returns:
        Tuple of (prompt_tokens, total_tokens, prompt_source, total_source)
    """
    if context_usage_percentage is not None and context_usage_percentage > 0:
        max_input_tokens = model_cache.get_max_input_tokens(model)
        total_tokens = int((context_usage_percentage / 100) * max_input_tokens)
        prompt_tokens = max(0, total_tokens - completion_tokens)
        return prompt_tokens, total_tokens, "subtraction", "API Kiro"
    
    # Fallback: no context usage data
    return 0, completion_tokens, "unknown", "tiktoken"


# ==================================================================================================
# First Token Retry Logic
# ==================================================================================================

async def stream_with_first_token_retry(
    make_request: Callable[[], Awaitable[httpx.Response]],
    stream_processor: Callable[[httpx.Response], AsyncGenerator[str, None]],
    initial_response: Optional[httpx.Response] = None,
    max_retries: int = FIRST_TOKEN_MAX_RETRIES,
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
    on_http_error: Optional[Callable[[int, str], Exception]] = None,
    on_all_retries_failed: Optional[Callable[[int, float], Exception]] = None,
) -> AsyncGenerator[str, None]:
    """
    Generic streaming with automatic retry on first token timeout.
    
    If model doesn't respond within first_token_timeout seconds,
    request is cancelled and a new one is made. Maximum max_retries attempts.
    
    This is seamless for user - they just see a delay,
    but eventually get a response (or error after all attempts).
    
    Args:
        make_request: Function to create new HTTP request (returns httpx.Response)
        stream_processor: Function that processes response and yields SSE strings.
                         Must use parse_kiro_stream internally for timeout handling.
        initial_response: Optional pre-validated response to use on first attempt.
                         If provided, make_request is only called on retries.
                         This allows reusing an already-opened HTTP 200 response.
        max_retries: Maximum number of attempts
        first_token_timeout: First token wait timeout (seconds)
        on_http_error: Optional callback to create exception for HTTP errors.
                      Receives (status_code, error_text), returns Exception.
                      If None, raises generic Exception.
        on_all_retries_failed: Optional callback to create exception when all retries fail.
                              Receives (max_retries, timeout), returns Exception.
                              If None, raises generic Exception.
    
    Yields:
        Strings in SSE format (format depends on stream_processor)
    
    Raises:
        Exception from on_http_error or on_all_retries_failed callbacks
    
    Example:
        >>> async def make_req():
        ...     return await http_client.request_with_retry("POST", url, payload, stream=True)
        >>> async def process(response):
        ...     async for chunk in stream_kiro_to_openai(response, ...):
        ...         yield chunk
        >>> # With initial response (reuse already-validated 200 response)
        >>> response = await make_req()
        >>> async for chunk in stream_with_first_token_retry(make_req, process, initial_response=response):
        ...     print(chunk)
    """
    last_error: Optional[Exception] = None
    
    for attempt in range(max_retries):
        response: Optional[httpx.Response] = None
        try:
            # Make request
            if attempt > 0:
                logger.warning(f"Retry attempt {attempt + 1}/{max_retries} after first token timeout")
            
            # On first attempt, reuse initial_response if provided
            if attempt == 0 and initial_response is not None:
                response = initial_response
                logger.debug("Reusing initial response for first attempt")
            else:
                response = await make_request()
            
            if response.status_code != 200:
                # Error from API - close response and raise exception
                try:
                    error_content = await response.aread()
                    error_text = error_content.decode('utf-8', errors='replace')
                except Exception:
                    error_text = "Unknown error"
                
                try:
                    await response.aclose()
                except Exception:
                    pass
                
                logger.error(f"Error from Kiro API: {response.status_code} - {error_text}")
                
                if on_http_error:
                    raise on_http_error(response.status_code, error_text)
                else:
                    raise Exception(f"Upstream API error ({response.status_code}): {error_text}")
            
            # Try to stream with first token timeout
            async for chunk in stream_processor(response):
                yield chunk
            
            # Successfully completed - exit
            return
            
        except FirstTokenTimeoutError as e:
            last_error = e
            logger.warning(
                f"[FirstTokenTimeout] Attempt {attempt + 1}/{max_retries} failed - "
                f"model did not respond within {first_token_timeout}s"
            )
            
            # Close current response if open
            if response:
                try:
                    await response.aclose()
                except Exception:
                    pass
            
            # Continue to next attempt
            continue
            
        except Exception as e:
            # Other errors - no retry, propagate
            # Use positional argument to avoid loguru interpreting curly braces in error message as format placeholders
            # f-string with repr() doesn't work because loguru still sees {type} inside the string
            error_msg = str(e) if str(e) else "(empty message)"
            logger.error("Unexpected error during streaming: {}", error_msg, exc_info=True)
            if response:
                try:
                    await response.aclose()
                except Exception:
                    pass
            raise
    
    # All attempts exhausted - raise error
    logger.error(
        f"[FirstTokenTimeout] All {max_retries} attempts exhausted - "
        f"model never responded within {first_token_timeout}s per attempt"
    )
    
    if on_all_retries_failed:
        raise on_all_retries_failed(max_retries, first_token_timeout)
    else:
        raise Exception(
            f"Model did not respond within {first_token_timeout}s after {max_retries} attempts. "
            "Please try again."
        )