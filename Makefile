.PHONY: setup dev check test docker-up docker-down

setup:
	uv sync --locked

dev:
	uv run --locked uvicorn factored_bck.app:create_app --factory --reload --host 127.0.0.1 --port 8000

check:
	uv lock --check
	uv run --locked ruff check src tests
	uv run --locked ruff format --check src tests
	uv run --locked pytest

test:
	uv run --locked pytest

docker-up:
	docker compose up --build -d --wait

docker-down:
	docker compose down
