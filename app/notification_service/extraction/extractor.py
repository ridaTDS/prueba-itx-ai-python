"""Cliente del motor de IA + bucle de extracción con reintentos guiados."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

from ..config import Settings
from .guardrails import ExtractionError, ExtractionErrorKind, ExtractionResult, parse_ai_response
from .prompt import build_correction_messages, build_messages

logger = logging.getLogger(__name__)

AI_EXTRACT_PATH = "/v1/ai/extract"


class AIServiceError(Exception):
    """Fallo técnico al llamar a la IA (red, timeout, 5xx, 429...)."""


@dataclass
class ExtractionOutcome:
    result: ExtractionResult | None
    attempts: int
    error: ExtractionError | None = None


class AIClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings):
        self._http = http
        self._settings = settings

    async def complete(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "messages": messages,
            # Parámetros estándar para extracción determinista; el mock los ignora
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            response = await self._http.post(
                AI_EXTRACT_PATH,
                json=payload,
                headers={"X-API-Key": self._settings.provider_api_key},
            )
        except httpx.HTTPError as exc:
            raise AIServiceError(f"AI request failed: {exc!r}") from exc

        if response.status_code >= 400:
            raise AIServiceError(f"AI returned HTTP {response.status_code}")
        try:
            body = response.json()
            return body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIServiceError("unexpected AI response schema") from exc


class NotificationExtractor:
    """Orquesta prompt -> IA -> guardrails, con un presupuesto máximo de llamadas a la IA.

    * Errores técnicos (timeout, 5xx): se reintenta con backoff exponencial.
    * Respuesta no válida o rechazo: se reintenta con un prompt de corrección que incluye el motivo.
    * Falta de información en la petición: no se reintenta (repetir no la va a arreglar).
    """

    def __init__(self, ai_client: AIClient, settings: Settings):
        self._ai = ai_client
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.ai_max_concurrency)

    async def extract(self, user_input: str, request_id: str) -> ExtractionOutcome:
        messages = build_messages(user_input)
        last_error: ExtractionError | None = None

        for attempt in range(1, self._settings.ai_max_attempts + 1):
            try:
                async with self._semaphore:
                    content = await self._ai.complete(messages)
            except AIServiceError as exc:
                logger.warning("request=%s ai_attempt=%d technical_error=%s", request_id, attempt, exc)
                last_error = ExtractionError(ExtractionErrorKind.MALFORMED, str(exc))
                await self._backoff(attempt)
                continue

            try:
                result = parse_ai_response(content, user_input)
            except ExtractionError as exc:
                last_error = exc
                logger.info("request=%s ai_attempt=%d rejected kind=%s reason=%s",
                            request_id, attempt, exc.kind.value, exc.reason)
                if not exc.retryable:
                    return ExtractionOutcome(result=None, attempts=attempt, error=exc)
                messages = build_correction_messages(user_input, content, exc.reason)
                continue

            if result.repairs:
                logger.info("request=%s ai_attempt=%d repaired=%s", request_id, attempt, result.repairs)
            return ExtractionOutcome(result=result, attempts=attempt)

        return ExtractionOutcome(result=None, attempts=self._settings.ai_max_attempts, error=last_error)

    async def _backoff(self, attempt: int) -> None:
        delay = self._settings.ai_retry_base_delay * (2 ** (attempt - 1))
        await asyncio.sleep(delay + random.uniform(0, delay))
