"""Record a portfolio snapshot and compare it with buy-and-hold."""

from __future__ import annotations

import sys

from wallet_rebalancer.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["track", *sys.argv[1:]]))
