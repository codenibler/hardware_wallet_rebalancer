from __future__ import annotations

import io
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from wallet_rebalancer.cli import (
    _bitvavo_top_up_command,
    _check_command,
    _interactive_bitvavo_command,
    _prompt_bitvavo_amount,
    _prompt_bitvavo_mode,
    _prompt_top_up,
    _track_command,
    build_parser,
)
from wallet_rebalancer.execution import DEFAULT_EXECUTION_STATE_PATH
from wallet_rebalancer.models import AssetPlan, PortfolioPlan


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


class InteractiveBitvavoPromptTests(unittest.TestCase):
    def test_demo_is_the_safe_default(self) -> None:
        with (
            patch("builtins.input", return_value=""),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertFalse(_prompt_bitvavo_mode())

    def test_live_must_be_typed_explicitly(self) -> None:
        stderr = io.StringIO()
        with (
            patch("builtins.input", side_effect=["yes", "LIVE"]),
            patch("sys.stderr", stderr),
        ):
            self.assertTrue(_prompt_bitvavo_mode())

        self.assertIn("enter Demo or Live", stderr.getvalue())

    def test_deposit_amount_must_be_positive(self) -> None:
        stderr = io.StringIO()
        with (
            patch("builtins.input", side_effect=["", "0", "250.50"]),
            patch("sys.stderr", stderr),
        ):
            self.assertEqual(_prompt_bitvavo_amount(), Decimal("250.50"))

        self.assertEqual(stderr.getvalue().count("Invalid amount:"), 2)

    def test_interactive_answers_become_bitvavo_arguments(self) -> None:
        with (
            patch("wallet_rebalancer.cli._prompt_bitvavo_mode", return_value=True),
            patch(
                "wallet_rebalancer.cli._prompt_bitvavo_amount",
                return_value=Decimal("250"),
            ),
            patch(
                "wallet_rebalancer.cli._bitvavo_top_up_command",
                return_value=0,
            ) as top_up,
        ):
            self.assertEqual(_interactive_bitvavo_command(), 0)

        args = top_up.call_args.args[0]
        self.assertEqual(args.amount, Decimal("250"))
        self.assertTrue(args.confirm)
        self.assertEqual(args.state_file, DEFAULT_EXECUTION_STATE_PATH)


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

    def test_bitvavo_top_up_is_preview_only_by_default(self) -> None:
        args = build_parser().parse_args(["bitvavo-top-up", "250"])

        self.assertEqual(args.amount, Decimal("250"))
        self.assertFalse(args.confirm)
        self.assertEqual(
            str(args.state_file),
            "reports/bitvavo_executions.json",
        )

    def test_bitvavo_top_up_requires_positive_amount(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit),
        ):
            build_parser().parse_args(["bitvavo-top-up", "0", "--confirm"])


class BitvavoCommandTests(unittest.TestCase):
    def test_preview_never_loads_trading_credentials(self) -> None:
        args = build_parser().parse_args(["bitvavo-top-up", "250"])
        app_config = SimpleNamespace(
            policy=SimpleNamespace(
                threshold=Decimal("0.05"),
                target_weights={"BTC": Decimal("1")},
            )
        )
        bitvavo_config = SimpleNamespace(max_top_up_eur=Decimal("1000"))
        read_client = SimpleNamespace(
            get_market_fee_bps=lambda asset: Decimal("25")
        )

        with (
            patch("wallet_rebalancer.cli.load_config", return_value=app_config),
            patch(
                "wallet_rebalancer.cli.load_bitvavo_config",
                return_value=bitvavo_config,
            ),
            patch(
                "wallet_rebalancer.cli.load_readonly_credentials",
                return_value=object(),
            ),
            patch("wallet_rebalancer.cli.load_trading_credentials") as trading,
            patch(
                "wallet_rebalancer.cli.BitvavoClient",
                return_value=read_client,
            ),
            patch("wallet_rebalancer.cli.BitvavoExecutionClient") as executor,
            patch(
                "wallet_rebalancer.cli._portfolio_inputs",
                return_value=(object(), object()),
            ),
            patch(
                "wallet_rebalancer.cli.build_buy_only_plan",
                return_value=object(),
            ),
            patch(
                "wallet_rebalancer.cli.prepare_top_up",
                return_value=object(),
            ),
            patch(
                "wallet_rebalancer.cli.render_prepared_top_up",
                return_value="preview",
            ),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(_bitvavo_top_up_command(args), 0)

        trading.assert_not_called()
        executor.assert_not_called()


class TrackingCommandTests(unittest.TestCase):
    @staticmethod
    def _portfolio_plan() -> PortfolioPlan:
        timestamp = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        assets = tuple(
            AssetPlan(
                asset=asset,
                amount=Decimal("1"),
                price_eur=Decimal("2"),
                current_value_eur=Decimal("2"),
                current_weight=Decimal("0.25"),
                target_weight=Decimal("0.25"),
                drift=Decimal("0"),
                desired_value_eur=Decimal("2"),
                desired_amount=Decimal("1"),
                trade_value_eur=Decimal("0"),
            )
            for asset in ("BTC", "ETH", "SOL", "LINK")
        )
        return PortfolioPlan(
            assets=assets,
            trades=(),
            current_total_eur=Decimal("8"),
            top_up_eur=Decimal("0"),
            estimated_fee_bps=Decimal("50"),
            estimated_fees_eur=Decimal("0"),
            desired_invested_total_eur=Decimal("8"),
            threshold=Decimal("0.05"),
            max_abs_drift=Decimal("0"),
            threshold_rebalance_needed=False,
            minimum_top_up_for_buy_only_eur=Decimal("0"),
            prices_as_of=timestamp,
            holdings_as_of=timestamp,
            price_source="test",
        )

    def test_check_records_the_snapshot_used_for_its_plan(self) -> None:
        args = build_parser().parse_args(["check", "--no-prompt", "--no-telegram"])

        with (
            patch("wallet_rebalancer.cli.load_config", return_value=object()),
            patch(
                "wallet_rebalancer.cli._plan_from_args",
                return_value=self._portfolio_plan(),
            ),
            patch("wallet_rebalancer.cli._track_plan_snapshot") as track,
            patch("wallet_rebalancer.cli.render_text", return_value="report"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(_check_command(args), 0)

        track.assert_called_once()

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

if __name__ == "__main__":
    unittest.main()
