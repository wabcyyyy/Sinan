"""多个池共享停止预算，非逐池累计。未完成任务依赖持久状态恢复。"""

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class Pool(Protocol):
    def shutdown(self) -> None: ...


async def drain(pools: tuple[Pool, ...], timeout: float = 30) -> None:
    try:
        await asyncio.wait_for(asyncio.gather(*(asyncio.to_thread(pool.shutdown) for pool in pools)), timeout)
    except TimeoutError:
        logger.warning("executor drain exceeded global %.1fs budget; unfinished trips recover after restart", timeout)
