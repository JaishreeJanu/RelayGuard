from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select
import logging

from app.core.arq import get_queue_pool
from app.database import get_db
from app.core.redis import get_redis
from app.models.notification import Notification, NotificationStatus
from app.schemas.notification import NotificationCreate, NotificationResponse
from app.services.idempotency import IdempotencyService
from sqlalchemy import func

logger = logging.getLogger("RelayGuardAPI")

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.post(
    "/notify",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=NotificationResponse,
    summary="Ingest a new transactional notification request asynchronously"
)
async def ingest_notification(
    payload_in: NotificationCreate,
    db: AsyncSession = Depends(get_db),
    redis_client=Depends(get_redis),
    queue_pool=Depends(get_queue_pool)  # Inject the queue pool
):
    # 1. Initialize the idempotency service with our injected Redis instance
    idempotency_service = IdempotencyService(redis_client)
    
    # 2. Check and acquire the atomic lock in Redis
    is_unique = await idempotency_service.try_acquire_lock(payload_in.idempotency_key)
    
    if not is_unique:
        # 🔥 SENIOR UPGRADE: Atomically increment a global edge mitigation counter in Redis
        await redis_client.incr("idempotency:blocked_count")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Duplicate request detected. A transaction with idempotency key "
                f"'{payload_in.idempotency_key}' is already being processed or completed."
            )
        )
        
    # 3. Create the Database tracking record (Source of Truth)
    new_notification = Notification(
        recipient=payload_in.recipient,
        payload=payload_in.payload,
        idempotency_key=payload_in.idempotency_key,
        status=NotificationStatus.PENDING  # Explicitly stating it's waiting for queue pick-up
    )
    
    db.add(new_notification)
    try:
        await db.commit()
        await db.refresh(new_notification)
    except IntegrityError:
        # If the Redis lock has expired but the DB key still exists, reuse the
        # existing record instead of failing the request with a 500.
        await db.rollback()
        existing = await db.execute(
            select(Notification).where(Notification.idempotency_key == payload_in.idempotency_key)
        )
        new_notification = existing.scalar_one()
    
    # 4. Hand off the task to the Redis Background Queue
    await queue_pool.enqueue_job("send_notification_task", notification_id=str(new_notification.id))
    
    # 5. Return the record tracking information back to the client immediately
    return new_notification

@router.post(
    "/requeue-backlog",
    status_code=status.HTTP_200_OK,
    summary="Reconcile and flush backlogged, failed, or dead-letter notifications"
)
async def requeue_failed_notifications(
    include_dlq: bool = Query(
        default=False, 
        description="If True, records in the Dead Letter Queue (DLQ) will also be resurrected and re-enqueued."
    ),
    db: AsyncSession = Depends(get_db),
    queue_pool=Depends(get_queue_pool)
):
    """
    Scans PostgreSQL for stuck transactions. By default, it safely targets transient 
    FAILED rows. If include_dlq is explicitly passed as True, it expands the operation 
    to clear out the Dead Letter Queue.
    """
    # 1. Dynamically build the targeted database filter list
    target_statuses = [NotificationStatus.FAILED]
    if include_dlq:
        target_statuses.append(NotificationStatus.DLQ)
        
    logger.info(f"🔍 Executing backlog reconciliation scan for statuses: {target_statuses}")

    # 2. Query PostgreSQL using the .in_() operator for clean filtering
    result = await db.execute(
        select(Notification).where(Notification.status.in_(target_statuses))
    )
    notifications_to_flush = result.scalars().all()
    
    requeued_count = 0
    
    # 3. Process the records back into the active distributed pool
    for notification in notifications_to_flush:
        previous_status = notification.status
        
        # Reset properties to grant the transaction a brand-new processing lifecycle
        notification.status = NotificationStatus.PENDING
        notification.retry_count = 0  
        
        # Fire the database text string ID back to the Redis queue workers
        await queue_pool.enqueue_job("send_notification_task", str(notification.id))
        requeued_count += 1
        
        logger.debug(f"🔄 Re-queued notification {notification.id} (Resurrected from {previous_status})")
        
    # 4. Save and persist all adjustments to PostgreSQL atomically
    if requeued_count > 0:
        await db.commit()
        logger.info(f"✅ Backlog Reconciliation Success: Flushed {requeued_count} tasks back to the ARQ workers.")
        
    return {
        "status": "success",
        "message": f"Successfully identified and re-enqueued {requeued_count} notifications.",
        "scope_applied": [str(s.value) for s in target_statuses]
    }

@router.get(
    "/status",
    status_code=status.HTTP_200_OK,
    summary="Fetch live aggregated telemetry data for the HUD dashboard"
)
async def get_system_telemetry(
    db: AsyncSession = Depends(get_db),
    redis_client = Depends(get_redis)
):
    """
    Acts as the data pipeline for the frontend dashboard. 
    Queries Redis for circuit breaker state and aggregates database records.
    """
    # 1. Fetch distributed Circuit Breaker metrics from Redis
    circuit_state = await redis_client.get("circuit:state") or "CLOSED"
    consecutive_failures = await redis_client.get("circuit:failures") or 0

    # 🔥 SENIOR UPGRADE: Fetch our edge rejection counter from Redis memory space
    blocked_count = await redis_client.get("idempotency:blocked_count") or 0

    # 2. Query PostgreSQL for aggregated notification status counts
    # Equivalent to: SELECT status, COUNT(id) FROM notifications GROUP BY status;
    stmt = select(Notification.status, func.count(Notification.id)).group_by(Notification.status)
    result = await db.execute(stmt)
    
    # Initialize a clean dictionary mapping for all valid database statuses
    metrics = {"PENDING": 0, "SENT": 0, "FAILED": 0, "DLQ": 0}
    
    # Map database row tuples dynamically into our metrics payload
    for row in result.all():
        db_status_enum = row[0]
        count = row[1]
        if db_status_enum:
            metrics[db_status_enum.value] = count

    # 3. 🔥 NEW: Fetch the 10 most recent notification rows for the streaming table
    # Since UUIDs are unordered, we select the rows directly. 
    # If your model has a created_at timestamp, use .order_by(Notification.created_at.desc())
    stmt_recent = select(Notification).limit(10)
    result_recent = await db.execute(stmt_recent)
    recent_records = result_recent.scalars().all()

    # Format database models into a clean, serializable JSON array
    serialized_notifications = [
        {
            "id": str(note.id),
            "recipient": note.recipient,
            "status": note.status.value if note.status else "UNKNOWN",
            "retry_count": note.retry_count,
            "idempotency_key": note.idempotency_key
        }
        for note in recent_records
    ]

    return {
        "circuit_breaker": {
            "state": circuit_state,
            "consecutive_failures": int(consecutive_failures)
        },
        # Pass it smoothly down the payload pipeline
        "idempotency": {
            "blocked_duplicates": int(blocked_count)
        },
        "database_metrics": metrics,
        "recent_notifications": serialized_notifications
    }