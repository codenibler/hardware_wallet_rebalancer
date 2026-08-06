"""Interactive Bitvavo top-up entry point and automated check wrapper."""

from __future__ import annotations

import sys

from wallet_rebalancer.cli import main


def _cli_arguments(arguments: list[str]) -> list[str]:
    if not arguments:
        return ["interactive-bitvavo"]
    return ["check", *arguments]


if __name__ == "__main__":
    raise SystemExit(main(_cli_arguments(sys.argv[1:])))
