import pytest
from fastapi.testclient import TestClient

from factored_bck.app import create_app
from factored_bck.settings import Settings


@pytest.fixture
def app():
    return create_app(Settings(_env_file=None, environment="test"))


@pytest.mark.parametrize("path,status", [("/health/live", "ok"), ("/health/ready", "ready")])
def test_health(app, path, status):
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {
        "status": status,
        "service": "Factored AI Backend",
        "version": "0.1.0",
    }
    assert response.headers["x-request-id"]


def test_request_ids_are_generated_per_request(app):
    with TestClient(app) as client:
        first = client.get("/health/live", headers={"X-Request-ID": "untrusted"})
        second = client.get("/health/live")
    assert first.headers["x-request-id"] != "untrusted"
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_not_found_has_error_envelope(app):
    with TestClient(app) as client:
        response = client.get("/missing")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "http_404",
            "message": "Resource not found",
            "request_id": response.headers["x-request-id"],
        }
    }


def test_method_not_allowed_preserves_allow_header(app):
    with TestClient(app) as client:
        response = client.post("/health/live")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]
    assert response.json()["error"]["code"] == "http_405"


def test_validation_does_not_echo_input(app):
    @app.get("/test-only/{value}")
    def require_integer(value: int):
        return value

    with TestClient(app) as client:
        response = client.get("/test-only/sensitive-input")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "sensitive-input" not in response.text


def test_internal_error_does_not_expose_details(app, caplog):
    @app.get("/test-only-failure")
    def fail():
        raise RuntimeError("private-credential-example")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-only-failure")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": response.headers["x-request-id"],
        }
    }
    assert "private-credential-example" not in response.text
    assert "private-credential-example" not in caplog.text
    assert response.headers["x-request-id"] in caplog.text


def test_documentation_can_be_disabled():
    app = create_app(Settings(_env_file=None, enable_docs=False))
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/health/live").status_code == 200


def test_openapi_contains_only_health_endpoints(app):
    with TestClient(app) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    assert set(response.json()["paths"]) == {"/health/live", "/health/ready"}
