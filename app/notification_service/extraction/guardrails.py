"""Guardrails: convierten la respuesta (ruidosa) del modelo en una ``Notification`` válida o en un error
clasificado.

El pipeline es determinista y se aplica en este orden:

1. **Clasificación de texto libre**: si no hay JSON, se distingue entre *rechazo* del modelo (reintentable),
   *falta de información* en la petición (no reintentable) y salida *ilegible*.
2. **Localización del JSON**: bloques Markdown (```json / ```), JSON embebido en texto o JSON truncado.
3. **Reparación**: comillas simples, claves sin comillas, valores ``True/False/None``, comas finales,
   texto residual (``...``) y llaves o comillas sin cerrar. Se hace con un pequeño lexer que respeta el
   contenido de las cadenas, en lugar de regex globales que podrían alterar el mensaje.
4. **Normalización del esquema**: alias de claves (``Recipient``, ``body``, ``channel``...), mayúsculas
   y campos extra (``confidence``, ``latency_ms``) que se descartan.
5. **Validación y anclaje (grounding)**: el destinatario debe ser un email o teléfono válido y **aparecer
   en la petición original** (evita alucinaciones); el canal se infiere o corrige según el destinatario;
   si el modelo omite el destinatario y la petición contiene exactamente uno, se toma de ella.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from enum import Enum

from ..domain import Notification

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w@.])\+?\d[\d\s-]{6,}\d(?![\w@])")
MIN_PHONE_DIGITS, MAX_PHONE_DIGITS = 9, 15

KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "to": ("to", "recipient", "destination", "destinatario", "receiver", "target", "address",
           "contact", "phone", "phone_number", "email", "email_address"),
    "message": ("message", "body", "text", "content", "msg", "mensaje", "texto", "contenido"),
    "type": ("type", "channel", "method", "medium", "tipo", "canal", "via"),
}

TYPE_ALIASES: dict[str, str] = {
    "email": "email", "e-mail": "email", "mail": "email", "correo": "email", "correo electrónico": "email",
    "sms": "sms", "text": "sms", "texto": "sms", "phone": "sms", "teléfono": "sms", "telefono": "sms",
    "mensaje de texto": "sms",
}

MISSING_INFO_MARKERS = ("unable to identify", "could not identify", "missing", "no he podido identificar",
                        "falta", "no se indica")
REFUSAL_MARKERS = ("lo siento", "no tengo permitido", "no puedo", "refused", "refuse", "viola", "violat",
                   "policy", "políticas", "politicas", "cannot comply", "can't help", "as an ai", "como ia",
                   "sensitive", "spam")

PROMPT_ARTIFACTS_RE = re.compile(r"</?request>", re.IGNORECASE)


class ExtractionErrorKind(str, Enum):
    REFUSAL = "refusal"                          # el modelo se niega a responder
    MISSING_INFORMATION = "missing_information"  # la petición no contiene destinatario o canal
    MALFORMED = "malformed"                      # no hay JSON recuperable
    INVALID = "invalid"                          # JSON legible, pero con datos inválidos o no anclados


class ExtractionError(Exception):
    def __init__(self, kind: ExtractionErrorKind, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason

    @property
    def retryable(self) -> bool:
        return self.kind is not ExtractionErrorKind.MISSING_INFORMATION


@dataclass
class ExtractionResult:
    notification: Notification
    repairs: list[str] = field(default_factory=list)


@dataclass
class Contacts:
    emails: list[str]
    phones: list[str]

    @property
    def all(self) -> list[str]:
        return self.emails + self.phones


# ---------------------------------------------------------------------------------------------------
# Utilidades de contacto
# ---------------------------------------------------------------------------------------------------
def digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def normalize_phone(value: str) -> str:
    value = value.strip()
    prefix = "+" if value.startswith("+") else ""
    return prefix + digits(value)


def is_email(value: str) -> bool:
    return EMAIL_RE.fullmatch(value.strip()) is not None


def is_phone(value: str) -> bool:
    if not re.fullmatch(r"\+?[\d\s().-]+", value.strip()):
        return False
    return MIN_PHONE_DIGITS <= len(digits(value)) <= MAX_PHONE_DIGITS


def find_contacts(text: str) -> Contacts:
    """Extracción determinista de emails y teléfonos (sin duplicados, en orden de aparición)."""
    emails = list(dict.fromkeys(match.lower() for match in EMAIL_RE.findall(text)))
    without_emails = EMAIL_RE.sub(" ", text)
    phones = []
    for match in PHONE_RE.findall(without_emails):
        phone = normalize_phone(match)
        if MIN_PHONE_DIGITS <= len(digits(phone)) <= MAX_PHONE_DIGITS and phone not in phones:
            phones.append(phone)
    return Contacts(emails=emails, phones=phones)


def is_grounded(recipient: str, user_input: str) -> bool:
    """El destinatario debe aparecer en la petición del usuario (no puede ser inventado)."""
    if is_email(recipient):
        return recipient.lower() in user_input.lower()
    recipient_digits = digits(recipient)
    return any(
        digits(phone).endswith(recipient_digits) or recipient_digits.endswith(digits(phone))
        for phone in find_contacts(user_input).phones
    )


# ---------------------------------------------------------------------------------------------------
# Localización y reparación del JSON
# ---------------------------------------------------------------------------------------------------
FENCED_BLOCK_RE = re.compile(r"```[a-zA-Z]*\s*(.*?)```", re.DOTALL)


def find_json_candidate(text: str) -> str | None:
    """Devuelve el fragmento que contiene el objeto JSON, aunque esté truncado."""
    fenced = FENCED_BLOCK_RE.search(text)
    if fenced and "{" in fenced.group(1):
        text = fenced.group(1)

    start = text.find("{")
    if start == -1:
        return None

    depth, quote = 0, None
    i = start
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return text[start:]  # truncado: se intentará cerrar en la reparación


def _read_string(text: str, i: int, quote: str) -> tuple[str, int, bool]:
    """Lee una cadena delimitada por ``quote`` desde ``text[i]``. Devuelve (contenido, nuevo índice, cerrada)."""
    buffer = []
    i += 1
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            buffer.append(text[i + 1] if quote == "'" else text[i:i + 2])
            i += 2
            continue
        if char == quote:
            return "".join(buffer), i + 1, True
        buffer.append(char)
        i += 1
    return "".join(buffer), i, False


def _to_strict_json(text: str) -> str:
    """Lexer tolerante que reescribe un objeto "tipo JSON" como JSON estricto.

    Respeta el contenido de las cadenas, así que no altera el mensaje aunque contenga ``:`` o comas.
    """
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            content, i, _ = _read_string(text, i, '"')
            out.append('"' + content + '"')
        elif char == "'":
            content, i, _ = _read_string(text, i, "'")
            out.append(json.dumps(content, ensure_ascii=False))
        elif char.isalpha() or char == "_":
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] in "_-"):
                j += 1
            word = text[i:j]
            k = j
            while k < len(text) and text[k] in " \t":
                k += 1
            is_key = k < len(text) and text[k] == ":"
            literals = {"true": "true", "false": "false", "null": "null",
                        "True": "true", "False": "false", "None": "null"}
            out.append(json.dumps(word) if is_key or word not in literals else literals[word])
            i = j
        elif char.isdigit() or (char == "-" and i + 1 < len(text) and text[i + 1].isdigit()):
            j = i + 1
            while j < len(text) and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            out.append(text[i:j])
            i = j
        elif char in "{[":
            depth += 1
            out.append(char)
            i += 1
        elif char in "}]":
            depth -= 1
            out.append(char)
            i += 1
        elif char in ",:" or char.isspace():
            out.append(char)
            i += 1
        else:
            i += 1  # ruido fuera de cadenas: "...", "…", backticks, etc.

    result = "".join(out).rstrip().rstrip(",")
    result = re.sub(r",\s*([}\]])", r"\1", result)       # comas finales
    result += "}" * max(depth, 0)                         # llaves sin cerrar (respuesta truncada)
    return result


def parse_json_object(candidate: str) -> dict:
    attempts = (
        lambda: json.loads(candidate, strict=False),          # strict=False: admite saltos de línea en cadenas
        lambda: json.loads(_to_strict_json(candidate), strict=False),
        lambda: ast.literal_eval(candidate),
    )
    for attempt in attempts:
        try:
            value = attempt()
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
        if isinstance(value, dict):
            return value
    raise ExtractionError(ExtractionErrorKind.MALFORMED, "the answer does not contain a valid JSON object")


# ---------------------------------------------------------------------------------------------------
# Normalización y validación
# ---------------------------------------------------------------------------------------------------
def _canonical_fields(data: dict) -> dict[str, object]:
    lowered = {str(key).strip().lower(): value for key, value in data.items()}

    # Respuestas anidadas, p. ej. {"notification": {...}}
    if not any(alias in lowered for aliases in KEY_ALIASES.values() for alias in aliases):
        nested = [value for value in lowered.values() if isinstance(value, dict)]
        if len(nested) == 1:
            return _canonical_fields(nested[0])

    canonical: dict[str, object] = {}
    for field_name, aliases in KEY_ALIASES.items():
        for alias in aliases:
            if alias in lowered and lowered[alias] not in (None, ""):
                canonical[field_name] = lowered[alias]
                break
    if "error" in lowered and "to" not in canonical:
        canonical["error"] = lowered["error"]
    return canonical


def _normalize_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return TYPE_ALIASES.get(value.strip().lower())


def _clean_message(value: object) -> str:
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    value = PROMPT_ARTIFACTS_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip().strip("\"'").strip()
    return value


def _classify_free_text(content: str) -> ExtractionError:
    lowered = content.lower()
    if any(marker in lowered for marker in MISSING_INFO_MARKERS):
        return ExtractionError(ExtractionErrorKind.MISSING_INFORMATION, content.strip()[:200])
    if any(marker in lowered for marker in REFUSAL_MARKERS):
        return ExtractionError(ExtractionErrorKind.REFUSAL, content.strip()[:200])
    return ExtractionError(ExtractionErrorKind.MALFORMED, "the answer is not a JSON object")


def parse_ai_response(content: str, user_input: str) -> ExtractionResult:
    """Aplica todos los guardrails a la respuesta del modelo."""
    if not isinstance(content, str) or not content.strip():
        raise ExtractionError(ExtractionErrorKind.MALFORMED, "empty answer")

    repairs: list[str] = []
    candidate = find_json_candidate(content)
    if candidate is None:
        raise _classify_free_text(content)
    if candidate.strip() != content.strip():
        repairs.append("extracted JSON from surrounding text/markdown")

    data = parse_json_object(candidate)
    fields = _canonical_fields(data)
    if set(data) - {"to", "message", "type"}:
        repairs.append("normalized keys / dropped extra fields")

    contacts = find_contacts(user_input)

    if "error" in fields:
        if contacts.all:
            raise ExtractionError(ExtractionErrorKind.INVALID,
                                  f"the model reported '{fields['error']}' but the request contains a recipient")
        raise ExtractionError(ExtractionErrorKind.MISSING_INFORMATION, str(fields["error"])[:200])

    # Destinatario
    recipient = fields.get("to")
    if recipient is not None and not isinstance(recipient, str):
        recipient = str(recipient)
    if not recipient:
        if len(contacts.all) == 1:
            recipient = contacts.all[0]
            repairs.append("recipient taken from the request (model omitted it)")
        elif not contacts.all:
            raise ExtractionError(ExtractionErrorKind.MISSING_INFORMATION, "the request has no email or phone")
        else:
            raise ExtractionError(ExtractionErrorKind.INVALID, "missing recipient ('to')")
    recipient = recipient.strip()

    if is_email(recipient):
        recipient = recipient.lower()
        recipient_type = "email"
    elif is_phone(recipient):
        recipient = normalize_phone(recipient)
        recipient_type = "sms"
    else:
        raise ExtractionError(ExtractionErrorKind.INVALID, f"'{recipient[:60]}' is not a valid email or phone number")

    if not is_grounded(recipient, user_input):
        raise ExtractionError(ExtractionErrorKind.INVALID,
                              f"recipient '{recipient}' does not appear in the request")

    # Canal: debe ser coherente con el destinatario
    declared_type = _normalize_type(fields.get("type"))
    if declared_type is None:
        repairs.append("type inferred from recipient")
    elif declared_type != recipient_type:
        repairs.append(f"type corrected from '{declared_type}' to '{recipient_type}'")
    notification_type = recipient_type

    # Mensaje
    message = _clean_message(fields.get("message"))
    if not message:
        raise ExtractionError(ExtractionErrorKind.INVALID, "missing message")

    return ExtractionResult(
        notification=Notification(to=recipient, message=message, type=notification_type),
        repairs=repairs,
    )
