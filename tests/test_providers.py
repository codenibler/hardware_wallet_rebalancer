from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

import requests

from wallet_rebalancer.config import (
    AppConfig,
    PolicyConfig,
    ProviderConfig,
    WalletConfig,
)
from wallet_rebalancer.models import TARGET_WEIGHTS
from wallet_rebalancer.providers import (
    EVERSTAKE_STAKE_ADDED_TOPIC,
    EVERSTAKE_UNSTAKE_TOPIC,
    ProviderError,
    PublicDataClient,
)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError("fake URL with secret identifier")
            error.response = self
            raise error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, gets=None, posts=None) -> None:
        self.headers = {}
        self.gets = list(gets or [])
        self.posts = list(posts or [])
        self.get_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self.gets.pop(0)

    def post(self, *args, **kwargs):
        return self.posts.pop(0)


def config() -> AppConfig:
    return AppConfig(
        wallet=WalletConfig(
            bitcoin_xpubs=("xpubSensitive",),
            ethereum_addresses=(
                "0x1111111111111111111111111111111111111111",
            ),
            staked_ethereum_addresses=(
                "0x2222222222222222222222222222222222222222",
            ),
            solana_addresses=("11111111111111111111111111111111",),
            solana_stake_accounts=("11111111111111111111111111111112",),
        ),
        providers=ProviderConfig(
            bitcoin_blockbook_url="https://btc.example",
            ethereum_rpc_url="https://eth.example",
            solana_rpc_url="https://sol.example",
            coingecko_url="https://prices.example",
            link_contract="0x514910771AF9Ca656af840dff83E8264EcF986CA",
        ),
        policy=PolicyConfig(
            target_weights=dict(TARGET_WEIGHTS),
            threshold=Decimal("0.05"),
            estimated_fee_bps=Decimal("0"),
            include_unconfirmed_bitcoin=False,
            max_price_age_seconds=900,
        ),
    )


