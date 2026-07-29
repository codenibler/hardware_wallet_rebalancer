from __future__ import annotations

import argparse
import io
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from wallet_rebalancer.cli import (
    _check_command,
    _prompt_top_up,
    _track_command,
    build_parser,
)


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

    def test_zero_fee_override_is_rejected(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit),
        ):
            build_parser().parse_args(["check", "--fee-bps", "0"])

    def test_track_defaults_to_july_28_benchmark(self) -> None:
        args = build_parser().parse_args(["track"])

        self.assertEqual(args.start_date.isoformat(), "2026-07-28")
        self.assertEqual(str(args.data_file), "reports/portfolio_tracking.json")
        self.assertEqual(str(args.charts_dir), "reports")
        self.assertEqual(args.deposit_eur, Decimal("0"))
        self.assertIsNone(args.deposit_fee_bps)

    def test_track_accepts_completed_deposit_and_fee_rate(self) -> None:
        args = build_parser().parse_args(
            [
                "track",
                "--deposit-eur",
                "1000",
                "--deposit-fee-bps",
                "50",
            ]
        )

        self.assertEqual(args.deposit_eur, Decimal("1000"))
        self.assertEqual(args.deposit_fee_bps, Decimal("50"))

    def test_track_start_date_requires_iso_format(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit),
        ):
            build_parser().parse_args(["track", "--start-date", "July 28"])


class TrackingCommandTests(unittest.TestCase):
    def test_deposit_uses_configured_fee_rate_by_default(self) -> None:
        args = build_parser().parse_args(
            ["track", "--deposit-eur", "1000", "--json"]
        )
        config = SimpleNamespace(
            policy=SimpleNamespace(estimated_fee_bps=Decimal("50"))
        )
        summary = SimpleNamespace(to_dict=lambda: {"ok": True})

        with (
            patch("wallet_rebalancer.cli.load_config", return_value=config),
            patch(
                "wallet_rebalancer.cli._portfolio_inputs",
                return_value=(object(), object()),
            ),
            patch(
                "wallet_rebalancer.cli.record_rebalance",
                return_value=summary,
            ) as record,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(_track_command(args), 0)

        self.assertEqual(record.call_args.kwargs["deposit_eur"], Decimal("1000"))
        self.assertEqual(
            record.call_args.kwargs["deposit_fee_bps"],
            Decimal("50"),
        )

    def test_deposit_fee_requires_a_deposit(self) -> None:
        args = build_parser().parse_args(["track", "--deposit-fee-bps", "50"])
        config = SimpleNamespace(
            policy=SimpleNamespace(estimated_fee_bps=Decimal("50"))
        )

        with (
            patch("wallet_rebalancer.cli.load_config", return_value=config),
            patch(
                "wallet_rebalancer.cli._portfolio_inputs",
                return_value=(object(), object()),
            ),
            self.assertRaisesRegex(ValueError, "--deposit-eur"),
        ):
            _track_command(args)


class TelegramDeliveryTests(unittest.TestCase):
    @staticmethod
    def _config():
        return SimpleNamespace(
            wallet=SimpleNamespace(bitcoin_xpubs=("xpub-test",))
        )

    def test_check_sends_to_telegram_by_default(self) -> None:
        args = build_parser().parse_args(["check", "--no-prompt"])
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token:secret",
            "TELEGRAM_CHAT_ID": "123456",
        }

        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "wallet_rebalancer.cli.load_config",
                return_value=self._config(),
            ),
            patch(
                "wallet_rebalancer.cli._plan_from_args",
                return_value=object(),
            ),
            patch(
                "wallet_rebalancer.cli.render_text",
                return_value="test report",
            ),
            patch(
                "wallet_rebalancer.cli.render_order_message",
                return_value="ordered trades",
            ),
            patch("wallet_rebalancer.cli.TelegramClient") as client_class,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(_check_command(args), 0)

        client_class.assert_called_once_with("test-token:secret")
        client_class.return_value.send_message.assert_called_once_with(
            "123456",
            "ordered trades",
            parse_mode="HTML",
        )

    def test_no_telegram_skips_delivery(self) -> None:
        args = build_parser().parse_args(
            ["check", "--no-prompt", "--no-telegram"]
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "wallet_rebalancer.cli.load_config",
                return_value=self._config(),
            ),
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

    def test_trade_plan_scans_venues_before_telegram_delivery(self) -> None:
        args = build_parser().parse_args(["check", "--no-prompt"])
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token:secret",
            "TELEGRAM_CHAT_ID": "123456",
        }
        plan = SimpleNamespace(trades=(object(),))
        venue_snapshot = object()

        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "wallet_rebalancer.cli.load_config",
                return_value=self._config(),
            ),
            patch("wallet_rebalancer.cli._plan_from_args", return_value=plan),
            patch("wallet_rebalancer.cli.render_text", return_value="report"),
            patch("wallet_rebalancer.cli.ExchangeScanner") as scanner_class,
            patch(
                "wallet_rebalancer.cli.render_order_message",
                return_value="orders with venues",
            ) as render_orders,
            patch("wallet_rebalancer.cli.TelegramClient"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            scanner_class.return_value.fetch_markets.return_value = venue_snapshot
            self.assertEqual(_check_command(args), 0)

        scanner_class.assert_called_once_with(
            invity_account_descriptor="xpub-test",
        )
        scanner_class.return_value.fetch_markets.assert_called_once_with(
            trades=plan.trades,
        )
        render_orders.assert_called_once_with(plan, venues=venue_snapshot)

    def test_venue_failure_does_not_block_portfolio_message(self) -> None:
        args = build_parser().parse_args(["check", "--no-prompt"])
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token:secret",
            "TELEGRAM_CHAT_ID": "123456",
        }
        plan = SimpleNamespace(trades=(object(),))

        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "wallet_rebalancer.cli.load_config",
                return_value=self._config(),
            ),
            patch("wallet_rebalancer.cli._plan_from_args", return_value=plan),
            patch("wallet_rebalancer.cli.render_text", return_value="report"),
            patch("wallet_rebalancer.cli.ExchangeScanner") as scanner_class,
            patch(
                "wallet_rebalancer.cli.render_order_message",
                return_value="orders without venues",
            ) as render_orders,
            patch("wallet_rebalancer.cli.TelegramClient"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            scanner_class.return_value.fetch_markets.side_effect = RuntimeError(
                "market data unavailable"
            )
            self.assertEqual(_check_command(args), 0)

        render_orders.assert_called_once_with(
            plan,
            venue_error="market data unavailable",
        )


if __name__ == "__main__":
    unittest.main()
