#!/usr/bin/env python3
"""
Quickly extract profileArn from Kiro IDE logs.

Usage:
    python get_profile_arn.py           # Print profileArn
    python get_profile_arn.py --update  # Also write to .env
"""

import json
import os
import re
import sys
from pathlib import Path


def find_latest_log_dir() -> Path | None:
    """Find the most recent Kiro session log directory."""
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    logs_root = Path(appdata) / "Kiro" / "logs"
    if not logs_root.exists():
        return None
    dirs = sorted(logs_root.iterdir(), reverse=True)
    for d in dirs:
        if d.is_dir():
            return d
    return None


def find_q_client_logs(session_dir: Path) -> list[Path]:
    """Find all q-client.log files in a session directory."""
    return list(session_dir.rglob("q-client.log"))


def extract_profile_arn(log_file: Path) -> str | None:
    """
    Extract profileArn from a q-client.log file.

    Looks for successful ListAvailableProfilesCommand responses.

    Args:
        log_file: Path to q-client.log

    Returns:
        The profileArn string, or None if not found.
    """
    pattern = re.compile(
        r'"commandName":"ListAvailableProfilesCommand".*?"profiles":\[.*?"arn":"(arn:aws:codewhisperer:[^"]+)"'
    )
    try:
        text = log_file.read_text(encoding="utf-8", errors="ignore")
        match = pattern.search(text)
        if match:
            return match.group(1)
    except OSError as e:
        print(f"  Warning: could not read {log_file}: {e}", file=sys.stderr)
    return None


def update_env_file(arn: str, env_path: Path) -> bool:
    """
    Write or update PROFILE_ARN in .env file.

    Args:
        arn: The profileArn value to set.
        env_path: Path to the .env file.

    Returns:
        True if the file was modified.
    """
    if not env_path.exists():
        print(f"  .env not found at {env_path}", file=sys.stderr)
        return False

    content = env_path.read_text(encoding="utf-8")
    new_line = f'PROFILE_ARN="{arn}"'

    # Replace existing (commented or active) PROFILE_ARN line
    updated = re.sub(
        r'^#?\s*PROFILE_ARN\s*=.*$',
        new_line,
        content,
        flags=re.MULTILINE,
    )

    if updated == content:
        # Not found — append it
        updated = content.rstrip() + f"\n{new_line}\n"

    env_path.write_text(updated, encoding="utf-8")
    return True


def main() -> None:
    update_env = "--update" in sys.argv

    session_dir = find_latest_log_dir()
    if not session_dir:
        print("Error: Kiro log directory not found. Is Kiro IDE installed?", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning: {session_dir}")

    # Search all session dirs (newest first) until ARN is found
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    logs_root = Path(appdata) / "Kiro" / "logs"
    all_session_dirs = sorted(logs_root.iterdir(), reverse=True) if logs_root.exists() else [session_dir]

    arn = None
    for sdir in all_session_dirs:
        if not sdir.is_dir():
            continue
        for log_file in find_q_client_logs(sdir):
            arn = extract_profile_arn(log_file)
            if arn:
                print(f"Found in: {log_file}")
                break
        if arn:
            break

    if not arn:
        print("Error: profileArn not found in logs.", file=sys.stderr)
        print("Tip: Open Kiro IDE and make sure you are logged in, then retry.", file=sys.stderr)
        sys.exit(1)

    print(f"\nprofileArn: {arn}")

    if update_env:
        env_path = Path(__file__).parent / ".env"
        if update_env_file(arn, env_path):
            print(f"Updated:    {env_path}")
        else:
            print(f"Add this to your .env manually:\n  PROFILE_ARN=\"{arn}\"")
    else:
        print(f'\nTo apply, run with --update flag or add to .env:\n  PROFILE_ARN="{arn}"')


if __name__ == "__main__":
    main()
