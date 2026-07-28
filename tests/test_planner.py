from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from wallet_rebalancer.models import Holdings, PriceBook
from wallet_rebalancer.planner import build_plan
from wallet_rebalancer.reporting import plan_to_dict, render_text


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
UNIT_PRICES = PriceBook(
    prices_eur={"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1},
    as_of=NOW,
    source="test",
)


class PlannerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
