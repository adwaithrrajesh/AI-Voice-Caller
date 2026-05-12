"""
command_service.py — RabbitMQ bridge for live-call commands.

Listens to `voice-commands.inbound` and forwards messages to the corresponding
LiveKit room via Data Channels. This allows the server to "nudge" or 
"interrupt" the agent mid-call.

Payload format:
    {
        "room_name": "call-xxxxxxxx",
        "type": "nudge",
        "data": {"text": "Ask about their budget"}
    }
"""

import asyncio
import json
import logging
import signal
from contextlib import suppress

import aio_pika
from aio_pika import ExchangeType
from livekit import api

from .config import settings

logger = logging.getLogger("command-service")

class CommandService:
    def __init__(self) -> None:
        self.shutdown_event = asyncio.Event()

    async def _handle_command(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            try:
                payload = json.loads(message.body)
            except json.JSONDecodeError:
                logger.error("Invalid JSON command: %r", message.body)
                return

            room_name = payload.get("room_name")
            cmd_type = payload.get("type")
            cmd_data = payload.get("data")

            if not room_name or not cmd_type:
                logger.error("Missing room_name or type in command: %r", payload)
                return

            logger.info("Forwarding command '%s' to room '%s'", cmd_type, room_name)

            lkapi = api.LiveKitAPI(
                url=settings.livekit_url,
                api_key=settings.livekit_api_key,
                api_secret=settings.livekit_api_secret,
            )

            try:
                # Send data to all participants in the room (the agent will pick it up)
                await lkapi.room.send_data(
                    api.SendDataRequest(
                        room=room_name,
                        data=json.dumps({"type": cmd_type, "data": cmd_data}).encode("utf-8"),
                        kind=api.DataPacket_Kind.RELIABLE,
                    )
                )
                logger.info("Command sent successfully to room=%s", room_name)
            except Exception as e:
                logger.error("Failed to send data to LiveKit room %s: %s", room_name, e)
            finally:
                await lkapi.aclose()

    async def run(self) -> None:
        logger.info("Starting Command Service (RabbitMQ -> LiveKit Data Channel)")
        
        connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        async with connection:
            channel = await connection.channel()
            
            # Setup topology
            exchange = await channel.declare_exchange(
                settings.commands_exchange, ExchangeType.DIRECT, durable=True
            )
            queue = await channel.declare_queue(settings.commands_queue, durable=True)
            await queue.bind(exchange, routing_key=settings.commands_routing_key)

            self._install_signal_handlers()

            async with queue.iterator() as consumer:
                logger.info("Listening for commands on queue=%s", settings.commands_queue)
                async for message in consumer:
                    if self.shutdown_event.is_set():
                        break
                    # We process commands concurrently as they are lightweight
                    asyncio.create_task(self._handle_command(message))

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._on_signal)

    def _on_signal(self) -> None:
        logger.info("Shutdown signal received.")
        self.shutdown_event.set()

def main() -> None:
    settings.require_telephony()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    )
    try:
        asyncio.run(CommandService().run())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
