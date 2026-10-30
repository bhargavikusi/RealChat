"""The worker's job loops (D-046)."""

import asyncio
from datetime import datetime

import pytest

from apps.worker import main as worker


class Ticks:
    def __init__(self) -> None:
        self.count = 0

    async def tick(self, now: datetime) -> None:
        self.count += 1


class StuckRetention:
    """A cleanup that takes a long time (until released)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def tick(self, now: datetime) -> None:
        self.started.set()
        await self.release.wait()


async def test_a_long_cleanup_does_not_delay_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "TICK_SECONDS", 0.001)
    matcher, idle, retention = Ticks(), Ticks(), StuckRetention()
    stop = asyncio.Event()
    running = asyncio.create_task(
        worker.run_jobs(
            (("matcher", matcher.tick), ("idle chats", idle.tick)),
            (("chat retention", retention.tick, 60.0),),
            stop,
        )
    )
    await asyncio.wait_for(retention.started.wait(), timeout=1)
    before = matcher.count
    await asyncio.sleep(0.05)
    assert matcher.count > before + 3  # still ticking while the cleanup runs
    assert idle.count > 3
    stop.set()
    await asyncio.wait_for(running, timeout=1)  # stopping does not wait for the cleanup
