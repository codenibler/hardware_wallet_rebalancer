from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from wallet_rebalancer.models import ZERO, Holdings, PriceBook
from wallet_rebalancer.planner import build_buy_only_plan, build_plan
from wallet_rebalancer.reporting import (
    plan_to_dict,
    render_order_message,
    render_text,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
UNIT_PRICES = PriceBook(
    prices_eur={"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1},
    as_of=NOW,
    source="test",
)


class PlannerTests(unittest.TestCase):
    def test_buy_only_plan_never_sells_overweight_assets(self) -> None:
        plan = build_buy_only_plan(
            Holdings(
                amounts={"BTC": 600, "ETH": 200, "SOL": 100, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="100",
        )

        self.assertTrue(all(trade.side == "BUY" for trade in plan.trades))
        self.assertEqual(
            {trade.asset: trade.notional_eur for trade in plan.trades},
            {"ETH": Decimal("50"), "SOL": Decimal("50")},
        )
        self.assertEqual(plan.estimated_fees_eur, Decimal("0"))

    def test_buy_only_plan_raises_lowest_sleeves_first(self) -> None:
        plan = build_buy_only_plan(
            Holdings(
                amounts={"BTC": 600, "ETH": 200, "SOL": 100, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="50",
        )

        self.assertEqual(
            {trade.asset: trade.notional_eur for trade in plan.trades},
            {"ETH": Decimal("18.75"), "SOL": Decimal("31.25")},
        )

    def test_buy_only_plan_reserves_estimated_fees(self) -> None:
        plan = build_buy_only_plan(
            Holdings(
                amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="100",
            estimated_fee_bps="25",
        )

        buys = sum((trade.notional_eur for trade in plan.trades), ZERO)
        self.assertEqual(buys + plan.estimated_fees_eur, Decimal("100"))
        self.assertTrue(all(trade.side == "BUY" for trade in plan.trades))

    def test_balanced_portfolio_needs_no_rebalance(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
        )

        self.assertFalse(plan.threshold_rebalance_needed)
        self.assertFalse(plan.has_trade_plan)
        self.assertEqual(plan.max_abs_drift, Decimal("0"))
        report = render_text(plan)
        self.assertIn("STATUS: NO REBALANCE NEEDED", report)
        self.assertIn("€1,000.00", report)
        self.assertNotIn("SAFETY", report)
        self.assertNotIn("read-only plan", report)

    def test_unbalanced_portfolio_produces_exact_buys_and_sells(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 800, "ETH": 100, "SOL": 50, "LINK": 50},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            threshold="0.05",
        )

        self.assertTrue(plan.threshold_rebalance_needed)
        trades = {(trade.side, trade.asset): trade for trade in plan.trades}
        self.assertEqual(trades[("SELL", "BTC")].notional_eur, Decimal("300.00"))
        self.assertEqual(trades[("BUY", "ETH")].notional_eur, Decimal("150.00"))
        self.assertEqual(trades[("BUY", "SOL")].notional_eur, Decimal("100.00"))
        self.assertEqual(trades[("BUY", "LINK")].notional_eur, Decimal("50.00"))
        order_message = render_order_message(plan)
        order_lines = order_message.splitlines()
        self.assertEqual(
            order_lines[0],
            "Greetings cryptopian. It seems your portfolio is out of balance.",
        )
        self.assertIn("has reached or exceeded the 5.00% threshold", order_lines[2])
        self.assertIn(
            "<pre>These are the planned orders (not submitted):",
            order_message,
        )
        self.assertIn("🔴 BTC,", order_message)
        self.assertIn("🟢 ETH,", order_message)
        self.assertIn("🟢 LINK,", order_message)
        self.assertIn("🟢 SOL,", order_message)
        self.assertIn(
            "€300.00, fee≈€0.00",
            order_message,
        )
        self.assertNotIn("reason=", order_message)
        self.assertIn("</pre>", order_message)
        self.assertTrue(order_lines[-1].startswith("Estimated total fees:"))

    def test_balanced_top_up_is_allocated_by_target(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="100",
        )

        self.assertFalse(plan.threshold_rebalance_needed)
        self.assertTrue(plan.has_trade_plan)
        trade_values = {
            trade.asset: trade.notional_eur for trade in plan.trades
        }
        self.assertEqual(
            trade_values,
            {
                "BTC": Decimal("50.00"),
                "ETH": Decimal("25.00"),
                "LINK": Decimal("10.00"),
                "SOL": Decimal("15.00"),
            },
        )
        self.assertIn("TOP-UP PLAN AVAILABLE", render_text(plan))
        top_up_message = render_order_message(plan)
        self.assertIn("Your portfolio is in balance", top_up_message)
        self.assertIn("no threshold rebalance is needed", top_up_message)
        self.assertNotIn("reason=", top_up_message)
        self.assertIn("<pre>These are the planned orders", top_up_message)
        self.assertNotIn("🔴", top_up_message)

    def test_balanced_telegram_message_says_no_rebalance_is_needed(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            threshold="0.05",
            estimated_fee_bps="50",
        )

        message = render_order_message(plan)
        self.assertIn(
            "Greetings cryptopian. Your portfolio is in balance.",
            message,
        )
        self.assertIn(
            "The divergence of 0.00% is below the 5.00% threshold.",
            message,
        )
        self.assertIn("No rebalancing trades are needed.", message)
        self.assertNotIn("out of balance", message)
        self.assertNotIn("Estimated total fees", message)

    def test_top_up_required_for_buy_only_is_calculated(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 600, "ETH": 200, "SOL": 100, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="100",
        )

        self.assertEqual(
            plan.minimum_top_up_for_buy_only_eur,
            Decimal("200"),
        )
        self.assertTrue(any(trade.side == "SELL" for trade in plan.trades))

    def test_buy_only_minimum_includes_estimated_fees(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 600, "ETH": 200, "SOL": 100, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            estimated_fee_bps="100",
        )

        self.assertEqual(
            plan.minimum_top_up_for_buy_only_eur,
            Decimal("202.00"),
        )

    def test_withdrawal_sells_a_balanced_portfolio_pro_rata(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="-100",
        )

        self.assertTrue(plan.has_withdrawal)
        self.assertFalse(plan.has_top_up)
        self.assertTrue(plan.has_trade_plan)
        self.assertEqual(plan.withdrawal_eur, Decimal("100"))
        self.assertTrue(all(trade.side == "SELL" for trade in plan.trades))
        self.assertEqual(
            {trade.asset: trade.notional_eur for trade in plan.trades},
            {
                "BTC": Decimal("50"),
                "ETH": Decimal("25"),
                "SOL": Decimal("15"),
                "LINK": Decimal("10"),
            },
        )
        self.assertEqual(plan.desired_invested_total_eur, Decimal("900"))

    def test_withdrawal_trims_the_overweight_asset_hardest(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 600, "ETH": 200, "SOL": 100, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="-200",
        )

        trades = {trade.asset: trade for trade in plan.trades}
        self.assertEqual(trades["BTC"].side, "SELL")
        self.assertEqual(trades["BTC"].notional_eur, Decimal("200"))
        self.assertEqual(trades["LINK"].side, "SELL")
        self.assertEqual(trades["LINK"].notional_eur, Decimal("20"))
        # SOL is underweight enough that returning to target while taking cash
        # out still requires buying it back up, while ETH lands on target and
        # is left alone.
        self.assertEqual(trades["SOL"].side, "BUY")
        self.assertEqual(trades["SOL"].notional_eur, Decimal("20"))
        self.assertNotIn("ETH", trades)

    def test_withdrawal_fees_are_paid_by_the_remaining_portfolio(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="-100",
            estimated_fee_bps="50",
        )
        sells = sum(
            (trade.notional_eur for trade in plan.trades if trade.side == "SELL"),
            ZERO,
        )
        buys = sum(
            (trade.notional_eur for trade in plan.trades if trade.side == "BUY"),
            ZERO,
        )

        self.assertAlmostEqual(
            sells - buys - plan.estimated_fees_eur,
            Decimal("100"),
            places=7,
        )
        self.assertAlmostEqual(
            plan.desired_invested_total_eur,
            Decimal("1000") - Decimal("100") - plan.estimated_fees_eur,
            places=7,
        )

    def test_sell_only_withdrawal_minimum_is_calculated(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 600, "ETH": 200, "SOL": 100, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="-100",
        )

        # SOL is the most underweight sleeve at a level of 100/0.15, so the
        # portfolio must shrink to that level before nothing has to be bought.
        self.assertEqual(
            plan.minimum_withdrawal_for_sell_only_eur,
            Decimal("1000") - Decimal("100") / Decimal("0.15"),
        )

    def test_withdrawing_at_the_sell_only_minimum_buys_nothing(self) -> None:
        holdings = Holdings(
            amounts={"BTC": 600, "ETH": 200, "SOL": 100, "LINK": 100},
            fetched_at=NOW,
        )
        minimum = build_plan(
            holdings,
            UNIT_PRICES,
            top_up_eur="-100",
        ).minimum_withdrawal_for_sell_only_eur
        plan = build_plan(holdings, UNIT_PRICES, top_up_eur=-minimum)

        self.assertTrue(all(trade.side == "SELL" for trade in plan.trades))

    def test_sell_only_minimum_is_reduced_by_estimated_fees(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 600, "ETH": 200, "SOL": 100, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            estimated_fee_bps="100",
        )
        before_fees = Decimal("1000") - Decimal("100") / Decimal("0.15")

        self.assertEqual(
            plan.minimum_withdrawal_for_sell_only_eur,
            before_fees * Decimal("0.99"),
        )

    def test_withdrawal_cannot_exceed_the_portfolio_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "is not funded by"):
            build_plan(
                Holdings(
                    amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                    fetched_at=NOW,
                ),
                UNIT_PRICES,
                top_up_eur="-1000",
            )

    def test_withdrawal_report_explains_the_released_cash(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="-100",
        )
        report = render_text(plan)
        message = render_order_message(plan)

        self.assertIn("WITHDRAWAL PLAN AVAILABLE", report)
        self.assertIn("Cash withdrawal:  €100.00", report)
        self.assertNotIn("Top-up capital", report)
        self.assertIn(
            "Cash check: gross sells - gross buys - estimated fees "
            "= €100.00 released",
            report,
        )
        self.assertIn("release €100.00", message)
        self.assertIn("🔴", message)
        self.assertNotIn("🟢", message)

    def test_withdrawal_report_names_the_sell_only_minimum(self) -> None:
        report = render_text(
            build_plan(
                Holdings(
                    amounts={"BTC": 600, "ETH": 200, "SOL": 100, "LINK": 100},
                    fetched_at=NOW,
                ),
                UNIT_PRICES,
                top_up_eur="-100",
            )
        )

        self.assertIn(
            "A sell-only exact rebalance would require withdrawing at least "
            "€333.33",
            report,
        )

    def test_withdrawal_audit_json_records_both_signs(self) -> None:
        payload = plan_to_dict(
            build_plan(
                Holdings(
                    amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                    fetched_at=NOW,
                ),
                UNIT_PRICES,
                top_up_eur="-100",
            )
        )

        self.assertTrue(payload["withdrawal_plan_included"])
        self.assertFalse(payload["top_up_plan_included"])
        self.assertEqual(payload["top_up_eur"], "-100")
        self.assertEqual(payload["withdrawal_eur"], "100")

    def test_fee_adjusted_plan_is_self_financing(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 800, "ETH": 100, "SOL": 50, "LINK": 50},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="100",
            estimated_fee_bps="50",
        )
        buys = sum(
            (trade.notional_eur for trade in plan.trades if trade.side == "BUY"),
            Decimal("0"),
        )
        sells = sum(
            (trade.notional_eur for trade in plan.trades if trade.side == "SELL"),
            Decimal("0"),
        )

        self.assertAlmostEqual(
            buys - sells + plan.estimated_fees_eur,
            Decimal("100"),
            places=7,
        )
        self.assertAlmostEqual(
            plan.desired_invested_total_eur,
            Decimal("1100") - plan.estimated_fees_eur,
            places=7,
        )
        fee_message = render_order_message(plan)
        self.assertIn("50 bps on gross traded value", fee_message)
        self.assertIn("fee≈€", fee_message)
        self.assertIn("Estimated total fees: €", fee_message)

    def test_json_keeps_decimal_precision_as_strings(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": "0.1", "ETH": "2", "SOL": "10", "LINK": "20"},
                fetched_at=NOW,
            ),
            PriceBook(
                prices_eur={
                    "BTC": "65000.123456",
                    "ETH": "2000.12",
                    "SOL": "80.1",
                    "LINK": "10.25",
                },
                as_of=NOW,
            ),
        )

        payload = plan_to_dict(plan)
        self.assertIsInstance(payload["assets"]["BTC"]["amount"], str)
        self.assertEqual(payload["assets"]["BTC"]["amount"], "0.1")
        self.assertIn("price_eur", payload["assets"]["BTC"])
        self.assertNotIn("price_usd", payload["assets"]["BTC"])
        self.assertEqual(payload["estimated_fee_bps"], "0")


if __name__ == "__main__":
    unittest.main()
