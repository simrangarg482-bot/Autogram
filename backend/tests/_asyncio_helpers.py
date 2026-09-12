"""
Run a coroutine in a brand-new, isolated event loop.

The suite has a handful of `asyncio`-driven tests and no `pytest-asyncio`
markers; run in the wrong order (after another test that touched the loop
policy or left a loop as "current"), a bare `asyncio.run()` can raise
"Event loop is closed" or reuse stale state. This helper sidesteps that: a
fresh loop per call, set as current for the duration (so libraries that call
`asyncio.get_event_loop()` — e.g. fakeredis.aioredis — bind to it), closed
after.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

_T = TypeVar("_T")


def run_async(make_coro: Callable[[], Awaitable[_T]]) -> _T:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(make_coro())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
