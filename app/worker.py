import logging
import random
from uuid import UUID

import httpx
from arq.connections import RedisSettings
from arq.worker import Retry
from sqlalchemy.future import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.notification import Notification, NotificationStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
FAILURE_THRESHOLD = 3
RECOVERY_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3  # Maximum allowed application deliveries

async def send_notification_task(ctx: dict, notification_id: str) -> None:
    """
    Background worker task that fetches the absolute fresh state of a notification from PostgreSQL before
    performing any action.
    """
    # 1. Track the exact attempt count provided automatically by ARQ's state context
    current_attempt = ctx.get("job_try", 1)
    logger.info(f"Worker received task. Fetching DB record for ID: {notification_id}")
    redis_client = ctx["redis"]

    circuit_state = await redis_client.get("circuit:state") or "CLOSED"
    
    if circuit_state == "OPEN":
        logger.warning(
            f"CIRCUIT BREAKER IS OPEN! Skipping network call for ID: {notification_id}. "
            f"Postponing execution to protect resources."
        )
        # By throwing an exception here, ARQ knows the task wasn't successfully completed.
        # It will leave the database entry alone and retry the message later.
        # raise RuntimeError("Outbound traffic paused due to open circuit breaker.")

        # Calculate a fixed defer window while circuit is tripped
        raise Retry(defer=15)

    # 1. Open an isolated, independent AsyncSession with PostgreSQL
    async with AsyncSessionLocal() as db_session:
        try:
            # Convert the incoming string ID safely back into a native UUID object
            uuid_obj = UUID(notification_id)
        except ValueError:
            logger.error(f"Invalid UUID format passed to worker: '{notification_id}'. Aborting task.")
            return

        # 2. Execute a strict SQLAlchemy 2.0 async query
        result = await db_session.execute(
            select(Notification).where(Notification.id == uuid_obj)
        )
        notification = result.scalar_one_or_none()
        
        # 3. Defensive Edge-Case Guard: What if the database record doesn't exist?
        if not notification:
            logger.critical(f"Database Integrity Error: Notification ID {notification_id} not found in PostgreSQL!")
            # We return cleanly. Returning allows ARQ to mark the job as finished, 
            # instead of throwing an error and retrying a ghost record forever.
            return
            
        # 4. Idempotency Edge-Case Guard: What if this job was already completed?
        # This protects against accidental message re-queuing or race conditions.
        if notification.status in [NotificationStatus.SENT, NotificationStatus.DLQ]:
            logger.warning(f"De-duplication trigger: Notification {notification_id} already marked as {notification.status}. Skipping processing.")
            return

        # --- Sub-Step 3.1 Complete ---
        logger.info(f"Record verified. Ready to dispatch message to: {notification.recipient}")
        
        # Synchronize our database record retry counter with ARQ's tracking state
        notification.retry_count = current_attempt - 1

        # (HTTP Client setup)
        # ASYNC HTTP CLIENT EXECUTION ---
        logger.info(
            f"Dispatching outbound HTTP request to mock vendor: {settings.mock_vendor_url}"
        )
        
        # We wrap the client invocation in an async context manager to guarantee 
        # that underlying underlying TCP sockets are cleaned up cleanly after execution.
        async with httpx.AsyncClient() as client:
            request_body = {
                "recipient": notification.recipient,
                "payload": notification.payload
            }
            
            # Safeguard: Explicitly configure a connection/read timeout.
            # Leaving this at default or infinite means an external vendor outage can
            # freeze your background worker instances permanently, causing a backup queue backup.
            try:
                response = await client.post(
                    settings.mock_vendor_url,
                        json=request_body,
                        timeout=5.0  # Safe boundary: give up after 5 seconds
                    )
                # If the vendor successfully responds with a 200 OK
                if response.status_code == 200:
                    notification.status = NotificationStatus.SENT
                    await db_session.commit()
                    
                    # =========================================================================
                    # 🍏 PHASE B: CIRCUIT HEALING ON SUCCESS
                    # =========================================================================
                    # If the request succeeded, clear any residual failure counters in Redis.
                    # This ensures a healthy circuit stays completely clean.
                    await redis_client.delete("circuit:failures")
                    logger.info(f"✅ Notification {notification_id} dispatched successfully.")
                    
                else:
                    # Treat non-200 HTTP codes (like 500) as an explicit network failure
                    raise httpx.HTTPStatusError("Vendor Error", request=response.request, response=response)
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                logger.error(f"⚠️ Delivery failed on attempt {current_attempt}. Error: {exc}")
                await db_session.rollback() # Rollback standard payload mutation steps to clear blockages
                
                # =========================================================================
                # PHASE C: INCREMENTING FAILURES & TRIPPING THE CIRCUIT
                # =========================================================================
                # Atomically increment the consecutive failure counter in Redis
                consecutive_failures = await redis_client.incr("circuit:failures")
                
                logger.warning(f"Consecutive vendor failures tracked in Redis: {consecutive_failures}/{FAILURE_THRESHOLD}")
                
                if consecutive_failures >= FAILURE_THRESHOLD:
                    # Trip the breaker into an OPEN state, enforcing an expiration timer
                    await redis_client.set(
                        name="circuit:state",
                        value="OPEN",
                        ex=RECOVERY_TIMEOUT_SECONDS  # Automatically deletes itself after 60s
                    )
                    logger.critical(
                        f"CRITICAL: Circuit breaker has TRIPPED to OPEN for the next "
                        f"{RECOVERY_TIMEOUT_SECONDS} seconds! All network traffic blocked."
                    )
                
                # =========================================================================
                #  SUB-STEP 3.4: EXPONENTIAL BACKOFF RETRY LOGIC WITH JITTER RETRY
                #  HANDLING & FAILURE STATE UPGRADES (Sub-Step 3.4 & 3.5)
                # =========================================================================
                if current_attempt >= MAX_RETRIES:
                    # We have completely run out of attempts. Move permanently to Dead Letter Queue (DLQ)
                    notification.status = NotificationStatus.DLQ
                    await db_session.commit()
                    logger.error(f"💀 Max retries ({MAX_RETRIES}) exhausted. ID {notification_id} moved to DLQ.")
                    return
                else:
                    # Update database to signal it temporarily failed but is pending a retry sequence
                    notification.status = NotificationStatus.FAILED
                    await db_session.commit()
                    
                    # Calculate Backoff: delay = base * (2^attempt)
                    # Attempt 1 failed -> delay = 2 * (2^1) = 4 seconds
                    # Attempt 2 failed -> delay = 2 * (2^2) = 8 seconds
                    base_delay = 2 * (2 ** current_attempt)
                    
                    # Add Jitter: Introduce +/- 20% random variance to break up synchronized queues
                    jitter = random.uniform(-0.2 * base_delay, 0.2 * base_delay)
                    final_defer_seconds = max(1, int(base_delay + jitter))
                    
                    logger.warning(f"Scheduling retry for ID {notification_id} in {final_defer_seconds}s (Backoff + Jitter)...")
                    
                    # Instruct ARQ to pause this specific job and wake it back up after our delay
                    raise Retry(defer=final_defer_seconds)


async def startup(ctx: dict) -> None:
    """Executes when the background process launches."""
    logger.info("⚡ RelayGuard Background Worker initialized and listening to Redis queue...")


async def shutdown(ctx: dict) -> None:
    """Executes if the process is terminated, allowing running tasks to finish safely."""
    logger.info("🛑 RelayGuard Background Worker gracefully shutting down.")


class WorkerSettings:
    """
    Configuration class that the 'arq' CLI automatically detects 
    to spin up the background architecture.
    """
    # Register all background functions the worker is allowed to run
    functions = [send_notification_task]
    
    # Point to the exact Redis instance
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    
    # Lifecycle hooks
    on_startup = startup
    on_shutdown = shutdown