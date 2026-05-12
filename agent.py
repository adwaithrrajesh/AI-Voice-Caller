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
    {"name": "Krish", "phone": "+91...", "lang_hint": "en-IN"}
make_call.py packs this into the room when it dispatches the agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Annotated, AsyncIterable

import aio_pika
from aio_pika import DeliveryMode
# pyrefly: ignore [missing-import]
import numpy as np
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from livekit import rtc
# pyrefly: ignore [missing-import]
from livekit.agents import (Agent,AgentSession,JobContext,ModelSettings,RunContext,TurnHandlingOptions,
    WorkerOptions,cli,function_tool)
# pyrefly: ignore [missing-import]
from livekit.agents.voice.transcription.text_transforms import replace as text_replace
# pyrefly: ignore [missing-import]
from livekit.plugins import sarvam

from prompts import build_instructions


# ---------------------------------------------------------------------------
# Text transforms: clean LLM output before it hits the TTS
# ---------------------------------------------------------------------------
# Sarvam's TTS uses a sentence tokenizer that splits on . ! ? — every split
# resets prosody. We collapse the LLM's chattier punctuation into commas
# and spaces so the TTS treats clauses as one breath-group, not many.
async def _smooth_punctuation(text):
    """Replace ellipses / em-dashes / multi-period with commas + space.

    Why: '...' and '—' force the TTS into hard pauses with flat prosody on
    re-entry. Commas keep prosody flowing across the clause.
    """
    import re as _re
    async for chunk in text:
        # collapse ellipsis '...' or '…' into ', '
        chunk = _re.sub(r"\s*(\.{3,}|…)\s*", ", ", chunk)
        # em/en dash → comma
        chunk = _re.sub(r"\s*[—–]\s*", ", ", chunk)
        # double-period mishaps → single
        chunk = _re.sub(r"\.{2}", ".", chunk)
        yield chunk


# Strip artifacts that the LLM produces for code-mixed words. These confuse
# Bulbul v3's pronouncer and make the read sound mechanical.
_CODEMIX_FIXUPS = {
    "FlowXperia-": "Flow Xperia ",
    "Flow Xperia-": "Flow Xperia ",
    " - ": ", ",
}

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

# --- TTS tuning (overridable via env vars without code changes) ------------
# For clear / crisp Indian English we default to `shubh` (v3 Customer
# Care category) — Customer Care voices are tuned for clarity and clean
# enunciation, exactly what "crisp Indian English" calls for. The
# Content Creation voices are warmer but slightly less articulate.
#   shubh   - Customer Care, crispest articulation (default for English)
#   ratan   - Customer Care, slightly deeper
#   rohan   - Customer Care, neutral male
#   aditya  - Content Creation, warmer/conversational
#   kabir   - Content Creation, deeper timbre
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "shubh")

# Digital gain applied to TTS PCM frames. 1.0 = no change. 2.0 ≈ +6 dB
# (perceived ~2x louder). 3.0 ≈ +9.5 dB (close to clipping ceiling for
# typical TTS output). Anything above 3.0 will clip and sound harsh.
TTS_GAIN = float(os.getenv("TTS_GAIN", "2.0"))


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
        "lang_hint": ctx.get("lang_hint", "en-IN"),
        "request_id": ctx.get("request_id"),
        "tenant_id": ctx.get("tenant_id"),
        "lead_id": ctx.get("lead_id"),
        "prompt": ctx.get("prompt"),
    }


# ---------------------------------------------------------------------------
# The Agent
# ---------------------------------------------------------------------------


