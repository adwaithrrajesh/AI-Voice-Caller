"""
call_dispatcher.py — shared call-placement logic.

This module exposes a single async function, `place_call`, used by both
the ad-hoc CLI (`make_call.py`) and the RabbitMQ consumer
(`dispatcher_service.py`). Keeping it in one place means the two entry
points always behave identically.

The function:
    1. Creates a unique LiveKit room with metadata about the prospect.
    2. Dispatches the named agent worker (`AGENT_NAME`) into that room.
    3. Asks LiveKit SIP to ring the prospect via the registered Vobiz
       outbound trunk.

It returns a CallResult dict so callers can log / report what happened.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from livekit import api

from .config import settings

logger = logging.getLogger("call-dispatcher")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CallDispatchError(Exception):
    """Base class for problems placing a call."""


class TransientCallError(CallDispatchError):
    """The error is likely temporary (network, broker, 5xx). Caller may retry."""


class PermanentCallError(CallDispatchError):
    """The error won't get better by retrying (bad phone, missing trunk, etc.)."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class CallResult:
    """Structured outcome of a place_call attempt."""

    request_id: str
    phone: str
    prospect_name: str
    lang_hint: str
    room_name: str | None = None
    sip_participant_identity: str | None = None
    status: str = "unknown"   # one of: ok, transient_error, permanent_error
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


async def place_call(
    *,
    phone: str,
    prospect_name: str = "there",
    lang_hint: str = "en-IN",
    request_id: str | None = None,
) -> CallResult:
    """Place a single outbound call.

    Config comes from `config.settings` (validated at startup). This
    function only re-checks input that varies per-call (the phone number).

    Raises:
        PermanentCallError: bad input or LiveKit/Vobiz says the call cannot
            be placed (bad trunk id, malformed number, etc.).
        TransientCallError: a transient error — caller should retry.
    """
    request_id = request_id or uuid.uuid4().hex
    result = CallResult(
        request_id=request_id,
        phone=phone,
        prospect_name=prospect_name,
        lang_hint=lang_hint,
    )

    # ---- Input validation (permanent errors) ------------------------------
    if not phone or not phone.startswith("+"):
        raise PermanentCallError(
            f"Phone must be E.164 (start with '+'): got {phone!r}"
        )

    # Telephony env should already have been validated at process startup
    # (require_telephony() in the entry-point). We re-check defensively
    # for callers that imported us programmatically.
    settings.require_telephony()

    # ---- Build room + dispatch payload -----------------------------------
    import json
    room_name = f"call-{uuid.uuid4().hex[:8]}"
    metadata = json.dumps({
        "name": prospect_name,
        "phone": phone,
        "lang_hint": lang_hint,
        "request_id": request_id,
    })

    logger.info(
        "Placing call request_id=%s phone=%s prospect=%s lang=%s room=%s",
        request_id, phone, prospect_name, lang_hint, room_name,
    )

    lkapi = api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )

    try:
        # 1. Create the room
        try:
            await lkapi.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=300,
                    max_participants=2,
                    metadata=metadata,
                )
            )
        except Exception as e:                     # noqa: BLE001 — rethrow classified
            raise TransientCallError(f"create_room failed: {e}") from e
        result.room_name = room_name

        # 2. Dispatch the agent worker
        try:
            await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=settings.agent_name,
                    room=room_name,
                    metadata=metadata,
                )
            )
        except Exception as e:                     # noqa: BLE001
            raise TransientCallError(f"agent dispatch failed: {e}") from e

        # 3. Ring the phone via Vobiz trunk
        try:
            sip_participant = await lkapi.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=settings.outbound_trunk_id,
                    sip_call_to=phone,
                    room_name=room_name,
                    participant_identity=f"sip-{phone}",
                    participant_name=prospect_name,
                    participant_metadata=metadata,
                    wait_until_answered=True,
                    ringing_timeout={"seconds": 30},
                    max_call_duration={"seconds": 600},
                    krisp_enabled=True,
                    play_dialtone=False,
                )
            )
        except Exception as e:                     # noqa: BLE001
            # SIP errors that are clearly bad input → permanent
            msg = str(e).lower()
            if any(s in msg for s in (
                "invalid number", "not found", "object cannot be found",
                "malformed", "invalid_argument", "no_answer",
                "user_busy", "rejected_by_user",
            )):
                raise PermanentCallError(f"create_sip_participant failed: {e}") from e
            raise TransientCallError(f"create_sip_participant failed: {e}") from e

        result.sip_participant_identity = sip_participant.participant_identity
        result.status = "ok"
        logger.info(
            "Call answered request_id=%s sip_identity=%s",
            request_id, result.sip_participant_identity,
        )
        return result

    finally:
        await lkapi.aclose()
