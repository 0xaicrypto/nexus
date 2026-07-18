"""Configuration management for Nexus Server.

Loads all server settings from environment variables with sensible defaults.
Organized by functional area:
  - Server basics (host, port, environment)
  - Security (JWT, CORS)
  - LLM providers (keys and defaults)
  - Rate limiting
  - Database
"""

import os
from typing import Optional


class ServerConfig:
    """Server configuration from environment variables."""

    # Server basics
    SERVER_HOST: str = os.getenv("SERVER_HOST", "0.0.0.0")
    SERVER_PORT: int = int(os.getenv("SERVER_PORT", "8001"))
    SERVER_SECRET: str = os.getenv("SERVER_SECRET", "dev-secret-key")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # CORS
    CORS_ALLOW_ORIGINS: str = os.getenv(
        "CORS_ALLOW_ORIGINS",
        # Include tauri:// and asset:// origins so a Tauri desktop
        # connecting to a remote server works without custom env config.
        "http://localhost:3000,http://localhost:5173,tauri://localhost,asset://localhost",
    )

    # API versioning — client↔server compatibility gate.
    # Bump API_VERSION on any breaking change; bump MIN_CLIENT_API_VERSION
    # to force clients older than that version to display an upgrade notice.
    API_VERSION: int = int(os.getenv("API_VERSION", "1"))
    MIN_CLIENT_API_VERSION: int = int(os.getenv("MIN_CLIENT_API_VERSION", "1"))

    # JWT / Auth
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_EXPIRATION_HOURS: int = int(
        os.getenv("JWT_EXPIRATION_HOURS", "24")
    )

    # LLM Configuration
    DEFAULT_LLM_PROVIDER: str = os.getenv(
        "DEFAULT_LLM_PROVIDER", "anthropic"
    )
    DEFAULT_LLM_MODEL: str = os.getenv(
        "DEFAULT_LLM_MODEL", "claude-3-sonnet-20240229"
    )

    # LLM API Keys
    GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    ANTHROPIC_API_KEY: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    # Moonshot AI Kimi (OpenAI-compatible). KIMI_API_KEY is canonical;
    # MOONSHOT_API_KEY (Moonshot's own docs) accepted as fallback.
    KIMI_API_KEY: Optional[str] = (
        os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY")
    )
    # Endpoint override for Kimi; defaults to Moonshot's public API.
    KIMI_BASE_URL: str = os.getenv(
        "KIMI_BASE_URL", "https://api.moonshot.ai/v1"
    )

    # DeepSeek (OpenAI-compatible Chat Completions API).
    DEEPSEEK_API_KEY: Optional[str] = os.getenv("DEEPSEEK_API_KEY")
    DEEPSEEK_BASE_URL: str = os.getenv(
        "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
    )

    # Tool API Keys (for server-side tool execution)
    TAVILY_API_KEY: Optional[str] = os.getenv("TAVILY_API_KEY")
    JINA_API_KEY: Optional[str] = os.getenv("JINA_API_KEY")

    # Rate Limiting
    RATE_LIMIT_LLM_REQUESTS_PER_MINUTE: int = int(
        os.getenv("RATE_LIMIT_LLM_REQUESTS_PER_MINUTE", "60")
    )
    RATE_LIMIT_OTHER_REQUESTS_PER_MINUTE: int = int(
        os.getenv("RATE_LIMIT_OTHER_REQUESTS_PER_MINUTE", "120")
    )

    # ── Stripe billing ─────────────────────────────────────────────
    # The secret key from your Stripe dashboard (live or test). Empty
    # = billing disabled; checkout/webhook endpoints return 501 and
    # the desktop just hides "Upgrade" CTAs. This lets the server boot
    # in a no-billing dev mode for unit tests and local play.
    STRIPE_SECRET_KEY: Optional[str] = os.getenv("STRIPE_SECRET_KEY")
    # Signing secret used to verify webhook payloads. Stripe shows
    # this in Dashboard → Developers → Webhooks → your endpoint.
    STRIPE_WEBHOOK_SECRET: Optional[str] = os.getenv("STRIPE_WEBHOOK_SECRET")
    # URL Stripe redirects the user back to after successful checkout
    # / cancel. Both must be reachable from the user's browser; for
    # local dev, http://localhost:<port> works because the desktop
    # opens a system browser pointed at the local server.
    STRIPE_SUCCESS_URL: str = os.getenv(
        "STRIPE_SUCCESS_URL", "http://localhost:8001/billing/success"
    )
    STRIPE_CANCEL_URL: str = os.getenv(
        "STRIPE_CANCEL_URL", "http://localhost:8001/billing/cancel"
    )
    # Per-tier Stripe Price IDs. You create these in Stripe Dashboard
    # (Products → New Product → set monthly + yearly prices, copy IDs
    # like "price_1ABCxyz..."). Empty value = that tier isn't sellable
    # in this deployment. See docs/BILLING.md for the canonical list.
    STRIPE_PRICE_PRO_MONTHLY:  Optional[str] = os.getenv("STRIPE_PRICE_PRO_MONTHLY")
    STRIPE_PRICE_PRO_YEARLY:   Optional[str] = os.getenv("STRIPE_PRICE_PRO_YEARLY")
    STRIPE_PRICE_PRO_PLUS_MONTHLY: Optional[str] = os.getenv("STRIPE_PRICE_PRO_PLUS_MONTHLY")
    STRIPE_PRICE_PRO_PLUS_YEARLY:  Optional[str] = os.getenv("STRIPE_PRICE_PRO_PLUS_YEARLY")
    STRIPE_PRICE_RADIOLOGY_MONTHLY: Optional[str] = os.getenv("STRIPE_PRICE_RADIOLOGY_MONTHLY")
    STRIPE_PRICE_RADIOLOGY_YEARLY:  Optional[str] = os.getenv("STRIPE_PRICE_RADIOLOGY_YEARLY")

    @property
    def billing_enabled(self) -> bool:
        return bool(self.STRIPE_SECRET_KEY and self.STRIPE_WEBHOOK_SECRET)

    def stripe_price_id(self, tier: str, cadence: str = "monthly") -> Optional[str]:
        """Resolve a tier+cadence pair to its Stripe Price ID.

        ``tier`` ∈ {pro, pro_plus, radiology}; ``cadence`` ∈ {monthly, yearly}.
        Returns None if that combination isn't configured in this deployment
        (operator hasn't created the product yet).
        """
        key = f"STRIPE_PRICE_{tier.upper()}_{cadence.upper()}"
        return getattr(self, key, None)

    # ── Twin (Nexus DigitalTwin) ────────────────────────────────────
    # When 1, /api/v1/llm/chat is served by a per-user DigitalTwin
    # instead of the direct LLM gateway. Default 1 because the user
    # explicitly committed to Phase D ("we haven't launched, data can
    # be wiped"). Tests flip this to "0" via NEXUS_USE_TWIN env to
    # exercise the legacy path.
    USE_TWIN: bool = os.getenv("NEXUS_USE_TWIN", "1") == "1"
    TWIN_BASE_DIR: str = os.getenv(
        "NEXUS_TWIN_BASE_DIR",
        os.path.expanduser("~/.nexus_server/twins"),
    )
    TWIN_IDLE_SECONDS: int = int(os.getenv("NEXUS_TWIN_IDLE_SECONDS", "1800"))

    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "sqlite:///./nexus_server.db"
    )

    def validate(self) -> None:
        """Validate configuration on startup."""
        if self.ENVIRONMENT == "production":
            assert self.SERVER_SECRET != "dev-secret-key", (
                "SERVER_SECRET must be set in production"
            )

        if not self.GEMINI_API_KEY and \
           not self.OPENAI_API_KEY and \
           not self.ANTHROPIC_API_KEY and \
           not self.KIMI_API_KEY:
            import warnings
            warnings.warn(
                "No LLM API keys configured. LLM endpoints will fail.",
                RuntimeWarning,
            )


def get_config() -> ServerConfig:
    """Get singleton configuration instance."""
    return ServerConfig()
