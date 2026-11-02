"""Guardrails frente a todas las variantes de respuesta que genera el motor de IA del reto."""
import random

import pytest

from notification_service.extraction.guardrails import (
    ExtractionError,
    ExtractionErrorKind,
    find_contacts,
    find_json_candidate,
    parse_ai_response,
)

EMAIL_INPUT = "Manda un correo a marta@app.com: Tienes un nuevo mensaje."
SMS_INPUT = "Avisar por SMS al 600-111-222 que la reserva ha sido confirmada."


def parse(content, user_input=EMAIL_INPUT):
    return parse_ai_response(content, user_input)


# --- Respuestas válidas o recuperables -----------------------------------------------------------
@pytest.mark.parametrize("content", [
    # Limpia
    '{"to": "marta@app.com", "message": "tienes un nuevo mensaje.", "type": "email"}',
    # Claves alternativas / mayúsculas
    '{"Recipient": "marta@app.com", "body": "tienes un nuevo mensaje.", "channel": "email"}',
    '{"To": "marta@app.com", "Message": "tienes un nuevo mensaje.", "Type": "email"}',
    '{"destination": "marta@app.com", "text": "tienes un nuevo mensaje.", "method": "email"}',
    # Campos extra
    '{"to": "marta@app.com", "message": "tienes un nuevo mensaje.", "type": "email", "confidence": 0.99, "latency_ms": 120}',
    # Falta el tipo -> se infiere
    '{"to": "marta@app.com", "message": "tienes un nuevo mensaje."}',
    # Falta el destinatario -> se toma de la petición (sólo hay uno)
    '{"message": "tienes un nuevo mensaje.", "type": "email"}',
    # Markdown
    'He extraído la información correctamente:\n```json\n{"to": "marta@app.com", "message": "tienes un nuevo mensaje.", "type": "email"}\n```',
    'Output:\n```\n{"to": "marta@app.com", "message": "tienes un nuevo mensaje.", "type": "email"}\n```',
    # Embebido en texto
    'Claro, el destino es marta@app.com. En formato JSON: {"to": "marta@app.com", "message": "tienes un nuevo mensaje.", "type": "email"}',
    # Truncado
    '{"to": "marta@app.com", "message": "tienes un nuevo mensaje.", "type": "email" ...',
    # Comillas simples
    "{'to': 'marta@app.com', 'message': 'tienes un nuevo mensaje.', 'type': 'email'}",
    # Claves sin comillas
    '{to: "marta@app.com", message: "tienes un nuevo mensaje.", type: "email"}',
    # Salto de línea dentro de la cadena (JSON no estricto)
    '{"to": "marta@app.com", "message": "tienes un nuevo mensaje.\n", "type": "email"}',
    # Anidado
    '{"notification": {"to": "marta@app.com", "message": "tienes un nuevo mensaje.", "type": "email"}}',
])
def test_recovers_a_valid_email_notification(content):
    result = parse(content)

    assert result.notification.to == "marta@app.com"
    assert result.notification.type == "email"
    assert result.notification.message == "tienes un nuevo mensaje."


def test_clean_answer_needs_no_repairs():
    result = parse('{"to": "marta@app.com", "message": "hola", "type": "email"}')

    assert result.repairs == []


def test_normalizes_phone_numbers_and_infers_sms():
    result = parse('{"to": "600-111-222", "message": "que la reserva ha sido confirmada."}', SMS_INPUT)

    assert result.notification.to == "600111222"
    assert result.notification.type == "sms"
    assert "type inferred from recipient" in result.repairs


def test_corrects_a_type_inconsistent_with_the_recipient():
    result = parse('{"to": "600111222", "message": "reserva confirmada", "type": "email"}', SMS_INPUT)

    assert result.notification.type == "sms"


def test_keeps_colons_and_commas_inside_the_message_when_repairing():
    content = "{to: 'marta@app.com', message: 'cita: mañana, a las 10:00', type: 'email'}"

    assert parse(content).notification.message == "cita: mañana, a las 10:00"


def test_removes_prompt_delimiters_echoed_by_the_model():
    content = '{"to": "marta@app.com", "message": "tienes un nuevo mensaje.</request>", "type": "email"}'

    assert parse(content).notification.message == "tienes un nuevo mensaje."


