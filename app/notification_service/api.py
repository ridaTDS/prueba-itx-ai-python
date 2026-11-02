"""Endpoints HTTP (contrato del reto)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from .domain import NotificationRequest, RequestStatus

router = APIRouter()

MAX_USER_INPUT_LENGTH = 1000


class CreateRequestBody(BaseModel):
    user_input: str = Field(min_length=1, max_length=MAX_USER_INPUT_LENGTH,
                            examples=["Manda un mail a feda@test.com diciendo hola"])

    @field_validator("user_input")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user_input must not be blank")
        return value.strip()


class CreateRequestResponse(BaseModel):
    id: str


class RequestStatusResponse(BaseModel):
    id: str
    status: RequestStatus


def _services(request: Request):
    return request.app.state.services


@router.post("/v1/requests", status_code=status.HTTP_201_CREATED, response_model=CreateRequestResponse,
             tags=["requests"], summary="Registrar una petición en lenguaje natural")
async def create_request(body: CreateRequestBody, request: Request) -> CreateRequestResponse:
    notification_request = NotificationRequest(user_input=body.user_input)
    await _services(request).repository.add(notification_request)
    return CreateRequestResponse(id=notification_request.id)


@router.post("/v1/requests/{request_id}/process", status_code=status.HTTP_202_ACCEPTED,
             response_model=RequestStatusResponse, tags=["requests"],
             summary="Procesar la petición en segundo plano (idempotente)")
async def process_request(request_id: str, request: Request) -> RequestStatusResponse:
    services = _services(request)
    notification_request, first_call = await services.repository.mark_process_requested(request_id)
    if notification_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if first_call:
        services.pipeline.submit(request_id)
    return RequestStatusResponse(id=notification_request.id, status=notification_request.status)


@router.get("/v1/requests/{request_id}", response_model=RequestStatusResponse, tags=["requests"],
            summary="Consultar el estado de una petición")
async def get_request(request_id: str, request: Request) -> RequestStatusResponse:
    notification_request = await _services(request).repository.get(request_id)
    if notification_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return RequestStatusResponse(id=notification_request.id, status=notification_request.status)


@router.get("/health", tags=["ops"], summary="Estado del servicio y de las colas")
async def health(request: Request) -> dict:
    services = _services(request)
    return {
        "status": "ok",
        **await services.repository.stats(),
        "pending_notifications": services.pipeline.pending_notifications,
    }
