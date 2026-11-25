import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.services.angel_one import AngelOneSession


@pytest.mark.asyncio
async def test_smartapi_sdk_calls_are_serialized():
    session = AngelOneSession()
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def slow_sdk_call(value):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with state_lock:
            active -= 1
        return value

    session._obj = SimpleNamespace(slow_sdk_call=slow_sdk_call)

    results = await asyncio.gather(
        *(
            session._run_sdk_call("slow_sdk_call", value)
            for value in range(8)
        )
    )

    assert sorted(results) == list(range(8))
    assert max_active == 1
