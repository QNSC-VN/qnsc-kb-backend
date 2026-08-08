import asyncio

from src.workers import loop


def test_sync_run_reuses_worker_event_loop():
    loop._worker_event_loop = None
    assert loop.sync_run(asyncio.sleep(0, result="first")) == "first"
    worker_loop = loop._worker_event_loop
    assert worker_loop is not None and not worker_loop.is_closed()
    assert loop.sync_run(asyncio.sleep(0, result="second")) == "second"
    assert loop._worker_event_loop is worker_loop
