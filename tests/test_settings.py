import pytest
from pydantic import ValidationError

from factored_bck.app import create_app
from factored_bck.settings import Settings


def test_environment_overrides_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("BCK_APP_NAME=From file\nBCK_ENABLE_DOCS=false\n")
    monkeypatch.setenv("BCK_APP_NAME", "From environment")
    config = Settings(_env_file=env_file)
    assert config.app_name == "From environment"
    assert config.enable_docs is False


def test_invalid_environment_blocks_app_startup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BCK_ENVIRONMENT", "invalid")
    with pytest.raises(ValidationError):
        create_app()


def test_invalid_boolean_is_rejected(monkeypatch):
    monkeypatch.setenv("BCK_ENABLE_DOCS", "maybe")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
