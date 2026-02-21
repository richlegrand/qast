"""Logging setup — replaces print() calls."""

import logging
import sys


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format=fmt,
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("pychromecast").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"casturl.{name}")
