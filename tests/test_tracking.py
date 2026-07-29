from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from wallet_rebalancer.models import Holdings, PriceBook
from wallet_rebalancer.tracking import record_rebalance, render_performance


START = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


class PortfolioTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.data_path = self.root / "tracking.json"
        self.chart_dir = self.root / "charts"

    def _record(
        self,
        amounts: dict[str, int],
        prices: dict[str, int],
        timestamp: datetime,
        *,
        note: str = "",
    ):
        return record_rebalance(
            Holdings(amounts=amounts, fetched_at=timestamp),
            PriceBook(prices_eur=prices, as_of=timestamp, source="test"),
            data_path=self.data_path,
            chart_dir=self.chart_dir,
            start_date=date(2026, 7, 28),
            note=note,
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
        self.assertTrue(summary.csv_path.exists())

        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["benchmark"]["start_date"], "2026-07-28")
        self.assertEqual(
            payload["benchmark"]["amounts"],
            {"BTC": "50", "ETH": "25", "SOL": "15", "LINK": "10"},
        )
        self.assertEqual(payload["observations"][0]["note"], "Initial allocation")
        value_chart = summary.value_chart_path.read_text(encoding="utf-8")
        self.assertIn("Rebalanced", value_chart)
        self.assertIn("Buy &amp; hold", value_chart)

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
