"""Cliente del provider de notificaciones con limitación de ritmo y reintentos."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

from ..config import Settings
from ..domain import Notification
from .rate_limiter import PacedRateLimiter

logger = logging.getLogger(__name__)

NOTIFY_PATH = "/v1/notify"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class DeliveryResult:
    delivered: bool
    attempts: int
    provider_id: str | None = None
    error: str | None = None


class NotificationSender:
    """Envía notificaciones respetando el límite del provider.

    * El rate limiter local evita superar las 50 peticiones / 10 s del provider.
    * 429: se pausa el limitador (frena a todos los workers) y se reintenta.
    * 5xx, timeouts y errores de red: backoff exponencial con jitter.
    * Otros 4xx (p. ej. 422): error definitivo, reintentar no lo arregla.
    * ``trace_id`` = id de la petición, para trazabilidad e idempotencia en el provider.
    """

    def __init__(self, http: httpx.AsyncClient, settings: Settings, rate_limiter: PacedRateLimiter):
        self._http = http
        self._settings = settings
        self._rate_limiter = rate_limiter

    async def send(self, notification: Notification, request_id: str) -> DeliveryResult:
        last_error = "unknown error"
        for attempt in range(1, self._settings.notify_max_attempts + 1):
            await self._rate_limiter.acquire()
            try:
                response = await self._http.post(
                    NOTIFY_PATH,
                    json=notification.model_dump(),
                    params={"trace_id": request_id},
                    headers={"X-API-Key": self._settings.provider_api_key},
                )
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc!r}"
                logger.warning("request=%s notify_attempt=%d %s", request_id, attempt, last_error)
                await asyncio.sleep(self._delay(attempt))
                continue

            if response.status_code == 200:
                provider_id = self._provider_id(response)
                return DeliveryResult(delivered=True, attempts=attempt, provider_id=provider_id)

            last_error = f"provider returned HTTP {response.status_code}"
            if response.status_code not in RETRYABLE_STATUS:
                logger.error("request=%s notify_attempt=%d permanent failure: %s", request_id, attempt, last_error)
                return DeliveryResult(delivered=False, attempts=attempt, error=last_error)

            delay = self._delay(attempt)
            if response.status_code == 429:
                self._rate_limiter.pause(delay)
            logger.warning("request=%s notify_attempt=%d %s, retrying in %.2fs", request_id, attempt, last_error, delay)
            await asyncio.sleep(delay)

        return DeliveryResult(delivered=False, attempts=self._settings.notify_max_attempts, error=last_error)

    def _delay(self, attempt: int) -> float:
        base = self._settings.notify_retry_base_delay * (2 ** (attempt - 1))
        return min(self._settings.notify_retry_max_delay, base + random.uniform(0, base))

    @staticmethod
    def _provider_id(response: httpx.Response) -> str | None:
        try:
            return response.json().get("provider_id")
        except ValueError:
            return None
