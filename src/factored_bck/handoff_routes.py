"""Customer and agent transports for the same handoff module used by tools."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from factored_bck.handoff import CreateHandoffArguments, HandoffId, IdempotencyKey, Triage
from factored_bck.handoff_store import HandoffStore
from factored_bck.routes import Login, Token, bearer

router = APIRouter(tags=["human handoffs"])
agent_scheme = HTTPBearer(auto_error=False, scheme_name="AgentTestSession")


def agent_bearer(
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(agent_scheme)],
):
    return bearer(authorization)


AgentToken = Annotated[str, Depends(agent_bearer)]


def handoffs(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.handoffs


Service = Annotated[HandoffStore, Depends(handoffs)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/me/handoffs")
def create(
    body: Triage,
    token: Token,
    service: Service,
    idempotency_key: Annotated[IdempotencyKey, Header()],
):
    return service.create(
        token, CreateHandoffArguments(triage=body, idempotency_key=idempotency_key)
    )


@router.get("/me/handoffs")
def list_customer(token: Token, service: Service, limit: Limit = 20, offset: Offset = 0):
    return service.list(token, limit, offset)


@router.get("/me/handoffs/{handoff_id}")
def get_customer(handoff_id: HandoffId, token: Token, service: Service):
    return service.get(token, handoff_id)


@router.post("/me/handoffs/{handoff_id}/cancel")
def cancel(handoff_id: HandoffId, token: Token, service: Service, body: NoArguments | None = None):
    return service.transition(token, handoff_id, "cancel")


@router.post("/agent/auth/login")
def login(body: Login, request: Request, service: Service):
    return service.agents.login(
        body.username, body.password, request.client.host if request.client else "unknown"
    )


@router.post("/agent/auth/logout")
def logout(token: AgentToken, service: Service, body: NoArguments | None = None):
    return service.agents.logout(token)


@router.get("/agent/handoffs")
def list_agent(token: AgentToken, service: Service, limit: Limit = 20, offset: Offset = 0):
    return service.list(token, limit, offset, agent=True)


@router.get("/agent/handoffs/{handoff_id}")
def get_agent(handoff_id: HandoffId, token: AgentToken, service: Service):
    return service.agent_get(token, handoff_id)


@router.post("/agent/handoffs/{handoff_id}/accept")
def accept(
    handoff_id: HandoffId, token: AgentToken, service: Service, body: NoArguments | None = None
):
    return service.transition(token, handoff_id, "accept")


@router.post("/agent/handoffs/{handoff_id}/resolve")
def resolve(
    handoff_id: HandoffId, token: AgentToken, service: Service, body: NoArguments | None = None
):
    return service.transition(token, handoff_id, "resolve")
