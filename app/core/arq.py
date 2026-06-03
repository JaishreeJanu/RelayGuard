from typing import Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import settings

_arq_pool: Optional[ArqRedis] = None


async def init_arq_pool() -> None:
    global _arq_pool
    _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))


async def close_arq_pool() -> None:
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.aclose()
        _arq_pool = None


async def get_queue_pool():
    if _arq_pool is None:
        raise RuntimeError("ARQ pool is not initialized")
    yield _arq_pool
