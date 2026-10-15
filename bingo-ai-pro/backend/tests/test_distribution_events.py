from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from services import distribution_events as events


class Connected:
    async def is_disconnected(self):
        return False


def test_stream_notifies_after_publish_without_external_reads():
    async def check():
        stream = events.draw_events(Connected())
        assert "event: ready" in await anext(stream)
        events.publish_draw("115058465")
        message = await anext(stream)
        assert "event: draw" in message
        assert '"issue": "115058465"' in message
        await stream.aclose()
    asyncio.run(check())


def test_disconnected_client_exits():
    class Gone:
        async def is_disconnected(self):
            return True
    async def check():
        stream = events.draw_events(Gone())
        await anext(stream)
        try:
            await anext(stream)
        except StopAsyncIteration:
            return
        raise AssertionError("disconnected stream remained active")
    asyncio.run(check())
