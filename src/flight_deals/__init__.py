"""Flight Deals Tracker package.

Configures the package-level logger on import (no network I/O, no side
effects beyond stdlib logging setup — safe to run even for `--help`).
Level is controlled via the ``FLIGHT_DEALS_LOG`` env var (default WARNING),
output goes to stderr so stdout stays clean for command output.
"""

import logging
import os

_logger = logging.getLogger(__name__)

if not _logger.handlers:
    _handler = logging.StreamHandler()  # stderr by default
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _logger.addHandler(_handler)
    _logger.propagate = False

_level_name = os.environ.get("FLIGHT_DEALS_LOG", "WARNING").upper()
_logger.setLevel(getattr(logging, _level_name, logging.WARNING))
