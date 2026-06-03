import os
from typing import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.arq import get_queue_pool
from app.core.redis import get_redis
from app.database import AsyncSessionLocal, engine, get_db
from app.main import app

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/1")


# =============================================================================
# 🌀 1. EVENT LOOP LIFECYCLE MANAGEMENT
# =============================================================================
@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Configures pytest-asyncio to utilize anyio's default asyncio engine."""
    return "asyncio"


# =============================================================================
# 💾 2. TRANSACTIONAL ISOLATED DATABASE FIXTURE
# =============================================================================
@pytest.fixture(scope="function")
async def db_session() -> AsyncGenerator:
    """
    Creates a highly isolated, transaction-backed SQLAlchemy session.
    Every database modification executed during a test is completely rolled back
    when the test function completes, guaranteeing a clean slate.
    """
    # Dispose pooled connections between tests to avoid cross-event-loop reuse.
    await engine.dispose()
    async with AsyncSessionLocal() as session:
        # Keep tests deterministic by clearing application rows per test.
        await session.execute(text("TRUNCATE TABLE notifications RESTART IDENTITY CASCADE"))
        await session.commit()
        yield session
        await session.rollback()


# =============================================================================
# 🔴 3. CLEAN REDIS TESTING FIXTURE
# =============================================================================
@pytest.fixture(scope="function")
async def redis_test_client() -> AsyncGenerator[Redis, None]:
    """
    Provides a Redis client pointing to an isolated testing database index.
    Flushes all tracking keys completely before a test executes.
    """
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    
    # Senior Defensive Guard: Flush the entire test keyspace before the test begins
    await client.flushdb()
    
    yield client
    
    # Clean up and release underlying connections after the test finishes
    await client.flushdb()
    await client.aclose()


# =============================================================================
# 🚀 4. FASTAPI CLIENT WITH DEPENDENCY INJECTION OVERRIDES
# =============================================================================
@pytest.fixture(scope="function")
async def client(db_session, redis_test_client) -> AsyncGenerator[AsyncClient, None]:
    """
    Yields an HTTPX AsyncClient configured to spam our live FastAPI application.
    Swaps out production database and Redis providers for our isolated testing fixtures.
    """
    # Define inner dependency override functions matching the fixture states
    async def _override_get_db():
        async with AsyncSessionLocal() as request_session:
            yield request_session

    async def _override_get_redis():
        yield redis_test_client

    mock_queue_pool = AsyncMock()
    mock_queue_pool.enqueue_job = AsyncMock()

    async def _override_get_queue_pool():
        yield mock_queue_pool

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _override_get_redis
    app.dependency_overrides[get_queue_pool] = _override_get_queue_pool

    # Initialize HTTPX client to communicate directly via ASGI in-memory transport layer
    # (This avoids overhead of parsing raw network interfaces during local testing loops)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    # Clean out our dependency overrides map once the execution loop concludes
    app.dependency_overrides.clear()