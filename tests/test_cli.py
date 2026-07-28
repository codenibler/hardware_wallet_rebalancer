from __future__ import annotations

import argparse
import io
import unittest
from decimal import Decimal
from unittest.mock import patch

from wallet_rebalancer.cli import _prompt_top_up, build_parser


class PromptTopUpTests(unittest.TestCase):
    def test_blank_input_defaults_to_zero(self) -> None:
        with (
            patch("builtins.input", return_value=""),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(_prompt_top_up(), Decimal("0"))

    def test_retries_until_amount_is_valid(self) -> None:
        stderr = io.StringIO()
        with (
            patch("builtins.input", side_effect=["not-a-number", "-1", "1000.25"]),
            patch("sys.stderr", stderr),
        ):
            self.assertEqual(_prompt_top_up(), Decimal("1000.25"))

        self.assertEqual(stderr.getvalue().count("Invalid amount:"), 2)

    def test_eof_explains_automation_option(self) -> None:
        with (
            patch("builtins.input", side_effect=EOFError),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            with self.assertRaisesRegex(ValueError, "--no-prompt"):
                _prompt_top_up()


class CheckArgumentTests(unittest.TestCase):
    def test_no_prompt_is_available_for_automation(self) -> None:
        args = build_parser().parse_args(["check", "--no-prompt"])
        self.assertTrue(args.no_prompt)

    def test_top_up_flag_was_removed(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit),
        ):
            build_parser().parse_args(["check", "--top-up", "100"])


if __name__ == "__main__":
    unittest.main()
