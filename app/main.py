"""Punto de entrada: ``uvicorn main:app --port 5000``."""
import logging

from fastapi import FastAPI

from notification_service.config import Settings
from notification_service.container import build_services, configure, lifespan_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # evita una línea de log por cada llamada saliente

settings = Settings.from_env()

app = FastAPI(
    title="Notification Service (Technical Test)",
    description="Convierte peticiones en lenguaje natural en notificaciones (email/SMS) usando un motor de IA.",
    version="1.0.0",
    lifespan=lifespan_for(lambda: build_services(settings)),
)
configure(app)
