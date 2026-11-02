"""Bucle de extracción: reintentos guiados, errores técnicos y presupuesto de llamadas a la IA."""
from notification_service.config import Settings
from notification_service.extraction.extractor import AIServiceError, NotificationExtractor
from notification_service.extraction.guardrails import ExtractionErrorKind

USER_INPUT = "Manda un correo a marta@app.com: Tienes un nuevo mensaje."
VALID = '{"to": "marta@app.com", "message": "tienes un nuevo mensaje.", "type": "email"}'
REFUSAL = "Refused: Content analysis flagged sensitive information."


class FakeAIClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def extractor_with(ai, **settings):
    return NotificationExtractor(ai, Settings(ai_retry_base_delay=0, **settings))


async def test_valid_answer_needs_a_single_call():
    ai = FakeAIClient(VALID)

    outcome = await extractor_with(ai).extract(USER_INPUT, "r1")

    assert outcome.result.notification.to == "marta@app.com"
    assert outcome.attempts == 1


async def test_refusal_is_retried_with_a_correction_prompt():
    ai = FakeAIClient(REFUSAL, VALID)

    outcome = await extractor_with(ai).extract(USER_INPUT, "r1")

    assert outcome.result is not None and outcome.attempts == 2
    correction = ai.calls[1]
    assert correction[-3] == {"role": "assistant", "content": REFUSAL}
    assert correction[-2]["role"] == "system"


async def test_gives_up_after_the_attempt_budget():
    ai = FakeAIClient(REFUSAL, REFUSAL, REFUSAL, VALID)

    outcome = await extractor_with(ai, ai_max_attempts=3).extract(USER_INPUT, "r1")

    assert outcome.result is None
    assert outcome.attempts == 3 and len(ai.calls) == 3
    assert outcome.error.kind is ExtractionErrorKind.REFUSAL


async def test_missing_information_fails_fast_without_retries():
    ai = FakeAIClient("I cannot process the request. I was unable to identify the destination (email/phone).")

    outcome = await extractor_with(ai).extract("Avisa a Pedro de que llego tarde", "r1")

    assert outcome.result is None and outcome.attempts == 1
    assert outcome.error.kind is ExtractionErrorKind.MISSING_INFORMATION


async def test_technical_errors_are_retried():
    ai = FakeAIClient(AIServiceError("timeout"), VALID)

    outcome = await extractor_with(ai).extract(USER_INPUT, "r1")

    assert outcome.result is not None and outcome.attempts == 2
