"""Modelo de dominio: la petición del usuario, su ciclo de vida y la notificación extraída."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class RequestStatus(str, Enum):
    QUEUED = "queued"          # recibida, pendiente de procesar
    PROCESSING = "processing"  # extrayendo con IA o esperando turno en el provider
    SENT = "sent"              # notificación entregada
    FAILED = "failed"          # no se ha podido extraer o enviar

    @property
    def is_final(self) -> bool:
        return self in (RequestStatus.SENT, RequestStatus.FAILED)


class Notification(BaseModel):
    """Contrato del provider de notificaciones."""

    to: str = Field(min_length=1)
    message: str = Field(min_length=1)
    type: Literal["email", "sms"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NotificationRequest:
    user_input: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: RequestStatus = RequestStatus.QUEUED
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    process_requested: bool = False
    notification: Notification | None = None
    ai_attempts: int = 0
    notify_attempts: int = 0
    provider_id: str | None = None
    error: str | None = None
