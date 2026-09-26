"""API mínima: salud, configuración y errores con identificador de solicitud."""

import logging
from importlib.metadata import version
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from factored_bck.settings import Settings

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok", "ready"]
    service: str
    version: str


def error_response(
    request: Request, status: int, code: str, message: str, headers: dict | None = None
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", uuid4().hex)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        headers={**(headers or {}), "X-Request-ID": request_id},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    app_version = version("factored-bck")
    app = FastAPI(
        title=config.app_name,
        version=app_version,
        debug=False,
        docs_url="/docs" if config.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.enable_docs else None,
    )
    app.state.settings = config

    @app.middleware("http")
    async def identify_request(request: Request, call_next):
        request.state.request_id = uuid4().hex
        try:
            response = await call_next(request)
        except Exception as exc:
            response = await unexpected_error(request, exc)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        messages = {404: "Resource not found", 405: "Method not allowed"}
        return error_response(
            request,
            exc.status_code,
            f"http_{exc.status_code}",
            messages.get(exc.status_code, "Request could not be completed"),
            exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError):
        return error_response(request, 422, "validation_error", "Invalid request")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        # No registrar payloads ni mensajes de excepción que puedan contener secretos.
        logger.error(
            "Unhandled error request_id=%s type=%s",
            getattr(request.state, "request_id", "unknown"),
            type(exc).__name__,
        )
        return error_response(request, 500, "internal_error", "Internal server error")

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    async def live():
        return HealthResponse(status="ok", service=config.app_name, version=app_version)

    @app.get("/health/ready", response_model=HealthResponse, tags=["health"])
    async def ready():
        # Esta base no tiene dependencias externas. No afirma disponibilidad del ETL.
        return HealthResponse(status="ready", service=config.app_name, version=app_version)

    return app
