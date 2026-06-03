import logging
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

class IdempotencyService:
    def __init__(self, redis: Redis):
        self.redis = redis
        # Lock configuration: 5 minutes (300 seconds) is standard for message ingestion
        self.expiry_seconds = 300 

    async def try_acquire_lock(self, key: str) -> bool:
        """
        Attempts to atomically reserve the idempotency key in Redis.
        Returns True if the key was successfully locked (unique request).
        Returns False if the key already exists (duplicate request).
        """
        redis_key = f"idempotency:{key}"
        
        # SET key value EX 300 NX
        # NX = Only set the key if it does not already exist
        # EX = Set an explicit expiration time so Redis auto-cleans old keys
        is_unique = await self.redis.set(
            name=redis_key,
            value="processing",
            ex=self.expiry_seconds,
            nx=True
        )
        
        if not is_unique:
            logger.warning(f"Duplicate request blocked by Idempotency Key: {key}")
            return False
            
        return True