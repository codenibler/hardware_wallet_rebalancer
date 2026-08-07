from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from wallet_rebalancer.config import load_config


VALID_ENV = {
    "HWR_BITCOIN_XPUBS": "xpubTestPublicAccount",
    "HWR_ETHEREUM_ADDRESSES": "0x1111111111111111111111111111111111111111",
    "STAKED_ETHEREUM_ADDRESSES": "",
    "HWR_SOLANA_ADDRESSES": "11111111111111111111111111111111",
    "STAKED_SOLANA_ADDRESSES": "",
}


class ConfigTests(unittest.TestCase):
    def load_with_env(self, **overrides: str):
        environment = {**VALID_ENV, **overrides}
        with patch.dict(os.environ, environment, clear=True):
            return load_config()

    def test_valid_environment_loads(self) -> None:
        config = self.load_with_env()

        self.assertEqual(len(config.wallet.bitcoin_xpubs), 1)
        self.assertEqual(str(config.policy.threshold), "0.05")
        self.assertEqual(config.policy.estimated_fee_bps, 50)
        self.assertEqual(sum(config.policy.target_weights.values()), 1)

    def test_comma_separated_wallet_identifiers_load(self) -> None:
        config = self.load_with_env(
            HWR_BITCOIN_XPUBS="xpubFirst,xpubSecond",
            HWR_ETHEREUM_ADDRESSES=(
                "0x1111111111111111111111111111111111111111,"
                "0x2222222222222222222222222222222222222222"
            ),
        )

        self.assertEqual(
            config.wallet.bitcoin_xpubs,
            ("xpubFirst", "xpubSecond"),
        )
        self.assertEqual(len(config.wallet.ethereum_addresses), 2)

    def test_staked_ethereum_addresses_load(self) -> None:
        address = "0x2222222222222222222222222222222222222222"
        config = self.load_with_env(STAKED_ETHEREUM_ADDRESSES=address)

        self.assertEqual(config.wallet.staked_ethereum_addresses, (address,))

    def test_staked_ethereum_address_cannot_duplicate_regular_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "staked address"):
            self.load_with_env(
                STAKED_ETHEREUM_ADDRESSES=(
                    "0x1111111111111111111111111111111111111111"
                )
            )

    def test_staked_solana_addresses_load(self) -> None:
        address = "11111111111111111111111111111112"
        config = self.load_with_env(STAKED_SOLANA_ADDRESSES=address)

        self.assertEqual(config.wallet.solana_stake_accounts, (address,))

    def test_staked_solana_address_cannot_duplicate_regular_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "stake account"):
            self.load_with_env(
                STAKED_SOLANA_ADDRESSES="11111111111111111111111111111111"
            )

    def test_legacy_solana_stake_variable_remains_supported(self) -> None:
        address = "11111111111111111111111111111112"
        config = self.load_with_env(HWR_SOLANA_STAKE_ACCOUNTS=address)

        self.assertEqual(config.wallet.solana_stake_accounts, (address,))

    def test_missing_required_identifier_is_rejected(self) -> None:
        environment = dict(VALID_ENV)
        del environment["HWR_BITCOIN_XPUBS"]
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "HWR_BITCOIN_XPUBS"):
                load_config()

    def test_placeholder_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "placeholder"):
            self.load_with_env(
                HWR_BITCOIN_XPUBS="replace_with_bitcoin_account_xpub"
            )

    def test_invalid_boolean_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "true or false"):
            self.load_with_env(HWR_INCLUDE_UNCONFIRMED_BITCOIN="sometimes")

    def test_zero_live_fee_assumption_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "HWR_ESTIMATED_FEE_BPS"):
            self.load_with_env(HWR_ESTIMATED_FEE_BPS="0")

    def test_non_https_remote_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.load_with_env(
                HWR_BITCOIN_BLOCKBOOK_URL="http://btc1.trezor.io"
            )

    def test_zero_ethereum_address_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "zero address"):
            self.load_with_env(
                HWR_ETHEREUM_ADDRESSES=(
                    "0x0000000000000000000000000000000000000000"
                )
            )


if __name__ == "__main__":
    unittest.main()
