"""Build-up — evidence-first research with compounding knowledge and study."""

__version__ = "0.2.0"
PRODUCT_NAME = "build-up"
COMMAND_NAME = "buildup"


def main() -> None:
    # Keep package imports lightweight for storage/tests and load CLI-only
    # dependencies only when the executable is invoked.
    from buildup.cli import main as cli_main

    cli_main()


__all__ = ["COMMAND_NAME", "PRODUCT_NAME", "__version__", "main"]
