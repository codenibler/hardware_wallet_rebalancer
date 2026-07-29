"""Persistent comparison of the rebalanced portfolio with buy-and-hold."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from typing import Iterable, Mapping

from .models import ASSETS, ZERO, Holdings, PriceBook, decimal_map


SCHEMA_VERSION = 1
DEFAULT_START_DATE = date(2026, 7, 28)
DEFAULT_DATA_PATH = Path("reports/portfolio_tracking.json")
DEFAULT_CHART_DIR = Path("reports")


@dataclass(frozen=True)
class PerformanceSummary:
    """Latest persisted comparison, with returns represented as ratios."""

    start_date: date
    observations: int
    actual_value_eur: Decimal
    buy_hold_value_eur: Decimal
    actual_return: Decimal
    buy_hold_return: Decimal
    outperformance: Decimal
    value_difference_eur: Decimal
    data_path: Path
    value_chart_path: Path
    returns_chart_path: Path
    csv_path: Path

    @property
    def verdict(self) -> str:
        if self.value_difference_eur > ZERO:
            return "REBALANCING AHEAD"
        if self.value_difference_eur < ZERO:
            return "BUY-AND-HOLD AHEAD"
        return "TIED"

    def to_dict(self) -> dict[str, object]:
        return {
            "start_date": self.start_date.isoformat(),
            "observations": self.observations,
            "verdict": self.verdict.lower().replace("-", "_").replace(" ", "_"),
            "actual_value_eur": str(self.actual_value_eur),
            "buy_hold_value_eur": str(self.buy_hold_value_eur),
            "actual_return": str(self.actual_return),
            "buy_hold_return": str(self.buy_hold_return),
            "outperformance": str(self.outperformance),
            "value_difference_eur": str(self.value_difference_eur),
            "data_path": str(self.data_path),
            "value_chart_path": str(self.value_chart_path),
            "returns_chart_path": str(self.returns_chart_path),
            "csv_path": str(self.csv_path),
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _value(
    amounts: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
) -> Decimal:
    return sum((amounts[asset] * prices[asset] for asset in ASSETS), ZERO)


def _asset_strings(values: Mapping[str, Decimal]) -> dict[str, str]:
    return {asset: str(values[asset]) for asset in ASSETS}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _read_store(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Tracking data is not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Tracking data must contain a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported tracking schema in {path}; "
            f"expected version {SCHEMA_VERSION}"
        )
    benchmark = raw.get("benchmark")
    observations = raw.get("observations")
    if not isinstance(benchmark, dict) or not isinstance(observations, list):
        raise ValueError("Tracking data is missing benchmark or observations")
    return raw


def _validate_store(
    payload: dict[str, object],
) -> tuple[date, dict[str, Decimal], Decimal, list[dict[str, object]]]:
    benchmark = payload["benchmark"]
    observations = payload["observations"]
    assert isinstance(benchmark, dict)
    assert isinstance(observations, list)

    try:
        start_date = date.fromisoformat(str(benchmark["start_date"]))
        amounts_raw = benchmark["amounts"]
        initial_value = _decimal(
            benchmark["initial_value_eur"],
            "benchmark.initial_value_eur",
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("Tracking benchmark is incomplete or invalid") from exc
    if not isinstance(amounts_raw, dict):
        raise ValueError("Tracking benchmark amounts must be an object")
    amounts = decimal_map(amounts_raw)
    if initial_value <= ZERO:
        raise ValueError("Tracking benchmark initial value must be positive")

    validated_observations: list[dict[str, object]] = []
    previous_time: datetime | None = None
    required = {
        "recorded_at",
        "actual_value_eur",
        "buy_hold_value_eur",
        "actual_return",
        "buy_hold_return",
        "outperformance",
        "value_difference_eur",
    }
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict) or not required <= observation.keys():
            raise ValueError(f"Tracking observation {index} is incomplete")
        try:
            recorded_at = _utc(
                datetime.fromisoformat(
                    str(observation["recorded_at"]).replace("Z", "+00:00")
                )
            )
        except ValueError as exc:
            raise ValueError(
                f"Tracking observation {index} has an invalid timestamp"
            ) from exc
        if previous_time is not None and recorded_at <= previous_time:
            raise ValueError("Tracking observations must be strictly chronological")
        previous_time = recorded_at
        for key in required - {"recorded_at"}:
            _decimal(observation[key], f"observations[{index}].{key}")
        validated_observations.append(observation)

    if not validated_observations:
        raise ValueError("Tracking data must contain at least one observation")
    return start_date, amounts, initial_value, validated_observations


def _observation(
    *,
    recorded_at: datetime,
    holdings: Holdings,
    prices: PriceBook,
    actual_amounts: Mapping[str, Decimal],
    benchmark_amounts: Mapping[str, Decimal],
    initial_value: Decimal,
    note: str,
) -> dict[str, object]:
    normalized_prices = prices.normalized()
    actual_value = _value(actual_amounts, normalized_prices)
    buy_hold_value = _value(benchmark_amounts, normalized_prices)
    actual_return = actual_value / initial_value - Decimal("1")
    buy_hold_return = buy_hold_value / initial_value - Decimal("1")
    value_difference = actual_value - buy_hold_value
    return {
        "recorded_at": _iso(recorded_at),
        "holdings_as_of": _iso(holdings.fetched_at),
        "prices_as_of": _iso(prices.as_of),
        "price_source": prices.source,
        "note": note,
        "actual_amounts": _asset_strings(actual_amounts),
        "prices_eur": _asset_strings(normalized_prices),
        "actual_value_eur": str(actual_value),
        "buy_hold_value_eur": str(buy_hold_value),
        "actual_return": str(actual_return),
        "buy_hold_return": str(buy_hold_return),
        "outperformance": str(actual_return - buy_hold_return),
        "value_difference_eur": str(value_difference),
    }


def _points(
    observations: Iterable[dict[str, object]],
    key: str,
) -> list[tuple[datetime, Decimal]]:
    return [
        (
            _utc(
                datetime.fromisoformat(
                    str(row["recorded_at"]).replace("Z", "+00:00")
                )
            ),
            _decimal(row[key], key),
        )
        for row in observations
    ]


def _format_euros(value: Decimal) -> str:
    return f"€{value:,.0f}"


def _format_percent(value: Decimal) -> str:
    return f"{value * 100:+.1f}%"


def _tick_indexes(length: int, maximum: int = 6) -> list[int]:
    if length <= maximum:
        return list(range(length))
    return sorted(
        {round(index * (length - 1) / (maximum - 1)) for index in range(maximum)}
    )


def _line_chart(
    *,
    title: str,
    subtitle: str,
    actual: list[tuple[datetime, Decimal]],
    benchmark: list[tuple[datetime, Decimal]],
    formatter,
    include_zero: bool,
) -> str:
    width, height = 1000, 560
    left, right, top, bottom = 105, 35, 80, 80
    plot_width = width - left - right
    plot_height = height - top - bottom
    all_values = [value for _, value in actual + benchmark]
    low = min(all_values)
    high = max(all_values)
    if include_zero:
        low = min(low, ZERO)
        high = max(high, ZERO)
    span = high - low
    minimum_padding = Decimal("0.01") if include_zero else Decimal("1")
    padding = (
        span * Decimal("0.08")
        if span
        else max(abs(high) * Decimal("0.08"), minimum_padding)
    )
    low -= padding
    high += padding
    span = high - low
    count = len(actual)

    def x(index: int) -> float:
        if count == 1:
            return left + plot_width / 2
        return left + index * plot_width / (count - 1)

    def y(value: Decimal) -> float:
        fraction = float((high - value) / span)
        return top + fraction * plot_height

    def polyline(points: list[tuple[datetime, Decimal]]) -> str:
        return " ".join(
            f"{x(index):.2f},{y(value):.2f}"
            for index, (_, value) in enumerate(points)
        )

    elements = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{escape(title)}">'
        ),
        "<style>"
        "text{font-family:Inter,ui-sans-serif,system-ui,sans-serif;fill:#334155}"
        ".title{font-size:24px;font-weight:700;fill:#0f172a}"
        ".subtitle{font-size:13px;fill:#64748b}"
        ".axis{font-size:12px;fill:#64748b}"
        ".legend{font-size:13px;font-weight:600}"
        "</style>",
        f'<rect width="{width}" height="{height}" rx="16" fill="#ffffff"/>',
        f'<text x="{left}" y="34" class="title">{escape(title)}</text>',
        f'<text x="{left}" y="58" class="subtitle">{escape(subtitle)}</text>',
    ]

    for tick in range(6):
        value = high - span * Decimal(tick) / Decimal("5")
        position = y(value)
        elements.extend(
            [
                (
                    f'<line x1="{left}" y1="{position:.2f}" '
                    f'x2="{left + plot_width}" y2="{position:.2f}" '
                    'stroke="#e2e8f0" stroke-width="1"/>'
                ),
                (
                    f'<text x="{left - 12}" y="{position + 4:.2f}" '
                    f'text-anchor="end" class="axis">'
                    f"{escape(formatter(value))}</text>"
                ),
            ]
        )

    for index in _tick_indexes(count):
        position = x(index)
        label = actual[index][0].strftime("%Y-%m-%d")
        elements.extend(
            [
                (
                    f'<line x1="{position:.2f}" y1="{top + plot_height}" '
                    f'x2="{position:.2f}" y2="{top + plot_height + 6}" '
                    'stroke="#94a3b8"/>'
                ),
                (
                    f'<text x="{position:.2f}" y="{top + plot_height + 26}" '
                    f'text-anchor="middle" class="axis">{label}</text>'
                ),
            ]
        )

    series = (
        ("Rebalanced", actual, "#2563eb"),
        ("Buy & hold", benchmark, "#f59e0b"),
    )
    for label, points, color in series:
        elements.append(
            f'<polyline points="{polyline(points)}" fill="none" '
            f'stroke="{color}" stroke-width="3" stroke-linejoin="round" '
            'stroke-linecap="round"/>'
        )
        for index, (_, value) in enumerate(points):
            elements.append(
                f'<circle cx="{x(index):.2f}" cy="{y(value):.2f}" r="4" '
                f'fill="#ffffff" stroke="{color}" stroke-width="2"/>'
            )
        legend_x = left + plot_width - (230 if label == "Rebalanced" else 105)
        elements.extend(
            [
                (
                    f'<line x1="{legend_x}" y1="34" x2="{legend_x + 22}" '
                    f'y2="34" stroke="{color}" stroke-width="3"/>'
                ),
                (
                    f'<text x="{legend_x + 28}" y="39" class="legend">'
                    f"{escape(label)}</text>"
                ),
            ]
        )

    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def _write_exports(
    *,
    observations: list[dict[str, object]],
    chart_dir: Path,
    start_date: date,
) -> tuple[Path, Path, Path]:
    value_path = chart_dir / "portfolio_value.svg"
    returns_path = chart_dir / "portfolio_returns.svg"
    csv_path = chart_dir / "portfolio_performance.csv"
    subtitle = (
        f"Fixed-unit benchmark initialized {start_date.isoformat()} · "
        f"{len(observations)} observation"
        f"{'' if len(observations) == 1 else 's'}"
    )
    _atomic_write(
        value_path,
        _line_chart(
            title="Portfolio value: rebalancing vs buy-and-hold",
            subtitle=subtitle,
            actual=_points(observations, "actual_value_eur"),
            benchmark=_points(observations, "buy_hold_value_eur"),
            formatter=_format_euros,
            include_zero=False,
        ),
    )
    _atomic_write(
        returns_path,
        _line_chart(
            title="Cumulative return: rebalancing vs buy-and-hold",
            subtitle=subtitle,
            actual=_points(observations, "actual_return"),
            benchmark=_points(observations, "buy_hold_return"),
            formatter=_format_percent,
            include_zero=True,
        ),
    )

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "recorded_at",
            "actual_value_eur",
            "buy_hold_value_eur",
            "actual_return_pct",
            "buy_hold_return_pct",
            "outperformance_pct",
            "value_difference_eur",
            "note",
        ]
    )
    for row in observations:
        writer.writerow(
            [
                row["recorded_at"],
                row["actual_value_eur"],
                row["buy_hold_value_eur"],
                _decimal(row["actual_return"], "actual_return") * Decimal("100"),
                _decimal(row["buy_hold_return"], "buy_hold_return")
                * Decimal("100"),
                _decimal(row["outperformance"], "outperformance")
                * Decimal("100"),
                row["value_difference_eur"],
                row.get("note", ""),
            ]
        )
    _atomic_write(csv_path, output.getvalue())
    return value_path, returns_path, csv_path


def _summary(
    *,
    start_date: date,
    observations: list[dict[str, object]],
    data_path: Path,
    value_chart_path: Path,
    returns_chart_path: Path,
    csv_path: Path,
) -> PerformanceSummary:
    latest = observations[-1]
    return PerformanceSummary(
        start_date=start_date,
        observations=len(observations),
        actual_value_eur=_decimal(
            latest["actual_value_eur"],
            "actual_value_eur",
        ),
        buy_hold_value_eur=_decimal(
            latest["buy_hold_value_eur"],
            "buy_hold_value_eur",
        ),
        actual_return=_decimal(latest["actual_return"], "actual_return"),
        buy_hold_return=_decimal(
            latest["buy_hold_return"],
            "buy_hold_return",
        ),
        outperformance=_decimal(
            latest["outperformance"],
            "outperformance",
        ),
        value_difference_eur=_decimal(
            latest["value_difference_eur"],
            "value_difference_eur",
        ),
        data_path=data_path,
        value_chart_path=value_chart_path,
        returns_chart_path=returns_chart_path,
        csv_path=csv_path,
    )


def record_rebalance(
    holdings: Holdings,
    prices: PriceBook,
    *,
    data_path: Path = DEFAULT_DATA_PATH,
    chart_dir: Path = DEFAULT_CHART_DIR,
    start_date: date = DEFAULT_START_DATE,
    note: str = "",
) -> PerformanceSummary:
    """Record a completed rebalance and refresh comparison exports.

    The first call freezes the supplied asset units as the buy-and-hold
    benchmark. Later calls value those original units at the new price snapshot
    while valuing the newly fetched real holdings separately.
    """

    if not isinstance(start_date, date):
        raise ValueError("start_date must be a date")
    if "\n" in note or "\r" in note:
        raise ValueError("Tracking note must fit on one line")
    if len(note) > 240:
        raise ValueError("Tracking note cannot exceed 240 characters")

    actual_amounts = holdings.normalized()
    normalized_prices = prices.normalized()
    recorded_at = max(_utc(holdings.fetched_at), _utc(prices.as_of))
    if recorded_at < datetime.combine(start_date, time.min, tzinfo=timezone.utc):
        raise ValueError(
            f"Snapshot {recorded_at.date()} predates benchmark start {start_date}"
        )

    if data_path.exists():
        payload = _read_store(data_path)
        (
            stored_start_date,
            benchmark_amounts,
            initial_value,
            observations,
        ) = _validate_store(payload)
        if stored_start_date != start_date:
            raise ValueError(
                f"Tracking file started on {stored_start_date}; "
                f"requested {start_date}"
            )
    else:
        benchmark_amounts = dict(actual_amounts)
        initial_value = _value(benchmark_amounts, normalized_prices)
        if initial_value <= ZERO:
            raise ValueError("The initial portfolio has no positive market value")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "benchmark": {
                "start_date": start_date.isoformat(),
                "initialized_at": _iso(recorded_at),
                "amounts": _asset_strings(benchmark_amounts),
                "initial_prices_eur": _asset_strings(normalized_prices),
                "initial_value_eur": str(initial_value),
            },
            "observations": [],
        }
        observations = []

    new_observation = _observation(
        recorded_at=recorded_at,
        holdings=holdings,
        prices=prices,
        actual_amounts=actual_amounts,
        benchmark_amounts=benchmark_amounts,
        initial_value=initial_value,
        note=note.strip(),
    )
    if observations:
        latest_time = datetime.fromisoformat(
            str(observations[-1]["recorded_at"]).replace("Z", "+00:00")
        )
        if recorded_at < _utc(latest_time):
            raise ValueError("Cannot append an older portfolio snapshot")
        if recorded_at == _utc(latest_time):
            if new_observation != observations[-1]:
                raise ValueError(
                    "A different observation already exists at this timestamp"
                )
        else:
            observations.append(new_observation)
    else:
        observations.append(new_observation)

    payload["observations"] = observations
    _atomic_write(data_path, json.dumps(payload, indent=2) + "\n")
    value_path, returns_path, csv_path = _write_exports(
        observations=observations,
        chart_dir=chart_dir,
        start_date=start_date,
    )
    return _summary(
        start_date=start_date,
        observations=observations,
        data_path=data_path,
        value_chart_path=value_path,
        returns_chart_path=returns_path,
        csv_path=csv_path,
    )


def render_performance(summary: PerformanceSummary) -> str:
    """Render the latest comparison for a terminal."""

    signed_difference = f"{summary.value_difference_eur:+,.2f}"
    return "\n".join(
        [
            "PORTFOLIO PERFORMANCE TRACKER",
            f"STATUS: {summary.verdict}",
            f"Benchmark start:       {summary.start_date.isoformat()}",
            f"Recorded observations: {summary.observations}",
            f"Rebalanced portfolio:  €{summary.actual_value_eur:,.2f}",
            f"Buy-and-hold portfolio: €{summary.buy_hold_value_eur:,.2f}",
            f"Value difference:      €{signed_difference}",
            f"Rebalanced return:     {summary.actual_return * 100:+.2f}%",
            f"Buy-and-hold return:   {summary.buy_hold_return * 100:+.2f}%",
            f"Outperformance:        {summary.outperformance * 100:+.2f} pp",
            "",
            f"History: {summary.data_path}",
            f"Values chart: {summary.value_chart_path}",
            f"Returns chart: {summary.returns_chart_path}",
            f"CSV export: {summary.csv_path}",
        ]
    )
