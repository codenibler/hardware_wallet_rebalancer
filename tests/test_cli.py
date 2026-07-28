from __future__ import annotations

import argparse
import io
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from wallet_rebalancer.cli import _check_command, _prompt_top_up, build_parser


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

    def test_telegram_is_enabled_by_default(self) -> None:
        args = build_parser().parse_args(["check"])
        self.assertFalse(args.no_telegram)

    def test_old_send_telegram_flag_was_removed(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit),
        ):
            build_parser().parse_args(["check", "--send-telegram"])


class TelegramDeliveryTests(unittest.TestCase):
    def test_check_sends_to_telegram_by_default(self) -> None:
        args = build_parser().parse_args(["check", "--no-prompt"])
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token:secret",
            "TELEGRAM_CHAT_ID": "123456",
        }

        with (
            patch.dict(os.environ, environment, clear=True),
            patch("wallet_rebalancer.cli.load_config", return_value=object()),
            patch(
                "wallet_rebalancer.cli._plan_from_args",
                return_value=object(),
            ),
            patch(
                "wallet_rebalancer.cli.render_text",
                return_value="test report",
            ),
            patch(
                "wallet_rebalancer.cli.render_action_image",
                return_value=b"png-image",
            ),
            patch("wallet_rebalancer.cli.TelegramClient") as client_class,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(_check_command(args), 0)

        client_class.assert_called_once_with("test-token:secret")
        client_class.return_value.send_photo.assert_called_once_with(
            "123456",
            b"png-image",
        )
        client_class.return_value.send_message.assert_not_called()

    def test_no_telegram_skips_delivery(self) -> None:
        args = build_parser().parse_args(
            ["check", "--no-prompt", "--no-telegram"]
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("wallet_rebalancer.cli.load_config", return_value=object()),
            patch(
                "wallet_rebalancer.cli._plan_from_args",
                return_value=object(),
            ),
            patch(
                "wallet_rebalancer.cli.render_text",
                return_value="test report",
            ),
            patch("wallet_rebalancer.cli.TelegramClient") as client_class,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(_check_command(args), 0)

        client_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
