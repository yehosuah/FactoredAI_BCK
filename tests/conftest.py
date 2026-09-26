import os

import pytest


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch):
    for name in os.environ:
        if name.startswith("BCK_"):
            monkeypatch.delenv(name)
