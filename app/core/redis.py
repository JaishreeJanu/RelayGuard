from redis.asyncio import ConnectionPool, Redis

from app.config import settings

pool = ConnectionPool.from_url(settings.redis_url, decode_responses=True)


async def get_redis() -> Redis:
    client = Redis(connection_pool=pool)
    try:
        yield client
    finally:
        await client.aclose()
