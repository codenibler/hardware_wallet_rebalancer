"""Cash-flow-adjusted comparison of rebalancing with buy-and-hold."""

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

from .charts import render_performance_chart
from .models import ASSETS, ZERO, Holdings, PriceBook, decimal_map


SCHEMA_VERSION = 2
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
    total_contributions_eur: Decimal
    total_benchmark_fees_eur: Decimal
    data_path: Path
    value_chart_path: Path
    returns_chart_path: Path
    performance_image_path: Path
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
            "total_contributions_eur": str(self.total_contributions_eur),
            "total_benchmark_fees_eur": str(self.total_benchmark_fees_eur),
            "data_path": str(self.data_path),
            "value_chart_path": str(self.value_chart_path),
            "returns_chart_path": str(self.returns_chart_path),
            "performance_image_path": str(self.performance_image_path),
            "csv_path": str(self.csv_path),
        }


@dataclass(frozen=True)
class _TrackingState:
    start_date: date
    benchmark_amounts: dict[str, Decimal]
    allocation_weights: dict[str, Decimal]
    initial_value: Decimal
    observations: list[dict[str, object]]
    cash_flows: list[dict[str, object]]


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


def _allocation_weights(
    amounts: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    values = {asset: amounts[asset] * prices[asset] for asset in ASSETS}
    total = sum(values.values(), ZERO)
    if total <= ZERO:
        raise ValueError("The benchmark has no positive market value")

    weights: dict[str, Decimal] = {}
    allocated = ZERO
    for asset in ASSETS[:-1]:
        weight = values[asset] / total
        weights[asset] = weight
        allocated += weight
    weights[ASSETS[-1]] = Decimal("1") - allocated
    return weights


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
    cash_flows = raw.get("cash_flows")
    if (
        not isinstance(benchmark, dict)
        or not isinstance(observations, list)
        or not isinstance(cash_flows, list)
    ):
        raise ValueError("Tracking data is missing benchmark or observations")
    return raw


def _validate_store(
    payload: dict[str, object],
) -> _TrackingState:
    benchmark = payload["benchmark"]
    observations = payload["observations"]
    cash_flows = payload["cash_flows"]
    assert isinstance(benchmark, dict)
    assert isinstance(observations, list)
    assert isinstance(cash_flows, list)

    try:
        start_date = date.fromisoformat(str(benchmark["start_date"]))
        amounts_raw = benchmark["amounts"]
        initial_amounts_raw = benchmark["initial_amounts"]
        allocation_weights_raw = benchmark["allocation_weights"]
        initial_value = _decimal(
            benchmark["initial_value_eur"],
            "benchmark.initial_value_eur",
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("Tracking benchmark is incomplete or invalid") from exc
    if (
        not isinstance(amounts_raw, dict)
        or not isinstance(initial_amounts_raw, dict)
        or not isinstance(allocation_weights_raw, dict)
    ):
        raise ValueError("Tracking benchmark amounts must be an object")
    amounts = decimal_map(amounts_raw)
    initial_amounts = decimal_map(initial_amounts_raw)
    allocation_weights = decimal_map(allocation_weights_raw)
    if initial_value <= ZERO:
        raise ValueError("Tracking benchmark initial value must be positive")
    if sum(allocation_weights.values(), ZERO) != Decimal("1"):
        raise ValueError("Tracking benchmark allocation weights must sum to 1")

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
        "external_cash_flow_eur",
        "deposit_fee_bps",
        "benchmark_fee_eur",
        "benchmark_net_invested_eur",
        "total_contributions_eur",
        "total_benchmark_fees_eur",
        "benchmark_amounts",
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
        for key in required - {"recorded_at", "benchmark_amounts"}:
            _decimal(observation[key], f"observations[{index}].{key}")
        benchmark_snapshot = observation["benchmark_amounts"]
        if not isinstance(benchmark_snapshot, dict):
            raise ValueError(
                f"observations[{index}].benchmark_amounts must be an object"
            )
        decimal_map(benchmark_snapshot)
        validated_observations.append(observation)

    if not validated_observations:
        raise ValueError("Tracking data must contain at least one observation")

    expected_amounts = dict(initial_amounts)
    total_contributions = ZERO
    total_fees = ZERO
    previous_flow_time: datetime | None = None
    observation_times = {
        str(observation["recorded_at"]) for observation in validated_observations
    }
    for index, cash_flow in enumerate(cash_flows):
        if not isinstance(cash_flow, dict):
            raise ValueError(f"Cash-flow event {index} must be an object")
        required_flow = {
            "recorded_at",
            "type",
            "gross_amount_eur",
            "fee_bps",
            "benchmark_fee_eur",
            "net_invested_eur",
            "benchmark_purchases",
        }
        if not required_flow <= cash_flow.keys():
            raise ValueError(f"Cash-flow event {index} is incomplete")
        flow_type = cash_flow["type"]
        if flow_type not in {
            "deposit",
            "detected_deposit",
            "detected_in_kind_deposit",
        }:
            raise ValueError(f"Cash-flow event {index} has an unsupported type")
        try:
            flow_time = _utc(
                datetime.fromisoformat(
                    str(cash_flow["recorded_at"]).replace("Z", "+00:00")
                )
            )
        except ValueError as exc:
            raise ValueError(
                f"Cash-flow event {index} has an invalid timestamp"
            ) from exc
        if previous_flow_time is not None and flow_time <= previous_flow_time:
            raise ValueError("Cash-flow events must be strictly chronological")
        previous_flow_time = flow_time
        if _iso(flow_time) not in observation_times:
            raise ValueError(
                f"Cash-flow event {index} has no matching observation"
            )

        gross = _decimal(
            cash_flow["gross_amount_eur"],
            f"cash_flows[{index}].gross_amount_eur",
        )
        fee_bps = _decimal(
            cash_flow["fee_bps"],
            f"cash_flows[{index}].fee_bps",
        )
        fee = _decimal(
            cash_flow["benchmark_fee_eur"],
            f"cash_flows[{index}].benchmark_fee_eur",
        )
        net = _decimal(
            cash_flow["net_invested_eur"],
            f"cash_flows[{index}].net_invested_eur",
        )
        invalid_amounts = (
            gross <= ZERO
            or fee < ZERO
            or net <= ZERO
            or fee + net != gross
        )
        if flow_type == "deposit":
            invalid_amounts = invalid_amounts or (
                fee_bps <= ZERO or fee_bps > Decimal("1000")
            )
        else:
            invalid_amounts = invalid_amounts or fee_bps != ZERO or fee != ZERO
        if invalid_amounts:
            raise ValueError(f"Cash-flow event {index} has invalid amounts")

        purchases_raw = cash_flow["benchmark_purchases"]
        if not isinstance(purchases_raw, dict):
            raise ValueError(
                f"cash_flows[{index}].benchmark_purchases must be an object"
            )
        purchases = decimal_map(purchases_raw)
        for asset in ASSETS:
            expected_amounts[asset] += purchases[asset]
        total_contributions += gross
        total_fees += fee

    if expected_amounts != amounts:
        raise ValueError("Tracking benchmark amounts do not match cash-flow events")
    latest = validated_observations[-1]
    if (
        _decimal(
            latest["total_contributions_eur"],
            "latest total_contributions_eur",
        )
        != total_contributions
        or _decimal(
            latest["total_benchmark_fees_eur"],
            "latest total_benchmark_fees_eur",
        )
        != total_fees
    ):
        raise ValueError("Tracking cash-flow totals do not match observations")

    return _TrackingState(
        start_date=start_date,
        benchmark_amounts=amounts,
        allocation_weights=allocation_weights,
        initial_value=initial_value,
        observations=validated_observations,
        cash_flows=cash_flows,
    )


def _simulate_benchmark_deposit(
    *,
    benchmark_amounts: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    allocation_weights: Mapping[str, Decimal],
    gross_amount_eur: Decimal,
    fee_bps: Decimal,
) -> tuple[dict[str, Decimal], dict[str, Decimal], Decimal, Decimal]:
    fee_rate = fee_bps / Decimal("10000")
    net_invested = gross_amount_eur / (Decimal("1") + fee_rate)
    fee = gross_amount_eur - net_invested

    purchases: dict[str, Decimal] = {}
    remaining_notional = net_invested
    for asset in ASSETS[:-1]:
        notional = net_invested * allocation_weights[asset]
        purchases[asset] = notional / prices[asset]
        remaining_notional -= notional
    purchases[ASSETS[-1]] = remaining_notional / prices[ASSETS[-1]]

    updated_amounts = {
        asset: benchmark_amounts[asset] + purchases[asset] for asset in ASSETS
    }
    return updated_amounts, purchases, fee, net_invested


def _detected_in_kind_deposit(
    *,
    previous_actual_amounts: Mapping[str, Decimal],
    actual_amounts: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
) -> tuple[dict[str, Decimal], Decimal] | None:
    """Return newly received asset units when the wallet only grew.

    Balances from the configured XPUB and wallet addresses are aggregated by
    the provider, so an internal transfer between configured addresses has no
    effect here.  A decrease in any tracked asset makes the change ambiguous
    (for example, a rebalance or a withdrawal), and is deliberately not
    classified as an external contribution.
    """

    additions = {
        asset: actual_amounts[asset] - previous_actual_amounts[asset]
        for asset in ASSETS
    }
    if any(amount < ZERO for amount in additions.values()):
        return None
    if not any(amount > ZERO for amount in additions.values()):
        return None
    return additions, _value(additions, prices)


def _backfill_latest_detected_deposit(payload: dict[str, object]) -> None:
    """Upgrade the most recent legacy zero-flow snapshot, if unambiguous.

    Earlier versions recorded balance changes but required a separately entered
    EUR deposit.  On the first run after upgrading, repair the most recent
    such snapshot from the observed incoming units.  Only the latest snapshot
    can be safely amended without replaying subsequent historical prices.
    """

    benchmark = payload["benchmark"]
    observations = payload["observations"]
    cash_flows = payload["cash_flows"]
    assert isinstance(benchmark, dict)
    assert isinstance(observations, list)
    assert isinstance(cash_flows, list)
    if len(observations) < 2:
        return

    latest = observations[-1]
    previous = observations[-2]
    if not isinstance(latest, dict) or not isinstance(previous, dict):
        return
    latest_time = str(latest.get("recorded_at", ""))
    if any(
        isinstance(flow, dict) and flow.get("recorded_at") == latest_time
        for flow in cash_flows
    ) or (
        _decimal(
            latest.get("external_cash_flow_eur", "0"),
            "external_cash_flow_eur",
        )
        != ZERO
    ):
        return

    previous_actual_raw = previous.get("actual_amounts")
    actual_raw = latest.get("actual_amounts")
    prices_raw = latest.get("prices_eur")
    if (
        not isinstance(previous_actual_raw, dict)
        or not isinstance(actual_raw, dict)
        or not isinstance(prices_raw, dict)
    ):
        return
    previous_actual = decimal_map(previous_actual_raw)
    actual = decimal_map(actual_raw)
    prices = decimal_map(prices_raw)
    detected = _detected_in_kind_deposit(
        previous_actual_amounts=previous_actual,
        actual_amounts=actual,
        prices=prices,
    )
    if detected is None:
        return
    _, gross = detected

    previous_benchmark_raw = previous.get("benchmark_amounts")
    if not isinstance(previous_benchmark_raw, dict):
        return
    previous_benchmark = decimal_map(previous_benchmark_raw)
    allocation_weights_raw = benchmark.get("allocation_weights")
    if not isinstance(allocation_weights_raw, dict):
        return
    allocation_weights = decimal_map(allocation_weights_raw)
    updated_benchmark, benchmark_purchases, _, net_invested = (
        _simulate_benchmark_deposit(
            benchmark_amounts=previous_benchmark,
            prices=prices,
            allocation_weights=allocation_weights,
            gross_amount_eur=gross,
            fee_bps=ZERO,
        )
    )
    holdings_as_of = _utc(
        datetime.fromisoformat(str(latest["holdings_as_of"]).replace("Z", "+00:00"))
    )
    prices_as_of = _utc(
        datetime.fromisoformat(str(latest["prices_as_of"]).replace("Z", "+00:00"))
    )
    recorded_at = _utc(
        datetime.fromisoformat(str(latest["recorded_at"]).replace("Z", "+00:00"))
    )
    rebuilt = _observation(
        recorded_at=recorded_at,
        holdings=Holdings(amounts=actual, fetched_at=holdings_as_of),
        prices=PriceBook(
            prices_eur=prices,
            as_of=prices_as_of,
            source=str(latest["price_source"]),
        ),
        actual_amounts=actual,
        benchmark_amounts=updated_benchmark,
        initial_value=_decimal(benchmark["initial_value_eur"], "initial_value_eur"),
        previous=previous,
        external_cash_flow_eur=gross,
        deposit_fee_bps=ZERO,
        benchmark_fee_eur=ZERO,
        benchmark_net_invested_eur=net_invested,
        total_contributions_eur=(
            _decimal(previous["total_contributions_eur"], "total_contributions_eur")
            + gross
        ),
        total_benchmark_fees_eur=_decimal(
            previous["total_benchmark_fees_eur"],
            "total_benchmark_fees_eur",
        ),
        pre_flow_actual_value_eur=_value(previous_actual, prices),
        pre_flow_buy_hold_value_eur=_value(previous_benchmark, prices),
        note=str(latest.get("note", "")),
    )
    observations[-1] = rebuilt
    cash_flows.append(
        {
            "recorded_at": latest_time,
            "type": "detected_deposit",
            "gross_amount_eur": str(gross),
            "fee_bps": "0",
            "benchmark_fee_eur": "0",
            "net_invested_eur": str(net_invested),
            "benchmark_purchases": _asset_strings(benchmark_purchases),
            "note": "Automatically detected wallet contribution",
        }
    )
    benchmark["amounts"] = _asset_strings(updated_benchmark)


def _migrate_legacy_in_kind_deposits(payload: dict[str, object]) -> None:
    """Replace legacy copied-unit benchmark deposits with fixed-allocation buys."""

    benchmark = payload["benchmark"]
    observations = payload["observations"]
    cash_flows = payload["cash_flows"]
    assert isinstance(benchmark, dict)
    assert isinstance(observations, list)
    assert isinstance(cash_flows, list)
    if not any(
        isinstance(flow, dict)
        and flow.get("type") == "detected_in_kind_deposit"
        for flow in cash_flows
    ):
        return

    initial_amounts_raw = benchmark.get("initial_amounts")
    allocation_weights_raw = benchmark.get("allocation_weights")
    if not isinstance(initial_amounts_raw, dict) or not isinstance(
        allocation_weights_raw,
        dict,
    ):
        return
    benchmark_amounts = decimal_map(initial_amounts_raw)
    allocation_weights = decimal_map(allocation_weights_raw)
    initial_value = _decimal(benchmark["initial_value_eur"], "initial_value_eur")
    flows_by_time = {
        str(flow["recorded_at"]): flow
        for flow in cash_flows
        if isinstance(flow, dict)
    }
    rebuilt = [observations[0]]
    total_contributions = ZERO
    total_fees = ZERO

    for raw in observations[1:]:
        if not isinstance(raw, dict) or not isinstance(rebuilt[-1], dict):
            return
        previous = rebuilt[-1]
        actual_raw = raw.get("actual_amounts")
        previous_actual_raw = previous.get("actual_amounts")
        prices_raw = raw.get("prices_eur")
        if (
            not isinstance(actual_raw, dict)
            or not isinstance(previous_actual_raw, dict)
            or not isinstance(prices_raw, dict)
        ):
            return
        actual = decimal_map(actual_raw)
        previous_actual = decimal_map(previous_actual_raw)
        prices = decimal_map(prices_raw)
        flow = flows_by_time.get(str(raw.get("recorded_at", "")))
        gross = ZERO
        fee = ZERO
        net = ZERO
        fee_bps = ZERO
        pre_flow_actual: Decimal | None = None
        pre_flow_benchmark: Decimal | None = None
        if flow is not None:
            if flow.get("type") == "detected_in_kind_deposit":
                received_raw = flow.get("benchmark_purchases")
                if not isinstance(received_raw, dict):
                    return
                gross = _value(decimal_map(received_raw), prices)
                (
                    benchmark_amounts,
                    purchases,
                    fee,
                    net,
                ) = _simulate_benchmark_deposit(
                    benchmark_amounts=benchmark_amounts,
                    prices=prices,
                    allocation_weights=allocation_weights,
                    gross_amount_eur=gross,
                    fee_bps=ZERO,
                )
                flow["type"] = "detected_deposit"
                flow["gross_amount_eur"] = str(gross)
                flow["fee_bps"] = "0"
                flow["benchmark_fee_eur"] = "0"
                flow["net_invested_eur"] = str(net)
                flow["benchmark_purchases"] = _asset_strings(purchases)
                flow["note"] = "Automatically detected wallet contribution"
            else:
                purchases_raw = flow.get("benchmark_purchases")
                if not isinstance(purchases_raw, dict):
                    return
                purchases = decimal_map(purchases_raw)
                benchmark_amounts = {
                    asset: benchmark_amounts[asset] + purchases[asset]
                    for asset in ASSETS
                }
                gross = _decimal(flow["gross_amount_eur"], "gross_amount_eur")
                fee = _decimal(flow["benchmark_fee_eur"], "benchmark_fee_eur")
                net = _decimal(flow["net_invested_eur"], "net_invested_eur")
                fee_bps = _decimal(flow["fee_bps"], "fee_bps")
            pre_flow_actual = _value(previous_actual, prices)
            pre_flow_benchmark = _value(
                decimal_map(previous["benchmark_amounts"]),
                prices,
            )
            total_contributions += gross
            total_fees += fee

        holdings_as_of = _utc(
            datetime.fromisoformat(
                str(raw["holdings_as_of"]).replace("Z", "+00:00")
            )
        )
        prices_as_of = _utc(
            datetime.fromisoformat(
                str(raw["prices_as_of"]).replace("Z", "+00:00")
            )
        )
        recorded_at = _utc(
            datetime.fromisoformat(
                str(raw["recorded_at"]).replace("Z", "+00:00")
            )
        )
        rebuilt.append(
            _observation(
                recorded_at=recorded_at,
                holdings=Holdings(amounts=actual, fetched_at=holdings_as_of),
                prices=PriceBook(
                    prices_eur=prices,
                    as_of=prices_as_of,
                    source=str(raw["price_source"]),
                ),
                actual_amounts=actual,
                benchmark_amounts=benchmark_amounts,
                initial_value=initial_value,
                previous=previous,
                external_cash_flow_eur=gross,
                deposit_fee_bps=fee_bps,
                benchmark_fee_eur=fee,
                benchmark_net_invested_eur=net,
                total_contributions_eur=total_contributions,
                total_benchmark_fees_eur=total_fees,
                pre_flow_actual_value_eur=pre_flow_actual,
                pre_flow_buy_hold_value_eur=pre_flow_benchmark,
                note=str(raw.get("note", "")),
            )
        )

    observations[:] = rebuilt
    benchmark["amounts"] = _asset_strings(benchmark_amounts)


def _observation(
    *,
    recorded_at: datetime,
    holdings: Holdings,
    prices: PriceBook,
    actual_amounts: Mapping[str, Decimal],
    benchmark_amounts: Mapping[str, Decimal],
    initial_value: Decimal,
    previous: Mapping[str, object] | None,
    external_cash_flow_eur: Decimal,
    deposit_fee_bps: Decimal,
    benchmark_fee_eur: Decimal,
    benchmark_net_invested_eur: Decimal,
    total_contributions_eur: Decimal,
    total_benchmark_fees_eur: Decimal,
    pre_flow_actual_value_eur: Decimal | None,
    pre_flow_buy_hold_value_eur: Decimal | None,
    note: str,
) -> dict[str, object]:
    normalized_prices = prices.normalized()
    actual_value = _value(actual_amounts, normalized_prices)
    buy_hold_value = _value(benchmark_amounts, normalized_prices)
    if previous is None:
        actual_return = actual_value / initial_value - Decimal("1")
        buy_hold_return = buy_hold_value / initial_value - Decimal("1")
    else:
        previous_actual_value = _decimal(
            previous["actual_value_eur"],
            "previous actual_value_eur",
        )
        previous_buy_hold_value = _decimal(
            previous["buy_hold_value_eur"],
            "previous buy_hold_value_eur",
        )
        if previous_actual_value <= ZERO or previous_buy_hold_value <= ZERO:
            raise ValueError("Previous portfolio values must be positive")
        if external_cash_flow_eur > ZERO:
            if (
                pre_flow_actual_value_eur is None
                or pre_flow_buy_hold_value_eur is None
                or pre_flow_actual_value_eur <= ZERO
                or pre_flow_buy_hold_value_eur <= ZERO
            ):
                raise ValueError("Deposit valuation before the cash flow is missing")
            actual_period_factor = (
                pre_flow_actual_value_eur
                / previous_actual_value
                * actual_value
                / (pre_flow_actual_value_eur + external_cash_flow_eur)
            )
            buy_hold_period_factor = (
                pre_flow_buy_hold_value_eur
                / previous_buy_hold_value
                * buy_hold_value
                / (pre_flow_buy_hold_value_eur + external_cash_flow_eur)
            )
        else:
            actual_period_factor = actual_value / previous_actual_value
            buy_hold_period_factor = buy_hold_value / previous_buy_hold_value
        previous_actual_return = _decimal(
            previous["actual_return"],
            "previous actual_return",
        )
        previous_buy_hold_return = _decimal(
            previous["buy_hold_return"],
            "previous buy_hold_return",
        )
        actual_return = (
            (Decimal("1") + previous_actual_return)
            * actual_period_factor
            - Decimal("1")
        )
        buy_hold_return = (
            (Decimal("1") + previous_buy_hold_return)
            * buy_hold_period_factor
            - Decimal("1")
        )
    value_difference = actual_value - buy_hold_value
    return {
        "recorded_at": _iso(recorded_at),
        "holdings_as_of": _iso(holdings.fetched_at),
        "prices_as_of": _iso(prices.as_of),
        "price_source": prices.source,
        "note": note,
        "actual_amounts": _asset_strings(actual_amounts),
        "prices_eur": _asset_strings(normalized_prices),
        "benchmark_amounts": _asset_strings(benchmark_amounts),
        "actual_value_eur": str(actual_value),
        "buy_hold_value_eur": str(buy_hold_value),
        "actual_return": str(actual_return),
        "buy_hold_return": str(buy_hold_return),
        "outperformance": str(actual_return - buy_hold_return),
        "value_difference_eur": str(value_difference),
        "external_cash_flow_eur": str(external_cash_flow_eur),
        "deposit_fee_bps": str(deposit_fee_bps),
        "benchmark_fee_eur": str(benchmark_fee_eur),
        "benchmark_net_invested_eur": str(benchmark_net_invested_eur),
        "total_contributions_eur": str(total_contributions_eur),
        "total_benchmark_fees_eur": str(total_benchmark_fees_eur),
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
        "text{font-family:Inter,ui-sans-serif,system-ui,sans-serif;fill:#e2e8f0}"
        ".title{font-size:24px;font-weight:700;fill:#f8fafc}"
        ".subtitle{font-size:13px;fill:#cbd5e1}"
        ".axis{font-size:12px;fill:#94a3b8}"
        ".legend{font-size:13px;font-weight:600;fill:#e2e8f0}"
        "</style>",
        f'<rect width="{width}" height="{height}" rx="16" fill="#0f172a"/>',
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
                    'stroke="#334155" stroke-width="1"/>'
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
                    'stroke="#64748b"/>'
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
                f'fill="#0f172a" stroke="{color}" stroke-width="2"/>'
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
) -> tuple[Path, Path, Path, Path]:
    value_path = chart_dir / "portfolio_value.svg"
    returns_path = chart_dir / "portfolio_returns.svg"
    csv_path = chart_dir / "portfolio_performance.csv"
    performance_image_path = chart_dir / "portfolio_performance.png"
    subtitle = (
        f"Buy-and-hold benchmark initialized {start_date.isoformat()} · "
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
    render_performance_chart(
        actual=_points(observations, "actual_value_eur"),
        benchmark=_points(observations, "buy_hold_value_eur"),
        start_date=start_date.isoformat(),
        path=performance_image_path,
    )
    _atomic_write(
        returns_path,
        _line_chart(
            title="Cumulative TWR: rebalancing vs buy-and-hold",
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
            "external_cash_flow_eur",
            "benchmark_fee_eur",
            "benchmark_net_invested_eur",
            "total_contributions_eur",
            "total_benchmark_fees_eur",
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
                row["external_cash_flow_eur"],
                row["benchmark_fee_eur"],
                row["benchmark_net_invested_eur"],
                row["total_contributions_eur"],
                row["total_benchmark_fees_eur"],
                row.get("note", ""),
            ]
        )
    _atomic_write(csv_path, output.getvalue())
    return value_path, returns_path, performance_image_path, csv_path


def _summary(
    *,
    start_date: date,
    observations: list[dict[str, object]],
    data_path: Path,
    value_chart_path: Path,
    returns_chart_path: Path,
    performance_image_path: Path,
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
        total_contributions_eur=_decimal(
            latest["total_contributions_eur"],
            "total_contributions_eur",
        ),
        total_benchmark_fees_eur=_decimal(
            latest["total_benchmark_fees_eur"],
            "total_benchmark_fees_eur",
        ),
        data_path=data_path,
        value_chart_path=value_chart_path,
        returns_chart_path=returns_chart_path,
        performance_image_path=performance_image_path,
        csv_path=csv_path,
    )


def _matches_existing_observation(
    observation: Mapping[str, object],
    *,
    holdings: Holdings,
    prices: PriceBook,
    actual_amounts: Mapping[str, Decimal],
    deposit_eur: Decimal,
    deposit_fee_bps: Decimal,
    note: str,
) -> bool:
    stored_actual_amounts = observation.get("actual_amounts")
    stored_prices = observation.get("prices_eur")
    if not isinstance(stored_actual_amounts, dict) or not isinstance(
        stored_prices,
        dict,
    ):
        return False
    base_match = (
        decimal_map(stored_actual_amounts) == dict(actual_amounts)
        and decimal_map(stored_prices) == prices.normalized()
        and observation.get("holdings_as_of") == _iso(holdings.fetched_at)
        and observation.get("prices_as_of") == _iso(prices.as_of)
        and observation.get("price_source") == prices.source
        and observation.get("note") == note
    )
    stored_fee_bps = _decimal(
            observation.get("deposit_fee_bps", "0"),
            "deposit_fee_bps",
        )
    if not base_match or stored_fee_bps != deposit_fee_bps:
        return False
    if deposit_eur == ZERO:
        # A repeat of an automatically detected in-kind contribution has no
        # user-supplied EUR amount to compare. Matching snapshot inputs make
        # it idempotent; the stored benchmark remains authoritative.
        return True
    return _decimal(
        observation.get("external_cash_flow_eur", "0"),
        "external_cash_flow_eur",
    ) == deposit_eur


def record_rebalance(
    holdings: Holdings,
    prices: PriceBook,
    *,
    data_path: Path = DEFAULT_DATA_PATH,
    chart_dir: Path = DEFAULT_CHART_DIR,
    start_date: date = DEFAULT_START_DATE,
    note: str = "",
    deposit_eur: Decimal | str | int | float = ZERO,
    deposit_fee_bps: Decimal | str | int | float = ZERO,
) -> PerformanceSummary:
    """Record a completed snapshot and refresh comparison exports.

    The first call initializes the buy-and-hold benchmark. A later completed
    deposit adds fee-adjusted benchmark units at that snapshot's prices and is
    excluded from both strategies' chained returns.
    """

    if not isinstance(start_date, date):
        raise ValueError("start_date must be a date")
    if "\n" in note or "\r" in note:
        raise ValueError("Tracking note must fit on one line")
    if len(note) > 240:
        raise ValueError("Tracking note cannot exceed 240 characters")
    deposit = _decimal(deposit_eur, "deposit_eur")
    fee_bps = _decimal(deposit_fee_bps, "deposit_fee_bps")
    if deposit < ZERO:
        raise ValueError("Deposit amount cannot be negative")
    if deposit == ZERO and fee_bps != ZERO:
        raise ValueError("Deposit fee bps require a positive deposit")
    if deposit > ZERO and (fee_bps <= ZERO or fee_bps > Decimal("1000")):
        raise ValueError("Deposit fee bps must be in (0, 1000]")

    actual_amounts = holdings.normalized()
    normalized_prices = prices.normalized()
    cleaned_note = note.strip()
    recorded_at = max(_utc(holdings.fetched_at), _utc(prices.as_of))
    if recorded_at < datetime.combine(start_date, time.min, tzinfo=timezone.utc):
        raise ValueError(
            f"Snapshot {recorded_at.date()} predates benchmark start {start_date}"
        )

    if data_path.exists():
        payload = _read_store(data_path)
        _migrate_legacy_in_kind_deposits(payload)
        _backfill_latest_detected_deposit(payload)
        state = _validate_store(payload)
        if state.start_date != start_date:
            raise ValueError(
                f"Tracking file started on {state.start_date}; "
                f"requested {start_date}"
            )
        benchmark_amounts = dict(state.benchmark_amounts)
        allocation_weights = dict(state.allocation_weights)
        initial_value = state.initial_value
        observations = state.observations
        cash_flows = state.cash_flows
    else:
        if deposit > ZERO:
            raise ValueError(
                "Initialize tracking before recording a later deposit"
            )
        benchmark_amounts = dict(actual_amounts)
        initial_value = _value(benchmark_amounts, normalized_prices)
        if initial_value <= ZERO:
            raise ValueError("The initial portfolio has no positive market value")
        allocation_weights = _allocation_weights(
            benchmark_amounts,
            normalized_prices,
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "benchmark": {
                "start_date": start_date.isoformat(),
                "initialized_at": _iso(recorded_at),
                "initial_amounts": _asset_strings(benchmark_amounts),
                "amounts": _asset_strings(benchmark_amounts),
                "allocation_weights": _asset_strings(allocation_weights),
                "initial_prices_eur": _asset_strings(normalized_prices),
                "initial_value_eur": str(initial_value),
            },
            "observations": [],
            "cash_flows": [],
        }
        observations = []
        cash_flows = []

    previous = observations[-1] if observations else None
    if previous is not None:
        latest_time = _utc(
            datetime.fromisoformat(
                str(previous["recorded_at"]).replace("Z", "+00:00")
            )
        )
        if recorded_at < latest_time:
            raise ValueError("Cannot append an older portfolio snapshot")
        if recorded_at == latest_time:
            if not _matches_existing_observation(
                previous,
                holdings=holdings,
                prices=prices,
                actual_amounts=actual_amounts,
                deposit_eur=deposit,
                deposit_fee_bps=fee_bps,
                note=cleaned_note,
            ):
                raise ValueError(
                    "A different observation already exists at this timestamp"
                )
            value_path, returns_path, performance_image_path, csv_path = _write_exports(
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
                performance_image_path=performance_image_path,
                csv_path=csv_path,
            )

    total_contributions = (
        _decimal(
            previous["total_contributions_eur"],
            "total_contributions_eur",
        )
        if previous is not None
        else ZERO
    )
    total_benchmark_fees = (
        _decimal(
            previous["total_benchmark_fees_eur"],
            "total_benchmark_fees_eur",
        )
        if previous is not None
        else ZERO
    )
    benchmark_fee = ZERO
    benchmark_net_invested = ZERO
    pre_flow_actual_value: Decimal | None = None
    pre_flow_buy_hold_value: Decimal | None = None
    if deposit > ZERO:
        assert previous is not None
        previous_actual_amounts_raw = previous.get("actual_amounts")
        if not isinstance(previous_actual_amounts_raw, dict):
            raise ValueError("Previous actual amounts are missing")
        previous_actual_amounts = decimal_map(previous_actual_amounts_raw)
        pre_flow_actual_value = _value(
            previous_actual_amounts,
            normalized_prices,
        )
        pre_flow_buy_hold_value = _value(
            benchmark_amounts,
            normalized_prices,
        )
        actual_value = _value(actual_amounts, normalized_prices)
        if actual_value <= pre_flow_actual_value:
            raise ValueError(
                "The deposit is not yet reflected in the fetched wallet balances"
            )

        (
            benchmark_amounts,
            benchmark_purchases,
            benchmark_fee,
            benchmark_net_invested,
        ) = _simulate_benchmark_deposit(
            benchmark_amounts=benchmark_amounts,
            prices=normalized_prices,
            allocation_weights=allocation_weights,
            gross_amount_eur=deposit,
            fee_bps=fee_bps,
        )
        total_contributions += deposit
        total_benchmark_fees += benchmark_fee
        cash_flows.append(
            {
                "recorded_at": _iso(recorded_at),
                "type": "deposit",
                "gross_amount_eur": str(deposit),
                "fee_bps": str(fee_bps),
                "benchmark_fee_eur": str(benchmark_fee),
                "net_invested_eur": str(benchmark_net_invested),
                "benchmark_purchases": _asset_strings(benchmark_purchases),
                "note": cleaned_note,
            }
        )
        benchmark_payload = payload["benchmark"]
        assert isinstance(benchmark_payload, dict)
        benchmark_payload["amounts"] = _asset_strings(benchmark_amounts)
    elif previous is not None:
        previous_actual_amounts_raw = previous.get("actual_amounts")
        if not isinstance(previous_actual_amounts_raw, dict):
            raise ValueError("Previous actual amounts are missing")
        previous_actual_amounts = decimal_map(previous_actual_amounts_raw)
        detected = _detected_in_kind_deposit(
            previous_actual_amounts=previous_actual_amounts,
            actual_amounts=actual_amounts,
            prices=normalized_prices,
        )
        if detected is not None:
            _, detected_value = detected
            pre_flow_actual_value = _value(
                previous_actual_amounts,
                normalized_prices,
            )
            pre_flow_buy_hold_value = _value(
                benchmark_amounts,
                normalized_prices,
            )
            (
                benchmark_amounts,
                benchmark_purchases,
                benchmark_fee,
                benchmark_net_invested,
            ) = _simulate_benchmark_deposit(
                benchmark_amounts=benchmark_amounts,
                prices=normalized_prices,
                allocation_weights=allocation_weights,
                gross_amount_eur=detected_value,
                fee_bps=ZERO,
            )
            deposit = detected_value
            total_contributions += deposit
            cash_flows.append(
                {
                    "recorded_at": _iso(recorded_at),
                    "type": "detected_deposit",
                    "gross_amount_eur": str(deposit),
                    "fee_bps": "0",
                    "benchmark_fee_eur": "0",
                    "net_invested_eur": str(benchmark_net_invested),
                    "benchmark_purchases": _asset_strings(benchmark_purchases),
                    "note": "Automatically detected wallet contribution",
                }
            )
            benchmark_payload = payload["benchmark"]
            assert isinstance(benchmark_payload, dict)
            benchmark_payload["amounts"] = _asset_strings(benchmark_amounts)

    new_observation = _observation(
        recorded_at=recorded_at,
        holdings=holdings,
        prices=prices,
        actual_amounts=actual_amounts,
        benchmark_amounts=benchmark_amounts,
        initial_value=initial_value,
        previous=previous,
        external_cash_flow_eur=deposit,
        deposit_fee_bps=fee_bps,
        benchmark_fee_eur=benchmark_fee,
        benchmark_net_invested_eur=benchmark_net_invested,
        total_contributions_eur=total_contributions,
        total_benchmark_fees_eur=total_benchmark_fees,
        pre_flow_actual_value_eur=pre_flow_actual_value,
        pre_flow_buy_hold_value_eur=pre_flow_buy_hold_value,
        note=cleaned_note,
    )
    observations.append(new_observation)

    payload["observations"] = observations
    payload["cash_flows"] = cash_flows
    _atomic_write(data_path, json.dumps(payload, indent=2) + "\n")
    value_path, returns_path, performance_image_path, csv_path = _write_exports(
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
        performance_image_path=performance_image_path,
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
            f"External deposits:     €{summary.total_contributions_eur:,.2f}",
            (
                "Benchmark buy fees:    "
                f"€{summary.total_benchmark_fees_eur:,.2f}"
            ),
            (
                "Rebalanced TWR:        "
                f"{summary.actual_return * 100:+.2f}%"
            ),
            (
                "Buy-and-hold TWR:      "
                f"{summary.buy_hold_return * 100:+.2f}%"
            ),
            f"Outperformance:        {summary.outperformance * 100:+.2f} pp",
            "",
            f"History: {summary.data_path}",
            f"Values chart: {summary.value_chart_path}",
            f"Returns chart: {summary.returns_chart_path}",
            f"Performance image: {summary.performance_image_path}",
            f"CSV export: {summary.csv_path}",
        ]
    )
