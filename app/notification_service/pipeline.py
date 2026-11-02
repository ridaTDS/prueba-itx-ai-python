"""Pipeline de procesamiento asíncrono.

``POST /process`` sólo encola la petición y responde al momento (202). El trabajo pesado ocurre en
segundo plano en dos etapas desacopladas por una cola:

1. **Extracción** (IA + guardrails): muy concurrente; la IA tarda 1,5-3 s pero no limita el ritmo.
2. **Envío** (provider): un número fijo de workers que respetan el límite de 50 envíos / 10 s.

Separarlas evita que la lentitud de la IA bloquee los envíos y que el límite del provider frene
las extracciones.
"""
from __future__ import annotations

import asyncio
import logging

from .domain import RequestStatus
from .extraction.extractor import NotificationExtractor
from .notifications.sender import NotificationSender
from .repository import RequestRepository

logger = logging.getLogger(__name__)


class NotificationPipeline:
    def __init__(self, repository: RequestRepository, extractor: NotificationExtractor,
                 sender: NotificationSender, notify_workers: int):
        self._repository = repository
        self._extractor = extractor
        self._sender = sender
        self._notify_workers = notify_workers
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        self._workers = [asyncio.create_task(self._notify_worker(i), name=f"notify-worker-{i}")
                         for i in range(self._notify_workers)]

    async def stop(self) -> None:
        for task in [*self._workers, *self._tasks]:
            task.cancel()
        await asyncio.gather(*self._workers, *self._tasks, return_exceptions=True)

    def submit(self, request_id: str) -> None:
        """Lanza el procesamiento en segundo plano (no bloquea al llamante)."""
        task = asyncio.create_task(self._extract(request_id), name=f"extract-{request_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def pending_notifications(self) -> int:
        return self._notify_queue.qsize()

    async def _extract(self, request_id: str) -> None:
        try:
            request = await self._repository.update(request_id, status=RequestStatus.PROCESSING)
            if request is None:
                return
            outcome = await self._extractor.extract(request.user_input, request_id)
            if outcome.result is None:
                reason = outcome.error.reason if outcome.error else "extraction failed"
                await self._repository.update(request_id, status=RequestStatus.FAILED,
                                              ai_attempts=outcome.attempts, error=reason)
                logger.info("request=%s status=failed stage=extraction reason=%s", request_id, reason)
                return
            await self._repository.update(request_id, notification=outcome.result.notification,
                                          ai_attempts=outcome.attempts)
            await self._notify_queue.put(request_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - un fallo inesperado no debe dejar la petición colgada
            logger.exception("request=%s unexpected error during extraction", request_id)
            await self._repository.update(request_id, status=RequestStatus.FAILED, error="internal error")

    async def _notify_worker(self, worker_id: int) -> None:
        while True:
            request_id = await self._notify_queue.get()
            try:
                await self._deliver(request_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("request=%s unexpected error during delivery", request_id)
                await self._repository.update(request_id, status=RequestStatus.FAILED, error="internal error")
            finally:
                self._notify_queue.task_done()

    async def _deliver(self, request_id: str) -> None:
        request = await self._repository.get(request_id)
        if request is None or request.notification is None:
            return
        result = await self._sender.send(request.notification, request_id)
        if result.delivered:
            await self._repository.update(request_id, status=RequestStatus.SENT,
                                          notify_attempts=result.attempts, provider_id=result.provider_id)
            logger.info("request=%s status=sent provider_id=%s", request_id, result.provider_id)
        else:
            await self._repository.update(request_id, status=RequestStatus.FAILED,
                                          notify_attempts=result.attempts, error=result.error)
            logger.info("request=%s status=failed stage=delivery reason=%s", request_id, result.error)
