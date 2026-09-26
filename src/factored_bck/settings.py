"""Configuración validada al crear la aplicación."""

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
