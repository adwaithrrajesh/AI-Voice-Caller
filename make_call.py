"""
make_call.py — initiate an outbound AI sales call.

Usage:
    python make_call.py +91XXXXXXXXXX --name "Krish" --lang hi-IN

Flow:
    1. Create a unique LiveKit room with metadata describing the prospect.
    2. Dispatch the named agent worker (`AGENT_NAME`) into that room.
    3. Ask LiveKit SIP to create a SIP participant on the registered
       Vobiz outbound trunk — this is what actually rings the phone.
    4. The agent picks up the room as soon as the prospect answers.

Prerequisites (one-time):
    - Vobiz outbound trunk created (you have SIP_DOMAIN, username,
      password, an outbound number).
    - LiveKit project provisioned (LIVEKIT_URL, API_KEY, API_SECRET).
    - LiveKit outbound trunk registered against Vobiz with:
          lk sip outbound create outbound_trunk.json
      The returned `SIPTrunkID` goes into OUTBOUND_TRUNK_ID in .env.
    - Agent worker is running:
          python agent.py start
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid

from dotenv import load_dotenv
from livekit import api

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
logger = logging.getLogger("make-call")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
OUTBOUND_TRUNK_ID = os.getenv("OUTBOUND_TRUNK_ID")
AGENT_NAME = os.getenv("AGENT_NAME", "sales-agent")

REQUIRED = {
    "LIVEKIT_URL": LIVEKIT_URL,
    "LIVEKIT_API_KEY": LIVEKIT_API_KEY,
    "LIVEKIT_API_SECRET": LIVEKIT_API_SECRET,
    "OUTBOUND_TRUNK_ID": OUTBOUND_TRUNK_ID,
}


def _validate_env() -> None:
    missing = [k for k, v in REQUIRED.items() if not v]
    if missing:
        sys.exit(
            "Missing required env vars: "
            + ", ".join(missing)
            + ". Add them to your .env file."
        )


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


async def place_call(
    *, phone: str, prospect_name: str, lang_hint: str
) -> None:
    _validate_env()

    room_name = f"call-{uuid.uuid4().hex[:8]}"
    metadata = json.dumps(
        {"name": prospect_name, "phone": phone, "lang_hint": lang_hint}
    )

    logger.info(
        "Placing call: phone=%s prospect=%s lang=%s room=%s trunk=%s",
        phone, prospect_name, lang_hint, room_name, OUTBOUND_TRUNK_ID,
    )

    lkapi = api.LiveKitAPI(
        url=LIVEKIT_URL,
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    )

    try:
        # 1. Create the room with metadata so the agent can read it.
        await lkapi.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                empty_timeout=300,        # auto-cleanup if nothing joins (5 min)
                max_participants=2,       # caller + agent only
                metadata=metadata,
            )
        )
        logger.info("Room created: %s", room_name)

        # 2. Dispatch the named agent worker into this room.
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room_name,
                metadata=metadata,
            )
        )
        logger.info("Agent '%s' dispatched to room %s", AGENT_NAME, room_name)

        # 3. Ring the phone via the Vobiz outbound trunk.
        sip_participant = await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=OUTBOUND_TRUNK_ID,
                sip_call_to=phone,
                room_name=room_name,
                participant_identity=f"sip-{phone}",
                participant_name=prospect_name,
                participant_metadata=metadata,
                # Wait inside this call until the callee actually answers.
                # Useful if you want to log answer/no-answer per attempt.
                wait_until_answered=True,
                # If they don't pick up within 30s, give up.
                ringing_timeout={"seconds": 30},
                # Hard cap so a runaway call can't burn budget.
                max_call_duration={"seconds": 600},
                # Krisp removes background noise on the SIP side. Recommended.
                krisp_enabled=True,
                play_dialtone=False,
            )
        )

        logger.info(
            "Call answered. SIP participant=%s room=%s",
            sip_participant.participant_identity,
            sip_participant.room_name,
        )
    finally:
        await lkapi.aclose()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Place an outbound AI sales call via Vobiz + LiveKit."
    )
    parser.add_argument(
        "phone",
        help="Phone number in E.164 format, e.g. +919876543210",
    )
    parser.add_argument(
        "--name", default="there", help="Prospect's name (used in opening line)"
    )
    parser.add_argument(
        "--lang",
        default="hi-IN",
        help=(
            "BCP-47 language hint for the agent's TTS. One of: en-IN, hi-IN, "
            "ta-IN, te-IN, bn-IN, mr-IN, gu-IN, kn-IN, ml-IN, pa-IN, od-IN."
        ),
    )
    args = parser.parse_args()

    if not args.phone.startswith("+"):
        sys.exit(
            "Phone must be in E.164 format and start with '+', e.g. +919876543210"
        )

    asyncio.run(
        place_call(
            phone=args.phone,
            prospect_name=args.name,
            lang_hint=args.lang,
        )
    )


if __name__ == "__main__":
    main()
