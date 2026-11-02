"""Composición de dependencias y ciclo de vida (arranque/parada de clientes HTTP y workers)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router
from .config import Settings
from .extraction.extractor import AIClient, NotificationExtractor
from .notifications.rate_limiter import PacedRateLimiter
from .notifications.sender import NotificationSender
from .pipeline import NotificationPipeline
from .repository import InMemoryRequestRepository

logger = logging.getLogger(__name__)


@dataclass
class Services:
    repository: InMemoryRequestRepository
    pipeline: NotificationPipeline
    http_clients: tuple[httpx.AsyncClient, ...] = ()


def _http_client(settings: Settings, pool_size: int) -> httpx.AsyncClient:
    # Pool del tamaño de la concurrencia real: un pool sobredimensionado consume mucha CPU en httpcore
    return httpx.AsyncClient(
        base_url=settings.provider_base_url,
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        limits=httpx.Limits(max_connections=pool_size, max_keepalive_connections=pool_size),
    )


def build_services(settings: Settings) -> Services:
    # Bulkhead: la IA y las notificaciones usan clientes (y pools) separados
    ai_http = _http_client(settings, settings.ai_max_concurrency)
    notify_http = _http_client(settings, settings.notify_workers)

    repository = InMemoryRequestRepository()
    extractor = NotificationExtractor(AIClient(ai_http, settings), settings)
    rate_limiter = PacedRateLimiter(settings.notify_rate_limit, settings.notify_rate_period_seconds)
    sender = NotificationSender(notify_http, settings, rate_limiter)
    pipeline = NotificationPipeline(repository, extractor, sender, settings.notify_workers)
    return Services(repository=repository, pipeline=pipeline, http_clients=(ai_http, notify_http))


def lifespan_for(services_factory):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services: Services = services_factory()
        app.state.services = services
        await services.pipeline.start()
        logger.info("Notification service started")
        try:
            yield
        finally:
            await services.pipeline.stop()
            for client in services.http_clients:
                await client.aclose()
            logger.info("Notification service stopped")

    return lifespan


def configure(app: FastAPI) -> FastAPI:
    app.include_router(router)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app
