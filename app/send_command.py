"""
send_command.py — helper to send a live-call command via RabbitMQ.

Usage:
    python send_command.py room-name nudge "Ask about their availability"
    python send_command.py room-name interrupt "Hello, can you hear me?"
"""

import argparse
import asyncio
import json
import uuid

import aio_pika
from .config import settings

async def main():
    parser = argparse.ArgumentParser(description="Send a command to a live voice agent room.")
    parser.add_argument("room", help="The LiveKit room name (e.g. call-xxxxxxxx)")
    parser.add_argument("type", choices=["nudge", "interrupt", "update_context"], help="Type of command")
    parser.add_argument("data", help="The text or JSON data for the command")
    
    args = parser.parse_args()

    # Try to parse data as JSON if it looks like it, otherwise treat as string
    try:
        data_payload = json.loads(args.data)
    except json.JSONDecodeError:
        data_payload = {"text": args.data}

    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        
        payload = {
            "room_name": args.room,
            "type": args.type,
            "data": data_payload
        }

        await channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(payload).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=settings.commands_queue,
        )

        print(f" [x] Sent '{args.type}' command to room '{args.room}' via queue '{settings.commands_queue}'")

if __name__ == "__main__":
    asyncio.run(main())
