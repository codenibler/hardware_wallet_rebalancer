from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from wallet_rebalancer.models import ZERO, Holdings, PriceBook
from wallet_rebalancer.tracking import record_rebalance, render_performance


START = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
AFTER_DEPOSIT = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
SECOND_DEPOSIT = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class PortfolioTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.data_path = self.root / "tracking.json"
        self.chart_dir = self.root / "charts"

    def _record(
        self,
        amounts: dict[str, Decimal | int],
        prices: dict[str, Decimal | int],
        timestamp: datetime,
        *,
        note: str = "",
        deposit_eur: Decimal | int = 0,
        deposit_fee_bps: Decimal | int = 0,
    ):
        return record_rebalance(
            Holdings(amounts=amounts, fetched_at=timestamp),
            PriceBook(prices_eur=prices, as_of=timestamp, source="test"),
            data_path=self.data_path,
            chart_dir=self.chart_dir,
            start_date=date(2026, 7, 28),
            note=note,
            deposit_eur=deposit_eur,
            deposit_fee_bps=deposit_fee_bps,
        )

    def test_first_record_freezes_units_and_creates_exports(self) -> None:
        summary = self._record(
            {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10},
            {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1},
            START,
            note="Initial allocation",
        )

        self.assertEqual(summary.actual_value_eur, Decimal("100"))
        self.assertEqual(summary.buy_hold_value_eur, Decimal("100"))
        self.assertEqual(summary.outperformance, Decimal("0"))
        self.assertEqual(summary.verdict, "TIED")
        self.assertEqual(summary.observations, 1)
        self.assertTrue(summary.value_chart_path.exists())
        self.assertTrue(summary.returns_chart_path.exists())
        self.assertTrue(summary.performance_image_path.exists())
        self.assertEqual(
            summary.performance_image_path.read_bytes()[:8],
            b"\x89PNG\r\n\x1a\n",
        )
        self.assertTrue(summary.csv_path.exists())

        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["benchmark"]["start_date"], "2026-07-28")
        self.assertEqual(
            payload["benchmark"]["amounts"],
            {"BTC": "50", "ETH": "25", "SOL": "15", "LINK": "10"},
        )
        self.assertEqual(payload["observations"][0]["note"], "Initial allocation")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["cash_flows"], [])
        value_chart = summary.value_chart_path.read_text(encoding="utf-8")
        self.assertIn("Rebalanced", value_chart)
        self.assertIn("Buy &amp; hold", value_chart)
        self.assertIn('fill="#0f172a"', value_chart)
        self.assertIn('stroke="#334155"', value_chart)
        returns_chart = summary.returns_chart_path.read_text(encoding="utf-8")
        self.assertIn('fill="#0f172a"', returns_chart)

    def test_later_record_compares_against_original_fixed_units(self) -> None:
        initial_amounts = {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10}
        self._record(
            initial_amounts,
            {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1},
            START,
        )
        summary = self._record(
            {"BTC": 40, "ETH": 30, "SOL": 20, "LINK": 10},
            {"BTC": 2, "ETH": 1, "SOL": 1, "LINK": 1},
            LATER,
            note="Post-rebalance snapshot",
        )

        self.assertEqual(summary.actual_value_eur, Decimal("140"))
        self.assertEqual(summary.buy_hold_value_eur, Decimal("150"))
        self.assertEqual(summary.actual_return, Decimal("0.4"))
        self.assertEqual(summary.buy_hold_return, Decimal("0.5"))
        self.assertEqual(summary.outperformance, Decimal("-0.1"))
        self.assertEqual(summary.value_difference_eur, Decimal("-10"))
        self.assertEqual(summary.verdict, "BUY-AND-HOLD AHEAD")
        self.assertEqual(summary.observations, 2)

        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["benchmark"]["amounts"],
            {asset: str(value) for asset, value in initial_amounts.items()},
        )
        report = render_performance(summary)
        self.assertIn("STATUS: BUY-AND-HOLD AHEAD", report)
        self.assertIn("Outperformance:        -10.00 pp", report)
        csv_export = summary.csv_path.read_text(encoding="utf-8")
        self.assertIn("actual_return_pct", csv_export)
        self.assertIn("Post-rebalance snapshot", csv_export)

    def test_identical_timestamp_and_data_is_idempotent(self) -> None:
        amounts = {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10}
        prices = {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1}

        self._record(amounts, prices, START)
        summary = self._record(amounts, prices, START)

        self.assertEqual(summary.observations, 1)

    def test_deposit_adds_fee_adjusted_units_to_both_strategies(self) -> None:
        initial = {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10}
        prices = {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1}
        self._record(initial, prices, START)

        gross_deposit = Decimal("100")
        fee_bps = Decimal("100")
        net_invested = gross_deposit / Decimal("1.01")
        additions: dict[str, Decimal] = {
            "BTC": net_invested * Decimal("0.50"),
            "ETH": net_invested * Decimal("0.25"),
            "SOL": net_invested * Decimal("0.15"),
        }
        additions["LINK"] = net_invested - sum(additions.values(), ZERO)
        after_deposit = {
            asset: Decimal(initial[asset]) + additions[asset]
            for asset in initial
        }

        summary = self._record(
            after_deposit,
            prices,
            LATER,
            note="Completed deposit",
            deposit_eur=gross_deposit,
            deposit_fee_bps=fee_bps,
        )

        expected_value = sum(after_deposit.values(), ZERO)
        expected_return = expected_value / Decimal("200") - Decimal("1")
        expected_fee = gross_deposit - net_invested
        self.assertEqual(summary.actual_value_eur, expected_value)
        self.assertEqual(summary.buy_hold_value_eur, expected_value)
        self.assertEqual(summary.actual_return, expected_return)
        self.assertEqual(summary.buy_hold_return, expected_return)
        self.assertEqual(summary.outperformance, ZERO)
        self.assertEqual(summary.total_contributions_eur, gross_deposit)
        self.assertEqual(summary.total_benchmark_fees_eur, expected_fee)

        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["cash_flows"]), 1)
        cash_flow = payload["cash_flows"][0]
        self.assertEqual(cash_flow["type"], "deposit")
        self.assertEqual(Decimal(cash_flow["gross_amount_eur"]), gross_deposit)
        self.assertEqual(Decimal(cash_flow["net_invested_eur"]), net_invested)
        self.assertEqual(Decimal(cash_flow["benchmark_fee_eur"]), expected_fee)
        self.assertEqual(
            {
                asset: Decimal(amount)
                for asset, amount in payload["benchmark"]["amounts"].items()
            },
            after_deposit,
        )
        latest = payload["observations"][-1]
        self.assertEqual(latest["external_cash_flow_eur"], "100")
        self.assertEqual(latest["deposit_fee_bps"], "100")
        csv_export = summary.csv_path.read_text(encoding="utf-8")
        self.assertIn("external_cash_flow_eur", csv_export)
        self.assertIn("benchmark_net_invested_eur", csv_export)

    def test_automatically_detects_incoming_asset_units(self) -> None:
        initial = {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10}
        prices = {"BTC": 2, "ETH": 4, "SOL": 3, "LINK": 5}
        self._record(initial, prices, START)
        after_purchase = {
            "BTC": 50,
            "ETH": Decimal("30.5"),
            "SOL": 15,
            "LINK": 10,
        }

        summary = self._record(after_purchase, prices, LATER)

        self.assertEqual(summary.actual_value_eur, Decimal("317"))
        self.assertEqual(summary.buy_hold_value_eur, Decimal("317"))
        self.assertEqual(summary.total_contributions_eur, Decimal("22"))
        self.assertEqual(summary.total_benchmark_fees_eur, ZERO)
        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        flow = payload["cash_flows"][0]
        self.assertEqual(flow["type"], "detected_deposit")
        self.assertEqual(Decimal(flow["gross_amount_eur"]), Decimal("22"))
        purchases = {
            asset: Decimal(amount)
            for asset, amount in flow["benchmark_purchases"].items()
        }
        self.assertEqual(purchases["BTC"], Decimal("220") / Decimal("59"))
        self.assertEqual(purchases["ETH"], Decimal("110") / Decimal("59"))
        self.assertEqual(purchases["SOL"], Decimal("66") / Decimal("59"))
        self.assertEqual(
            sum((purchases[asset] * Decimal(prices[asset]) for asset in purchases), ZERO),
            Decimal("22"),
        )
        latest = payload["observations"][-1]
        self.assertEqual(latest["external_cash_flow_eur"], "22.0")
        self.assertEqual(latest["deposit_fee_bps"], "0")

    def test_does_not_mistake_a_rebalance_for_an_incoming_deposit(self) -> None:
        initial = {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10}
        prices = {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1}
        self._record(initial, prices, START)

        self._record(
            {"BTC": 45, "ETH": 30, "SOL": 15, "LINK": 10},
            prices,
            LATER,
        )

        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["cash_flows"], [])
        latest = payload["observations"][-1]
        self.assertEqual(latest["external_cash_flow_eur"], "0")
        self.assertEqual(
            latest["benchmark_amounts"],
            {"BTC": "50", "ETH": "25", "SOL": "15", "LINK": "10"},
        )

    def test_repairs_the_latest_legacy_untracked_incoming_assets(self) -> None:
        initial = {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10}
        prices = {"BTC": 2, "ETH": 4, "SOL": 3, "LINK": 5}
        self._record(initial, prices, START)
        after_purchase = {"BTC": 50, "ETH": Decimal("30.5"), "SOL": 15, "LINK": 10}
        self._record(after_purchase, prices, LATER)

        # Re-create the legacy version of the second snapshot: it retained
        # the received ETH only in the actual portfolio and had no cash flow.
        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        payload["cash_flows"] = []
        payload["benchmark"]["amounts"] = {
            asset: str(amount) for asset, amount in initial.items()
        }
        legacy = payload["observations"][-1]
        legacy.update(
            {
                "benchmark_amounts": {
                    asset: str(amount) for asset, amount in initial.items()
                },
                "buy_hold_value_eur": "295",
                "actual_return": str(Decimal("22") / Decimal("295")),
                "buy_hold_return": "0",
                "outperformance": str(Decimal("22") / Decimal("295")),
                "value_difference_eur": "22",
                "external_cash_flow_eur": "0",
                "deposit_fee_bps": "0",
                "benchmark_fee_eur": "0",
                "benchmark_net_invested_eur": "0",
                "total_contributions_eur": "0",
                "total_benchmark_fees_eur": "0",
            }
        )
        self.data_path.write_text(json.dumps(payload), encoding="utf-8")

        summary = self._record(after_purchase, prices, AFTER_DEPOSIT)

        repaired = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.assertEqual(len(repaired["cash_flows"]), 1)
        self.assertEqual(
            repaired["cash_flows"][0]["type"],
            "detected_deposit",
        )
        self.assertEqual(summary.buy_hold_value_eur, Decimal("317"))
        self.assertEqual(summary.total_contributions_eur, Decimal("22"))

    def test_returns_remain_cash_flow_adjusted_after_a_deposit(self) -> None:
        initial = {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10}
        prices = {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1}
        self._record(initial, prices, START)
        gross_deposit = Decimal("100")
        net_invested = gross_deposit / Decimal("1.01")
        additions: dict[str, Decimal] = {
            "BTC": net_invested * Decimal("0.50"),
            "ETH": net_invested * Decimal("0.25"),
            "SOL": net_invested * Decimal("0.15"),
        }
        additions["LINK"] = net_invested - sum(additions.values(), ZERO)
        amounts = {
            "BTC": Decimal("50") + additions["BTC"],
            "ETH": Decimal("25") + additions["ETH"],
            "SOL": Decimal("15") + additions["SOL"],
            "LINK": Decimal("10") + additions["LINK"],
        }
        self._record(
            amounts,
            prices,
            LATER,
            deposit_eur=gross_deposit,
            deposit_fee_bps=100,
        )

        summary = self._record(
            amounts,
            {"BTC": 2, "ETH": 2, "SOL": 2, "LINK": 2},
            AFTER_DEPOSIT,
        )

        value_after_deposit = sum(amounts.values(), ZERO)
        doubled_value = sum(
            (amount * Decimal("2") for amount in amounts.values()),
            ZERO,
        )
        expected_return = (
            value_after_deposit
            / Decimal("200")
            * (doubled_value / value_after_deposit)
            - Decimal("1")
        )
        self.assertEqual(summary.actual_return, expected_return)
        self.assertEqual(summary.buy_hold_return, expected_return)
        self.assertEqual(summary.total_contributions_eur, gross_deposit)

    def test_multiple_deposits_accumulate_without_rebalancing_benchmark(self) -> None:
        amounts = {
            "BTC": Decimal("50"),
            "ETH": Decimal("25"),
            "SOL": Decimal("15"),
            "LINK": Decimal("10"),
        }
        prices = {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1}
        self._record(amounts, prices, START)

        for timestamp, gross in (
            (LATER, Decimal("100")),
            (SECOND_DEPOSIT, Decimal("50")),
        ):
            net = gross / Decimal("1.005")
            purchases = {
                "BTC": net * Decimal("0.50"),
                "ETH": net * Decimal("0.25"),
                "SOL": net * Decimal("0.15"),
            }
            purchases["LINK"] = net - sum(purchases.values(), ZERO)
            amounts = {
                asset: amounts[asset] + purchases[asset] for asset in amounts
            }
            summary = self._record(
                amounts,
                prices,
                timestamp,
                deposit_eur=gross,
                deposit_fee_bps=50,
            )

        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["cash_flows"]), 2)
        self.assertEqual(summary.total_contributions_eur, Decimal("150"))
        self.assertEqual(summary.actual_value_eur, summary.buy_hold_value_eur)
        self.assertEqual(
            {
                asset: Decimal(amount)
                for asset, amount in payload["benchmark"]["amounts"].items()
            },
            amounts,
        )

    def test_deposit_requires_updated_wallet_balances(self) -> None:
        amounts = {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10}
        prices = {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1}
        self._record(amounts, prices, START)

        with self.assertRaisesRegex(ValueError, "not yet reflected"):
            self._record(
                amounts,
                prices,
                LATER,
                deposit_eur=100,
                deposit_fee_bps=50,
            )

    def test_first_snapshot_cannot_also_be_a_later_deposit(self) -> None:
        with self.assertRaisesRegex(ValueError, "Initialize tracking"):
            self._record(
                {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10},
                {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1},
                START,
                deposit_eur=100,
                deposit_fee_bps=50,
            )

    def test_rejects_snapshot_before_july_28_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "predates benchmark start"):
            self._record(
                {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10},
                {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1},
                datetime(2026, 7, 27, 23, 59, tzinfo=timezone.utc),
            )

    def test_existing_tracker_cannot_silently_change_start_date(self) -> None:
        amounts = {"BTC": 50, "ETH": 25, "SOL": 15, "LINK": 10}
        prices = {"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1}
        self._record(amounts, prices, START)

        with self.assertRaisesRegex(ValueError, "started on 2026-07-28"):
            record_rebalance(
                Holdings(amounts=amounts, fetched_at=LATER),
                PriceBook(prices_eur=prices, as_of=LATER),
                data_path=self.data_path,
                chart_dir=self.chart_dir,
                start_date=date(2026, 7, 29),
            )


if __name__ == "__main__":
    unittest.main()
