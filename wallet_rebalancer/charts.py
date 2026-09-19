"""Dark-mode PNG charts used by scheduled Telegram reports."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import PortfolioPlan

BACKGROUND = "#0f172a"
GRID = "#334155"
TEXT = "#e2e8f0"
MUTED = "#94a3b8"
PERFORMANCE_BLUE = "#3b82f6"
PERFORMANCE_BLUE_RGB = (59, 130, 246)
PERFORMANCE_AMBER = "#f59e0b"
PERFORMANCE_GREEN = "#22c55e"
PERFORMANCE_RED = "#ef4444"
TARGET = "#f8fafc"
ASSET_COLORS = {
    "BTC": "#fbbf24",
    "ETH": "#8b5cf6",
    "SOL": "#22c55e",
    "LINK": "#3b82f6",
}


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        [
            "DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    # Pillow's bundled bitmap font keeps chart creation independent of host
    # font packages in minimal systemd/container installations.
    return ImageFont.load_default(size=size)


def _save(image: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".png",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        image.save(temporary_path, format="PNG", optimize=True)
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return path


def _money(value: Decimal) -> str:
    return f"€{value:,.0f}"


def _money_signed(value: Decimal) -> str:
    return f"€{value:+,.0f}"


def _percent(value: Decimal) -> str:
    return f"{value * 100:+.1f}%"


def _dotted_line(
    draw: ImageDraw.ImageDraw,
    *,
    start: int,
    end: int,
    y: int,
    color: str,
    width: int = 4,
) -> None:
    for position in range(start, end, 16):
        draw.line(
            (position, y, min(position + 8, end), y),
            fill=color,
            width=width,
        )


def render_allocation_chart(
    plan: PortfolioPlan,
    path: Path = Path("reports/portfolio_allocation.png"),
) -> Path:
    """Render EUR balances against target allocation on a shared scale."""

    image = Image.new("RGB", (1200, 675), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font, body_font = _font(30, bold=True), _font(18)
    label_font, small_font = _font(20, bold=True), _font(15)
    left, right, top, bottom = 115, 1085, 130, 570
    plot_height = bottom - top
    rows = list(plan.assets)
    maximum_weight = max(
        [row.current_weight for row in rows]
        + [row.target_weight for row in rows]
        + [Decimal("0.01")]
    )
    axis_max = max(Decimal("0.10"), maximum_weight * Decimal("1.18"))

    def y(weight: Decimal) -> int:
        return bottom - int(float(weight / axis_max) * plot_height)

    draw.text((left, 34), "Portfolio allocation", fill=TEXT, font=title_font)
    timestamp = plan.prices_as_of.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    draw.text(
        (left, 78),
        f"Current value {_money(plan.current_total_eur)}  ·  prices {timestamp}",
        fill=MUTED,
        font=body_font,
    )

    for tick in range(6):
        weight = axis_max * Decimal(tick) / Decimal(5)
        position = y(weight)
        draw.line((left, position, right, position), fill=GRID, width=1)
        draw.text(
            (left - 14, position),
            _money(plan.current_total_eur * weight),
            fill=MUTED,
            font=small_font,
            anchor="rm",
        )
        draw.text(
            (right + 14, position),
            f"{weight * 100:.0f}%",
            fill=MUTED,
            font=small_font,
            anchor="lm",
        )

    slot = (right - left) / len(rows)
    for index, row in enumerate(rows):
        center = int(left + slot * (index + 0.5))
        half_width = int(slot * 0.28)
        bar_top = y(row.current_weight)
        draw.rounded_rectangle(
            (center - half_width, bar_top, center + half_width, bottom),
            radius=9,
            fill=ASSET_COLORS[row.asset],
        )
        draw.text(
            (center, min(bottom - 18, bar_top + 22)),
            f"{_money(row.current_value_eur)}  ({row.current_weight * 100:.1f}%)",
            fill=TEXT,
            font=small_font,
            anchor="mm",
        )
        draw.text(
            (center, bottom + 30),
            row.asset,
            fill=TEXT,
            font=label_font,
            anchor="mm",
        )
        target_y = y(row.target_weight)
        _dotted_line(
            draw,
            start=center - half_width - 18,
            end=center + half_width + 18,
            y=target_y,
            color=TARGET,
        )
        draw.text(
            (center, target_y - 10),
            f"target {row.target_weight * 100:.0f}%",
            fill=TARGET,
            font=small_font,
            anchor="mb",
        )

    _dotted_line(
        draw,
        start=left,
        end=left + 38,
        y=633,
        color=TARGET,
    )
    draw.text(
        (left + 50, 633),
        "Intended allocation",
        fill=TEXT,
        font=small_font,
        anchor="lm",
    )
    return _save(image, path)


def _period_returns(
    cumulative: list[tuple[datetime, Decimal]],
) -> list[Decimal]:
    """Derive period-over-period returns from a chained cumulative return."""

    one = Decimal("1")
    periods = [Decimal(0)]
    for index in range(1, len(cumulative)):
        previous_base = one + cumulative[index - 1][1]
        current_base = one + cumulative[index][1]
        if previous_base == 0:
            periods.append(Decimal(0))
            continue
        periods.append(current_base / previous_base - one)
    return periods


def render_performance_chart(
    *,
    actual: Iterable[tuple[datetime, Decimal]],
    benchmark: Iterable[tuple[datetime, Decimal]],
    actual_pnl: Iterable[tuple[datetime, Decimal]],
    benchmark_pnl: Iterable[tuple[datetime, Decimal]],
    actual_returns: Iterable[tuple[datetime, Decimal]],
    start_date: str,
    path: Path,
) -> Path:
    """Render three stacked panels for rebalanced vs buy-and-hold:

    a cumulative PnL line chart on top, an equity curve of portfolio value
    in the middle, and the rebalanced portfolio's period-over-period return
    as bars underneath.
    """

    actual_points, benchmark_points = list(actual), list(benchmark)
    actual_pnl_points, benchmark_pnl_points = list(actual_pnl), list(benchmark_pnl)
    period_returns = _period_returns(list(actual_returns))
    count = len(actual_points)

    image = Image.new("RGB", (1200, 820), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font, body_font = _font(30, bold=True), _font(18)
    small_font = _font(15)

    left, right = 115, 1145
    pnl_top, pnl_bottom = 140, 290
    value_top, value_bottom = 335, 575
    returns_top, returns_bottom = 615, 700
    axis_label_y = 730
    summary_y = 785

    def x(index: int) -> int:
        if count == 1:
            return (left + right) // 2
        return left + int(index * (right - left) / (count - 1))

    draw.text((left, 34), "Portfolio performance", fill=TEXT, font=title_font)
    draw.text(
        (left, 78),
        f"Rebalancing vs buy-and-hold  ·  benchmark since {start_date}",
        fill=MUTED,
        font=body_font,
    )
    for label, color, legend_x in (
        ("Rebalanced", PERFORMANCE_BLUE, 760),
        ("Buy & hold", PERFORMANCE_AMBER, 955),
    ):
        draw.line((legend_x, 47, legend_x + 30, 47), fill=color, width=5)
        draw.text((legend_x + 40, 47), label, fill=TEXT, font=small_font, anchor="lm")

    # Cumulative PnL line chart: profit/loss in EUR versus capital contributed.
    draw.text(
        (left, pnl_top - 24),
        "Cumulative PnL (EUR)",
        fill=MUTED,
        font=small_font,
    )
    pnl_values = [value for _, value in actual_pnl_points + benchmark_pnl_points]
    pnl_low = min(pnl_values + [Decimal(0)])
    pnl_high = max(pnl_values + [Decimal(0)])
    pnl_padding = max(
        (pnl_high - pnl_low) * Decimal("0.10"),
        max(abs(pnl_high), abs(pnl_low)) * Decimal("0.02"),
        Decimal(1),
    )
    pnl_low, pnl_high = pnl_low - pnl_padding, pnl_high + pnl_padding
    pnl_span = pnl_high - pnl_low

    def pnl_y(value: Decimal) -> int:
        fraction = float((pnl_high - value) / pnl_span)
        return pnl_top + int(fraction * (pnl_bottom - pnl_top))

    for tick in range(6):
        value = pnl_high - pnl_span * Decimal(tick) / Decimal(5)
        position = pnl_y(value)
        draw.line((left, position, right, position), fill=GRID, width=1)
        draw.text(
            (left - 14, position),
            _money_signed(value),
            fill=MUTED,
            font=small_font,
            anchor="rm",
        )
    draw.line(
        (left, pnl_y(Decimal(0)), right, pnl_y(Decimal(0))),
        fill=MUTED,
        width=1,
    )

    actual_pnl_coordinates = [
        (x(index), pnl_y(value)) for index, (_, value) in enumerate(actual_pnl_points)
    ]
    benchmark_pnl_coordinates = [
        (x(index), pnl_y(value))
        for index, (_, value) in enumerate(benchmark_pnl_points)
    ]
    if len(benchmark_pnl_coordinates) > 1:
        draw.line(
            benchmark_pnl_coordinates,
            fill=PERFORMANCE_AMBER,
            width=4,
            joint="curve",
        )
    if len(actual_pnl_coordinates) > 1:
        draw.line(
            actual_pnl_coordinates,
            fill=PERFORMANCE_BLUE,
            width=4,
            joint="curve",
        )

    # Equity curve: portfolio value in EUR.
    draw.text(
        (left, value_top - 24),
        "Portfolio value (EUR)",
        fill=MUTED,
        font=small_font,
    )
    values = [value for _, value in actual_points + benchmark_points]
    low, high = min(values), max(values)
    padding = max(
        (high - low) * Decimal("0.10"),
        abs(high) * Decimal("0.02"),
        Decimal(1),
    )
    low, high = low - padding, high + padding
    span = high - low

    def value_y(value: Decimal) -> int:
        fraction = float((high - value) / span)
        return value_top + int(fraction * (value_bottom - value_top))

    for tick in range(6):
        value = high - span * Decimal(tick) / Decimal(5)
        position = value_y(value)
        draw.line((left, position, right, position), fill=GRID, width=1)
        draw.text(
            (left - 14, position),
            _money(value),
            fill=MUTED,
            font=small_font,
            anchor="rm",
        )

    actual_coordinates = [
        (x(index), value_y(value)) for index, (_, value) in enumerate(actual_points)
    ]
    benchmark_coordinates = [
        (x(index), value_y(value))
        for index, (_, value) in enumerate(benchmark_points)
    ]

    # Shade the area under the rebalanced (actual) line for an equity-curve look.
    if len(actual_coordinates) > 1:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).polygon(
            [
                *actual_coordinates,
                (actual_coordinates[-1][0], value_bottom),
                (actual_coordinates[0][0], value_bottom),
            ],
            fill=(*PERFORMANCE_BLUE_RGB, 40),
        )
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(image)

    if len(benchmark_coordinates) > 1:
        draw.line(benchmark_coordinates, fill=PERFORMANCE_AMBER, width=4, joint="curve")
    if len(actual_coordinates) > 1:
        draw.line(actual_coordinates, fill=PERFORMANCE_BLUE, width=4, joint="curve")

    # Period-return bar chart: green above zero, red below zero.
    draw.text(
        (left, returns_top - 24),
        "Rebalanced return per period",
        fill=MUTED,
        font=small_font,
    )
    returns_mid = (returns_top + returns_bottom) // 2
    half_height = (returns_bottom - returns_top) / 2
    magnitude = max((abs(value) for value in period_returns), default=Decimal(0))
    scale = magnitude * Decimal("1.15") if magnitude > 0 else Decimal("0.01")

    def bar_y(value: Decimal) -> int:
        return returns_mid - int(float(value / scale) * half_height)

    for fraction in (Decimal(1), Decimal("0.5"), Decimal(0), Decimal("-0.5"), Decimal(-1)):
        tick_value = scale * fraction
        position = bar_y(tick_value)
        draw.line(
            (left, position, right, position),
            fill=MUTED if fraction == 0 else GRID,
            width=1,
        )
        draw.text(
            (left - 14, position),
            _percent(tick_value),
            fill=MUTED,
            font=small_font,
            anchor="rm",
        )

    slot = (right - left) / (count - 1) if count > 1 else (right - left)
    bar_half_width = max(2, int(slot * 0.35))
    for index, period_return in enumerate(period_returns):
        center = x(index)
        top_y = bar_y(max(period_return, Decimal(0)))
        bottom_y = bar_y(min(period_return, Decimal(0)))
        color = PERFORMANCE_GREEN if period_return >= 0 else PERFORMANCE_RED
        draw.rectangle(
            (center - bar_half_width, top_y, center + bar_half_width, bottom_y),
            fill=color,
        )

    indexes = sorted(
        {
            0,
            count - 1,
            *(round(i * (count - 1) / 4) for i in range(5)),
        }
    )
    for index in indexes:
        timestamp = actual_points[index][0]
        draw.text(
            (x(index), axis_label_y),
            timestamp.strftime("%Y-%m-%d"),
            fill=MUTED,
            font=small_font,
            anchor="mm",
        )

    latest_actual = actual_points[-1][1]
    latest_benchmark = benchmark_points[-1][1]
    difference = latest_actual - latest_benchmark
    draw.text(
        (left, summary_y),
        f"Latest: {_money(latest_actual)} vs {_money(latest_benchmark)}  ·  difference {difference:+,.0f} EUR",
        fill=TEXT,
        font=body_font,
        anchor="lm",
    )
    return _save(image, path)
