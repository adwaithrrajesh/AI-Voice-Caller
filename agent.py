"""
LiveKit + Sarvam AI outbound-sales voice agent.

Pipeline:
    [phone / mic] -> Sarvam STT (Saarika) -> Sarvam-30B LLM
                  -> Sarvam TTS (Bulbul) -> [phone / speaker]

Run modes (LiveKit Agents CLI):
    python agent.py console   # local mic + speakers, no server / no telephony
    python agent.py dev       # connect to LiveKit Cloud as a worker (dev)
    python agent.py start     # production worker (used for real calls)

Environment:
    SARVAM_API_KEY        required
    LIVEKIT_URL           required for dev/start (e.g. wss://xxx.livekit.cloud)
    LIVEKIT_API_KEY       required for dev/start
    LIVEKIT_API_SECRET    required for dev/start
    AGENT_NAME            optional, default "sales-agent" — must match the
                          name make_call.py dispatches with

Per-call context is delivered via room metadata, a JSON blob like:
    {"name": "Krish", "phone": "+91...", "lang_hint": "hi-IN"}
make_call.py packs this into the room when it dispatches the agent.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import sarvam

from prompts import build_instructions

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
logger = logging.getLogger("sales-agent")

AGENT_NAME = os.getenv("AGENT_NAME", "sales-agent")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_call_context(metadata: str | None) -> dict:
    """Parse the JSON metadata that make_call.py attaches to the room.

    Falls back to env vars / defaults so console mode keeps working.
    """
    ctx: dict = {}
    if metadata:
        try:
            ctx = json.loads(metadata)
        except json.JSONDecodeError:
            logger.warning("room.metadata is not valid JSON: %r", metadata)

    return {
        "name": ctx.get("name") or os.getenv("PROSPECT_NAME", "there"),
        "phone": ctx.get("phone"),
        "lang_hint": ctx.get("lang_hint", "hi-IN"),
    }


# ---------------------------------------------------------------------------
# The Agent
# ---------------------------------------------------------------------------


class SalesAgent(Agent):
    """Outbound-sales agent with multilingual support and lead-capture tools."""

    def __init__(self, *, prospect_name: str, phone: str | None) -> None:
        super().__init__(instructions=build_instructions(prospect_name))
        self.prospect_name = prospect_name
        self.phone = phone
        self.lead_state: dict = {
            "name": prospect_name,
            "phone": phone,
            "company": None,
            "email": None,
            "interest_level": None,
            "notes": [],
            "demo_scheduled": False,
            "demo_slot": None,
        }

    # -- Lifecycle -----------------------------------------------------------

    async def on_enter(self) -> None:
        """Greet the prospect immediately on join.

        On telephony calls this fires right after the prospect picks up,
        so the agent always speaks first.
        """
        await self.session.generate_reply(
            instructions=(
                "Open the call now. Greet the prospect by name, introduce "
                "yourself as Maya from FlowXperia, and ask if they have a "
                "quick minute. Keep it under two short sentences."
            )
        )

    # -- Function tools (callable by the LLM mid-conversation) --------------

    @function_tool()
    async def capture_lead_details(
        self,
        ctx: RunContext,
        name: Annotated[str, "Prospect's full name"],
        company: Annotated[str, "Their company name"],
        email: Annotated[str, "Their work email"],
        interest_level: Annotated[
            str, "One of: hot, warm, cold, not-interested"
        ],
        notes: Annotated[str, "Free-form notes about pain-points discussed"],
    ) -> str:
        """Persist the lead details captured during the conversation."""
        self.lead_state.update(
            name=name,
            company=company,
            email=email,
            interest_level=interest_level,
        )
        self.lead_state["notes"].append(notes)
        logger.info("Captured lead: %s", self.lead_state)
        return "Lead saved."

    @function_tool()
    async def schedule_demo(
        self,
        ctx: RunContext,
        date: Annotated[str, "Demo date in YYYY-MM-DD"],
        time: Annotated[str, "Demo time in 24h HH:MM, IST"],
        email: Annotated[str, "Email to send the calendar invite to"],
    ) -> str:
        """Book a demo slot. In production wire this to your calendar API."""
        self.lead_state["demo_scheduled"] = True
        self.lead_state["demo_slot"] = f"{date} {time} IST -> {email}"
        logger.info("Demo scheduled: %s", self.lead_state["demo_slot"])
        return f"Demo confirmed for {date} at {time} IST. Invite sent to {email}."

    @function_tool()
    async def end_call(
        self,
        ctx: RunContext,
        reason: Annotated[
            str, "Why the call is ending: 'completed', 'not-interested', 'wrong-number', etc."
        ],
    ) -> str:
        """Cleanly wrap up the call."""
        logger.info(
            "Ending call. Reason=%s | Final state=%s", reason, self.lead_state
        )
        await self.session.say(
            "Thanks for your time, have a great day!",
            allow_interruptions=False,
        )
        await ctx.session.aclose()
        return "Call ended."


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def entrypoint(ctx: JobContext) -> None:
    """Wire up AgentSession with Sarvam STT + LLM + TTS, then start it."""

    if not os.getenv("SARVAM_API_KEY"):
        raise RuntimeError(
            "SARVAM_API_KEY is not set. Add it to your .env file or export it."
        )

    # Connect to the room first so room.metadata is available.
    await ctx.connect()

    call_ctx = parse_call_context(ctx.room.metadata)
    logger.info(
        "Joining room=%s for prospect=%s phone=%s lang_hint=%s",
        ctx.room.name,
        call_ctx["name"],
        call_ctx["phone"],
        call_ctx["lang_hint"],
    )

    # STT — auto-detects Indian languages each utterance.
    stt = sarvam.STT(
        model="saarika:v2.5",
        language="unknown",
        flush_signal=True,
    )

    # LLM — Sarvam-30B (use sarvam-30b-16k / sarvam-105b for more context/quality).
    llm = sarvam.LLM(
        model="sarvam-30b",
        temperature=0.4,
    )

    # TTS — Bulbul. We seed it with the lang_hint passed by the dispatcher;
    # for stricter language-matching, listen to user_input_transcribed and
    # update tts.target_language_code at runtime.
    tts = sarvam.TTS(
        model="bulbul:v2",
        target_language_code=call_ctx["lang_hint"],
        speaker="anushka",
        pitch=0.0,
        pace=1.0,
        loudness=1.0,
    )

    session = AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        # Sarvam STT does VAD + endpointing internally — let it drive turns.
        # Do NOT pass `vad=`.
        turn_handling=TurnHandlingOptions(turn_detection="stt"),
    )

    await session.start(
        room=ctx.room,
        agent=SalesAgent(
            prospect_name=call_ctx["name"],
            phone=call_ctx["phone"],
        ),
    )


if __name__ == "__main__":
    # `agent_name` is what make_call.py uses to target this worker via
    # CreateAgentDispatchRequest. Without it, the agent would auto-join
    # every room — we want explicit dispatch only.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=AGENT_NAME,
        )
    )
