"""Render Telegram portfolio reports as clear visual action cards."""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import PortfolioPlan, TradeInstruction


WIDTH = 1200
MARGIN = 64
CARD_RADIUS = 28
BACKGROUND = "#F3F6FA"
SURFACE = "#FFFFFF"
INK = "#172033"
MUTED = "#647084"
LINE = "#DDE3EB"
BUY = "#138A4B"
BUY_SURFACE = "#E6F7EE"
SELL = "#D13C45"
SELL_SURFACE = "#FCEBED"
ACCENT = "#335CFF"
NO_ACTION = "#138A4B"

_REGULAR_FONT_PATHS = (
    Path("/usr/share/fonts/noto/NotoSans-Regular.ttf"),
    Path("/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
)
_BOLD_FONT_PATHS = (
    Path("/usr/share/fonts/noto/NotoSans-Bold.ttf"),
    Path("/usr/share/fonts/Adwaita/AdwaitaMono-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = _BOLD_FONT_PATHS if bold else _REGULAR_FONT_PATHS
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _percent(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def _amount(trade: TradeInstruction) -> str:
    decimals = {"BTC": 8, "ETH": 8, "SOL": 8, "LINK": 6}[trade.asset]
    return f"{trade.amount:,.{decimals}f}"


def _text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    value: str,
    *,
    size: int,
    color: str = INK,
    bold: bool = False,
    anchor: str | None = None,
) -> None:
    draw.text(
        xy,
        value,
        fill=color,
        font=_font(size, bold=bold),
        anchor=anchor,
    )


def _pill(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    *,
    fill: str,
    text_color: str = "#FFFFFF",
) -> None:
    draw.rounded_rectangle(box, radius=24, fill=fill)
    center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    _text(
        draw,
        center,
        label,
        size=25,
        color=text_color,
        bold=True,
        anchor="mm",
    )


def _status(plan: PortfolioPlan) -> tuple[str, str]:
    if plan.threshold_rebalance_needed:
        return "REBALANCE REQUIRED", SELL
    if plan.has_top_up:
        return "DEPLOY TOP-UP", ACCENT
    return "NO ACTION REQUIRED", NO_ACTION


def _trade_card(
    draw: ImageDraw.ImageDraw,
    trade: TradeInstruction,
    *,
    index: int,
    y: int,
) -> None:
    color = BUY if trade.side == "BUY" else SELL
    surface = BUY_SURFACE if trade.side == "BUY" else SELL_SURFACE
    draw.rounded_rectangle(
        (MARGIN, y, WIDTH - MARGIN, y + 150),
        radius=CARD_RADIUS,
        fill=surface,
    )
    _pill(
        draw,
        (MARGIN + 24, y + 37, MARGIN + 174, y + 113),
        f"{index}  {trade.side}",
        fill=color,
    )
    _text(
        draw,
        (MARGIN + 205, y + 36),
        f"{trade.side.title()} {_amount(trade)} {trade.asset}",
        size=36,
        bold=True,
    )
    _text(
        draw,
        (MARGIN + 205, y + 91),
        f"Snapshot price  {_money(trade.snapshot_price_usd)} / {trade.asset}",
        size=24,
        color=MUTED,
    )
    _text(
        draw,
        (WIDTH - MARGIN - 28, y + 61),
        _money(trade.notional_usd),
        size=38,
        color=color,
        bold=True,
        anchor="rm",
    )
    _text(
        draw,
        (WIDTH - MARGIN - 28, y + 105),
        "estimated notional",
        size=21,
        color=MUTED,
        anchor="rm",
    )


def render_action_image(plan: PortfolioPlan) -> bytes:
    """Return a PNG report with green buy cards and red sell cards."""

    trade_count = len(plan.trades) if plan.has_trade_plan else 0
    action_height = max(180, trade_count * 174)
    height = 690 + action_height
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    status, status_color = _status(plan)
    _text(draw, (MARGIN, 62), "PORTFOLIO REBALANCE", size=48, bold=True)
    _text(
        draw,
        (MARGIN, 125),
        "BTC 50%  •  ETH 25%  •  SOL 15%  •  LINK 10%",
        size=25,
        color=MUTED,
    )
    _pill(
        draw,
        (WIDTH - MARGIN - 345, 56, WIDTH - MARGIN, 120),
        status,
        fill=status_color,
    )

    draw.rounded_rectangle(
        (MARGIN, 190, WIDTH - MARGIN, 330),
        radius=CARD_RADIUS,
        fill=SURFACE,
    )
    metrics = (
        ("CURRENT VALUE", _money(plan.current_total_usd)),
        ("NEW CAPITAL", _money(plan.top_up_usd)),
        ("EST. FEES", _money(plan.estimated_fees_usd)),
    )
    metric_width = (WIDTH - 2 * MARGIN) // 3
    for index, (label, value) in enumerate(metrics):
        x = MARGIN + index * metric_width
        if index:
            draw.line((x, 220, x, 300), fill=LINE, width=2)
        _text(draw, (x + 34, 221), label, size=21, color=MUTED, bold=True)
        _text(draw, (x + 34, 261), value, size=34, bold=True)

    _text(draw, (MARGIN, 382), "ALLOCATION", size=27, color=MUTED, bold=True)
    draw.rounded_rectangle(
        (MARGIN, 425, WIDTH - MARGIN, 548),
        radius=CARD_RADIUS,
        fill=SURFACE,
    )
    asset_width = (WIDTH - 2 * MARGIN) // len(plan.assets)
    for index, asset in enumerate(plan.assets):
        x = MARGIN + index * asset_width
        if index:
            draw.line((x, 452, x, 521), fill=LINE, width=2)
        _text(draw, (x + 30, 450), asset.asset, size=26, bold=True)
        _text(
            draw,
            (x + 30, 493),
            f"{_percent(asset.current_weight)}  →  "
            f"{_percent(asset.target_weight)}",
            size=25,
            color=ACCENT,
            bold=True,
        )

    action_top = 614
    if trade_count:
        _text(
            draw,
            (MARGIN, 584),
            "ACTIONS — SELL FIRST, THEN BUY",
            size=27,
            color=MUTED,
            bold=True,
        )
        for index, trade in enumerate(plan.trades, start=1):
            _trade_card(
                draw,
                trade,
                index=index,
                y=action_top + (index - 1) * 174,
            )
    else:
        _text(draw, (MARGIN, 584), "ACTION", size=27, color=MUTED, bold=True)
        draw.rounded_rectangle(
            (MARGIN, action_top, WIDTH - MARGIN, action_top + 150),
            radius=CARD_RADIUS,
            fill=BUY_SURFACE,
        )
        _pill(
            draw,
            (MARGIN + 24, action_top + 37, MARGIN + 240, action_top + 113),
            "COMPLETE",
            fill=NO_ACTION,
        )
        _text(
            draw,
            (MARGIN + 275, action_top + 75),
            "No transactions are required.",
            size=36,
            color=NO_ACTION,
            bold=True,
            anchor="lm",
        )

    _text(
        draw,
        (MARGIN, height - 46),
        f"Prices: {plan.prices_as_of:%Y-%m-%d %H:%M UTC}  •  "
        f"Maximum drift: {_percent(plan.max_abs_drift)}",
        size=21,
        color=MUTED,
    )

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
