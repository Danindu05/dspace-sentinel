"""Tiny in-process async pub/sub used to feed the SSE stream.

Each subscriber owns an ``asyncio.Queue``; when a queue is full the oldest event
is dropped so a slow browser can never stall the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class BusEvent:
    """One published event: ``event`` is the SSE event name, ``data`` the JSON payload."""

    event: str
    data: Any
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict form."""
        return {"event": self.event, "data": self.data, "ts": self.ts}


class Subscription:
    """A subscriber's queue; iterate with ``async for`` or call :meth:`get`."""

    def __init__(self, bus: Bus, maxsize: int) -> None:
        self._bus = bus
        self.queue: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.closed = False

    def _offer(self, event: BusEvent) -> None:
        if self.closed:
            return
        while True:
            try:
                self.queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    return

    async def get(self, timeout: float | None = None) -> BusEvent | None:
        """Wait for the next event; ``None`` when ``timeout`` elapses first."""
        if timeout is None:
            return await self.queue.get()
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        """Unsubscribe."""
        self.closed = True
        self._bus.unsubscribe(self)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> BusEvent:
        if self.closed:
            raise StopAsyncIteration
        return await self.queue.get()


class Bus:
    """Broadcast events to every current subscriber (non-blocking)."""

    def __init__(self, default_maxsize: int = 256) -> None:
        self._subs: set[Subscription] = set()
        self._default_maxsize = default_maxsize
        self.published = 0
        self.last_event_ts: float | None = None

    def subscribe(self, maxsize: int | None = None) -> Subscription:
        """Create a new subscription (queue size ``maxsize``, drop-oldest when full)."""
        sub = Subscription(self, maxsize or self._default_maxsize)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Remove a subscription (idempotent)."""
        self._subs.discard(sub)
        sub.closed = True

    @asynccontextmanager
    async def listen(self, maxsize: int | None = None) -> AsyncIterator[Subscription]:
        """``async with bus.listen() as sub:`` — subscribes and always unsubscribes."""
        sub = self.subscribe(maxsize)
        try:
            yield sub
        finally:
            sub.close()

    def publish(self, event: str, data: Any = None) -> int:
        """Deliver ``event`` to every subscriber; returns the number of receivers."""
        evt = BusEvent(event=event, data=data)
        self.published += 1
        self.last_event_ts = evt.ts
        for sub in list(self._subs):
            try:
                sub._offer(evt)
            except Exception:  # pragma: no cover - defensive, a subscriber must never break the loop
                log.exception("bus subscriber failed")
        return len(self._subs)

    @property
    def subscriber_count(self) -> int:
        """Number of live subscriptions."""
        return len(self._subs)


__all__ = ["Bus", "BusEvent", "Subscription"]
