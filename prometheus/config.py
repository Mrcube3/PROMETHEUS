"""Runtime configuration. Everything is env-overridable with a PROM_ prefix.

Defaults are chosen so that a clean checkout runs end to end with no secrets, in
the most conservative posture: heuristic model provider, signature-only settlement
verification explicitly labelled SIMULATION, and a short horizon so a full
lifecycle can be observed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal


def _env(key: str, default: str) -> str:
    return os.environ.get(f"PROM_{key}", default)


def _env_int(key: str, default: int) -> int:
    return int(_env(key, str(default)))


def _env_dec(key: str, default: str) -> Decimal:
    return Decimal(_env(key, default))


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def _env_list(key: str, default: str) -> list[str]:
    return [p.strip() for p in _env(key, default).split(",") if p.strip()]


# Settlement verifier identifiers.
VERIFIER_ONCHAIN = "onchain"
VERIFIER_FACILITATOR = "facilitator"
VERIFIER_SIGNATURE_ONLY = "signature_only"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class ConfigError(RuntimeError):
    """Refusal to start in a configuration that would misrepresent settlement."""


@dataclass
class Config:
    # --- storage -------------------------------------------------------------
    db_path: str = field(
        default_factory=lambda: _env(
            "DB_PATH", "/tmp/prometheus.db" if os.environ.get("VERCEL") else "prometheus.db"
        )
    )

    # --- market --------------------------------------------------------------
    binance_hosts: list[str] = field(
        default_factory=lambda: _env_list(
            "BINANCE_HOSTS", "https://api.binance.com,https://data-api.binance.vision"
        )
    )
    venue: str = field(default_factory=lambda: _env("VENUE", "BINANCE_SPOT"))
    assets: list[str] = field(default_factory=lambda: _env_list("ASSETS", "BTCUSDT,ETHUSDT,SOLUSDT"))
    http_timeout_s: float = field(default_factory=lambda: float(_env("HTTP_TIMEOUT_S", "10")))

    # --- horizons ------------------------------------------------------------
    # Supported horizon labels; the engine resolves each to a duration.
    horizons: list[str] = field(default_factory=lambda: _env_list("HORIZONS", "10M,15M"))

    # --- model ---------------------------------------------------------------
    model_provider: str = field(default_factory=lambda: _env("MODEL_PROVIDER", "heuristic"))
    model_name: str = field(default_factory=lambda: _env("MODEL_NAME", ""))
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )
    anthropic_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "ANTHROPIC_BASE_URL", "https://api.anthropic.com"
        ).rstrip("/")
    )
    ollama_host: str = field(default_factory=lambda: _env("OLLAMA_HOST", "").rstrip("/"))

    # --- pricing -------------------------------------------------------------
    base_price: Decimal = field(default_factory=lambda: _env_dec("BASE_PRICE", "0.25"))
    min_price: Decimal = field(default_factory=lambda: _env_dec("MIN_PRICE", "0.05"))
    max_price: Decimal = field(default_factory=lambda: _env_dec("MAX_PRICE", "5.00"))
    price_currency: str = field(default_factory=lambda: _env("PRICE_CURRENCY", "USDT"))
    price_decimals: int = field(default_factory=lambda: _env_int("PRICE_DECIMALS", 6))
    min_reputation_sample: int = field(default_factory=lambda: _env_int("MIN_REPUTATION_SAMPLE", 20))

    # --- x402 ----------------------------------------------------------------
    # Binance's current Agentic Wallet payment flow speaks x402 v2 on BSC.
    # Version 1 remains decodable for reconciliation of old persisted invoices,
    # but new invoices default to the current wire format.
    x402_version: int = field(default_factory=lambda: _env_int("X402_VERSION", 2))
    x402_scheme: str = field(default_factory=lambda: _env("X402_SCHEME", "exact"))
    # x402 v2 uses CAIP-2 network labels. Binance's current wallet reference
    # identifies BSC as eip155:56 and its common BSC USDT address below.
    x402_network: str = field(default_factory=lambda: _env("X402_NETWORK", "eip155:56"))
    x402_chain_id: int = field(default_factory=lambda: _env_int("X402_CHAIN_ID", 56))
    x402_asset: str = field(
        default_factory=lambda: _env("X402_ASSET", "0x55d398326f99059fF775485246999027B3197955")
    )
    x402_asset_name: str = field(default_factory=lambda: _env("X402_ASSET_NAME", "Tether USD"))
    x402_asset_version: str = field(default_factory=lambda: _env("X402_ASSET_VERSION", "1"))
    # BSC USDT does not expose EIP-3009 transferWithAuthorization. Permit2 is
    # the universal x402 exact fallback for ordinary ERC-20 tokens.
    x402_asset_transfer_method: str = field(
        default_factory=lambda: _env("X402_ASSET_TRANSFER_METHOD", "permit2")
    )
    x402_asset_decimals: int = field(default_factory=lambda: _env_int("X402_ASSET_DECIMALS", 18))
    # Receive-only merchant address. PROMETHEUS holds no key for it and has no
    # code path that can spend from it: there is no withdrawal capability in this
    # build. Operators running the on-chain verifier MUST set their own.
    x402_pay_to: str = field(
        default_factory=lambda: _env("X402_PAY_TO", ZERO_ADDRESS)
    )
    x402_timeout_seconds: int = field(default_factory=lambda: _env_int("X402_TIMEOUT_SECONDS", 300))
    x402_facilitator_url: str = field(default_factory=lambda: _env("X402_FACILITATOR_URL", "").rstrip("/"))

    # --- Binance Agent OS (binance-cli) --------------------------------------
    # Read-only corroboration surface. No credential is ever read or passed.
    agentos_enabled: bool = field(default_factory=lambda: _env_bool("AGENTOS_ENABLED", True))
    agentos_binary: str = field(default_factory=lambda: _env("AGENTOS_BINARY", "binance-cli"))
    agentos_api_env: str = field(default_factory=lambda: _env("AGENTOS_API_ENV", "prod"))
    # Windows has no upstream binance-cli build; the official Linux binary is
    # reached through a WSL distribution when one is configured.
    agentos_wsl_distro: str = field(default_factory=lambda: _env("AGENTOS_WSL_DISTRO", ""))
    agentos_wsl_user: str = field(default_factory=lambda: _env("AGENTOS_WSL_USER", "root"))
    agentos_tolerance_bps: Decimal = field(
        default_factory=lambda: _env_dec("AGENTOS_TOLERANCE_BPS", "50")
    )
    # When true, a DISPUTED snapshot is refused rather than published. An
    # UNAVAILABLE second witness never blocks: absence of corroboration is not
    # the same as contradiction.
    agentos_require_agreement: bool = field(
        default_factory=lambda: _env_bool("AGENTOS_REQUIRE_AGREEMENT", True)
    )

    # --- settlement verification --------------------------------------------
    settlement_verifier: str = field(
        default_factory=lambda: _env("SETTLEMENT_VERIFIER", VERIFIER_SIGNATURE_ONLY)
    )
    evm_rpc_url: str = field(
        default_factory=lambda: _env("EVM_RPC_URL", "https://bsc-dataseed.bnbchain.org")
    )
    required_confirmations: int = field(default_factory=lambda: _env_int("REQUIRED_CONFIRMATIONS", 1))

    # --- scheduler -----------------------------------------------------------
    autopilot: bool = field(default_factory=lambda: _env_bool("AUTOPILOT", True))
    generate_interval_s: int = field(default_factory=lambda: _env_int("GENERATE_INTERVAL_S", 120))
    resolve_interval_s: int = field(default_factory=lambda: _env_int("RESOLVE_INTERVAL_S", 30))
    max_open_listings: int = field(default_factory=lambda: _env_int("MAX_OPEN_LISTINGS", 12))

    # --- api -----------------------------------------------------------------
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8402))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "").rstrip("/"))

    @property
    def environment(self) -> str:
        """Human-readable environment label recorded on every passport."""
        if self.settlement_verifier == VERIFIER_SIGNATURE_ONLY:
            return "SIMULATION"
        if self.settlement_verifier == VERIFIER_ONCHAIN:
            return "TESTNET" if self.x402_chain_id == 97 else "LIVE"
        return "UNVERIFIED"

    def validate(self) -> None:
        """Refuse configurations that would let the product overstate itself.

        The failure mode being prevented: running the on-chain verifier against an
        unset receive address, which would make every payment "verify" against
        nothing and get reported as TESTNET or LIVE settlement.
        """
        if self.settlement_verifier not in (
            VERIFIER_ONCHAIN, VERIFIER_FACILITATOR, VERIFIER_SIGNATURE_ONLY
        ):
            raise ConfigError(
                f"PROM_SETTLEMENT_VERIFIER={self.settlement_verifier!r} is not a known verifier"
            )
        if self.settlement_verifier == VERIFIER_ONCHAIN:
            if self.x402_pay_to.lower() == ZERO_ADDRESS:
                raise ConfigError(
                    "PROM_SETTLEMENT_VERIFIER=onchain requires PROM_X402_PAY_TO to be set to a "
                    "real receive address. Refusing to start: verifying payments against the zero "
                    "address would report settlement that did not happen."
                )
            if self.x402_asset.lower() == ZERO_ADDRESS:
                raise ConfigError(
                    "PROM_SETTLEMENT_VERIFIER=onchain requires PROM_X402_ASSET to be a real "
                    "ERC-20 contract address."
                )
        if self.settlement_verifier == VERIFIER_FACILITATOR and not self.x402_facilitator_url:
            raise ConfigError(
                "PROM_SETTLEMENT_VERIFIER=facilitator requires PROM_X402_FACILITATOR_URL"
            )
        if self.x402_asset_transfer_method not in ("eip3009", "permit2"):
            raise ConfigError(
                "PROM_X402_ASSET_TRANSFER_METHOD must be 'eip3009' or 'permit2'"
            )
        if self.min_price > self.max_price:
            raise ConfigError("PROM_MIN_PRICE must not exceed PROM_MAX_PRICE")
        if not (self.min_price <= self.base_price <= self.max_price):
            raise ConfigError("PROM_BASE_PRICE must lie between PROM_MIN_PRICE and PROM_MAX_PRICE")
        if not self.assets:
            raise ConfigError("PROM_ASSETS must list at least one symbol")
        from .signals.schema import HORIZONS

        if not self.horizons:
            raise ConfigError("PROM_HORIZONS must list at least one horizon")
        unknown = [h for h in self.horizons if h not in HORIZONS]
        if unknown:
            raise ConfigError(
                f"PROM_HORIZONS contains unsupported values {unknown}; "
                f"supported: {sorted(HORIZONS)}"
            )

    def base_url(self) -> str:
        return self.public_base_url or f"http://{self.host}:{self.port}"

    def atomic_units(self, amount: Decimal) -> int:
        """Convert a decimal price into integer atomic units of the settlement asset."""
        scaled = amount * (Decimal(10) ** self.x402_asset_decimals)
        if scaled != scaled.to_integral_value():
            # Never silently round a price the buyer is charged.
            raise ValueError(
                f"price {amount} is not representable in {self.x402_asset_decimals} decimals"
            )
        return int(scaled)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        cfg = Config()
        cfg.validate()
        _config = cfg
    return _config


def reset_config() -> None:
    """Test hook -- forces the next get_config() to re-read the environment."""
    global _config
    _config = None
