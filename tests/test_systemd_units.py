from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ScheduledPortfolioCheckTests(unittest.TestCase):
    def test_daily_tracking_service_also_runs_noninteractive_balance_check(
        self,
    ) -> None:
        service = (
            ROOT / "deploy/systemd/hwr-tracking.service.example"
        ).read_text(encoding="utf-8")

        tracking_command = "python tracking.py --note"
        balance_command = "python main.py --no-prompt"
        self.assertIn("Type=oneshot", service)
        self.assertIn(tracking_command, service)
        self.assertIn(balance_command, service)
        self.assertLess(
            service.index(tracking_command),
            service.index(balance_command),
        )

    def test_combined_service_is_triggered_daily_at_20_00(self) -> None:
        timer = (
            ROOT / "deploy/systemd/hwr-tracking.timer.example"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "OnCalendar=*-*-* 20:00:00 Europe/Amsterdam",
            timer,
        )
        self.assertIn("Unit=hwr-tracking.service", timer)


if __name__ == "__main__":
    unittest.main()
