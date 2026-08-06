from __future__ import annotations

import unittest

from main import _cli_arguments


class MainEntryPointTests(unittest.TestCase):
    def test_bare_main_uses_interactive_bitvavo_workflow(self) -> None:
        self.assertEqual(_cli_arguments([]), ["interactive-bitvavo"])

    def test_main_options_continue_to_run_portfolio_check(self) -> None:
        self.assertEqual(
            _cli_arguments(["--no-prompt"]),
            ["check", "--no-prompt"],
        )


if __name__ == "__main__":
    unittest.main()
