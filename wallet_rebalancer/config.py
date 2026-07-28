"""TOML configuration loading and public-identifier validation."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from .models import ASSETS, TARGET_WEIGHTS


XPUB_PREFIXES = (
    "xpub",
    "ypub",
    "zpub",
    "Ypub",
    "Zpub",
    "tpub",
    "upub",
    "vpub",
    "Upub",
    "Vpub",
)
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {character: index for index, character in enumerate(BASE58_ALPHABET)}


@dataclass(frozen=True)
class WalletConfig:
    bitcoin_xpubs: tuple[str, ...]
    ethereum_addresses: tuple[str, ...]
    solana_addresses: tuple[str, ...]
    solana_stake_accounts: tuple[str, ...]


@dataclass(frozen=True)
class ProviderConfig:
    bitcoin_blockbook_url: str
    ethereum_rpc_url: str
    solana_rpc_url: str
    coingecko_url: str
    link_contract: str


@dataclass(frozen=True)
class PolicyConfig:
    target_weights: dict[str, Decimal]
    threshold: Decimal
    estimated_fee_bps: Decimal
    include_unconfirmed_bitcoin: bool
    max_price_age_seconds: int


@dataclass(frozen=True)
class AppConfig:
    wallet: WalletConfig
    providers: ProviderConfig
    policy: PolicyConfig


def _unique_strings(values: object, label: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(
        isinstance(item, str) for item in values
    ):
        raise ValueError(f"{label} must be a TOML list of strings")
    cleaned = tuple(item.strip() for item in values)
    if any(not item for item in cleaned):
        raise ValueError(f"{label} cannot contain blank values")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{label} contains duplicates")
    if any("replace_with" in item for item in cleaned):
        raise ValueError(f"{label} still contains an example placeholder")
    return cleaned


def _decode_base58(value: str) -> bytes:
    number = 0
    for character in value:
        try:
            digit = BASE58_INDEX[character]
        except KeyError as exc:
            raise ValueError("invalid Base58 character") from exc
        number = number * 58 + digit
    raw = (
        number.to_bytes((number.bit_length() + 7) // 8, "big")
        if number
        else b""
    )
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + raw


def _validate_solana_address(value: str, label: str) -> None:
    try:
        decoded = _decode_base58(value)
    except ValueError as exc:
        raise ValueError(f"{label} contains an invalid Solana address") from exc
    if len(decoded) != 32:
        raise ValueError(f"{label} contains an invalid Solana address")


def _validate_url(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a URL string")
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be an HTTP(S) URL")
    if parsed.scheme != "https" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError(f"{label} must use HTTPS unless it is local")
    return url


def _section(
    raw: dict[str, object],
    name: str,
    allowed_keys: set[str],
) -> dict[str, object]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise ValueError(f"Missing [{name}] section")
    unknown = sorted(set(section) - allowed_keys)
    if unknown:
        raise ValueError(f"Unknown [{name}] settings: {unknown}")
    return section


def load_config(path: Path) -> AppConfig:
    """Load a local config without ever logging its public identifiers."""

    if not path.exists():
        raise FileNotFoundError(
            f"Configuration not found: {path}. Copy config.example.toml "
            "to config.toml and fill in public account identifiers."
        )
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    unknown_sections = sorted(set(raw) - {"wallet", "providers", "policy"})
    if unknown_sections:
        raise ValueError(f"Unknown configuration sections: {unknown_sections}")

    wallet_raw = _section(
        raw,
        "wallet",
        {
            "bitcoin_xpubs",
            "ethereum_addresses",
            "solana_addresses",
            "solana_stake_accounts",
        },
    )
    bitcoin_xpubs = _unique_strings(
        wallet_raw.get("bitcoin_xpubs", []),
        "wallet.bitcoin_xpubs",
    )
    ethereum_addresses = _unique_strings(
        wallet_raw.get("ethereum_addresses", []),
        "wallet.ethereum_addresses",
    )
    solana_addresses = _unique_strings(
        wallet_raw.get("solana_addresses", []),
        "wallet.solana_addresses",
    )
    solana_stake_accounts = _unique_strings(
        wallet_raw.get("solana_stake_accounts", []),
        "wallet.solana_stake_accounts",
    )

    if not bitcoin_xpubs:
        raise ValueError("At least one Bitcoin XPUB is required")
    if not ethereum_addresses:
        raise ValueError("At least one Ethereum address is required")
    if not solana_addresses:
        raise ValueError("At least one Solana address is required")
    if any(not value.startswith(XPUB_PREFIXES) for value in bitcoin_xpubs):
        raise ValueError("wallet.bitcoin_xpubs contains an unsupported XPUB prefix")
    if any(not EVM_ADDRESS_RE.fullmatch(value) for value in ethereum_addresses):
        raise ValueError("wallet.ethereum_addresses contains an invalid address")
    if any(int(value, 16) == 0 for value in ethereum_addresses):
        raise ValueError("wallet.ethereum_addresses contains the zero address")
    if len({value.lower() for value in ethereum_addresses}) != len(
        ethereum_addresses
    ):
        raise ValueError("wallet.ethereum_addresses contains duplicates")
    for address in (*solana_addresses, *solana_stake_accounts):
        _validate_solana_address(address, "wallet.solana addresses")
    if set(solana_addresses) & set(solana_stake_accounts):
        raise ValueError("A Solana address cannot also be listed as a stake account")

    providers_raw = _section(
        raw,
        "providers",
        {
            "bitcoin_blockbook_url",
            "ethereum_rpc_url",
            "solana_rpc_url",
            "coingecko_url",
            "link_contract",
        },
    )

    def provider_url(key: str) -> str:
        env_key = f"HWR_{key.upper()}"
        value = os.getenv(env_key, providers_raw.get(key))
        return _validate_url(value, f"providers.{key}")

    link_contract = providers_raw.get("link_contract")
    if not isinstance(link_contract, str) or not EVM_ADDRESS_RE.fullmatch(
        link_contract
    ):
        raise ValueError("providers.link_contract must be an Ethereum address")

    policy_raw = _section(
        raw,
        "policy",
        {
            "target_btc",
            "target_eth",
            "target_sol",
            "target_link",
            "threshold",
            "estimated_fee_bps",
            "include_unconfirmed_bitcoin",
            "max_price_age_seconds",
        },
    )
    targets = {
        asset: Decimal(
            str(
                policy_raw.get(
                    f"target_{asset.lower()}",
                    TARGET_WEIGHTS[asset],
                )
            )
        )
        for asset in ASSETS
    }
    if any(not value.is_finite() for value in targets.values()):
        raise ValueError("Policy target weights must be finite")
    if any(value <= 0 for value in targets.values()) or sum(
        targets.values(), Decimal("0")
    ) != Decimal("1"):
        raise ValueError("Policy target weights must be positive and sum to 1")

    threshold = Decimal(str(policy_raw.get("threshold", "0.05")))
    estimated_fee_bps = Decimal(
        str(policy_raw.get("estimated_fee_bps", "0"))
    )
    max_price_age_seconds = int(
        policy_raw.get("max_price_age_seconds", 900)
    )
    include_unconfirmed = policy_raw.get(
        "include_unconfirmed_bitcoin",
        False,
    )
    numeric_settings = [threshold, estimated_fee_bps]
    if any(not value.is_finite() for value in numeric_settings):
        raise ValueError("Policy numeric settings must be finite")
    if threshold < 0 or threshold > 1:
        raise ValueError("policy.threshold must be between 0 and 1")
    if estimated_fee_bps < 0 or estimated_fee_bps > 1_000:
        raise ValueError("policy.estimated_fee_bps must be in [0, 1000]")
    if max_price_age_seconds <= 0:
        raise ValueError("policy.max_price_age_seconds must be positive")
    if not isinstance(include_unconfirmed, bool):
        raise ValueError("policy.include_unconfirmed_bitcoin must be boolean")

    return AppConfig(
        wallet=WalletConfig(
            bitcoin_xpubs=bitcoin_xpubs,
            ethereum_addresses=ethereum_addresses,
            solana_addresses=solana_addresses,
            solana_stake_accounts=solana_stake_accounts,
        ),
        providers=ProviderConfig(
            bitcoin_blockbook_url=provider_url("bitcoin_blockbook_url"),
            ethereum_rpc_url=provider_url("ethereum_rpc_url"),
            solana_rpc_url=provider_url("solana_rpc_url"),
            coingecko_url=provider_url("coingecko_url"),
            link_contract=link_contract,
        ),
        policy=PolicyConfig(
            target_weights=targets,
            threshold=threshold,
            estimated_fee_bps=estimated_fee_bps,
            include_unconfirmed_bitcoin=include_unconfirmed,
            max_price_age_seconds=max_price_age_seconds,
        ),
    )
