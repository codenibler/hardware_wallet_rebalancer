"""Environment-only configuration and public-identifier validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
BASE58_INDEX = {
    character: index
    for index, character in enumerate(BASE58_ALPHABET)
}

DEFAULT_PROVIDER_URLS = {
    "bitcoin_blockbook_url": "https://btc1.trezor.io",
    "ethereum_rpc_url": "https://ethereum-rpc.publicnode.com",
    "solana_rpc_url": "https://api.mainnet-beta.solana.com",
    "coingecko_url": "https://api.coingecko.com/api/v3",
}
DEFAULT_LINK_CONTRACT = "0x514910771AF9Ca656af840dff83E8264EcF986CA"


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


def _csv_env(name: str, *, required: bool) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        if required:
            raise ValueError(f"{name} is required in .env")
        return ()

    values = tuple(item.strip() for item in raw.split(",")) if raw.strip() else ()
    if required and not values:
        raise ValueError(f"{name} must contain at least one value")
    if any(not item for item in values):
        raise ValueError(f"{name} cannot contain blank values")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} contains duplicates")
    if any("replace_with" in item for item in values):
        raise ValueError(f"{name} still contains an example placeholder")
    return values


def _decimal_env(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _integer_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _boolean_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


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


def _validate_url(value: str, label: str) -> str:
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


def _provider_url(key: str) -> str:
    env_name = f"HWR_{key.upper()}"
    value = os.getenv(env_name, DEFAULT_PROVIDER_URLS[key])
    return _validate_url(value, env_name)


def load_config() -> AppConfig:
    """Load and validate configuration exclusively from the environment."""

    bitcoin_xpubs = _csv_env("HWR_BITCOIN_XPUBS", required=True)
    ethereum_addresses = _csv_env("HWR_ETHEREUM_ADDRESSES", required=True)
    solana_addresses = _csv_env("HWR_SOLANA_ADDRESSES", required=True)
    solana_stake_accounts = _csv_env(
        "HWR_SOLANA_STAKE_ACCOUNTS",
        required=False,
    )

    if any(not value.startswith(XPUB_PREFIXES) for value in bitcoin_xpubs):
        raise ValueError("HWR_BITCOIN_XPUBS contains an unsupported XPUB prefix")
    if any(not EVM_ADDRESS_RE.fullmatch(value) for value in ethereum_addresses):
        raise ValueError("HWR_ETHEREUM_ADDRESSES contains an invalid address")
    if any(int(value, 16) == 0 for value in ethereum_addresses):
        raise ValueError("HWR_ETHEREUM_ADDRESSES contains the zero address")
    if len({value.lower() for value in ethereum_addresses}) != len(
        ethereum_addresses
    ):
        raise ValueError("HWR_ETHEREUM_ADDRESSES contains duplicates")
    for address in (*solana_addresses, *solana_stake_accounts):
        _validate_solana_address(address, "HWR_SOLANA addresses")
    if set(solana_addresses) & set(solana_stake_accounts):
        raise ValueError(
            "A Solana address cannot also be listed as a stake account"
        )

    link_contract = os.getenv("HWR_LINK_CONTRACT", DEFAULT_LINK_CONTRACT).strip()
    if not EVM_ADDRESS_RE.fullmatch(link_contract):
        raise ValueError("HWR_LINK_CONTRACT must be an Ethereum address")

    targets = {
        asset: _decimal_env(
            f"HWR_TARGET_{asset}",
            TARGET_WEIGHTS[asset],
        )
        for asset in ASSETS
    }
    if any(value <= 0 for value in targets.values()) or sum(
        targets.values(),
        Decimal("0"),
    ) != Decimal("1"):
        raise ValueError(
            "HWR_TARGET_* weights must be positive and sum to 1"
        )

    threshold = _decimal_env("HWR_THRESHOLD", Decimal("0.05"))
    estimated_fee_bps = _decimal_env(
        "HWR_ESTIMATED_FEE_BPS",
        Decimal("50"),
    )
    include_unconfirmed = _boolean_env(
        "HWR_INCLUDE_UNCONFIRMED_BITCOIN",
        False,
    )
    max_price_age_seconds = _integer_env(
        "HWR_MAX_PRICE_AGE_SECONDS",
        900,
    )
    if threshold < 0 or threshold > 1:
        raise ValueError("HWR_THRESHOLD must be between 0 and 1")
    if estimated_fee_bps <= 0 or estimated_fee_bps > 1_000:
        raise ValueError("HWR_ESTIMATED_FEE_BPS must be in (0, 1000]")
    if max_price_age_seconds <= 0:
        raise ValueError("HWR_MAX_PRICE_AGE_SECONDS must be positive")

    return AppConfig(
        wallet=WalletConfig(
            bitcoin_xpubs=bitcoin_xpubs,
            ethereum_addresses=ethereum_addresses,
            solana_addresses=solana_addresses,
            solana_stake_accounts=solana_stake_accounts,
        ),
        providers=ProviderConfig(
            bitcoin_blockbook_url=_provider_url("bitcoin_blockbook_url"),
            ethereum_rpc_url=_provider_url("ethereum_rpc_url"),
            solana_rpc_url=_provider_url("solana_rpc_url"),
            coingecko_url=_provider_url("coingecko_url"),
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
