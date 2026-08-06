from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from wallet_rebalancer.bitvavo import (
    BitvavoClient,
    BitvavoConfig,
    BitvavoCredentials,
    BitvavoError,
    BitvavoExecutionClient,
    load_bitvavo_config,
    load_readonly_credentials,
    load_trading_credentials,
)
from wallet_rebalancer.execution import (
    acknowledge_reviewed_run,
    complete_recovered_run,
    execute_top_up,
    prepare_top_up,
    recover_filled_orders,
)
from wallet_rebalancer.models import Holdings, PriceBook
from wallet_rebalancer.planner import build_buy_only_plan

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
ASSETS = ("BTC", "ETH", "SOL", "LINK")


def bitvavo_config() -> BitvavoConfig:
    return BitvavoConfig(
        operator_id=101,
        withdrawal_addresses={
            asset: f"wallet-{asset.lower()}-address" for asset in ASSETS
        },
        withdrawal_networks={
            "BTC": "BTC",
            "ETH": "ETH",
            "SOL": "SOL",
            "LINK": "ETH",
        },
        max_top_up_eur=Decimal("1000"),
        max_price_deviation_bps=Decimal("200"),
    )


def readonly_credentials() -> BitvavoCredentials:
    return BitvavoCredentials("readonly-key", "readonly-secret")


def trading_credentials() -> BitvavoCredentials:
    return BitvavoCredentials("trading-key", "trading-secret")


def balanced_plan():
    return build_buy_only_plan(
        Holdings(
            amounts={"BTC": 500, "ETH": 250, "SOL": 150, "LINK": 100},
            fetched_at=NOW,
        ),
        PriceBook(
            prices_eur={asset: 1 for asset in ASSETS},
            as_of=NOW,
            source="test",
        ),
        top_up_eur=100,
        estimated_fee_bps=0,
    )


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.headers: dict[str, str] = {}
        self.response = response
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


class BitvavoClientTests(unittest.TestCase):
    def test_api_error_preserves_sanitized_bitvavo_reason(self) -> None:
        session = FakeSession(
            FakeResponse(
                {"errorCode": 409, "error": "Account verification required"},
                status_code=400,
            )
        )
        client = BitvavoClient(
            bitvavo_config(),
            readonly_credentials(),
            session=session,
        )

        with self.assertRaisesRegex(
            BitvavoError,
            "error 409.*Account verification required",
        ):
            client.get_balances()

    def test_filtered_public_metadata_accepts_live_object_shape(self) -> None:
        market_session = FakeSession(
            FakeResponse({"market": "BTC-EUR", "status": "trading"})
        )
        asset_session = FakeSession(
            FakeResponse({"symbol": "BTC", "networks": ["BTC"]})
        )

        market = BitvavoClient(
            bitvavo_config(),
            readonly_credentials(),
            session=market_session,
        ).get_market("BTC")
        asset = BitvavoClient(
            bitvavo_config(),
            readonly_credentials(),
            session=asset_session,
        ).get_asset("BTC")

        self.assertEqual(market["market"], "BTC-EUR")
        self.assertEqual(asset["networks"], ["BTC"])

    def test_private_get_signs_path_and_query_string(self) -> None:
        session = FakeSession(FakeResponse({"taker": "0.0025"}))
        client = BitvavoClient(
            bitvavo_config(),
            readonly_credentials(),
            session=session,
            clock_ms=lambda: 123456789,
        )

        self.assertEqual(client.get_market_fee_bps("BTC"), Decimal("25"))

        call = session.calls[0]
        signed = "123456789GET/v2/account/fees?market=BTC-EUR"
        expected = hmac.new(
            b"readonly-secret",
            signed.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            call["headers"]["Bitvavo-Access-Signature"],
            expected,
        )
        self.assertEqual(
            call["headers"]["Bitvavo-Access-Key"],
            "readonly-key",
        )

    def test_readonly_client_exposes_no_write_methods(self) -> None:
        client = BitvavoClient(bitvavo_config(), readonly_credentials())

        self.assertFalse(hasattr(client, "create_market_buy"))
        self.assertFalse(hasattr(client, "withdraw"))

    def test_readonly_client_rejects_internal_write_before_network(self) -> None:
        session = FakeSession(FakeResponse({"orderId": "unexpected"}))
        client = BitvavoClient(
            bitvavo_config(),
            readonly_credentials(),
            session=session,
        )

        with self.assertRaisesRegex(BitvavoError, "read-only"):
            client._request(
                "POST",
                "/order",
                body={"market": "BTC-EUR"},
                private=True,
            )

        self.assertEqual(session.calls, [])

    def test_market_buy_signs_the_exact_compact_body(self) -> None:
        session = FakeSession(FakeResponse({"orderId": "order-1"}))
        client = BitvavoExecutionClient(
            bitvavo_config(),
            trading_credentials(),
            session=session,
            clock_ms=lambda: 123456789,
        )

        client.create_market_buy("BTC", Decimal("25.50"), "client-id")

        call = session.calls[0]
        body = str(call["data"])
        self.assertEqual(json.loads(body)["amountQuote"], "25.50")
        signed = f"123456789POST/v2/order{body}"
        expected = hmac.new(
            b"trading-secret",
            signed.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            call["headers"]["Bitvavo-Access-Signature"],
            expected,
        )
        self.assertEqual(
            call["headers"]["Bitvavo-Access-Key"],
            "trading-key",
        )

    def test_order_reconciliation_uses_bitvavo_order_id(self) -> None:
        session = FakeSession(FakeResponse({"status": "filled"}))
        client = BitvavoExecutionClient(
            bitvavo_config(),
            trading_credentials(),
            session=session,
        )

        client.get_order("BTC", "bitvavo-order-id")

        self.assertIn(
            "market=BTC-EUR&orderId=bitvavo-order-id",
            session.calls[0]["url"],
        )
        self.assertNotIn("clientOrderId", session.calls[0]["url"])


