"""Configuración validada al crear la aplicación."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BCK_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = Field(default="Factored AI Backend", min_length=1, max_length=100)
    environment: Literal["development", "test", "production"] = "development"
    enable_docs: bool = True
    data_enabled: bool = False
    db_host: str = "localhost"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "factored"
    db_user: str = "backend_api"
    db_password_file: Path | None = None
    demo_password_file: Path | None = None
    session_seconds: int = Field(default=3600, ge=60, le=86400)
