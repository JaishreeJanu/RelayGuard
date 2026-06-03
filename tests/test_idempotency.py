import pytest
from fastapi import status
from unittest.mock import AsyncMock

from app.main import app
from app.core.arq import get_queue_pool
from app.models.notification import NotificationStatus


@pytest.mark.anyio
async def test_idempotency_blocks_duplicate_requests(client, redis_test_client):
    """
    GIVEN an active, unique notification ingestion payload
    WHEN the client submits the exact same request twice in back-to-back succession
    THEN the first request should yield HTTP 202, and the second request must be 
         short-circuited by Redis and yield an HTTP 409 Conflict error.
    """
    # 1. Setup our background task queue mock
    mock_queue_pool = AsyncMock()
    mock_queue_pool.enqueue_job = AsyncMock()
    
    async def override_queue_pool():
        yield mock_queue_pool
        
    app.dependency_overrides[get_queue_pool] = override_queue_pool

    # 2. Arrange a sample request payload with a static token
    shared_key = "idempotency-test-token-999"
    payload = {
        "recipient": "recipient@relayguard.io",
        "payload": {"body": "Testing idempotency limits"},
        "idempotency_key": shared_key
    }

    # 3. Act - Run Request #1 (The Unique Request)
    first_response = await client.post("/api/v1/notifications/notify", json=payload)
    
    # Assert Request #1 passed perimeter defenses cleanly
    assert first_response.status_code == status.HTTP_202_ACCEPTED
    assert first_response.json()["status"] == NotificationStatus.PENDING.value

    # 4. Act - Run Request #2 (The Duplicate Twin Request)
    second_response = await client.post("/api/v1/notifications/notify", json=payload)

    # Assert Request #2 was completely stopped by our Redis layer
    assert second_response.status_code == status.HTTP_409_CONFLICT
    assert "Duplicate request detected" in second_response.json()["detail"]

    # 5. Assert Structural Efficiency: Ensure the worker queue was ONLY called once!
    # If it was called twice, our architectural isolation failed.
    assert mock_queue_pool.enqueue_job.call_count == 1


@pytest.mark.anyio
async def test_idempotency_expires_after_timeout(client, redis_test_client, mocker):
    """
    GIVEN a successful unique request that was locked in Redis
    WHEN the key's TTL duration is surpassed or simulated to expire
    THEN a new request using the exact same key should be treated as unique again.
    """
    mock_queue_pool = AsyncMock()
    async def override_queue_pool():
        yield mock_queue_pool
    app.dependency_overrides[get_queue_pool] = override_queue_pool

    shared_key = "temporary-token-888"
    payload = {
        "recipient": "user@relayguard.io",
        "payload": {"body": "Testing expiration limits"},
        "idempotency_key": shared_key
    }

    # Submit request #1 to populate the lock in Redis
    first_resp = await client.post("/api/v1/notifications/notify", json=payload)
    assert first_resp.status_code == status.HTTP_202_ACCEPTED

    # Simulate time passing by manually removing the key from our isolated Redis test instance
    # This imitates what happens automatically when the 5-minute TTL expires
    await redis_test_client.delete(f"idempotency:{shared_key}")

    # Submit request #2 with the exact same payload parameters
    second_resp = await client.post("/api/v1/notifications/notify", json=payload)

    # It should pass cleanly instead of returning a 409!
    assert second_resp.status_code == status.HTTP_202_ACCEPTED
    assert mock_queue_pool.enqueue_job.call_count == 2