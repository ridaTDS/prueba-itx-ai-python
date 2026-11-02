import json

import httpx
import pytest
import respx

from notification_service.config import Settings
from notification_service.extraction.extractor import AIClient, AIServiceError

BASE_URL = "http://provider.test"


@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE_URL) as http:
        yield AIClient(http, Settings(provider_api_key="secret"))


@respx.mock
async def test_sends_messages_with_api_key_and_returns_content(client):
    route = respx.post(f"{BASE_URL}/v1/ai/extract").respond(
        200, json={"choices": [{"message": {"role": "assistant", "content": "{}"}}]})

    content = await client.complete([{"role": "user", "content": "hola"}])

    assert content == "{}"
    request = route.calls.last.request
    assert request.headers["X-API-Key"] == "secret"
    assert json.loads(request.content)["messages"] == [{"role": "user", "content": "hola"}]


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(500),
    httpx.Response(429),
    httpx.Response(200, json={"unexpected": True}),
    httpx.Response(200, text="not json"),
])
async def test_wraps_technical_failures(client, response):
    respx.post(f"{BASE_URL}/v1/ai/extract").mock(return_value=response)

    with pytest.raises(AIServiceError):
        await client.complete([])


@respx.mock
async def test_wraps_network_errors(client):
    respx.post(f"{BASE_URL}/v1/ai/extract").mock(side_effect=httpx.ConnectTimeout("boom"))

    with pytest.raises(AIServiceError):
        await client.complete([])
