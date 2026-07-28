from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wallet_rebalancer.config import load_config


VALID_CONFIG = """
[wallet]
bitcoin_xpubs = ["xpubTestPublicAccount"]
ethereum_addresses = ["0x1111111111111111111111111111111111111111"]
solana_addresses = ["11111111111111111111111111111111"]
solana_stake_accounts = []

[providers]
bitcoin_blockbook_url = "https://btc1.trezor.io"
ethereum_rpc_url = "https://ethereum-rpc.publicnode.com"
solana_rpc_url = "https://api.mainnet-beta.solana.com"
coingecko_url = "https://api.coingecko.com/api/v3"
link_contract = "0x514910771AF9Ca656af840dff83E8264EcF986CA"

[policy]
target_btc = 0.50
target_eth = 0.25
target_sol = 0.15
target_link = 0.10
threshold = 0.05
"""


class ConfigTests(unittest.TestCase):
    def write_config(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_config_loads(self) -> None:
        config = load_config(self.write_config(VALID_CONFIG))

        self.assertEqual(len(config.wallet.bitcoin_xpubs), 1)
        self.assertEqual(str(config.policy.threshold), "0.05")
        self.assertEqual(sum(config.policy.target_weights.values()), 1)

    def test_placeholder_is_rejected(self) -> None:
        invalid = VALID_CONFIG.replace(
            "xpubTestPublicAccount",
            "replace_with_bitcoin_account_xpub",
        )
        with self.assertRaisesRegex(ValueError, "placeholder"):
            load_config(self.write_config(invalid))

    def test_unknown_setting_is_rejected(self) -> None:
        invalid = VALID_CONFIG.replace(
            'threshold = 0.05',
            'threshold = 0.05\nrecovery_seed = "never"',
        )
        with self.assertRaisesRegex(ValueError, "Unknown"):
            load_config(self.write_config(invalid))

    def test_non_https_remote_provider_is_rejected(self) -> None:
        invalid = VALID_CONFIG.replace(
            "https://btc1.trezor.io",
            "http://btc1.trezor.io",
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            load_config(self.write_config(invalid))

    def test_zero_ethereum_address_is_rejected(self) -> None:
        invalid = VALID_CONFIG.replace(
            "0x1111111111111111111111111111111111111111",
            "0x0000000000000000000000000000000000000000",
        )
        with self.assertRaisesRegex(ValueError, "zero address"):
            load_config(self.write_config(invalid))


if __name__ == "__main__":
    unittest.main()
