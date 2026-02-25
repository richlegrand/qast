"""TTY input helper for when stdin is piped."""

from __future__ import annotations

import sys


def tty_input(prompt: str = "") -> str:
    """Like input(), but reads from /dev/tty when stdin is not a terminal."""
    if sys.stdin.isatty():
        return input(prompt)
    with open("/dev/tty") as tty:
        if prompt:
            print(prompt, end="", flush=True)
        return tty.readline().rstrip("\n")
