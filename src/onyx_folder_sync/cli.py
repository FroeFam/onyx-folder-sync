"""Command line entry point. Placeholder until the commands are implemented."""

import argparse
from collections.abc import Sequence

from onyx_folder_sync import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onyx-folder-sync",
        description="Keeps local or mounted folders in sync with an Onyx knowledge base.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