# --- Respuestas no recuperables ------------------------------------------------------------------
@pytest.mark.parametrize("content", [
    "Lo siento, como IA no tengo permitido procesar datos de contacto personales.",
    "Error: El contenido del mensaje viola las políticas de seguridad (Potential Spam).",
    "Refused: Content analysis flagged sensitive information.",
])
def test_classifies_refusals_as_retryable(content):
    with pytest.raises(ExtractionError) as error:
        parse(content)

    assert error.value.kind is ExtractionErrorKind.REFUSAL
    assert error.value.retryable


def test_missing_information_is_not_retryable():
    content = "I cannot process the request. I was unable to identify the destination (email/phone) in your prompt."

    with pytest.raises(ExtractionError) as error:
        parse(content, "Avisa a Pedro de que llego tarde")

    assert error.value.kind is ExtractionErrorKind.MISSING_INFORMATION
    assert not error.value.retryable


def test_structured_error_without_contact_in_request_is_missing_information():
    with pytest.raises(ExtractionError) as error:
        parse('{"error": "missing recipient"}', "Avisa a Pedro de que llego tarde")

    assert error.value.kind is ExtractionErrorKind.MISSING_INFORMATION


def test_structured_error_when_request_has_a_contact_is_retryable():
    with pytest.raises(ExtractionError) as error:
        parse('{"error": "missing recipient"}')

    assert error.value.kind is ExtractionErrorKind.INVALID
    assert error.value.retryable


def test_rejects_hallucinated_recipients():
    with pytest.raises(ExtractionError) as error:
        parse('{"to": "otra.persona@app.com", "message": "hola", "type": "email"}')

    assert error.value.kind is ExtractionErrorKind.INVALID
    assert "does not appear in the request" in error.value.reason


def test_rejects_invalid_recipients():
    with pytest.raises(ExtractionError) as error:
        parse('{"to": "Marta", "message": "hola", "type": "email"}')

    assert error.value.kind is ExtractionErrorKind.INVALID


def test_rejects_empty_messages():
    with pytest.raises(ExtractionError) as error:
        parse('{"to": "marta@app.com", "message": "  ", "type": "email"}')

    assert error.value.kind is ExtractionErrorKind.INVALID


@pytest.mark.parametrize("content", ["", "   ", "{{{{", "texto sin sentido"])
def test_unparseable_answers_are_malformed(content):
    with pytest.raises(ExtractionError) as error:
        parse(content)

    assert error.value.kind is ExtractionErrorKind.MALFORMED


# --- Utilidades ----------------------------------------------------------------------------------
def test_find_contacts_extracts_emails_and_phones_without_times():
    contacts = find_contacts("Recordatorio a ana@x.es y al +34 699 888 777: cita a las 10:00 del 12/05")

    assert contacts.emails == ["ana@x.es"]
    assert contacts.phones == ["+34699888777"]


def test_find_json_candidate_returns_none_without_braces():
    assert find_json_candidate("sin json") is None


# --- Propiedad: todas las respuestas del mock del reto ------------------------------------------
def test_every_mock_variant_is_recovered_or_classified():
    """Recorre miles de respuestas del generador del reto: ninguna rompe el parser."""
    import responses  # provider/responses.py del reto

    cases = [
        ("marta@app.com", "tienes un nuevo mensaje.", "email", EMAIL_INPUT),
        ("600-111-222", "que la reserva ha sido confirmada.", "sms", SMS_INPUT),
    ]
    rng = random.Random(42)
    random.seed(42)
    recovered = refused = 0
    for _ in range(5000):
        target, message, notification_type, user_input = rng.choice(cases)
        content = responses.generate_ai_response(target, message, notification_type)
        try:
            result = parse_ai_response(content, user_input)
        except ExtractionError as error:
            assert error.kind is ExtractionErrorKind.REFUSAL
            refused += 1
            continue
        assert result.notification.type == notification_type
        assert result.notification.to.replace("-", "") == target.replace("-", "")
        recovered += 1

    # El 90 % de las respuestas se recupera sin volver a llamar a la IA (sólo los rechazos necesitan reintento)
    assert recovered / (recovered + refused) > 0.85