class ProviderTests(unittest.TestCase):
    def test_holdings_are_converted_from_chain_base_units(self) -> None:
        session = FakeSession(
            gets=[
                FakeResponse(
                    {"balance": "123456789", "unconfirmedBalance": "1000"}
                ),
                FakeResponse(
                    {"txids": ["0xeverstake-transaction"], "totalPages": 1}
                ),
            ],
            posts=[
                FakeResponse({"jsonrpc": "2.0", "result": "0x1"}),
                FakeResponse({"jsonrpc": "2.0", "result": "0x12"}),
                FakeResponse(
                    {"jsonrpc": "2.0", "result": "0x1bc16d674ec80000"}
                ),
                FakeResponse(
                    {"jsonrpc": "2.0", "result": "0x6aaf7c8516d0c0000"}
                ),
                FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "result": {
                            "logs": [
                                {
                                    "address": "0x2222222222222222222222222222222222222222",
                                    "topics": [
                                        EVERSTAKE_STAKE_ADDED_TOPIC,
                                        (
                                            "0x000000000000000000000000"
                                            "1111111111111111111111111111111111111111"
                                        ),
                                    ],
                                    "data": (
                                        "0x000000000000000000000000000000000000000000000000"
                                        "29a2241af62c0000"
                                        "000000000000000000000000000000000000000000000000"
                                        "0000000000000002"
                                    ),
                                }
                            ]
                        },
                    }
                ),
                FakeResponse({"jsonrpc": "2.0", "result": {"value": 5000000000}}),
                FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "result": [{"signature": "sol-everstake-transaction"}],
                    }
                ),
                FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "result": {
                            "meta": {
                                "err": None,
                                "fee": 5000,
                                "preBalances": [20000000000, 1000000000000],
                                "postBalances": [12999995000, 1007000005000],
                            },
                            "transaction": {
                                "message": {
                                    "accountKeys": [
                                        "11111111111111111111111111111111",
                                        "11111111111111111111111111111112",
                                    ]
                                }
                            },
                        },
                    }
                ),
            ],
        )
        holdings = PublicDataClient(config(), session=session).fetch_holdings()

        self.assertEqual(holdings.normalized()["BTC"], Decimal("1.23456789"))
        self.assertEqual(holdings.pending_bitcoin, Decimal("0.00001"))
        self.assertEqual(holdings.normalized()["ETH"], Decimal("5"))
        self.assertEqual(holdings.normalized()["SOL"], Decimal("12"))
        self.assertEqual(holdings.normalized()["LINK"], Decimal("123"))

    def test_everstake_events_are_filtered_to_the_wallet_and_pool(self) -> None:
        session = FakeSession(
            gets=[
                FakeResponse(
                    {"txids": ["0xeverstake-transaction"], "totalPages": 1}
                )
            ],
            posts=[
                FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "result": {
                            "logs": [
                                {
                                    "address": "0x2222222222222222222222222222222222222222",
                                    "topics": [
                                        EVERSTAKE_UNSTAKE_TOPIC,
                                        (
                                            "0x000000000000000000000000"
                                            "1111111111111111111111111111111111111111"
                                        ),
                                    ],
                                    "data": "0x" + f"{10**18:064x}" + f"{2:064x}",
                                },
                                {
                                    "address": "0x2222222222222222222222222222222222222222",
                                    "topics": [
                                        EVERSTAKE_STAKE_ADDED_TOPIC,
                                        (
                                            "0x000000000000000000000000"
                                            "1111111111111111111111111111111111111111"
                                        ),
                                    ],
                                    "data": (
                                        "0x" + f"{3 * 10**18:064x}" + f"{2:064x}"
                                    ),
                                },
                            ]
                        },
                    }
                )
            ]
        )

        total = PublicDataClient(config(), session=session)._fetch_everstake_deposited_wei()

        self.assertEqual(total, 2 * 10**18)

    def test_everstake_sol_deltas_net_deposits_against_withdrawals(self) -> None:
        session = FakeSession(
            posts=[
                FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "result": [
                            {"signature": "sol-deposit"},
                            {"signature": "sol-withdrawal"},
                        ],
                    }
                ),
                FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "result": {
                            "meta": {
                                "err": None,
                                "fee": 5000,
                                "preBalances": [20000000000, 1000000000000],
                                "postBalances": [16999995000, 1003000005000],
                            },
                            "transaction": {
                                "message": {
                                    "accountKeys": [
                                        "11111111111111111111111111111111",
                                        "11111111111111111111111111111112",
                                    ]
                                }
                            },
                        },
                    }
                ),
                FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "result": {
                            "meta": {
                                "err": None,
                                "fee": 7000,
                                "preBalances": [1003000005000, 16999995000],
                                "postBalances": [1002000005000, 17999995000],
                            },
                            "transaction": {
                                "message": {
                                    "accountKeys": [
                                        "11111111111111111111111111111112",
                                        "11111111111111111111111111111111",
                                    ]
                                }
                            },
                        },
                    }
                ),
            ]
        )

        total = PublicDataClient(
            config(), session=session
        )._fetch_everstake_sol_deposited_lamports()

        self.assertEqual(total, 2 * 10**9)

    def test_price_response_and_timestamp_are_validated(self) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {
            "bitcoin": {"eur": 60000, "last_updated_at": now},
            "ethereum": {"eur": 2000, "last_updated_at": now},
            "solana": {"eur": 100, "last_updated_at": now},
            "chainlink": {"eur": 10, "last_updated_at": now},
        }
        session = FakeSession(gets=[FakeResponse(payload)])
        prices = PublicDataClient(config(), session=session).fetch_prices()

        self.assertEqual(prices.normalized()["BTC"], Decimal("60000"))
        self.assertEqual(
            session.get_calls[0][1]["params"]["vs_currencies"],
            "eur",
        )

    def test_provider_error_does_not_leak_xpub(self) -> None:
        session = FakeSession(gets=[FakeResponse({}, status_code=500)])
        with self.assertRaises(ProviderError) as caught:
            PublicDataClient(config(), session=session).fetch_bitcoin()

        self.assertIn("HTTP 500", str(caught.exception))
        self.assertNotIn("xpubSensitive", str(caught.exception))
        self.assertNotIn("secret identifier", str(caught.exception))

    def test_default_session_retries_transient_get_failures(self) -> None:
        session = PublicDataClient(config()).session
        retry = session.get_adapter("https://").max_retries

        self.assertEqual(retry.total, 3)
        self.assertEqual(retry.connect, 3)
        self.assertEqual(retry.read, 3)
        self.assertEqual(retry.status, 3)
        self.assertEqual(retry.allowed_methods, frozenset(("GET",)))
        self.assertEqual(
            retry.status_forcelist,
            (429, 500, 502, 503, 504),
        )


if __name__ == "__main__":
    unittest.main()
