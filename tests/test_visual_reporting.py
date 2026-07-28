from __future__ import annotations

import unittest
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image, ImageColor

from wallet_rebalancer.models import Holdings, PriceBook
from wallet_rebalancer.planner import build_plan
from wallet_rebalancer.visual_reporting import (
    BUY,
    BUY_SURFACE,
    SELL,
    SELL_SURFACE,
    WIDTH,
    render_action_image,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
PRICES = PriceBook(
    prices_usd={"BTC": 1, "ETH": 1, "SOL": 1, "LINK": 1},
    as_of=NOW,
    source="test",
)


class VisualReportTests(unittest.TestCase):
    def open_report(self, amounts: dict[str, int]) -> Image.Image:
        plan = build_plan(
            Holdings(amounts=amounts, fetched_at=NOW),
            PRICES,
        )
        image = Image.open(BytesIO(render_action_image(plan)))
        image.load()
        return image

    def test_trade_report_contains_green_buy_and_red_sell_cards(self) -> None:
        image = self.open_report(
            {"BTC": 800, "ETH": 100, "SOL": 50, "LINK": 50}
        )
        colors = {
            color
            for _, color in image.getcolors(
                maxcolors=image.width * image.height
            )
        }

        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.width, WIDTH)
        self.assertIn(ImageColor.getrgb(BUY), colors)
        self.assertIn(ImageColor.getrgb(BUY_SURFACE), colors)
        self.assertIn(ImageColor.getrgb(SELL), colors)
        self.assertIn(ImageColor.getrgb(SELL_SURFACE), colors)

    def test_balanced_report_renders_no_action_card(self) -> None:
        image = self.open_report(
            {"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100}
        )
        colors = {
            color
            for _, color in image.getcolors(
                maxcolors=image.width * image.height
            )
        }

        self.assertIn(ImageColor.getrgb(BUY_SURFACE), colors)
        self.assertNotIn(ImageColor.getrgb(SELL_SURFACE), colors)


if __name__ == "__main__":
    unittest.main()
