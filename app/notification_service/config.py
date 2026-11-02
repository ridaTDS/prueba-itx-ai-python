"""Configuración del servicio. Todos los valores se pueden sobrescribir con variables de entorno."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    # Provider (IA + notificaciones)
    provider_base_url: str = "http://localhost:3001"
    provider_api_key: str = "test-dev-2026"
    http_timeout_seconds: float = 10.0

    # Extracción con IA
    ai_max_attempts: int = 3            # llamadas totales a la IA por petición (1 + 2 reintentos)
    ai_max_concurrency: int = 100       # extracciones simultáneas (= tamaño del pool HTTP de la IA)
    ai_retry_base_delay: float = 0.2

    # Notificaciones (el provider admite 50 peticiones / 10 s y 50 concurrentes)
    notify_rate_limit: int = 40         # margen sobre el límite del provider (50 / 10 s) por latencias variables
    notify_rate_period_seconds: float = 10.0
    notify_workers: int = 20
    notify_max_attempts: int = 6
    notify_retry_base_delay: float = 0.5
    notify_retry_max_delay: float = 10.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            provider_base_url=_env("PROVIDER_BASE_URL", cls.provider_base_url),
            provider_api_key=_env("PROVIDER_API_KEY", cls.provider_api_key),
            http_timeout_seconds=_env_float("HTTP_TIMEOUT_SECONDS", cls.http_timeout_seconds),
            ai_max_attempts=_env_int("AI_MAX_ATTEMPTS", cls.ai_max_attempts),
            ai_max_concurrency=_env_int("AI_MAX_CONCURRENCY", cls.ai_max_concurrency),
            ai_retry_base_delay=_env_float("AI_RETRY_BASE_DELAY", cls.ai_retry_base_delay),
            notify_rate_limit=_env_int("NOTIFY_RATE_LIMIT", cls.notify_rate_limit),
            notify_rate_period_seconds=_env_float("NOTIFY_RATE_PERIOD_SECONDS", cls.notify_rate_period_seconds),
            notify_workers=_env_int("NOTIFY_WORKERS", cls.notify_workers),
            notify_max_attempts=_env_int("NOTIFY_MAX_ATTEMPTS", cls.notify_max_attempts),
            notify_retry_base_delay=_env_float("NOTIFY_RETRY_BASE_DELAY", cls.notify_retry_base_delay),
            notify_retry_max_delay=_env_float("NOTIFY_RETRY_MAX_DELAY", cls.notify_retry_max_delay),
        )
