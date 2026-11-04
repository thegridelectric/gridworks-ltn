"""The LTN's one clock: wall time on a box, or a value a test moves.
Every plant-time read and wait goes through it, while IO durations stay
on the wall."""

import asyncio
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, tzinfo

from gwltn.sema.property_format import UTCMilliseconds


class Clock(ABC):
    @abstractmethod
    def now(self) -> float:
        """Plant time as unix seconds, the drop-in for time.time()."""

    def now_ms(self) -> UTCMilliseconds:
        return int(self.now() * 1000)

    def local_now(self, tz: tzinfo) -> datetime:
        return datetime.fromtimestamp(self.now(), tz)

    @abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Return once plant time has advanced by seconds."""


class WallClock(Clock):
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class Sleepers:
    """The waits pending on a stepped clock: each a target time and the
    future that resolves once the clock reaches it. Wakes are handed to
    the sleeper's own loop, so a step arriving on another thread is safe."""

    def __init__(self) -> None:
        self.pending: list[
            tuple[float, asyncio.AbstractEventLoop, asyncio.Future[None]]
        ] = []
        self.lock = threading.Lock()

    async def wait_until(self, target: float, now: float) -> None:
        if now >= target:
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        with self.lock:
            self.pending.append((target, loop, fut))
        await fut

    def wake(self, now: float) -> None:
        with self.lock:
            due = [p for p in self.pending if p[0] <= now]
            self.pending = [p for p in self.pending if p[0] > now]
        for _, loop, fut in due:
            loop.call_soon_threadsafe(self.resolve, fut)

    @staticmethod
    def resolve(fut: asyncio.Future[None]) -> None:
        if not fut.done():
            fut.set_result(None)


class ManualClock(Clock):
    """A stored value the test moves with advance(); never touches wall time."""

    def __init__(self, start_s: float) -> None:
        self.time_s = start_s
        self.sleepers = Sleepers()

    def now(self) -> float:
        return self.time_s

    def advance(self, seconds: float) -> None:
        self.time_s += seconds
        self.sleepers.wake(self.time_s)

    async def sleep(self, seconds: float) -> None:
        await self.sleepers.wait_until(self.time_s + seconds, self.time_s)
