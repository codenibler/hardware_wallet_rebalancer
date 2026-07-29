from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from wallet_rebalancer.exchange_scanner import (
    ExchangeQuote,
    VenueMarketSnapshot,
)
from wallet_rebalancer.models import Holdings, PriceBook
from wallet_rebalancer.planner import build_plan
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

    def test_telegram_orders_include_ranked_buy_and_sell_venues(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 800, "ETH": 100, "SOL": 50, "LINK": 50},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            threshold="0.05",
            estimated_fee_bps="50",
        )

        def quote(
            asset: str,
            exchange_id: str,
            exchange_name: str,
            ask: str,
            bid: str,
        ) -> ExchangeQuote:
            return ExchangeQuote(
                asset=asset,
                exchange_id=exchange_id,
                exchange_name=exchange_name,
                pair=f"{asset}-EUR",
                ask_eur=Decimal(ask),
                ask_size=Decimal("1000"),
                bid_eur=Decimal(bid),
                bid_size=Decimal("1000"),
                taker_fee_bps=Decimal("10"),
                quoted_at=NOW,
                trade_url="https://example.test",
            )

        venues = VenueMarketSnapshot(
            as_of=NOW,
            quotes={
                asset: (
                    quote(asset, "low_ask", "Low Ask", "0.99", "0.96"),
                    quote(asset, "middle", "Middle", "1.00", "0.97"),
                    quote(asset, "high_bid", "High Bid", "1.01", "0.98"),
                )
                for asset in ("BTC", "ETH", "SOL", "LINK")
            },
            failures=(),
        )

        message = render_order_message(plan, venues=venues)

        self.assertIn("venue=High Bid", message)
        self.assertIn("venue=Low Ask", message)
        self.assertNotIn("reason=", message)
        self.assertIn(
            "Top venues: 1) High Bid net €0.98/BTC",
            message,
        )
        self.assertIn(
            "Top venues: 1) Low Ask all-in €0.99/ETH",
            message,
        )
        self.assertIn("Estimated total trading fees:", message)
        self.assertIn("(recommended venues)", message)

    def test_telegram_marks_provider_costs_as_included_in_quote(self) -> None:
        plan = build_plan(
            Holdings(
                amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
                fetched_at=NOW,
            ),
            UNIT_PRICES,
            top_up_eur="100",
        )
        venues = VenueMarketSnapshot(
            as_of=NOW,
            quotes={
                asset: (
                    ExchangeQuote(
                        asset=asset,
                        exchange_id="banxa",
                        exchange_name="Banxa (SEPA)",
                        pair=f"{asset}-EUR",
                        ask_eur=Decimal("1.01"),
                        ask_size=Decimal("1000"),
                        bid_eur=Decimal("1.01"),
                        bid_size=Decimal("0"),
                        taker_fee_bps=Decimal("0"),
                        quoted_at=NOW,
                        trade_url="https://banxa.com",
                        supported_sides=frozenset(("BUY",)),
                        fee_eur_override=Decimal("0"),
                        fee_included_in_quote=True,
                    ),
                )
                for asset in ("BTC", "ETH", "SOL", "LINK")
            },
            failures=(),
        )

        message = render_order_message(plan, venues=venues)

        self.assertIn("venue=Banxa (SEPA)", message)
        self.assertIn("fee=included in quote", message)
        self.assertIn("Provider-inclusive costs", message)

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
