"""
make_call.py — ad-hoc CLI for placing a single outbound call.

Thin wrapper around `call_dispatcher.place_call`. Same code path as
`dispatcher_service.py` (the RabbitMQ consumer).

Usage:
    python make_call.py +91XXXXXXXXXX --name "Krish" --lang en-IN
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .call_dispatcher import (
    CallDispatchError,
    PermanentCallError,
    TransientCallError,
    place_call,
)
from .config import settings


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
        default="en-IN",
        help=(
            "BCP-47 language hint for the agent's TTS. Default en-IN "
            "(Indian English, clear/crisp). Other options: hi-IN, ta-IN, "
            "te-IN, bn-IN, mr-IN, gu-IN, kn-IN, ml-IN, pa-IN, od-IN."
        ),
    )
    args = parser.parse_args()

    # Validate config up front — fail fast with a focused message if any
    # required telephony env is missing.
    settings.require_telephony()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    )

    try:
        result = asyncio.run(
            place_call(
                phone=args.phone,
                prospect_name=args.name,
                lang_hint=args.lang,
            )
        )
    except PermanentCallError as e:
        sys.exit(f"Call rejected (won't retry): {e}")
    except TransientCallError as e:
        sys.exit(f"Call failed (transient — retry possible): {e}")
    except CallDispatchError as e:
        sys.exit(f"Call dispatch error: {e}")

    print(f"OK request_id={result.request_id} room={result.room_name} "
          f"sip_identity={result.sip_participant_identity}")


if __name__ == "__main__":
    main()
