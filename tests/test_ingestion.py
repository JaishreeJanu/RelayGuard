import pytest
from fastapi import status
from unittest.mock import AsyncMock
from sqlalchemy.future import select

from app.main import app
from app.core.arq import get_queue_pool
from app.models.notification import Notification, NotificationStatus


@pytest.mark.anyio
async def test_ingestion_success(client, db_session, mocker):
    """
    GIVEN a perfectly valid notification payload
    WHEN hit the POST /api/v1/notifications/notify endpoint
    THEN it must return HTTP 202, record a PENDING row in Postgres,
    and accurately enqueue a background task token in the queue pool.
    """
    # 1. Setup a mock queue pool to intercept background task routing
    mock_queue_pool = AsyncMock()
    mock_queue_pool.enqueue_job = AsyncMock()
    
    # Overriding the queue dependency dynamically just for this execution context
    async def override_queue_pool():
        yield mock_queue_pool
        
    app.dependency_overrides[get_queue_pool] = override_queue_pool

    # 2. Arrange our valid input request parameters
    valid_payload = {
        "recipient": "customer@relayguard.de",
        "payload": {"template_id": "welcome_email", "name": "Janu"},
        "idempotency_key": "test-token-uuid-001"
    }

    # 3. Act: Send the outbound transaction using the client fixture from conftest
    response = await client.post("/api/v1/notifications/notify", json=valid_payload)

    # 4. Assert network response specifications
    assert response.status_code == status.HTTP_202_ACCEPTED
    response_json = response.json()
    assert response_json["recipient"] == "customer@relayguard.de"
    assert response_json["status"] == NotificationStatus.PENDING.value
    assert "id" in response_json

    # 5. Assert Database Persistence: Did it write to the isolated Postgres instance?
    result = await db_session.execute(
        select(Notification).where(Notification.idempotency_key == "test-token-uuid-001")
    )
    db_record = result.scalar_one_or_none()
    assert db_record is not None
    assert db_record.recipient == "customer@relayguard.de"
    assert db_record.status == NotificationStatus.PENDING

    # 6. Assert Queue Interaction: Was the correct database text UUID string passed to ARQ?
    mock_queue_pool.enqueue_job.assert_called_once_with(
        "send_notification_task", notification_id=str(db_record.id)
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_payload, missing_field_name",
    [
        # Scenario A: Missing recipient parameter
        ({"payload": {"msg": "hi"}, "idempotency_key": "key-1"}, "recipient"),
        # Scenario B: Missing payload parameter
        ({"recipient": "test@test.com", "idempotency_key": "key-2"}, "payload"),
        # Scenario C: Missing idempotency_key parameter
        ({"recipient": "test@test.com", "payload": {"msg": "hi"}}, "idempotency_key"),
    ]
)
async def test_ingestion_validation_missing_fields(client, invalid_payload, missing_field_name):
    """
    GIVEN a notification request payload missing required parameters
    WHEN hit the ingestion route
    THEN Pydantic must block execution immediately at the perimeter with an HTTP 422.
    """
    response = await client.post("/api/v1/notifications/notify", json=invalid_payload)
    
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    # Verify the error validation explicitly names the field causing the rejection
    assert missing_field_name in response.text


@pytest.mark.anyio
async def test_ingestion_validation_overflow_limits(client):
    """
    GIVEN a request where a field exceeds our strict schema string length parameters
    WHEN hit the ingestion route
    THEN it must be intercepted before harming the database layer, returning an HTTP 422.
    """
    malicious_payload = {
        "recipient": "a" * 513,  # Exceeds max_length=512 limit defined on our schema field
        "payload": {"data": "test"},
        "idempotency_key": "token-xyz"
    }
    
    response = await client.post("/api/v1/notifications/notify", json=malicious_payload)
    
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY