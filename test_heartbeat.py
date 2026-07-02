#!/usr/bin/env python3
"""
Heartbeat verification script for kiro-gateway.

Connects to the local gateway and measures time between events on the SSE stream.
A healthy heartbeat-enabled gateway should send keepalive events every ~5 seconds
while the model is thinking, preventing clients from timing out.

Usage:
    python test_heartbeat.py [--port 8000] [--api-key my-secret] [--model claude-haiku-4.5]

The script reports:
  - When each SSE event arrived and what type it was
  - The gap (seconds) between consecutive events
  - Whether any gap exceeded the client-timeout threshold (15s default)
"""

import argparse
import sys
import time

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

try:
    from dotenv import dotenv_values
except ImportError:
    dotenv_values = None


def load_api_key(cli_key: str | None) -> str:
    """Load API key from CLI arg, .env file, or fall back to example value."""
    if cli_key:
        return cli_key
    if dotenv_values:
        env = dotenv_values(".env")
        key = env.get("PROXY_API_KEY")
        if key:
            return key
    return "my-super-secret-password-123"


def classify_line(line: str) -> str:
    """Return a human-readable label for an SSE line."""
    if line.startswith(": keep-alive"):
        return "💓 HEARTBEAT (OpenAI keepalive comment)"
    if '"type":"ping"' in line or '"type": "ping"' in line:
        return "💓 HEARTBEAT (Anthropic ping event)"
    if line.startswith("data: [DONE]"):
        return "✅ DONE"
    if line.startswith("data: "):
        return "📦 DATA"
    if line.startswith("event: "):
        return f"📌 EVENT ({line.strip()})"
    if line.startswith(": "):
        return f"💬 COMMENT ({line.strip()})"
    return f"   {line.strip()}"


def run_test(base_url: str, api_key: str, model: str, timeout_warn: float = 10.0):
    url = f"{base_url}/v1/chat/completions"
    payload = {
        "model": model,
        "stream": True,
        "messages": [{"role": "user", "content": "Count from 1 to 5, one number per line."}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    print(f"\n{'='*60}")
    print(f"  Gateway heartbeat test")
    print(f"  URL:   {url}")
    print(f"  Model: {model}")
    print(f"{'='*60}\n")
    print(f"{'Time':>8}  {'Gap':>6}  Event")
    print(f"{'-'*8}  {'-'*6}  {'-'*40}")

    start = time.monotonic()
    last_event_time = start
    max_gap = 0.0
    event_count = 0
    heartbeat_count = 0

    try:
        with httpx.Client(timeout=60.0) as client:
            with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode()
                    print(f"\n❌ HTTP {resp.status_code}: {body[:300]}")
                    return

                for raw_line in resp.iter_lines():
                    now = time.monotonic()
                    elapsed = now - start
                    gap = now - last_event_time
                    last_event_time = now

                    if not raw_line.strip():
                        continue

                    label = classify_line(raw_line)
                    event_count += 1
                    if "HEARTBEAT" in label:
                        heartbeat_count += 1

                    gap_str = f"{gap:.1f}s" if gap > 0.1 else "<0.1s"
                    warn = " ⚠️  LONG GAP!" if gap > timeout_warn else ""
                    max_gap = max(max_gap, gap)

                    print(f"{elapsed:7.1f}s  {gap_str:>6}  {label}{warn}")

                    if raw_line.strip() == "data: [DONE]":
                        break

    except httpx.ConnectError:
        print(f"\n❌ Cannot connect to {base_url}")
        print("   Is the gateway running? Start it with: python main.py")
        return
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")

    print(f"\n{'='*60}")
    print(f"  Summary")
    print(f"  Total events  : {event_count}")
    print(f"  Heartbeats    : {heartbeat_count}")
    print(f"  Max gap       : {max_gap:.1f}s")
    if max_gap > timeout_warn:
        print(f"  ⚠️  Max gap exceeded {timeout_warn}s — clients may time out!")
    else:
        print(f"  ✅ All gaps under {timeout_warn}s — heartbeat is working correctly")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Test kiro-gateway heartbeat mechanism")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default="claude-haiku-4.5")
    parser.add_argument(
        "--warn-gap",
        type=float,
        default=10.0,
        help="Warn if any gap between events exceeds this many seconds (default: 10)",
    )
    args = parser.parse_args()

    api_key = load_api_key(args.api_key)
    base_url = f"http://{args.host}:{args.port}"
    run_test(base_url, api_key, args.model, args.warn_gap)


if __name__ == "__main__":
    main()
