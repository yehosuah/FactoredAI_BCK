"""Customer confirmation transport. Never register confirm/cancel as model tools."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict

from factored_bck.confirmations import CardCommand, CommandKey, ConfirmationId, Confirmations
from factored_bck.routes import Token

router = APIRouter(tags=["customer action confirmation"])


def confirmations(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.confirmations


Service = Annotated[Confirmations, Depends(confirmations)]


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


@router.post("/me/action-confirmations")
def prepare(
    body: CardCommand,
    token: Token,
    service: Service,
    idempotency_key: Annotated[CommandKey, Header()],
):
    return service.prepare(token, body, idempotency_key)


@router.get("/me/action-confirmations/{confirmation_id}")
def get(confirmation_id: ConfirmationId, token: Token, service: Service):
    return service.get(token, confirmation_id)


@router.post("/me/action-confirmations/{confirmation_id}/confirm")
def confirm(
    confirmation_id: ConfirmationId, token: Token, service: Service, body: EmptyBody | None = None
):
    return service.confirm(token, confirmation_id)


@router.post("/me/action-confirmations/{confirmation_id}/cancel")
def cancel(
    confirmation_id: ConfirmationId, token: Token, service: Service, body: EmptyBody | None = None
):
    return service.cancel(token, confirmation_id)
