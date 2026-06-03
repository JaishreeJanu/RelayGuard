import pytest
import respx
import httpx
from httpx import Response
from arq.worker import Retry
import uuid

from app.worker import send_notification_task
from app.models.notification import Notification, NotificationStatus


# =============================================================================
# 🛠️ REUSABLE WORKER INTERFACE FIXTURE
# =============================================================================
@pytest.fixture(autouse=True)
def patch_worker_db_session(mocker, db_session):
    """
    An automatic fixture that hijacks the worker's internal AsyncSessionLocal call.
    Forces the background worker to execute inside our isolated test transaction,
    preventing side-effects from leaking out into the development database.
    """
    class MockSessionContext:
        def __init__(self, active_session):
            self.active_session = active_session
            
        async def __aenter__(self):
            return self.active_session
            
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            # Pass cleanly: do not close the parent test fixture session prematurely
            pass

    def mock_session_factory():
        return MockSessionContext(db_session)

    # Patch the engine initialization inside the worker module
    mocker.patch("app.worker.AsyncSessionLocal", mock_session_factory)


# =============================================================================
# 🧪 RESILIENCY TEST CASES
# =============================================================================

@pytest.mark.anyio
@respx.mock
async def test_worker_successful_delivery_clears_failures(redis_test_client, db_session):
    """
    GIVEN a pending database notification and residual circuit failure history
    WHEN the worker successfully dispatches the payload with an HTTP 200 OK
    THEN the DB record status must become SENT, and Redis failure history must be wiped.
    """
    # 1. Simulate a healthy downstream vendor response interface
    respx.post("http://localhost:8001/mock/send").mock(return_value=Response(200, json={"status": "ok"}))

    # 2. Pre-seed our isolated Redis client with 2 existing failures
    await redis_test_client.set("circuit:failures", 2)

    # 3. Write a mock pending record to our test transaction database
    note_id = uuid.uuid4()
    notification = Notification(
        id=note_id,
        recipient="success@relayguard.io",
        payload={"alert": "test"},
        status=NotificationStatus.PENDING,
        idempotency_key="worker-token-001"
    )
    db_session.add(notification)
    await db_session.commit()

    # 4. Act: Invoke the worker task manually
    ctx = {"redis": redis_test_client, "job_try": 1}
    await send_notification_task(ctx, str(note_id))

    # 5. Assert: Verify database status mutations
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT

    # 6. Assert: Verify Redis circuit healing sequence
    residual_failures = await redis_test_client.get("circuit:failures")
    assert residual_failures is None  # The failure counter was completely cleared!


@pytest.mark.anyio
@respx.mock
async def test_worker_failures_trip_circuit_breaker(redis_test_client, db_session):
    """
    GIVEN a failing vendor environment returning HTTP 500 errors
    WHEN multiple notifications encounter consecutive failures matching our threshold
    THEN the circuit breaker state in Redis must instantly flip to OPEN.
    """
    # 1. Simulate a crashed vendor endpoint
    respx.post("http://localhost:8001/mock/send").mock(return_value=Response(500))

    # 2. Setup 3 independent failing transactions to trigger the threshold limits
    ctx = {"redis": redis_test_client, "job_try": 1}
    
    for i in range(3):
        note_id = uuid.uuid4()
        notification = Notification(
            id=note_id,
            recipient=f"fail-{i}@relayguard.io",
            payload={"alert": "crash"},
            status=NotificationStatus.PENDING,
            idempotency_key=f"fail-token-{i}"
        )
        db_session.add(notification)
        await db_session.commit()

        # Act & Assert: Execute the loop catching the expected exceptions
        # On attempts 1 and 2, it calculates backoff and raises an ARQ Retry
        # On attempt 3, it hits the maximum failure boundary condition
        try:
            await send_notification_task(ctx, str(note_id))
        except (Retry, httpx.HTTPStatusError):
            pass

    # 3. Assert: Verify the distributed circuit state has flipped to OPEN in Redis
    circuit_state = await redis_test_client.get("circuit:state")
    assert circuit_state == "OPEN"


@pytest.mark.anyio
@respx.mock
async def test_worker_intercepts_when_circuit_is_open(redis_test_client, db_session):
    """
    GIVEN an active OPEN circuit breaker configuration in Redis
    WHEN a worker attempts to process a notification task
    THEN it must intercept execution and raise a Retry block immediately,
         WITHOUT ever initiating outbound network traffic.
    """
    # 1. Setup a network route spy (we will assert this is never called)
    route_spy = respx.post("http://localhost:8001/mock/send").mock(return_value=Response(200))

    # 2. Pre-set the global state to OPEN inside Redis memory space
    await redis_test_client.set("circuit:state", "OPEN")

    # 3. Provision a sample target record row
    note_id = uuid.uuid4()
    notification = Notification(
        id=note_id,
        recipient="blocked@relayguard.io",
        payload={"alert": "blocked"},
        status=NotificationStatus.PENDING,
        idempotency_key="blocked-token-003"
    )
    db_session.add(notification)
    await db_session.commit()

    # 4. Act & Assert: Worker must throw an immediate Retry exception
    ctx = {"redis": redis_test_client, "job_try": 1}
    
    with pytest.raises(Retry) as retry_info:
        await send_notification_task(ctx, str(note_id))
        
    # Verify the task was postponed cleanly for a safe cool-down window
    assert retry_info.value.defer_score == 15000
    
    # CRITICAL SECURITY PROOF: Verify the outbound HTTP router was never hit!
    assert route_spy.called is False