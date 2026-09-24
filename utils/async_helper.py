"""
Async utilities — thread pool, task pool, sync-to-async bridge.
"""
import asyncio
import concurrent.futures
from typing import Coroutine, List, Any, Callable
from config.settings import ASYNC_POOL_SIZE
import re

# Global thread pool for sync-to-async offloading
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=ASYNC_POOL_SIZE)


class AsyncTaskPool:
    """Semaphore-based async task pool with queuing."""

    def __init__(self, max_concurrent: int = None):
        self.sem = asyncio.Semaphore(max_concurrent or ASYNC_POOL_SIZE)
        self._active = 0
        self._done = 0
        self._errors = 0

    async def _wrap(self, coro: Coroutine):
        async with self.sem:
            self._active += 1
            try:
                result = await coro
                self._done += 1
                return result
            except Exception:
                self._errors += 1
                raise
            finally:
                self._active -= 1

    async def batch_run(self, coro_list: List[Coroutine]) -> List[Any]:
        tasks = [self._wrap(c) for c in coro_list]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def run_single(self, coro: Coroutine) -> Any:
        return await self._wrap(coro)

    @property
    def stats(self) -> dict:
        return {"active": self._active, "done": self._done, "errors": self._errors}


async def run_in_thread(fn: Callable, *args, **kwargs) -> Any:
    """Run sync function in thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_thread_pool, lambda: fn(*args, **kwargs))


def shutdown_thread_pool():
    _thread_pool.shutdown(wait=True)
