"""Convenience entry point for a one-shot portfolio check."""

from __future__ import annotations

import sys

from wallet_rebalancer.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["check", *sys.argv[1:]]))
