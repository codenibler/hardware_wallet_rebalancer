"""Guarded Bitvavo purchase and hardware-wallet withdrawal workflow."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterator

from .bitvavo import (
    BitvavoClient,
    BitvavoConfig,
    BitvavoError,
    BitvavoExecutionClient,
)
from .models import ZERO, PortfolioPlan

DEFAULT_EXECUTION_STATE_PATH = Path("reports/bitvavo_executions.json")
STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PreparedPurchase:
    asset: str
    amount_quote_eur: Decimal
    expected_amount: Decimal
    best_ask_eur: Decimal
    withdrawal_minimum: Decimal
    withdrawal_fee: Decimal
    destination_hint: str
    network: str


@dataclass(frozen=True)
class PreparedTopUp:
    plan: PortfolioPlan
    purchases: tuple[PreparedPurchase, ...]
    available_eur: Decimal


@dataclass(frozen=True)
class ExecutionResult:
    run_id: str
    purchases: tuple[PreparedPurchase, ...]
    withdrawn_amounts: dict[str, Decimal]


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BitvavoError(f"Bitvavo returned an invalid {label}") from exc
    if not result.is_finite():
        raise BitvavoError(f"Bitvavo returned a non-finite {label}")
    return result


def _destination_hint(address: str) -> str:
    if len(address) <= 12:
        return address
    return f"{address[:6]}…{address[-6:]}"


def _supported_networks(asset_data: dict[str, object]) -> set[str]:
    raw = asset_data.get("networks")
    if not isinstance(raw, list):
        raise BitvavoError("Bitvavo asset data omitted withdrawal networks")
    networks: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            networks.add(item)
        elif isinstance(item, dict):
            for key in ("network", "name", "id"):
                if item.get(key):
                    networks.add(str(item[key]))
                    break
    return networks


def prepare_top_up(
    client: BitvavoClient,
    config: BitvavoConfig,
    plan: PortfolioPlan,
) -> PreparedTopUp:
    """Validate balances, markets, prices, minimums, and destinations."""

    if plan.top_up_eur > config.max_top_up_eur:
        raise ValueError(
            f"Top-up €{plan.top_up_eur} exceeds configured maximum "
            f"€{config.max_top_up_eur}"
        )
    if not plan.trades:
        raise ValueError("The buy-only plan contains no purchases")
    if any(trade.side != "BUY" for trade in plan.trades):
        raise ValueError("Bitvavo execution accepts buy-only plans")

    balances = client.get_balances()
    available_eur = balances["EUR"]
    if available_eur < plan.top_up_eur:
        raise ValueError(
            f"Bitvavo has €{available_eur} available, but the requested "
            f"top-up is €{plan.top_up_eur}"
        )

    purchases: list[PreparedPurchase] = []
    for trade in plan.trades:
        market = client.get_market(trade.asset)
        if market.get("market") != f"{trade.asset}-EUR":
            raise BitvavoError(f"Bitvavo returned the wrong {trade.asset} market")
        if market.get("status") != "trading":
            raise ValueError(f"{trade.asset}-EUR is not currently trading")
        try:
            notional_decimals = int(str(market["notionalDecimals"]))
        except (KeyError, ValueError) as exc:
            raise BitvavoError(
                f"Bitvavo omitted {trade.asset} notional precision"
            ) from exc
        if notional_decimals < 0 or notional_decimals > 18:
            raise BitvavoError("Bitvavo returned invalid notional precision")
        quantum = Decimal("1").scaleb(-notional_decimals)
        amount_quote = trade.notional_eur.quantize(quantum, rounding=ROUND_DOWN)
        minimum_quote = _decimal(
            market.get("minOrderInQuoteAsset"),
            f"{trade.asset} minimum order",
        )
        if amount_quote < minimum_quote:
            raise ValueError(
                f"Planned {trade.asset} buy of €{amount_quote} is below "
                f"Bitvavo's €{minimum_quote} minimum"
            )

        ask = client.get_best_ask(trade.asset)
        deviation_bps = (
            abs(ask - trade.snapshot_price_eur)
            / trade.snapshot_price_eur
            * Decimal("10000")
        )
        if deviation_bps > config.max_price_deviation_bps:
            raise ValueError(
                f"Bitvavo's {trade.asset} ask differs from the planning "
                f"price by {deviation_bps:.1f} bps"
            )

        asset_data = client.get_asset(trade.asset)
        if asset_data.get("withdrawalStatus") != "OK":
            raise ValueError(f"Bitvavo {trade.asset} withdrawals are unavailable")
        network = config.withdrawal_networks[trade.asset]
        networks = _supported_networks(asset_data)
        if network not in networks:
            supported = ", ".join(sorted(networks)) or "none"
            raise ValueError(
                f"Configured {trade.asset} network {network!r} is not offered "
                f"by Bitvavo (offered: {supported})"
            )
        withdrawal_minimum = _decimal(
            asset_data.get("withdrawalMinAmount"),
            f"{trade.asset} withdrawal minimum",
        )
        withdrawal_fee = _decimal(
            asset_data.get("withdrawalFee"),
            f"{trade.asset} withdrawal fee",
        )
        expected_amount = amount_quote / ask
        if expected_amount < withdrawal_minimum:
            raise ValueError(
                f"Expected {trade.asset} purchase {expected_amount} is below "
                f"Bitvavo's withdrawal minimum {withdrawal_minimum}"
            )
        if expected_amount <= withdrawal_fee:
            raise ValueError(
                f"Expected {trade.asset} purchase does not exceed its "
                "current withdrawal fee"
            )

        purchases.append(
            PreparedPurchase(
                asset=trade.asset,
                amount_quote_eur=amount_quote,
                expected_amount=expected_amount,
                best_ask_eur=ask,
                withdrawal_minimum=withdrawal_minimum,
                withdrawal_fee=withdrawal_fee,
                destination_hint=_destination_hint(
                    config.withdrawal_addresses[trade.asset]
                ),
                network=network,
            )
        )

    required = sum((item.amount_quote_eur for item in purchases), ZERO)
    if required + plan.estimated_fees_eur > plan.top_up_eur:
        raise ValueError("Rounded Bitvavo orders and estimated fees exceed top-up")
    return PreparedTopUp(
        plan=plan,
        purchases=tuple(purchases),
        available_eur=available_eur,
    )


def render_prepared_top_up(prepared: PreparedTopUp) -> str:
    lines = [
        "BITVAVO TOP-UP EXECUTION",
        f"Deposit amount: €{prepared.plan.top_up_eur:,.2f}",
        f"Available EUR: €{prepared.available_eur:,.2f}",
        f"Estimated trading fees: €{prepared.plan.estimated_fees_eur:,.2f}",
        "Purchases and withdrawal destinations:",
    ]
    for purchase in prepared.purchases:
        lines.append(
            f"  {purchase.asset}: buy €{purchase.amount_quote_eur:,.2f}, "
            f"then send via {purchase.network} to {purchase.destination_hint}"
        )
    return "\n".join(lines)


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": STATE_SCHEMA_VERSION, "runs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Bitvavo execution state is invalid JSON: {path}") from exc
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != STATE_SCHEMA_VERSION
        or not isinstance(data.get("runs"), list)
    ):
        raise ValueError(f"Bitvavo execution state has an unsupported format: {path}")
    return data


@contextmanager
def _execution_lock(state_path: Path) -> Iterator[None]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Bitvavo execution is already running") from exc
        yield


def _update_run(
    state_path: Path,
    state: dict[str, object],
    run: dict[str, object],
    **changes: object,
) -> None:
    run.update(changes)
    _atomic_write(state_path, state)


def _wait_for_filled_order(
    client: BitvavoExecutionClient,
    asset: str,
    order_id: str,
    *,
    sleep: Callable[[float], None],
    attempts: int = 15,
) -> dict[str, object]:
    order: dict[str, object] | None = None
    for attempt in range(attempts):
        try:
            order = client.get_order(asset, order_id)
        except BitvavoError as exc:
            if exc.error_code != 240:
                raise
            if attempt + 1 < attempts:
                sleep(2)
                continue
            raise BitvavoError(
                f"Timed out waiting for the Bitvavo {asset} order",
                error_code=240,
            ) from exc
        status = order.get("status")
        fills = order.get("fills")
        settled = isinstance(fills, list) and bool(fills) and all(
            isinstance(fill, dict) and fill.get("settled") is True
            for fill in fills
        )
        if status == "filled" and settled:
            return order
        if status in {"canceled", "expired"}:
            raise BitvavoError(
                f"Bitvavo {asset} order ended with status {status}"
            )
        if attempt + 1 < attempts:
            sleep(2)
    raise BitvavoError(f"Timed out waiting for the Bitvavo {asset} order")


def _is_filled_and_settled(order: dict[str, object]) -> bool:
    fills = order.get("fills")
    return (
        order.get("status") == "filled"
        and isinstance(fills, list)
        and bool(fills)
        and all(
            isinstance(fill, dict) and fill.get("settled") is True
            for fill in fills
        )
    )


def execute_top_up(
    read_client: BitvavoClient,
    execution_client: BitvavoExecutionClient,
    prepared: PreparedTopUp,
    *,
    state_path: Path = DEFAULT_EXECUTION_STATE_PATH,
    sleep: Callable[[float], None] = time.sleep,
) -> ExecutionResult:
    """Place all buys, reconcile fills, then withdraw only bought quantities."""

    with _execution_lock(state_path):
        state = _load_state(state_path)
        runs = state["runs"]
        assert isinstance(runs, list)
        unresolved = [
            row
            for row in runs
            if isinstance(row, dict)
            and row.get("status")
            in {
                "placing_orders",
                "withdrawing",
                "manual_review",
                "recovering",
                "recovery_withdrawal_submitted",
            }
        ]
        if unresolved:
            raise RuntimeError(
                "A previous Bitvavo run requires review before another can start"
            )

        run_id = str(uuid.uuid4())
        run: dict[str, object] = {
            "run_id": run_id,
            "status": "placing_orders",
            "top_up_eur": str(prepared.plan.top_up_eur),
            "orders": {},
            "withdrawals": {},
        }
        runs.append(run)
        _atomic_write(state_path, state)

        try:
            filled_orders: dict[str, dict[str, object]] = {}
            orders = run["orders"]
            assert isinstance(orders, dict)
            for purchase in prepared.purchases:
                client_order_id = str(
                    uuid.uuid5(uuid.UUID(run_id), f"order:{purchase.asset}")
                )
                orders[purchase.asset] = {
                    "client_order_id": client_order_id,
                    "amount_quote_eur": str(purchase.amount_quote_eur),
                    "status": "submitting",
                }
                _atomic_write(state_path, state)
                response = execution_client.create_market_buy(
                    purchase.asset,
                    purchase.amount_quote_eur,
                    client_order_id,
                )
                order_id = str(response.get("orderId", ""))
                if not order_id:
                    raise BitvavoError(
                        f"Bitvavo omitted the {purchase.asset} order ID"
                    )
                order_row = orders[purchase.asset]
                assert isinstance(order_row, dict)
                order_row.update(
                    {
                        "order_id": order_id,
                        "status": str(response.get("status", "submitted")),
                    }
                )
                _atomic_write(state_path, state)
                filled = (
                    response
                    if _is_filled_and_settled(response)
                    else _wait_for_filled_order(
                        execution_client,
                        purchase.asset,
                        order_id,
                        sleep=sleep,
                    )
                )
                filled_orders[purchase.asset] = filled
                order_row.update(
                    {
                        "status": "filled",
                        "filled_amount": str(filled.get("filledAmount", "")),
                        "filled_amount_quote": str(
                            filled.get("filledAmountQuote", "")
                        ),
                    }
                )
                _atomic_write(state_path, state)

            balances = read_client.get_balances()
            withdrawal_amounts: dict[str, Decimal] = {}
            for purchase in prepared.purchases:
                order = filled_orders[purchase.asset]
                amount = _decimal(
                    order.get("filledAmount"),
                    f"{purchase.asset} filled amount",
                )
                fills = order.get("fills")
                assert isinstance(fills, list)
                base_fees = sum(
                    (
                        _decimal(fill.get("fee"), f"{purchase.asset} fill fee")
                        for fill in fills
                        if isinstance(fill, dict)
                        and fill.get("feeCurrency") == purchase.asset
                    ),
                    ZERO,
                )
                amount -= base_fees
                if amount < purchase.withdrawal_minimum:
                    raise BitvavoError(
                        f"Filled {purchase.asset} amount is below its "
                        "withdrawal minimum"
                    )
                if amount <= purchase.withdrawal_fee:
                    raise BitvavoError(
                        f"Filled {purchase.asset} amount does not cover withdrawal fee"
                    )
                if balances[purchase.asset] < amount:
                    raise BitvavoError(
                        f"Available {purchase.asset} balance is below the filled amount"
                    )
                withdrawal_amounts[purchase.asset] = amount

            _update_run(state_path, state, run, status="withdrawing")
            withdrawals = run["withdrawals"]
            assert isinstance(withdrawals, dict)
            for purchase in prepared.purchases:
                amount = withdrawal_amounts[purchase.asset]
                idempotency_key = f"hwr-{run_id}-{purchase.asset.lower()}"
                withdrawals[purchase.asset] = {
                    "idempotency_key": idempotency_key,
                    "amount": str(amount),
                    "network": purchase.network,
                    "status": "submitting",
                }
                _atomic_write(state_path, state)
                response = execution_client.withdraw(
                    asset=purchase.asset,
                    amount=amount,
                    idempotency_key=idempotency_key,
                )
                withdrawal_row = withdrawals[purchase.asset]
                assert isinstance(withdrawal_row, dict)
                withdrawal_row.update(
                    {
                        "status": "submitted",
                        "withdrawal_id": str(response.get("id", "")),
                        "fee": str(response.get("fee", "")),
                    }
                )
                _atomic_write(state_path, state)

            _update_run(state_path, state, run, status="completed")
            return ExecutionResult(
                run_id=run_id,
                purchases=prepared.purchases,
                withdrawn_amounts=withdrawal_amounts,
            )
        except BaseException as exc:
            _update_run(
                state_path,
                state,
                run,
                status="manual_review",
                error=str(exc),
            )
            raise


def recover_filled_orders(
    read_client: BitvavoClient,
    execution_client: BitvavoExecutionClient,
    config: BitvavoConfig,
    run_id: str,
    *,
    state_path: Path = DEFAULT_EXECUTION_STATE_PATH,
) -> dict[str, Decimal]:
    """Withdraw filled orders from a failed run without placing new orders."""

    with _execution_lock(state_path):
        state = _load_state(state_path)
        runs = state["runs"]
        assert isinstance(runs, list)
        run = next(
            (
                row
                for row in runs
                if isinstance(row, dict) and row.get("run_id") == run_id
            ),
            None,
        )
        if run is None:
            raise ValueError(f"Bitvavo run {run_id} was not found")
        if run.get("status") == "recovery_withdrawal_submitted":
            recovery = run.get("recovery_withdrawals")
            if not isinstance(recovery, dict):
                raise ValueError("Recovered run has invalid withdrawal state")
            return {
                asset: _decimal(row.get("amount"), f"{asset} recovery amount")
                for asset, row in recovery.items()
                if isinstance(row, dict)
            }
        if run.get("status") not in {"manual_review", "recovering"}:
            raise ValueError("Only a failed or recovering run can be recovered")

        orders = run.get("orders")
        withdrawals = run.get("withdrawals")
        if not isinstance(orders, dict) or not orders:
            raise ValueError("Failed run has no recorded orders to recover")
        if not isinstance(withdrawals, dict) or withdrawals:
            raise ValueError(
                "Failed run already contains withdrawals; reconcile manually"
            )

        recovery = run.get("recovery_withdrawals")
        if recovery is None:
            balances = read_client.get_balances()
            prepared_recovery: dict[str, dict[str, str]] = {}
            for asset, row in orders.items():
                if asset not in config.withdrawal_addresses:
                    raise ValueError(f"Unsupported recovery asset {asset}")
                if not isinstance(row, dict) or row.get("status") != "filled":
                    raise ValueError(f"Recorded {asset} order is not filled")
                order_id = str(row.get("order_id", ""))
                if not order_id:
                    raise ValueError(f"Recorded {asset} order has no order ID")
                order = execution_client.get_order(asset, order_id)
                fills = order.get("fills")
                if order.get("status") != "filled" or not isinstance(fills, list):
                    raise ValueError(f"Bitvavo {asset} order is not fully filled")
                if not fills or not all(
                    isinstance(fill, dict) and fill.get("settled") is True
                    for fill in fills
                ):
                    raise ValueError(f"Bitvavo {asset} fills are not settled")
                amount = _decimal(
                    order.get("filledAmount"),
                    f"{asset} filled amount",
                )
                amount -= sum(
                    (
                        _decimal(fill.get("fee"), f"{asset} fill fee")
                        for fill in fills
                        if isinstance(fill, dict)
                        and fill.get("feeCurrency") == asset
                    ),
                    ZERO,
                )

                asset_data = read_client.get_asset(asset)
                if asset_data.get("withdrawalStatus") != "OK":
                    raise ValueError(f"Bitvavo {asset} withdrawals are unavailable")
                network = config.withdrawal_networks[asset]
                if network not in _supported_networks(asset_data):
                    raise ValueError(f"Configured {asset} network is unavailable")
                minimum = _decimal(
                    asset_data.get("withdrawalMinAmount"),
                    f"{asset} withdrawal minimum",
                )
                fee = _decimal(
                    asset_data.get("withdrawalFee"),
                    f"{asset} withdrawal fee",
                )
                if amount < minimum or amount <= fee:
                    raise ValueError(f"Recovered {asset} amount cannot be withdrawn")
                if balances.get(asset, ZERO) < amount:
                    raise ValueError(
                        f"Bitvavo {asset} balance is below the recovered amount"
                    )
                prepared_recovery[asset] = {
                    "order_id": order_id,
                    "amount": str(amount),
                    "network": network,
                    "idempotency_key": f"hwr-r-{run_id}-{asset.lower()}",
                    "status": "prepared",
                }

            run["recovery_withdrawals"] = prepared_recovery
            run["status"] = "recovering"
            run.pop("recovery_error", None)
            _atomic_write(state_path, state)
            recovery = prepared_recovery
        if not isinstance(recovery, dict):
            raise ValueError("Failed run has invalid recovery state")

        recovered_amounts: dict[str, Decimal] = {}
        for asset, row in recovery.items():
            if not isinstance(row, dict):
                raise ValueError("Failed run has invalid recovery row")
            amount = _decimal(row.get("amount"), f"{asset} recovery amount")
            recovered_amounts[asset] = amount
            if row.get("status") == "submitted":
                continue
            row["status"] = "submitting"
            _atomic_write(state_path, state)
            try:
                response = execution_client.withdraw(
                    asset=asset,
                    amount=amount,
                    idempotency_key=str(row["idempotency_key"]),
                )
            except BaseException as exc:
                run["recovery_error"] = str(exc)
                _atomic_write(state_path, state)
                raise
            withdrawal_id = str(response.get("id", ""))
            if not withdrawal_id:
                run["recovery_error"] = "Bitvavo omitted the withdrawal ID"
                _atomic_write(state_path, state)
                raise BitvavoError("Bitvavo omitted the withdrawal ID")
            row.update(
                {
                    "status": "submitted",
                    "withdrawal_id": withdrawal_id,
                    "fee": str(response.get("fee", "")),
                }
            )
            run.pop("recovery_error", None)
            _atomic_write(state_path, state)

        run["status"] = "recovery_withdrawal_submitted"
        _atomic_write(state_path, state)
        return recovered_amounts


def complete_recovered_run(state_path: Path, run_id: str) -> None:
    """Unlock a recovered run after its withdrawal reaches the wallet."""

    with _execution_lock(state_path):
        state = _load_state(state_path)
        runs = state["runs"]
        assert isinstance(runs, list)
        for run in runs:
            if isinstance(run, dict) and run.get("run_id") == run_id:
                if run.get("status") != "recovery_withdrawal_submitted":
                    raise ValueError(
                        "Recovery must be submitted before it can be completed"
                    )
                run["status"] = "recovered"
                _atomic_write(state_path, state)
                return
        raise ValueError(f"Bitvavo run {run_id} was not found")


def acknowledge_reviewed_run(state_path: Path, run_id: str) -> None:
    """Mark a manually reconciled failed run so future executions may proceed."""

    with _execution_lock(state_path):
        state = _load_state(state_path)
        runs = state["runs"]
        assert isinstance(runs, list)
        for run in runs:
            if isinstance(run, dict) and run.get("run_id") == run_id:
                if run.get("status") != "manual_review":
                    raise ValueError("Only a manual-review run can be acknowledged")
                run["status"] = "reviewed"
                _atomic_write(state_path, state)
                return
        raise ValueError(f"Bitvavo run {run_id} was not found")
