"""
dispatcher_service.py — RabbitMQ-driven call-placement microservice.

Consumes JSON messages from `voice-calls.outbound` and turns each one
into an outbound phone call (via call_dispatcher.place_call →
LiveKit → Vobiz → PSTN).

Scalability guarantees:

  * Validated config at startup (config.py / pydantic-settings) — fails
    fast with a focused error if any required env is missing.
  * Hard concurrency cap via `asyncio.Semaphore(CALLS_PREFETCH)` —
    belt-and-suspenders alongside RabbitMQ's prefetch_count.
  * In-flight tasks are tracked. On SIGINT/SIGTERM the service stops
    pulling new messages and waits up to `SHUTDOWN_DRAIN_TIMEOUT_SECONDS`
    for in-flight calls to finish before exiting. k8s pod terminationGracePeriod
    should be slightly larger than this number.
  * `aio_pika.connect_robust` reconnects automatically if RabbitMQ is
    transiently unavailable.

Run:
    python dispatcher_service.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from contextlib import suppress

import aio_pika
from aio_pika import ExchangeType
from aio_pika.abc import AbstractIncomingMessage

from .call_dispatcher import (
    PermanentCallError,
    TransientCallError,
    place_call,
)
from .config import settings


logger = logging.getLogger("dispatcher")


# ===========================================================================
# Dispatcher
# ===========================================================================


class Dispatcher:
    """RabbitMQ consumer that places outbound calls.

    Encapsulates connection, topology setup, message handling, and
    graceful shutdown so the lifecycle is testable and observable.
    """

    def __init__(self) -> None:
        self.semaphore = asyncio.Semaphore(settings.calls_prefetch)
        self.in_flight: set[asyncio.Task] = set()
        self.shutdown_event = asyncio.Event()

    # --------------------------- topology --------------------------------

    async def _declare_topology(
        self, channel: aio_pika.abc.AbstractChannel
    ) -> aio_pika.abc.AbstractQueue:
        """Declare DLX/DLQ + main exchange/queue, idempotently."""
        # Dead-letter side first — main queue references it.
        dlx = await channel.declare_exchange(
            settings.calls_dlx, ExchangeType.DIRECT, durable=True
        )
        dlq = await channel.declare_queue(settings.calls_dlq, durable=True)
        await dlq.bind(dlx, routing_key=settings.calls_dlq_routing_key)

        # Main exchange + work queue
        work_exchange = await channel.declare_exchange(
            settings.calls_exchange, ExchangeType.DIRECT, durable=True
        )
        work_queue = await channel.declare_queue(
            settings.calls_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": settings.calls_dlx,
                "x-dead-letter-routing-key": settings.calls_dlq_routing_key,
            },
        )
        await work_queue.bind(work_exchange, routing_key=settings.calls_routing_key)

        logger.info(
            "Topology ready: %s --(%s)--> %s   (DLQ: %s --(%s)--> %s)",
            settings.calls_exchange, settings.calls_routing_key, settings.calls_queue,
            settings.calls_dlx, settings.calls_dlq_routing_key, settings.calls_dlq,
        )
        return work_queue

    # ------------------------- per-message --------------------------------

    async def _handle_message(self, message: AbstractIncomingMessage) -> None:
        """Process a single call request.

        The semaphore is held for the entire handler so we can never have
        more than `calls_prefetch` calls in flight regardless of how
        message delivery is paced.
        """
        async with self.semaphore:
            await self._dispatch_one(message)

    async def _dispatch_one(self, message: AbstractIncomingMessage) -> None:
        body = message.body
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.error("Invalid JSON, dropping to DLQ: %r", body[:200])
            await message.reject(requeue=False)
            return

        phone = payload.get("phone")
        name = payload.get("name", "there")
        lang = payload.get("lang", "en-IN")
        request_id = payload.get("request_id")

        if not phone:
            logger.error("Missing 'phone' field, dropping to DLQ: %r", payload)
            await message.reject(requeue=False)
            return

        logger.info(
            "Received call request: phone=%s name=%s lang=%s request_id=%s "
            "delivery_tag=%s redelivered=%s",
            phone, name, lang, request_id,
            message.delivery_tag, message.redelivered,
        )
        print(f"\n[QUEUE RECEIVED] phone={phone} name={name} request_id={request_id}")

        try:
            result = await place_call(
                phone=phone,
                prospect_name=name,
                lang_hint=lang,
                request_id=request_id,
            )
        except PermanentCallError as e:
            logger.error(
                "Permanent failure for phone=%s request_id=%s: %s — sending to DLQ",
                phone, request_id, e,
            )
            await message.reject(requeue=False)
            return
        except TransientCallError as e:
            logger.warning(
                "Transient failure for phone=%s request_id=%s: %s — will requeue",
                phone, request_id, e,
            )
            await message.nack(requeue=True)
            return
        except Exception as e:                       # noqa: BLE001
            logger.exception(
                "Unexpected error for phone=%s request_id=%s: %s",
                phone, request_id, e,
            )
            await message.reject(requeue=False)
            return

        logger.info(
            "Call dispatched ok phone=%s request_id=%s room=%s",
            phone, result.request_id, result.room_name,
        )
        print(f"[CALL STATUS] phone={phone} status={result.status} room={result.room_name}\n")
        await message.ack()

    # ------------------------- task tracking ------------------------------

    def _spawn_handler(self, message: AbstractIncomingMessage) -> None:
        """Create a task for a message and register it for drain tracking."""
        task = asyncio.create_task(self._handle_message(message))
        self.in_flight.add(task)
        # Auto-discard from the set when done so it doesn't grow unbounded.
        task.add_done_callback(self.in_flight.discard)

    # --------------------------- main loop --------------------------------

    async def run(self) -> None:
        logger.info(
            "Connecting to RabbitMQ at %s (prefetch=%d, drain_timeout=%.1fs)",
            settings.rabbitmq_url,
            settings.calls_prefetch,
            settings.shutdown_drain_timeout_seconds,
        )

        connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        async with connection:
            channel = await connection.channel()
            # `prefetch_count` caps in-flight messages per consumer (RabbitMQ side).
            await channel.set_qos(prefetch_count=settings.calls_prefetch)

            work_queue = await self._declare_topology(channel)

            self._install_signal_handlers()

            async with work_queue.iterator() as consumer:
                logger.info("Listening on queue=%s ...", settings.calls_queue)
                async for message in consumer:
                    if self.shutdown_event.is_set():
                        # Don't accept new work — let the consumer iterator
                        # close cleanly so the broker re-queues anything
                        # not yet acked.
                        break
                    self._spawn_handler(message)

            await self._drain()

        logger.info("Dispatcher exit clean.")

    # --------------------------- shutdown --------------------------------

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._on_signal, sig)

    def _on_signal(self, sig: signal.Signals) -> None:
        if self.shutdown_event.is_set():
            return  # already shutting down
        logger.warning(
            "Received %s. Stopping consumer; waiting up to %.1fs for "
            "in-flight calls to finish.",
            sig.name,
            settings.shutdown_drain_timeout_seconds,
        )
        self.shutdown_event.set()

    async def _drain(self) -> None:
        """Wait for in-flight tasks up to the configured timeout."""
        if not self.in_flight:
            return

        pending = len(self.in_flight)
        logger.info("Draining %d in-flight call(s) ...", pending)

        try:
            await asyncio.wait_for(
                asyncio.gather(*list(self.in_flight), return_exceptions=True),
                timeout=settings.shutdown_drain_timeout_seconds,
            )
            logger.info("Drain complete: all in-flight calls finished.")
        except asyncio.TimeoutError:
            still = len(self.in_flight)
            logger.warning(
                "Drain timed out with %d call(s) still in flight. They "
                "will be terminated. RabbitMQ will redeliver them since "
                "they were never acked.",
                still,
            )
            for t in list(self.in_flight):
                t.cancel()


# ===========================================================================
# Entry point
# ===========================================================================


def main() -> None:
    # Validate before configuring logging so config errors surface cleanly.
    settings.require_dispatcher()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    )

    try:
        asyncio.run(Dispatcher().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
