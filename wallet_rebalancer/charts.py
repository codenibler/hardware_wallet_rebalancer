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
BLUE = "#3b82f6"
AMBER = "#f59e0b"


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
    targets: list[tuple[int, int]] = []
    for index, row in enumerate(rows):
        center = int(left + slot * (index + 0.5))
        half_width = int(slot * 0.28)
        bar_top = y(row.current_weight)
        draw.rounded_rectangle(
            (center - half_width, bar_top, center + half_width, bottom),
            radius=9,
            fill=BLUE,
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
        targets.append((center, y(row.target_weight)))

    if len(targets) > 1:
        draw.line(targets, fill=AMBER, width=5, joint="curve")
    for (center, position), row in zip(targets, rows):
        draw.ellipse(
            (center - 7, position - 7, center + 7, position + 7),
            fill=BACKGROUND,
            outline=AMBER,
            width=4,
        )
        draw.text(
            (center, position - 18),
            f"target {row.target_weight * 100:.0f}%",
            fill=AMBER,
            font=small_font,
            anchor="ms",
        )

    draw.rectangle((left, 625, left + 24, 641), fill=BLUE)
    draw.text(
        (left + 34, 633),
        "Balance (EUR)",
        fill=TEXT,
        font=small_font,
        anchor="lm",
    )
    draw.line((left + 205, 633, left + 235, 633), fill=AMBER, width=4)
    draw.text(
        (left + 245, 633),
        "Desired allocation",
        fill=TEXT,
        font=small_font,
        anchor="lm",
    )
    return _save(image, path)


def render_performance_chart(
    *,
    actual: Iterable[tuple[datetime, Decimal]],
    benchmark: Iterable[tuple[datetime, Decimal]],
    start_date: str,
    path: Path,
) -> Path:
    """Render historical rebalanced and buy-and-hold portfolio values."""

    actual_points, benchmark_points = list(actual), list(benchmark)
    image = Image.new("RGB", (1200, 675), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font, body_font = _font(30, bold=True), _font(18)
    small_font = _font(15)
    left, right, top, bottom = 115, 1145, 130, 570
    values = [value for _, value in actual_points + benchmark_points]
    low, high = min(values), max(values)
    padding = max(
        (high - low) * Decimal("0.10"),
        abs(high) * Decimal("0.02"),
        Decimal(1),
    )
    low, high = low - padding, high + padding
    span = high - low

    def x(index: int) -> int:
        if len(actual_points) == 1:
            return (left + right) // 2
        return left + int(index * (right - left) / (len(actual_points) - 1))

    def y(value: Decimal) -> int:
        return top + int(float((high - value) / span) * (bottom - top))

    draw.text((left, 34), "Portfolio performance", fill=TEXT, font=title_font)
    draw.text(
        (left, 78),
        f"Rebalancing vs buy-and-hold  ·  benchmark since {start_date}",
        fill=MUTED,
        font=body_font,
    )
    for tick in range(6):
        value = high - span * Decimal(tick) / Decimal(5)
        position = y(value)
        draw.line((left, position, right, position), fill=GRID, width=1)
        draw.text(
            (left - 14, position),
            _money(value),
            fill=MUTED,
            font=small_font,
            anchor="rm",
        )

    for label, points, color in (
        ("Rebalanced", actual_points, BLUE),
        ("Buy & hold", benchmark_points, AMBER),
    ):
        coordinates = [(x(index), y(value)) for index, (_, value) in enumerate(points)]
        if len(coordinates) > 1:
            draw.line(coordinates, fill=color, width=5, joint="curve")
        for point in coordinates:
            draw.ellipse(
                (point[0] - 5, point[1] - 5, point[0] + 5, point[1] + 5),
                fill=BACKGROUND,
                outline=color,
                width=3,
            )
        legend_x = 760 if label == "Rebalanced" else 955
        draw.line((legend_x, 47, legend_x + 30, 47), fill=color, width=5)
        draw.text((legend_x + 40, 47), label, fill=TEXT, font=small_font, anchor="lm")

    indexes = sorted(
        {
            0,
            len(actual_points) - 1,
            *(round(i * (len(actual_points) - 1) / 4) for i in range(5)),
        }
    )
    for index in indexes:
        timestamp = actual_points[index][0]
        draw.text(
            (x(index), bottom + 30),
            timestamp.strftime("%Y-%m-%d"),
            fill=MUTED,
            font=small_font,
            anchor="mm",
        )

    latest_actual = actual_points[-1][1]
    latest_benchmark = benchmark_points[-1][1]
    difference = latest_actual - latest_benchmark
    draw.text(
        (left, 633),
        f"Latest: {_money(latest_actual)} vs {_money(latest_benchmark)}  ·  difference {difference:+,.0f} EUR",
        fill=TEXT,
        font=body_font,
        anchor="lm",
    )
    return _save(image, path)
