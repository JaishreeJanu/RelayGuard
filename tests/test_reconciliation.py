import pytest
from fastapi import status
from unittest.mock import AsyncMock
from sqlalchemy.future import select
import uuid

from app.main import app
from app.core.arq import get_queue_pool
from app.models.notification import Notification, NotificationStatus


@pytest.fixture(scope="function")
async def seed_reconciliation_data(db_session):
    """
    Fixture to pre-seed our isolated test database transaction with a controlled
    backlog: 2 records marked as FAILED and 1 record marked as DLQ.
    """
    failed_note_1 = Notification(
        id=uuid.uuid4(),
        recipient="failed1@relayguard.io",
        payload={"alert": "retry1"},
        status=NotificationStatus.FAILED,
        retry_count=2,
        idempotency_key="reconcile-token-001"
    )
    failed_note_2 = Notification(
        id=uuid.uuid4(),
        recipient="failed2@relayguard.io",
        payload={"alert": "retry2"},
        status=NotificationStatus.FAILED,
        retry_count=1,
        idempotency_key="reconcile-token-002"
    )
    dlq_note = Notification(
        id=uuid.uuid4(),
        recipient="poisonpill@relayguard.io",
        payload={"alert": "dead"},
        status=NotificationStatus.DLQ,
        retry_count=3,
        idempotency_key="reconcile-token-003"
    )

    db_session.add_all([failed_note_1, failed_note_2, dlq_note])
    await db_session.commit()
    
    # Return the explicit tracking records to the test methods for validation
    return {
        "failed_ids": [str(failed_note_1.id), str(failed_note_2.id)],
        "dlq_id": str(dlq_note.id)
    }


@pytest.mark.anyio
async def test_rerequeue_backlog_default_behavior(client, db_session, seed_reconciliation_data):
    """
    GIVEN a database containing both FAILED and DLQ notifications
    WHEN hitting POST /api/v1/notifications/requeue-backlog without query parameters
    THEN it must only re-enqueue the FAILED records, resetting their retry metrics,
         while leaving the DLQ record completely untouched.
    """
    # 1. Setup our background queue pool spy
    mock_queue_pool = AsyncMock()
    mock_queue_pool.enqueue_job = AsyncMock()
    
    async def override_queue_pool():
        yield mock_queue_pool
    app.dependency_overrides[get_queue_pool] = override_queue_pool

    # 2. Act: Trigger the reconciliation route with default parameters (include_dlq=False)
    response = await client.post("/api/v1/notifications/requeue-backlog")

    # 3. Assert: API Response verification
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "success"
    assert response.json()["message"] == "Successfully identified and re-enqueued 2 notifications."
    assert "FAILED" in response.json()["scope_applied"]
    assert "DLQ" not in response.json()["scope_applied"]

    # 4. Assert: Verify Queue Side-Effects
    # Enqueue should have been triggered exactly twice (once for each failed record)
    assert mock_queue_pool.enqueue_job.call_count == 2
    
    # 5. Assert: Verify PostgreSQL State Mutations
    # Fetch all records to check state transitions
    result = await db_session.execute(select(Notification))
    all_records = result.scalars().all()
    
    for record in all_records:
        if str(record.id) in seed_reconciliation_data["failed_ids"]:
            # Failed records must be reset back to PENDING with a clean retry count budget
            assert record.status == NotificationStatus.PENDING
            assert record.retry_count == 0
        elif str(record.id) == seed_reconciliation_data["dlq_id"]:
            # DLQ records must remain untouched to prevent poison-pill loops
            assert record.status == NotificationStatus.DLQ
            assert record.retry_count == 3


@pytest.mark.anyio
async def test_rerequeue_backlog_including_dlq(client, db_session, seed_reconciliation_data):
    """
    GIVEN a database containing both FAILED and DLQ notifications
    WHEN hitting POST /api/v1/notifications/requeue-backlog with include_dlq=true
    THEN it must expand its filter boundaries, converting ALL backlogged records 
         to PENDING and dropping all 3 items back into the execution queue.
    """
    mock_queue_pool = AsyncMock()
    mock_queue_pool.enqueue_job = AsyncMock()
    async def override_queue_pool():
        yield mock_queue_pool
    app.dependency_overrides[get_queue_pool] = override_queue_pool

    # Act: Trigger the reconciliation route explicitly forcing DLQ resurrection
    response = await client.post("/api/v1/notifications/requeue-backlog?include_dlq=true")

    # Assert: Verify response summary counts
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["message"] == "Successfully identified and re-enqueued 3 notifications."
    assert "DLQ" in response.json()["scope_applied"]

    # Assert: All 3 records successfully hit the queue layer
    assert mock_queue_pool.enqueue_job.call_count == 3

    # Assert: Every single record in the database should now be reset to PENDING
    result = await db_session.execute(select(Notification))
    all_records = result.scalars().all()
    
    for record in all_records:
        assert record.status == NotificationStatus.PENDING
        assert record.retry_count == 0