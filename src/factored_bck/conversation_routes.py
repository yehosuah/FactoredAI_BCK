"""Small authenticated customer transport; no generic tool execution or model confirmation."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from factored_bck.conversation_contract import (
    ConversationState,
    CreateConversation,
    Identifier,
    Key,
    SubmitTurn,
)
from factored_bck.conversations import Conversations
from factored_bck.routes import Token

router = APIRouter(tags=["customer conversations"])


def conversations(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.conversations


Service = Annotated[Conversations, Depends(conversations)]


@router.post("/me/conversations", response_model=ConversationState)
def create(
    body: CreateConversation,
    token: Token,
    service: Service,
    idempotency_key: Annotated[Key, Header()],
):
    return service.create(token, body, idempotency_key)


@router.get("/me/conversations/{conversation_id}", response_model=ConversationState)
def get(
    conversation_id: Identifier,
    token: Token,
    service: Service,
    after: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    return service.get(token, conversation_id, after=after, limit=limit)


@router.post("/me/conversations/{conversation_id}/turns", response_model=ConversationState)
def submit(
    conversation_id: Identifier,
    body: SubmitTurn,
    token: Token,
    service: Service,
    idempotency_key: Annotated[Key, Header()],
):
    return service.submit(token, conversation_id, body, idempotency_key)
