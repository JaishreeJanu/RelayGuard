from datetime import datetime
from typing import Any, Dict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.notification import NotificationStatus


class NotificationCreate(BaseModel):
    """
    Schema representing the strict payload expected from an upstream client 
    during a notification ingestion request (POST /api/v1/notify).
    """
    recipient: str = Field(
        ..., 
        description="The destination address (e.g., email, phone number, or webhook URL).",
        min_length=1, 
        max_length=512,
        examples=["user@example.com"]
    )
    payload: Dict[str, Any] = Field(
        ..., 
        description="Arbitrary JSON payload containing template variables or raw message context.",
        examples=[{"template_id": "welcome_email", "user_name": "Alex"}]
    )
    idempotency_key: str = Field(
        ..., 
        description="A unique client-generated UUID or token used to guarantee request safety.",
        min_length=1, 
        max_length=255,
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"]
    )


class NotificationResponse(BaseModel):
    """
    Schema representing the complete notification entity returned to clients
    when polling for delivery statuses (GET /api/v1/notify/{id}).
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    recipient: str
    payload: Dict[str, Any]
    status: NotificationStatus
    retry_count: int
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
