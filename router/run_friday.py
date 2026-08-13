#!/usr/bin/env python3
"""Backward-compatible entry point for the build-up CLI.

This file delegates to the friday package.
Usage:
    python run_friday.py      # legacy launcher
    python -m friday          # also works
"""

from friday.cli import main

if __name__ == "__main__":
    main()
