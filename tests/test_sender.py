import httpx
import pytest
import respx

from notification_service.config import Settings
from notification_service.domain import Notification
from notification_service.notifications.rate_limiter import PacedRateLimiter
from notification_service.notifications.sender import NotificationSender

BASE_URL = "http://provider.test"
NOTIFICATION = Notification(to="marta@app.com", message="hola", type="email")


@pytest.fixture
async def sender():
    settings = Settings(provider_api_key="secret", notify_max_attempts=3,
                        notify_retry_base_delay=0, notify_retry_max_delay=0)
    async with httpx.AsyncClient(base_url=BASE_URL) as http:
        yield NotificationSender(http, settings, PacedRateLimiter(1000, 1))


@respx.mock
async def test_delivers_with_api_key_and_trace_id(sender):
    route = respx.post(f"{BASE_URL}/v1/notify").respond(200, json={"status": "delivered", "provider_id": "p-1"})

    result = await sender.send(NOTIFICATION, "req-1")

    assert result.delivered and result.provider_id == "p-1" and result.attempts == 1
    request = route.calls.last.request
    assert request.headers["X-API-Key"] == "secret"
    assert request.url.params["trace_id"] == "req-1"


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retries_transient_errors(sender, status):
    respx.post(f"{BASE_URL}/v1/notify").mock(side_effect=[
        httpx.Response(status), httpx.Response(200, json={"provider_id": "p-2"})])

    result = await sender.send(NOTIFICATION, "req-1")

    assert result.delivered and result.attempts == 2


@respx.mock
async def test_retries_network_errors(sender):
    respx.post(f"{BASE_URL}/v1/notify").mock(side_effect=[
        httpx.ReadTimeout("slow"), httpx.Response(200, json={"provider_id": "p-3"})])

    assert (await sender.send(NOTIFICATION, "req-1")).delivered


@respx.mock
async def test_does_not_retry_permanent_errors(sender):
    route = respx.post(f"{BASE_URL}/v1/notify").respond(422)

    result = await sender.send(NOTIFICATION, "req-1")

    assert not result.delivered and route.call_count == 1
    assert "422" in result.error


@respx.mock
async def test_gives_up_after_max_attempts(sender):
    route = respx.post(f"{BASE_URL}/v1/notify").respond(500)

    result = await sender.send(NOTIFICATION, "req-1")

    assert not result.delivered and route.call_count == 3
