import os

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from co_scientist.core.constants import (
    THINKING_FLOOR_MAX_TOKENS as THINKING_FLOOR_MAX_TOKENS,
)

DEFAULT_MODEL = "openrouter/inclusionai/ling-3.1-flash"
THINKING_FLOOR_TIMEOUT_SECONDS = float(THINKING_FLOOR_MAX_TOKENS) / 75.0


class Settings(BaseSettings):
    # App-operation physical-call caps remain separate from research budgets.
    app_llm_max_calls: int = Field(default=4, ge=1)
    app_llm_client_calls_per_day: int = Field(default=64, ge=1)
    app_llm_global_calls_per_day: int = Field(default=1024, ge=1)
    app_llm_client_tokens_per_day: int = Field(default=2_000_000, ge=1)
    app_llm_global_tokens_per_day: int = Field(default=32_000_000, ge=1)
    app_llm_max_output_tokens: int = Field(default=32768, ge=1)
    app_llm_max_input_bytes: int = Field(default=256_000, ge=1)
    # The existing global call/token caps cover every funded physical call,
    # including research. BYOK still shares admission to compute resources.
    provider_client_calls_per_day: int = Field(default=512, ge=1)
    provider_client_tokens_per_day: int = Field(default=16_000_000, ge=1)
    provider_host_calls_per_day: int = Field(default=512, ge=1)
    provider_host_tokens_per_day: int = Field(default=16_000_000, ge=1)
    provider_max_input_bytes: int = Field(default=1_000_000, ge=1)
    provider_max_output_tokens: int = Field(default=131072, ge=1)
    anonymous_sessions_per_host_per_day: int = Field(default=16, ge=1)
    anonymous_sessions_per_day: int = Field(default=1000, ge=1)
    runs_per_host_per_day: int = Field(default=32, ge=1)
    runs_per_day: int = Field(default=200, ge=1)
    anonymous_write_global_requests_per_day: int = Field(default=8192, ge=1)
    anonymous_write_client_requests_per_day: int = Field(default=512, ge=1)
    anonymous_write_host_requests_per_day: int = Field(default=1024, ge=1)
    anonymous_write_global_bytes_per_day: int = Field(default=256 * 1024 * 1024, ge=1)
    anonymous_write_client_bytes_per_day: int = Field(default=32 * 1024 * 1024, ge=1)
    anonymous_write_host_bytes_per_day: int = Field(default=64 * 1024 * 1024, ge=1)
    anonymous_store_max_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    staged_document_global_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    staged_document_client_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    staged_document_host_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    user_message_run_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    user_message_client_bytes: int = Field(default=16 * 1024 * 1024, ge=1)
    user_message_host_bytes: int = Field(default=32 * 1024 * 1024, ge=1)
    user_message_global_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    human_corpus_run_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    human_corpus_client_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    human_corpus_host_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    human_corpus_global_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    free_runs_per_host_per_day: int = Field(default=6, ge=1)
    free_runs_globally_per_day: int = Field(default=20, ge=1)
    concurrent_runs_per_host: int = Field(default=10, ge=1)
    continuations_per_run_per_day: int = Field(default=3, ge=1)
    continuations_per_client_per_day: int = Field(default=8, ge=1)
    continuations_per_host_per_day: int = Field(default=16, ge=1)
    continuations_per_day: int = Field(default=100, ge=1)

    # Production model choices are explicit hosting overrides; changing these
    # defaults alone does not change production.
    model_name: str = DEFAULT_MODEL
    supervisor_model_name: str | None = DEFAULT_MODEL
    chat_model_name: str | None = DEFAULT_MODEL
    # LiteLLM and the engine consume provider environment variables, not this
    # Settings object.

    # The module entrypoint uses this port; Makefile, Docker and browser
    # harnesses pass their own explicit ports.
    host: str = "0.0.0.0"  # bind address; 0.0.0.0 for container/dev use
    port: int = 8008

    # The engine MCP client reads MCP_SERVER_URL from the environment rather
    # than Settings.
    mcp_server_url: str = "http://localhost:8888/mcp"

    # K-factor stays deployment-tunable while the engine owns tournament math
    # and initial ratings.
    elo_k_factor: int = 24

    # Deterministic safety hard blocks run first and cannot be overridden by
    # semantic assessment.
    semantic_safety_enabled: bool = True
    # Safety stays on the worker tier rather than the strategic supervisor tier.
    semantic_safety_model: str | None = DEFAULT_MODEL

    log_capture_enabled: bool = True
    log_capture_level: str = "INFO"
    log_capture_max_rows: int = 20000
    logs_admin_token: str = ""
    # Browsers need open ingestion to report their own failures, so bound it per
    # client.
    logs_ingest_per_minute: int = 120

    # One concurrent-run ceiling spans all tiers per researcher; provider spend
    # is bounded separately.
    max_concurrent_runs: int = 10

    # SQLite's single writer, not provider concurrency, limits cohort width;
    # leave writer headroom for ordinary API requests.
    worker_pool_size: int = 8

    status_probe_timeout_seconds: float = 3.0
    status_probe_cache_ttl_seconds: float = 30.0

    # Continuously polled local health snapshots use a short TTL to bound
    # aggregate queries.
    health_check_cache_ttl_seconds: float = 5.0
    # Low disk degrades rather than fails liveness: killing the container frees
    # no space.
    health_check_min_free_disk_bytes: int = 100 * 1024 * 1024

    byok_encryption_key: str = ""
    # Error tracking is off unless a DSN is set; see docs/MONITORING.md.
    sentry_dsn: str = ""
    sentry_environment: str = ""
    # Disabled until the organization and both secrets are configured.
    sentry_alert_organization: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]*)?$")
    sentry_alert_client_secret: SecretStr = SecretStr("")
    honeycomb_marker_api_key: SecretStr = SecretStr("")
    # Removing the daily cap does not remove the free express-only envelope.
    free_runs_per_day: int = 3

    # Hermetic tests select deterministic entailment; production defaults to
    # semantic assessment.
    claim_verifier_model: str | None = None

    # Hermetic tests select metadata resolution; production defaults to live
    # identifier checks.
    evidence_resolver: str = "live"

    model_config = SettingsConfigDict(
        env_file=(None if os.getenv("PYTHON_DOTENV_DISABLED") == "1" else ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    @property
    def effective_chat_model(self) -> str:
        return self.chat_model_name or self.model_name

    @property
    def effective_supervisor_model(self) -> str:
        """Diagnostics mirror the generator's actual supervisor fallback."""
        return self.supervisor_model_name or self.model_name


settings = Settings()


PROVIDER_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "azure": ("AZURE_OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}
# LiteLLM reads process credentials; one map keeps offline and safety admission
# in agreement.


def provider_credential_names(model_name: str) -> tuple[str, ...]:
    provider = model_name.split("/", 1)[0].lower()
    return PROVIDER_CREDENTIAL_ENV.get(provider, ())


def has_provider_credential(model_name: str) -> bool:
    return any(os.getenv(name) for name in provider_credential_names(model_name))


def any_provider_credential() -> bool:
    return any(os.getenv(name) for names in PROVIDER_CREDENTIAL_ENV.values() for name in names)


BYOK_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "anthropic/claude-sonnet-5-5",
    "deepseek": "deepseek/deepseek-flash",
    "gemini": "gemini/gemini-3.8-flash",
    "openai": "openai/gpt-6.1-sol",
    "openrouter": "openrouter/z-ai/glm-5.3-flash",
}
# A local default per provider avoids guessing which other models a key covers.
# It seeds every role; existing runs retain their stored model selections.


def byok_default_model(provider: str) -> str | None:
    return BYOK_PROVIDER_DEFAULT_MODELS.get(provider)


def byok_enabled() -> bool:
    """Key-bearing requests require encryption support and must never be
    accepted for unprotected storage.
    """
    return bool(settings.byok_encryption_key)
