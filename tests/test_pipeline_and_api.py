"""API + pipeline completos con dobles de la IA y del provider (sin red)."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from notification_service.config import Settings
from notification_service.container import Services, configure, lifespan_for
from notification_service.domain import RequestStatus
from notification_service.extraction.extractor import NotificationExtractor
from notification_service.notifications.sender import DeliveryResult
from notification_service.pipeline import NotificationPipeline
from notification_service.repository import InMemoryRequestRepository

VALID = '{"to": "marta@app.com", "message": "hola", "type": "email"}'


class ScriptedAI:
    """Devuelve respuestas según la petición del usuario."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        await asyncio.sleep(0)
        user_input = messages[-1]["content"]
        for key, answer in self.answers.items():
            if key in user_input:
                return answer.pop(0) if isinstance(answer, list) else answer
        return "Refused: no script"


class FakeSender:
    def __init__(self, delivered=True):
        self.delivered = delivered
        self.sent = []

    async def send(self, notification, request_id):
        self.sent.append((request_id, notification))
        return DeliveryResult(delivered=self.delivered, attempts=1, provider_id="p-1" if self.delivered else None,
                              error=None if self.delivered else "provider returned HTTP 500")


def build_app(ai, sender):
    settings = Settings(ai_retry_base_delay=0)

    def factory():
        repository = InMemoryRequestRepository()
        pipeline = NotificationPipeline(repository, NotificationExtractor(ai, settings), sender, notify_workers=2)
        return Services(repository=repository, pipeline=pipeline)

    return configure(FastAPI(lifespan=lifespan_for(factory)))


@pytest.fixture
def ai():
    return ScriptedAI({"marta@app.com": VALID, "Pedro": "I cannot process the request. I was unable to identify the destination."})


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
async def client(ai, sender):
    app = build_app(ai, sender)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            yield http


async def wait_for_status(client, request_id, expected, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        body = (await client.get(f"/v1/requests/{request_id}")).json()
        if body["status"] == expected or asyncio.get_running_loop().time() > deadline:
            return body
        await asyncio.sleep(0.01)


async def create(client, user_input):
    response = await client.post("/v1/requests", json={"user_input": user_input})
    assert response.status_code == 201
    return response.json()["id"]


async def test_full_flow_ends_in_sent(client, sender):
    request_id = await create(client, "Manda un correo a marta@app.com: hola")
    assert (await client.get(f"/v1/requests/{request_id}")).json() == {"id": request_id, "status": "queued"}

    response = await client.post(f"/v1/requests/{request_id}/process")

    assert response.status_code == 202
    assert response.json() == {"id": request_id, "status": "queued"}
    assert (await wait_for_status(client, request_id, "sent"))["status"] == "sent"
    assert sender.sent[0][1].to == "marta@app.com"


async def test_process_is_idempotent(client, ai, sender):
    request_id = await create(client, "Manda un correo a marta@app.com: hola")

    responses = await asyncio.gather(*(client.post(f"/v1/requests/{request_id}/process") for _ in range(5)))

    assert {r.status_code for r in responses} == {202}
    await wait_for_status(client, request_id, "sent")
    assert ai.calls == 1 and len(sender.sent) == 1


async def test_request_without_recipient_ends_in_failed(client, sender):
    request_id = await create(client, "Avisa a Pedro de que llego tarde")
    await client.post(f"/v1/requests/{request_id}/process")

    assert (await wait_for_status(client, request_id, "failed"))["status"] == "failed"
    assert sender.sent == []


async def test_delivery_failure_ends_in_failed(ai):
    app = build_app(ai, FakeSender(delivered=False))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            request_id = await create(client, "Manda un correo a marta@app.com: hola")
            await client.post(f"/v1/requests/{request_id}/process")

            assert (await wait_for_status(client, request_id, "failed"))["status"] == "failed"


@pytest.mark.parametrize("path, method", [("/v1/requests/unknown", "get"), ("/v1/requests/unknown/process", "post")])
async def test_unknown_request_returns_404(client, path, method):
    response = await getattr(client, method)(path)

    assert response.status_code == 404


@pytest.mark.parametrize("body", [{}, {"user_input": ""}, {"user_input": "   "}, {"user_input": "x" * 1001},
                                  {"user_input": 123}])
async def test_invalid_input_returns_422(client, body):
    response = await client.post("/v1/requests", json=body)

    assert response.status_code == 422


async def test_health_reports_status_counts_and_ai_ratio(client):
    request_id = await create(client, "Manda un correo a marta@app.com: hola")
    await client.post(f"/v1/requests/{request_id}/process")
    await wait_for_status(client, request_id, "sent")

    health = (await client.get("/health")).json()

    assert health["status"] == "ok"
    assert health["requests_by_status"]["sent"] == 1
    assert health["ai_calls_per_processed_request"] == 1.0


async def test_repository_never_moves_back_from_a_final_status():
    repository = InMemoryRequestRepository()
    from notification_service.domain import NotificationRequest

    request = NotificationRequest(user_input="x")
    await repository.add(request)
    await repository.update(request.id, status=RequestStatus.SENT)
    await repository.update(request.id, status=RequestStatus.PROCESSING)

    assert (await repository.get(request.id)).status is RequestStatus.SENT
