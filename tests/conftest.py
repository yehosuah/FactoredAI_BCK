import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from psycopg.types.json import Jsonb

from factored_bck.settings import Settings
from factored_bck.store import Store


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch):
    for name in os.environ:
        if name.startswith("BCK_"):
            monkeypatch.delenv(name)


@pytest.fixture(scope="module")
def postgres_settings():
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl or os.geteuid() == 0:
        pytest.skip("Requires local initdb/pg_ctl and a non-root user; no Docker required")
    # Short socket path avoids the Unix domain socket filename length limit.
    with TemporaryDirectory(prefix="bck-metrics-", dir="/tmp") as directory:
        data = str(Path(directory) / "data")
        subprocess.run(
            [initdb, "-D", data, "-A", "trust", "-U", "metrics_test", "--no-locale", "-E", "UTF8"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [
                pg_ctl,
                "-D",
                data,
                "-l",
                str(Path(directory) / "postgres.log"),
                "-o",
                f"-F -h '' -k {directory}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            yield Settings(
                _env_file=None, db_host=directory, db_name="postgres", db_user="metrics_test"
            )
        finally:
            subprocess.run(
                [pg_ctl, "-D", data, "-m", "immediate", "-w", "stop"],
                check=True,
                capture_output=True,
                timeout=30,
            )


@pytest.fixture
def store(postgres_settings):
    store = Store(postgres_settings)
    with store.connect() as pg:
        pg.execute("DROP SCHEMA IF EXISTS simulator CASCADE")
        pg.execute("DROP SCHEMA IF EXISTS bank CASCADE")
        pg.execute("CREATE SCHEMA simulator")
        pg.execute("CREATE SCHEMA bank")
        # Minimal controlled ETL contract fixture; no organizer data or ETL checkout.
        pg.execute("CREATE TABLE bank.releases(release_id text PRIMARY KEY, manifest jsonb)")
        pg.execute("CREATE TABLE bank.current_release(singleton boolean, release_id text)")
        pg.execute(
            "INSERT INTO bank.releases VALUES ('test-release',%s)",
            (Jsonb({"contract_version": "card-support-etl-v1"}),),
        )
        pg.execute("INSERT INTO bank.current_release VALUES (true,'test-release')")
    store.initialize()
    return store
