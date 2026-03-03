"""TTY input helper for when stdin is piped."""

from __future__ import annotations

import os
import sys


def tty_input(prompt: str = "") -> str:
    """Like input(), but reads from /dev/tty when stdin is not a terminal.

    When stdin is piped, opens /dev/tty directly and ensures echo is enabled
    (an upstream process like ffmpeg may have disabled it).
    """
    if sys.stdin.isatty():
        return input(prompt)
    if os.name == "nt":
        # Windows has no /dev/tty or termios; best effort fallback.
        if prompt:
            print(prompt, end="", flush=True)
        return sys.stdin.readline().rstrip("\r\n")
    import termios
    with open("/dev/tty") as tty:
        fd = tty.fileno()
        old = termios.tcgetattr(fd)
        try:
            # Restore sane terminal modes — upstream ffmpeg may have
            # disabled echo, canonical mode, and CR→NL translation.
            new = termios.tcgetattr(fd)
            new[0] |= termios.ICRNL    # iflag: CR → NL
            new[3] |= termios.ECHO | termios.ICANON  # lflag: echo + line mode
            termios.tcsetattr(fd, termios.TCSANOW, new)
            if prompt:
                print(prompt, end="", flush=True)
            return tty.readline().rstrip("\n")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
