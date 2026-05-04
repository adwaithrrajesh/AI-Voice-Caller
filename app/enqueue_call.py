"""
enqueue_call.py — publish a single call request to the voice-calls queue.

Useful for smoke-testing the dispatcher microservice without writing a
full producer. Anything that can publish JSON to `voice-calls` /
routing-key `outbound` will work — your CRM, a webhook handler, a cron,
or this script.

Usage:
    python enqueue_call.py +919876543210 --name "Krish" --lang en-IN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

import aio_pika
from aio_pika import DeliveryMode, ExchangeType

from .config import settings


async def publish(payload: dict) -> None:
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        # The dispatcher already creates the exchange durable=True. Re-
        # declaring with the same args is a no-op, but it makes this script
        # safe to run before the dispatcher exists.
        exchange = await channel.declare_exchange(
            settings.calls_exchange, ExchangeType.DIRECT, durable=True
        )
        body = json.dumps(payload).encode("utf-8")
        message = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            message_id=payload["request_id"],
        )
        await exchange.publish(message, routing_key=settings.calls_routing_key)
        print(
            f"Published request_id={payload['request_id']} "
            f"phone={payload['phone']} -> {settings.calls_exchange} "
            f"({settings.calls_routing_key})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a single call request to the RabbitMQ voice-calls queue."
    )
    parser.add_argument("phone", help="E.164 phone, e.g. +919876543210")
    parser.add_argument("--name", default="there", help="Prospect name")
    parser.add_argument("--lang", default="en-IN", help="BCP-47 language hint")
    parser.add_argument(
        "--request-id",
        default=None,
        help="Optional request id for tracing (auto-generated if omitted)",
    )
    args = parser.parse_args()

    # Producers only need RabbitMQ.
    settings.require_producer()

    if not args.phone.startswith("+"):
        sys.exit("Phone must be E.164 (start with '+'), e.g. +919876543210")

    payload = {
        "phone": args.phone,
        "name": args.name,
        "lang": args.lang,
        "request_id": args.request_id or uuid.uuid4().hex,
    }
    asyncio.run(publish(payload))


if __name__ == "__main__":
    main()
