"""Integración con el provider REAL del reto (provider/app.py), servido en memoria con ASGI.

Se ejecuta el código del mock tal cual (latencias de 1,5-3 s, respuestas ruidosas, rate limit), sin
Docker: sólo se desactiva el envío de métricas a InfluxDB.
"""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.integration

provider = pytest.importorskip("app", reason="provider mock not importable")


@pytest.fixture
async def service(monkeypatch):
    from notification_service.config import Settings
    from notification_service.container import Services, configure, lifespan_for
    from notification_service.extraction.extractor import AIClient, NotificationExtractor
    from notification_service.notifications.rate_limiter import PacedRateLimiter
    from notification_service.notifications.sender import NotificationSender
    from notification_service.pipeline import NotificationPipeline
    from notification_service.repository import InMemoryRequestRepository

    monkeypatch.setattr(provider.influx_client, "write_points", lambda *args, **kwargs: None)
    settings = Settings(provider_base_url="http://provider", ai_retry_base_delay=0)
    provider_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=provider.app),
                                      base_url=settings.provider_base_url, timeout=10)

    def factory():
        repository = InMemoryRequestRepository()
        extractor = NotificationExtractor(AIClient(provider_http, settings), settings)
        sender = NotificationSender(provider_http, settings, PacedRateLimiter(40, 10))
        pipeline = NotificationPipeline(repository, extractor, sender, notify_workers=4)
        return Services(repository=repository, pipeline=pipeline, http_clients=(provider_http,))

    app = configure(FastAPI(lifespan=lifespan_for(factory)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://service") as client:
            yield client


async def run(client, user_input, timeout=25):
    request_id = (await client.post("/v1/requests", json={"user_input": user_input})).json()["id"]
    assert (await client.post(f"/v1/requests/{request_id}/process")).status_code == 202
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status = (await client.get(f"/v1/requests/{request_id}")).json()["status"]
        if status in ("sent", "failed"):
            return status
        await asyncio.sleep(0.2)
    return status


async def test_requests_from_the_load_test_are_delivered(service):
    inputs = [
        "Enviar email a juan@example.com diciendo que su paquete está en camino.",
        "Avisar por SMS al 600-111-222 que la reserva ha sido confirmada.",
        "Manda un correo a marta@app.com: Tienes un nuevo mensaje.",
        "Recordatorio por teléfono al 699888777: cita a las 10:00",
    ] * 3

    statuses = await asyncio.gather(*(run(service, user_input) for user_input in inputs))

    # La IA rechaza ~10 % de las veces; con 3 intentos la probabilidad de fallar es ~0,1 %
    assert statuses.count("sent") >= len(inputs) - 1


async def test_request_without_recipient_fails(service):
    assert await run(service, "Avisa a Pedro de que llego tarde") == "failed"