class BitvavoConfigTests(unittest.TestCase):
    def test_loads_fixed_wallet_allowlist(self) -> None:
        environment = {
            "HWR_BITVAVO_OPERATOR_ID": "101",
            **{
                f"HWR_BITVAVO_WITHDRAW_{asset}_ADDRESS": f"address-{asset}"
                for asset in ASSETS
            },
            "HWR_BITVAVO_WITHDRAW_BTC_NETWORK": "BTC",
            "HWR_BITVAVO_WITHDRAW_ETH_NETWORK": "ETH",
            "HWR_BITVAVO_WITHDRAW_SOL_NETWORK": "SOL",
            "HWR_BITVAVO_WITHDRAW_LINK_NETWORK": "ETH",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = load_bitvavo_config()

        self.assertEqual(config.operator_id, 101)
        self.assertEqual(config.withdrawal_addresses["BTC"], "address-BTC")
        self.assertEqual(config.max_top_up_eur, Decimal("1000"))

    def test_loads_separate_readonly_and_trading_credentials(self) -> None:
        environment = {
            "BITVAVO_READONLY_API_KEY": "read-key",
            "BITVAVO_READONLY_API_SECRET": "read-secret",
            "BITVAVO_TRADING_API_KEY": "trade-key",
            "BITVAVO_TRADING_API_SECRET": "trade-secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            readonly = load_readonly_credentials()
            trading = load_trading_credentials()

        self.assertEqual(readonly.api_key, "read-key")
        self.assertEqual(readonly.api_secret, "read-secret")
        self.assertEqual(trading.api_key, "trade-key")
        self.assertEqual(trading.api_secret, "trade-secret")

    def test_missing_address_is_rejected(self) -> None:
        environment = {
            "HWR_BITVAVO_OPERATOR_ID": "101",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "WITHDRAW_BTC_ADDRESS"):
                load_bitvavo_config()


class FakeBitvavoClient:
    def __init__(
        self,
        *,
        bad_network: bool = False,
        transient_order_misses: int = 0,
    ) -> None:
        self.bad_network = bad_network
        self.transient_order_misses = transient_order_misses
        self.balance_reads = 0
        self.created: list[tuple[str, Decimal, str]] = []
        self.withdrawals: list[dict[str, object]] = []

    def get_balances(self):
        self.balance_reads += 1
        return {
            "EUR": Decimal("100"),
            "BTC": Decimal("100"),
            "ETH": Decimal("100"),
            "SOL": Decimal("100"),
            "LINK": Decimal("100"),
        }

    def get_market(self, asset):
        return {
            "market": f"{asset}-EUR",
            "status": "trading",
            "notionalDecimals": 2,
            "minOrderInQuoteAsset": "5",
        }

    def get_best_ask(self, asset):
        return Decimal("1")

    def get_asset(self, asset):
        networks = ["WRONG"] if self.bad_network else [
            bitvavo_config().withdrawal_networks[asset]
        ]
        return {
            "withdrawalStatus": "OK",
            "withdrawalMinAmount": "0.01",
            "withdrawalFee": "0.001",
            "networks": networks,
        }

    def create_market_buy(self, asset, amount_quote_eur, client_order_id):
        self.created.append((asset, amount_quote_eur, client_order_id))
        return {"orderId": f"order-{asset}", "status": "new"}

    def get_order(self, asset, order_id):
        if order_id != f"order-{asset}":
            raise AssertionError("reconciliation did not use Bitvavo order ID")
        if self.transient_order_misses:
            self.transient_order_misses -= 1
            raise BitvavoError(
                "Bitvavo rejected GET /order (error 240)",
                error_code=240,
            )
        amount = {
            "BTC": "50",
            "ETH": "25",
            "SOL": "15",
            "LINK": "10",
        }[asset]
        return {
            "status": "filled",
            "filledAmount": amount,
            "filledAmountQuote": amount,
            "fills": [
                {
                    "settled": True,
                    "fee": "0",
                    "feeCurrency": "EUR",
                }
            ],
        }

    def withdraw(self, **kwargs):
        self.withdrawals.append(kwargs)
        return {"id": f"withdrawal-{kwargs['asset']}", "fee": "0.001"}


class BitvavoExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.state_path = Path(self.temporary_directory.name) / "state.json"

    def test_prepare_and_execute_buys_then_withdraws(self) -> None:
        read_client = FakeBitvavoClient()
        execution_client = FakeBitvavoClient()
        prepared = prepare_top_up(
            read_client,
            bitvavo_config(),
            balanced_plan(),
        )

        result = execute_top_up(
            read_client,
            execution_client,
            prepared,
            state_path=self.state_path,
            sleep=lambda _: None,
        )

        self.assertEqual(
            [row[0] for row in execution_client.created],
            list(ASSETS),
        )
        self.assertEqual(
            [row["asset"] for row in execution_client.withdrawals],
            list(ASSETS),
        )
        self.assertEqual(read_client.created, [])
        self.assertEqual(read_client.withdrawals, [])
        self.assertEqual(read_client.balance_reads, 2)
        self.assertEqual(execution_client.balance_reads, 0)
        self.assertEqual(result.withdrawn_amounts["BTC"], Decimal("50"))
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["runs"][0]["status"], "completed")
        self.assertNotIn("wallet-btc-address", self.state_path.read_text())

    def test_wrong_network_fails_before_orders(self) -> None:
        client = FakeBitvavoClient(bad_network=True)

        with self.assertRaisesRegex(ValueError, "not offered"):
            prepare_top_up(client, bitvavo_config(), balanced_plan())

        self.assertEqual(client.created, [])

    def test_transient_order_240_is_retried(self) -> None:
        read_client = FakeBitvavoClient()
        execution_client = FakeBitvavoClient(transient_order_misses=2)
        prepared = prepare_top_up(
            read_client,
            bitvavo_config(),
            balanced_plan(),
        )

        result = execute_top_up(
            read_client,
            execution_client,
            prepared,
            state_path=self.state_path,
            sleep=lambda _: None,
        )

        self.assertEqual(result.withdrawn_amounts["BTC"], Decimal("50"))
        self.assertEqual(execution_client.transient_order_misses, 0)

    def test_failed_run_requires_review_and_can_be_acknowledged(self) -> None:
        client = FakeBitvavoClient()
        prepared = prepare_top_up(client, bitvavo_config(), balanced_plan())
        client.get_order = lambda asset, client_order_id: {
            "status": "expired",
            "fills": [],
        }

        with self.assertRaisesRegex(RuntimeError, "expired"):
            execute_top_up(
                client,
                client,
                prepared,
                state_path=self.state_path,
                sleep=lambda _: None,
            )

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        run_id = state["runs"][0]["run_id"]
        self.assertEqual(state["runs"][0]["status"], "manual_review")
        acknowledge_reviewed_run(self.state_path, run_id)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["runs"][0]["status"], "reviewed")

    def test_recovery_withdraws_filled_order_without_new_purchase(self) -> None:
        run_id = "3c04d388-bc0b-4a54-b9f7-19c05a42efe9"
        self.state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "runs": [
                        {
                            "run_id": run_id,
                            "status": "manual_review",
                            "orders": {
                                "BTC": {
                                    "status": "filled",
                                    "order_id": "order-BTC",
                                }
                            },
                            "withdrawals": {},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        read_client = FakeBitvavoClient()
        execution_client = FakeBitvavoClient()

        amounts = recover_filled_orders(
            read_client,
            execution_client,
            bitvavo_config(),
            run_id,
            state_path=self.state_path,
        )

        self.assertEqual(amounts, {"BTC": Decimal("50")})
        self.assertEqual(execution_client.created, [])
        self.assertEqual(len(execution_client.withdrawals), 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["runs"][0]["status"],
            "recovery_withdrawal_submitted",
        )

        recover_filled_orders(
            read_client,
            execution_client,
            bitvavo_config(),
            run_id,
            state_path=self.state_path,
        )
        self.assertEqual(len(execution_client.withdrawals), 1)

        complete_recovered_run(self.state_path, run_id)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["runs"][0]["status"], "recovered")


if __name__ == "__main__":
    unittest.main()
