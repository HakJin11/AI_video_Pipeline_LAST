import asyncio
import time
import uuid
from typing import Any, Callable

_jobs: dict[str, dict[str, Any]] = {}


def create_job() -> str:
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {"status": "queued", "result": None, "error": None, "elapsed_sec": None}
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    return _jobs.get(job_id)


async def run_job(job_id: str, fn: Callable[..., Any], *args: Any) -> None:
    _jobs[job_id]["status"] = "running"
    started = time.monotonic()
    try:
        result = await asyncio.to_thread(fn, *args)
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["result"] = result
    except Exception as exc:  # noqa: BLE001 - surface any failure to the polling client
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
    finally:
        _jobs[job_id]["elapsed_sec"] = round(time.monotonic() - started, 1)


_loop: asyncio.AbstractEventLoop | None = None


def bind_loop() -> None:
    """Remember the server's event loop (call from the startup hook) so jobs can be started from worker
    threads too -- a render that queues the lesson translations when it ends, or the resume after a
    restart. asyncio.create_task from a thread raised "no running event loop" and left cuts at 대기."""
    global _loop
    _loop = asyncio.get_running_loop()


def start_job(fn: Callable[..., Any], *args: Any) -> str:
    global _loop
    job_id = create_job()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        _loop = loop
        loop.create_task(run_job(job_id, fn, *args))
    elif _loop is not None:
        asyncio.run_coroutine_threadsafe(run_job(job_id, fn, *args), _loop)
    else:
        import threading
        threading.Thread(target=lambda: asyncio.run(run_job(job_id, fn, *args)), daemon=True).start()
    return job_id
