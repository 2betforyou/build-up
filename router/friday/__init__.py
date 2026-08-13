"""build-up — local deep-research agent and personal study coach."""

__version__ = "0.1.0"
PRODUCT_NAME = "build-up"
COMMAND_NAME = "buildup"


def main() -> None:
    # Keep package imports lightweight for storage/tests and load CLI-only
    # dependencies only when the executable is invoked.
    from friday.cli import main as cli_main

    cli_main()


__all__ = ["COMMAND_NAME", "PRODUCT_NAME", "__version__", "main"]
