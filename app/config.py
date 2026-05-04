"""
config.py — single source of truth for all configuration.

Built on `pydantic-settings`. The model below is THE place that decides:
    - which env vars exist
    - which are required (and when)
    - which are optional with defaults
    - what type / range each should have
    - what error message you see if it's wrong

Every other module imports `settings` from here. No `os.getenv` calls
anywhere else.

Validation is two-tiered:

  1. *Field-level* validation runs the moment `Settings()` is instantiated
     (e.g. LIVEKIT_URL must start with `wss://`, TTS_GAIN must be in [1.0, 3.0]).
     This catches typos immediately at process startup.

  2. *Use-case validation* runs explicitly via `settings.require_*()`.
     Different entry-points need different env. Console mode needs
     SARVAM_API_KEY only; the dispatcher microservice additionally needs
     LiveKit, Vobiz trunk id, and RabbitMQ. Each entry-point calls its
     own `require_*` so it fails fast with a clear, scoped message
     instead of failing partway through processing.

Usage:
    from config import settings
    settings.require_sarvam()   # at startup of agent.py / make_call.py
    settings.require_telephony()  # at startup of call_dispatcher
    settings.require_dispatcher() # at startup of dispatcher_service.py
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All environment configuration for the voice-agent stack.

    Reads from `.env` (if present) and falls back to OS environment.
    Field names map to UPPERCASE env vars (case-insensitive).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----------------------------------------------------------------------
    # Sarvam AI — STT / LLM / TTS
    # ----------------------------------------------------------------------
    sarvam_api_key: str | None = Field(
        default=None,
        description="Sarvam dashboard key. REQUIRED for any agent run "
        "(console / dev / start). Get from https://dashboard.sarvam.ai",
    )

    # ----------------------------------------------------------------------
    # LiveKit Cloud — required for telephony (dev / start / make_call /
    # dispatcher). Not needed for console mode.
    # ----------------------------------------------------------------------
    livekit_url: str | None = Field(
        default=None,
        description="LiveKit Cloud project URL, must start with wss://",
    )
    livekit_api_key: str | None = Field(
        default=None, description="LiveKit Cloud API key (APIxxxxxx)"
    )
    livekit_api_secret: str | None = Field(
        default=None, description="LiveKit Cloud API secret"
    )

    # ----------------------------------------------------------------------
    # LiveKit ⇄ Vobiz outbound SIP trunk
    # ----------------------------------------------------------------------
    outbound_trunk_id: str | None = Field(
        default=None,
        description="Returned by `lk sip outbound create outbound_trunk.json`. "
        "Looks like ST_xxxxxxxxxxxxxxxx.",
    )
    agent_name: str = Field(
        default="sales-agent",
        description="Name used by both the worker (agent.py) and dispatcher "
        "(make_call.py / dispatcher_service.py). They MUST match.",
    )

    # ----------------------------------------------------------------------
    # TTS tuning — see Sarvam Bulbul v3 plugin docs
    # ----------------------------------------------------------------------
    tts_speaker: str = Field(
        default="shubh",
        description="Bulbul v3 speaker. Default 'shubh' for crisp Indian "
        "English. Other v3 male voices: ratan, rohan, aditya, kabir.",
    )
    tts_gain: float = Field(
        default=2.0,
        ge=1.0,
        le=3.0,
        description="Digital gain on TTS PCM output. 1.0=identity, "
        "2.0≈+6dB (default), 3.0≈+9.5dB (clipping ceiling).",
    )

    # ----------------------------------------------------------------------
    # RabbitMQ — required for the dispatcher microservice and producers.
    # ----------------------------------------------------------------------
    rabbitmq_url: str = Field(
        default="amqp://guest:guest@localhost:5672/",
        description="amqp:// URI. Local docker-compose default shown.",
    )
    calls_exchange: str = Field(default="voice-calls")
    calls_queue: str = Field(default="voice-calls.outbound")
    calls_routing_key: str = Field(default="outbound")
    calls_dlx: str = Field(default="voice-calls.dlx")
    calls_dlq: str = Field(default="voice-calls.dlq")
    calls_dlq_routing_key: str = Field(default="outbound-failed")
    calls_prefetch: int = Field(
        default=5,
        ge=1,
        le=200,
        description="Max concurrent in-flight calls per dispatcher worker. "
        "Tune to your Vobiz channel limit. Total system concurrency = "
        "N_workers × CALLS_PREFETCH.",
    )

    # ----------------------------------------------------------------------
    # Operational / scaling
    # ----------------------------------------------------------------------
    shutdown_drain_timeout_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        description="On SIGTERM/SIGINT, the dispatcher stops accepting "
        "new messages and waits this many seconds for in-flight calls "
        "to finish before forcing exit. k8s pod termination grace period "
        "should be slightly larger than this.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", description="Root log level for all services."
    )

    # ----------------------------------------------------------------------
    # Console-mode helpers (no telephony)
    # ----------------------------------------------------------------------
    prospect_name: str = Field(
        default="there",
        description="Used in console mode (no telephony) for the opening line.",
    )

    # ----------------------------------------------------------------------
    # Field-level validators — run on Settings() instantiation
    # ----------------------------------------------------------------------

    @field_validator("livekit_url")
    @classmethod
    def _validate_livekit_url(cls, v: str | None) -> str | None:
        if v and not v.startswith(("wss://", "ws://")):
            raise ValueError(
                "LIVEKIT_URL must start with wss:// (or ws:// for local dev)"
            )
        return v

    @field_validator("rabbitmq_url")
    @classmethod
    def _validate_rabbitmq_url(cls, v: str) -> str:
        if not v.startswith(("amqp://", "amqps://")):
            raise ValueError(
                "RABBITMQ_URL must start with amqp:// or amqps://"
            )
        return v

    @field_validator("outbound_trunk_id")
    @classmethod
    def _validate_trunk_id(cls, v: str | None) -> str | None:
        if v and not v.startswith("ST_"):
            raise ValueError(
                "OUTBOUND_TRUNK_ID should start with 'ST_' (LiveKit SIP "
                "trunk identifiers all do). Did you paste the wrong value?"
            )
        return v

    # ----------------------------------------------------------------------
    # Use-case gates — call from each entry-point, fail-fast with scope
    # ----------------------------------------------------------------------

    def require_sarvam(self) -> None:
        """Required by anything that runs the AgentSession (console / dev / start)."""
        if not self.sarvam_api_key:
            _bail(["SARVAM_API_KEY"])

    def require_telephony(self) -> None:
        """Required by anything that places a phone call (make_call, dispatcher)."""
        self.require_sarvam()
        missing = [
            name for name, val in [
                ("LIVEKIT_URL", self.livekit_url),
                ("LIVEKIT_API_KEY", self.livekit_api_key),
                ("LIVEKIT_API_SECRET", self.livekit_api_secret),
                ("OUTBOUND_TRUNK_ID", self.outbound_trunk_id),
            ] if not val
        ]
        if missing:
            _bail(missing)

    def require_dispatcher(self) -> None:
        """Required by dispatcher_service.py specifically."""
        self.require_telephony()
        # rabbitmq_url has a default, but the validator already enforced
        # the scheme. Nothing else to require here, but the method exists
        # so the entry-point reads symmetrically and future RabbitMQ-only
        # vars (e.g. credentials, TLS cert paths) get added here.

    def require_producer(self) -> None:
        """Required by anything that publishes to RabbitMQ (enqueue_call.py)."""
        # producer doesn't need Sarvam or LiveKit — only the broker.
        # rabbitmq_url scheme already validated at instantiation.
        pass


def _bail(missing: list[str]) -> None:
    """Print a single, focused error message and raise SystemExit."""
    raise SystemExit(
        "\n[config error] Missing required environment variable(s):\n"
        + "".join(f"   - {n}\n" for n in missing)
        + "\nFix: add these to your `.env` file (see `.env.example`),\n"
        "or export them in your shell. Then restart the process.\n"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance, instantiated on first call.

    Wrapped in `lru_cache` so .env is parsed once per process. Field-level
    validation errors surface here — the SystemExit path below makes
    them readable instead of dumping a Pydantic traceback.
    """
    try:
        return Settings()
    except ValidationError as exc:
        lines = ["\n[config error] Invalid environment configuration:\n"]
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"])
            lines.append(f"   - {field.upper()}: {err['msg']}\n")
        lines.append("\nFix the values in `.env` and restart.\n")
        raise SystemExit("".join(lines)) from exc


# Module-level convenience handle. Importing modules use:
#     from config import settings
settings: Settings = get_settings()
