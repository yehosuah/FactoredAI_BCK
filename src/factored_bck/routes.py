"""Authenticated card-support APIs; customer scope comes exclusively from the session."""

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from factored_bck.metrics import METRICS_ROUTE

router = APIRouter()
session_scheme = HTTPBearer(auto_error=False, scheme_name="TestSession")


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "block", "pause", "reactivate", "activate", "replacement", "unrecognized-charge"
    ]
    transaction_id: str | None = Field(default=None, min_length=1, max_length=30)
    process_date: date | None = None


def store(request: Request):
    if request.app.state.store is None:
        raise HTTPException(503)
    return request.app.state.store


def bearer(authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(session_scheme)]):
    if not authorization or authorization.scheme.lower() != "bearer":
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    token = authorization.credentials
    if not token or len(token) > 200:
        raise HTTPException(401)
    return token


DB = Annotated[Any, Depends(store)]
Token = Annotated[str, Depends(bearer)]


def user(db: DB, token: Token):
    return db.session(token)


Principal = Annotated[dict, Depends(user)]


@router.post("/auth/login", tags=["test authentication"])
def login(body: Login, request: Request, db: DB):
    peer = request.client.host if request.client else "unknown"
    return db.login(body.username, body.password, peer)


@router.post("/auth/logout", tags=["test authentication"])
def logout(_user: Principal, token: Token, db: DB):
    return db.logout(token)


@router.get("/me", tags=["card support"])
def me(principal: Principal):
    return {**principal, "mode": "test_simulator"}


@router.get("/me/cards", tags=["card support"])
def cards(principal: Principal, db: DB):
    return db.cards(principal)


@router.get("/me/cards/{product_id}", tags=["card support"])
def card(product_id: str, principal: Principal, db: DB):
    return db.card(principal, product_id)


@router.get("/me/cards/{product_id}/movements", tags=["card support"])
def movements(
    product_id: str,
    principal: Principal,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_date: date | None = None,
):
    return db.movements(principal, product_id, limit, before_date)


@router.post("/me/cards/{product_id}/actions", tags=["simulated actions"])
def action(
    product_id: str,
    body: Action,
    idempotency_key: Annotated[
        str, Header(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    ],
    principal: Principal,
    db: DB,
):
    if body.action != "unrecognized-charge" and (body.transaction_id or body.process_date):
        raise HTTPException(422)
    return db.action(
        principal, product_id, body.action, idempotency_key, body.transaction_id, body.process_date
    )


@router.get("/me/handoff", tags=["card support"])
def handoff(principal: Principal, db: DB):
    return db.handoff(principal)


@router.get("/operations/etl", tags=["operations"])
def etl_status(_principal: Principal, db: DB):
    return db.etl_status()


@router.get(METRICS_ROUTE, tags=["operations"])
def metrics(request: Request, response: Response, _principal: Principal, db: DB):
    response.headers["Cache-Control"] = "no-store"
    try:
        http = request.app.state.metrics.snapshot()
    except Exception:
        http = {"status": "unavailable", "reason": "collection_unavailable"}
    try:
        actions = db.action_metrics()
    except Exception:
        actions = {"status": "unavailable", "reason": "aggregation_unavailable"}
    try:
        handoffs = request.app.state.handoffs.metrics()
    except Exception:
        handoffs = {"status": "unavailable", "reason": "aggregation_unavailable"}
    return {
        "http": http,
        "card_actions": actions,
        "handoffs": handoffs,
        "limitations": [
            "HTTP metrics are best-effort, per application instance and reset on restart",
            "HTTP counters and bounded recent latency samples have different windows",
            "Latency ends at response headers; body streaming and client network are excluded",
            "Action counts cover all customers and all retained committed evidence",
            "Idempotent replays do not add committed actions",
            "Failed or rejected action attempts are not persisted; action failures are unknown",
            "HTTP success and committed simulated actions do not establish safe resolution",
            "No refund, issuance, shipping or fraud decision is asserted",
            "HTTP and database snapshots are not atomic with each other",
            "Handoff counts cover all customers; assigned does not mean accepted or resolved",
        ],
    }