class SalesAgent(Agent):
    """Outbound-sales agent with multilingual support and lead-capture tools."""

    def __init__(self, *, prospect_name: str, phone: str | None, session: AgentSession, call_ctx: dict) -> None:
        custom_prompt = call_ctx.get("prompt", "")
        super().__init__(instructions=build_instructions(prospect_name, custom_prompt))
        self.prospect_name = prospect_name
        self.phone = phone
        self.agent_session = session
        self.call_ctx = call_ctx
        self.lead_state: dict = {
            "name": prospect_name,
            "phone": phone,
            "company": None,
            "email": None,
            "interest_level": None,
            "notes": [],
            "demo_scheduled": False,
            "demo_slot": None,
            "prompt": call_ctx["prompt"]
        }

    # -- Lifecycle -----------------------------------------------------------

    async def on_enter(self) -> None:
        """Speak first the moment the call connects.

        Minimal nudge — the actual opening line is defined inside the
        system prompt. This keeps persona changes a one-file edit.
        """
        await self.session.generate_reply(
            instructions=(
                "The call has just connected. Open with your natural "
                "Manoj-style greeting, mention Flowxperia, and ask if they "
                "have a minute. Vary the exact wording — do not repeat any "
                "example line from the prompt verbatim."
            )
        )

    # -- Audio post-processing: digital gain --------------------------------

    async def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ) -> AsyncIterable[rtc.AudioFrame]:
        """Apply digital gain to every TTS audio frame on its way out.

        Bulbul v3 doesn't expose a `loudness` parameter (only v2 does),
        and LiveKit's RoomOutputOptions has no volume knob — so the only
        place we can boost perceived loudness is here, in the audio
        pipeline. We multiply each int16 PCM sample by TTS_GAIN and clip
        to the int16 range to avoid wrap-around distortion.

        TTS_GAIN env var: 1.0=no change, 2.0≈+6dB (default), 3.0≈+9.5dB.
        """
        gain = TTS_GAIN

        # Get the default Sarvam-TTS audio stream (this calls the plugin).
        async for frame in Agent.default.tts_node(self, text, model_settings):
            if gain == 1.0:
                yield frame
                continue

            # frame.data is bytes of interleaved int16 PCM samples.
            samples = np.frombuffer(frame.data, dtype=np.int16)
            boosted = np.clip(
                samples.astype(np.int32) * gain, -32768, 32767
            ).astype(np.int16)

            yield rtc.AudioFrame(
                data=boosted.tobytes(),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
                samples_per_channel=frame.samples_per_channel,
            )

    # -- Real-time Data Commands --------------------------------------------

    async def handle_command(self, payload: dict) -> None:
        """Handle an incoming command from the server via Data Channel."""
        cmd_type = payload.get("type")
        data = payload.get("data")

        logger.info("Agent received command: type=%s", cmd_type)

        if cmd_type == "nudge":
            # Direct the LLM to mention something specific immediately.
            text = data.get("text") if isinstance(data, dict) else str(data)
            await self.session.generate_reply(
                instructions=f"The supervisor has nudged you with this note: '{text}'. "
                             f"Incorporate this into your next response naturally."
            )

        elif cmd_type == "interrupt":
            # Force the agent to speak a specific string right now.
            text = data.get("text") if isinstance(data, dict) else str(data)
            await self.session.say(text, allow_interruptions=True)

        elif cmd_type == "update_context":
            # Update internal state or system instructions mid-call.
            if isinstance(data, dict):
                self.lead_state.update(data)
                logger.info("Updated lead state from command: %s", self.lead_state)

        else:
            logger.warning("Unknown command type: %s", cmd_type)

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

        # Broadcast the captured lead back to the room so the server/UI
        # knows immediately without waiting for a webhook.
        await self.agent_session.room.local_participant.publish_data(
            json.dumps({
                "event": "lead_captured",
                "data": self.lead_state
            }),
            reliability=rtc.DataPacketKind.RELIABLE
        )

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
        # Summary will be sent in the on_shutdown or session end hook
        await ctx.session.aclose()
        return "Call ended."

    # -- Summary Generation --------------------------------------------------

    async def send_summary(self) -> None:
        """Generate a call summary using the LLM and publish it to RabbitMQ."""
        logger.info("Generating call summary for %s...", self.phone)
        
        # Ask the LLM to summarize the conversation from its own history.
        summary_reply = await self.session.generate_reply(
            instructions=(
                "The call has ended. Provide a concise JSON summary of this call. "
                "Include: 'summary' (brief paragraph), 'disposition' (e.g. interested, "
                "busy, wrong-number), 'next_steps', and 'sentiment' (positive/neutral/negative)."
            )
        )
        
        # Extract text from the reply.
        summary_text = ""
        async for chunk in summary_reply.text:
            summary_text += chunk

        try:
            # Try to parse as JSON if the LLM followed instructions, else wrap in dict
            try:
                summary_data = json.loads(summary_text)
            except json.JSONDecodeError:
                summary_data = {"raw_summary": summary_text}

            payload = {
                "request_id": self.call_ctx.get("request_id"),
                "tenant_id": self.call_ctx.get("tenant_id"),
                "lead_id": self.call_ctx.get("lead_id"),
                "phone": self.phone,
                "summary": summary_data,
                "lead_state": self.lead_state
            }

            from app.config import settings
            connection = await aio_pika.connect_robust(settings.rabbitmq_url)
            async with connection:
                channel = await connection.channel()
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps(payload).encode(),
                        delivery_mode=DeliveryMode.PERSISTENT,
                    ),
                    routing_key=settings.summaries_queue,
                )
            logger.info("Published call summary to %s", settings.summaries_queue)
        except Exception as e:
            logger.error("Failed to generate/send summary: %s", e)


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
    print(f"\n[AGENT JOINED] room={ctx.room.name} prospect={call_ctx['name']} phone={call_ctx['phone']}")
    
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

    # LLM — Sarvam-30B. Temperature 0.75 gives strong lexical variation
    # so two turns never sound identical. Drop to 0.5 if you see it go
    # off-script.
    llm = sarvam.LLM(
        model="sarvam-30b",
        temperature=0.75,
    )

    # TTS — Bulbul v3 with `shubh`.
    #
    # Why this combo for Malayalam:
    # - Bulbul v3 is the only Sarvam model with LLM-inferred prosody and
    #   native code-mixed (Manglish) handling. In Sarvam's published blind
    #   A/B against ElevenLabs and Cartesia Sonic-3 across 11 languages,
    #   v3 won the 8 kHz telephony evaluation outright.
    # - `shubh` is Sarvam's documented best-quality voice for Malayalam
    #   specifically (the ml-IN list is short — shubh is on it).
    # - v3 does NOT support pitch / loudness — intentionally absent.
    # - 192k bitrate is the highest mp3 grade the API offers; cleaner
    #   voiceband over telephony than the 128k default.
    tts = sarvam.TTS(
        model="bulbul:v3",
        target_language_code=call_ctx["lang_hint"],
        speaker=TTS_SPEAKER,            # default 'aditya'; override via TTS_SPEAKER env var
        pace=1.0,
        temperature=0.9,                # max-useful prosodic variation in v3
        speech_sample_rate=24000,       # v3 native rate
        # The Sarvam plugin tokenizes by sentence then synthesizes per-chunk,
        # so prosody is "reset" between chunks. Pushing both knobs to their
        # plugin maximums (200 / 500) keeps as much text together as legally
        # possible, giving v3 the longest possible spans for inflection.
        max_chunk_length=500,
        min_buffer_size=200,
        output_audio_bitrate="192k",    # higher fidelity than the 128k default
    )

    session = AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        # Sarvam STT does VAD + endpointing internally — let it drive turns.
        # Do NOT pass `vad=`.
        turn_handling=TurnHandlingOptions(turn_detection="stt"),
        # Text transforms applied between LLM output and TTS input.
        # `filter_markdown` and `filter_emoji` are the LiveKit defaults —
        # we keep them and add our own smoothing for punctuation +
        # code-mix fixups so v3 gets cleaner spans to apply prosody to.
        tts_text_transforms=[
            "filter_markdown",
            "filter_emoji",
            _smooth_punctuation,
            text_replace(_CODEMIX_FIXUPS, case_sensitive=True),
        ],
    )

    agent = SalesAgent(
        prospect_name=call_ctx["name"],
        phone=call_ctx["phone"],
        session=session,
        call_ctx=call_ctx,
    )

    # -- 2-Way Data Connection: Listener -----------------------------------
    @ctx.room.on("data_received")
    def on_data(data: rtc.DataPacket):
        """Handle real-time JSON commands sent to the room."""
        if data.participant is None:
            return  # skip system messages if any

        try:
            payload = json.loads(data.data)
            # Route to the agent's command handler
            asyncio.create_task(agent.handle_command(payload))
        except Exception:
            logger.warning("Failed to parse data packet: %r", data.data)

    await session.start(
        room=ctx.room,
        agent=agent,
    )

    # When the session ends (call hangup), send the summary
    await agent.send_summary()


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
