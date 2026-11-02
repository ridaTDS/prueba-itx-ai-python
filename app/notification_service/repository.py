"""Persistencia de las peticiones.

Se usa un repositorio en memoria detrás de una interfaz: el pipeline no depende del almacenamiento,
así que se puede sustituir por Redis o una base de datos sin tocar el resto del código.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Protocol

from .domain import NotificationRequest, RequestStatus


class RequestRepository(Protocol):
    async def add(self, request: NotificationRequest) -> None: ...

    async def get(self, request_id: str) -> NotificationRequest | None: ...

    async def mark_process_requested(self, request_id: str) -> tuple[NotificationRequest | None, bool]: ...

    async def update(self, request_id: str, **changes) -> NotificationRequest | None: ...


class InMemoryRequestRepository:
    def __init__(self) -> None:
        self._items: dict[str, NotificationRequest] = {}
        self._lock = asyncio.Lock()

    async def add(self, request: NotificationRequest) -> None:
        async with self._lock:
            self._items[request.id] = request

    async def get(self, request_id: str) -> NotificationRequest | None:
        return self._items.get(request_id)

    async def mark_process_requested(self, request_id: str) -> tuple[NotificationRequest | None, bool]:
        """Marca la petición para procesar y devuelve ``(petición, es_la_primera_vez)``.

        Es atómico: sólo la primera llamada la marca, lo que hace que ``POST /process`` sea
        idempotente aunque el cliente lo repita o lo llame en paralelo.
        """
        async with self._lock:
            request = self._items.get(request_id)
            if request is None:
                return None, False
            if request.process_requested:
                return request, False
            request.process_requested = True
            request.updated_at = datetime.now(timezone.utc)
            return request, True

    async def update(self, request_id: str, **changes) -> NotificationRequest | None:
        async with self._lock:
            request = self._items.get(request_id)
            if request is None:
                return None
            if request.status.is_final and "status" in changes:
                # Un estado final no vuelve atrás
                changes.pop("status")
            for key, value in changes.items():
                setattr(request, key, value)
            request.updated_at = datetime.now(timezone.utc)
            return request

    async def stats(self) -> dict:
        """Métricas agregadas para observabilidad (/health)."""
        counts = {status.value: 0 for status in RequestStatus}
        ai_calls = processed = notify_calls = 0
        for request in list(self._items.values()):
            counts[request.status.value] += 1
            if request.ai_attempts:
                processed += 1
                ai_calls += request.ai_attempts
            notify_calls += request.notify_attempts
        return {
            "requests_by_status": counts,
            "ai_calls": ai_calls,
            "ai_calls_per_processed_request": round(ai_calls / processed, 3) if processed else None,
            "notify_calls": notify_calls,
        }
