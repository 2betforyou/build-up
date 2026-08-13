"""Structured logging setup with file rotation."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from friday.config import FridayConfig


def setup_logging(cfg: FridayConfig) -> logging.Logger:
    """Configure rotating file + stderr handlers for the 'friday' logger."""
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("friday")
    if logger.handlers:
        return logger  # already configured
    logger.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(
        cfg.logs_dir / "friday.log",
        maxBytes=cfg.log_max_bytes,
        backupCount=cfg.log_backup_count,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(sh)
    return logger
