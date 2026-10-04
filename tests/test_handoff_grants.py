"""Deployment grants validate role identity before exposing restricted source columns."""

from pathlib import Path

import psycopg
import pytest
from psycopg import sql

GRANTS = Path("deploy/handoff-read-grants.sql").read_text()
QUOTED_ROLE = 'configured backend"; SELECT 1; --'


@pytest.fixture
def grant_store(store):
    with store.connect() as pg:
        pg.execute("CREATE ROLE backend_api LOGIN")
        pg.execute("CREATE ROLE configured_backend LOGIN")
        pg.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(QUOTED_ROLE)))
        pg.execute(
            "CREATE TABLE bank.customers(release_id text,customer_id text,segment text,email text)"
        )
        pg.execute(
            "CREATE TABLE bank.service_agents(release_id text,agent_id text,agent_type text,"
            "experience_level text,languages text,specialty text,avg_csat numeric,"
            "agent_status text,email text)"
        )
    try:
        yield store
    finally:
        with store.connect() as pg:
            for name in (
                "backend_handoff_reader",
                "backend_api",
                "configured_backend",
                QUOTED_ROLE,
            ):
                if pg.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone():
                    pg.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
                    pg.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def apply(store, role=None):
    with store.connect() as pg:
        if role is not None:
            pg.execute("SELECT set_config('factored_bck.backend_role',%s,true)", (role,))
        pg.execute(GRANTS)


def member(store, role):
    with store.connect() as pg:
        return pg.execute(
            "SELECT pg_has_role(%s,'backend_handoff_reader','MEMBER') AS allowed", (role,)
        ).fetchone()["allowed"]


def test_existing_login_reader_is_rejected_before_any_source_grants(grant_store):
    with grant_store.connect() as pg:
        pg.execute("CREATE ROLE backend_handoff_reader LOGIN")
    with pytest.raises(psycopg.errors.RaiseException, match="reader_must_be_nologin"):
        apply(grant_store, "backend_api")
    with grant_store.connect() as pg:
        assert not pg.execute(
            "SELECT has_column_privilege('backend_handoff_reader','bank.customers',"
            "'customer_id','SELECT') AS allowed"
        ).fetchone()["allowed"]
    assert not member(grant_store, "backend_api")


@pytest.mark.parametrize("role", [None, "", "absent_backend"])
def test_missing_or_unknown_deployment_role_fails_before_creating_reader(grant_store, role):
    with pytest.raises(
        psycopg.errors.RaiseException, match="backend_role_required|backend_role_invalid"
    ):
        apply(grant_store, role)
    with grant_store.connect() as pg:
        assert (
            pg.execute("SELECT 1 FROM pg_roles WHERE rolname='backend_handoff_reader'").fetchone()
            is None
        )


@pytest.mark.parametrize("role", ["backend_api", "configured_backend", QUOTED_ROLE])
def test_only_explicit_backend_role_inherits_narrow_grants_and_reapply_is_safe(grant_store, role):
    apply(grant_store, role)
    apply(grant_store, role)
    assert member(grant_store, role)
    if role != "backend_api":
        assert not member(grant_store, "backend_api")
    with grant_store.connect() as pg:
        assert pg.execute(
            "SELECT NOT rolcanlogin AS safe FROM pg_roles WHERE rolname='backend_handoff_reader'"
        ).fetchone()["safe"]
        pg.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        pg.execute("SELECT customer_id,segment FROM bank.customers")
        pg.execute("SELECT agent_id,languages,specialty FROM bank.service_agents")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute("SELECT email FROM bank.customers")


def test_all_grants_roll_back_if_source_contract_is_incomplete(grant_store):
    with grant_store.connect() as pg:
        pg.execute("ALTER TABLE bank.service_agents DROP COLUMN languages")
    with pytest.raises(psycopg.errors.UndefinedColumn):
        apply(grant_store, "configured_backend")
    with grant_store.connect() as pg:
        assert (
            pg.execute("SELECT 1 FROM pg_roles WHERE rolname='backend_handoff_reader'").fetchone()
            is None
        )


@pytest.mark.parametrize(
    "extra",
    [
        "contact",
        "table",
        "parent",
        "owner",
        "schema_create",
        "other_table",
        "grant_option",
        "attribute",
        "default_acl",
        "database",
        "function",
    ],
)
def test_existing_reader_with_extra_privileges_is_rejected_without_new_membership(
    grant_store, extra
):
    with grant_store.connect() as pg:
        pg.execute("CREATE ROLE backend_handoff_reader NOLOGIN")
        if extra == "contact":
            pg.execute("GRANT SELECT(email) ON bank.customers TO backend_handoff_reader")
        elif extra == "table":
            pg.execute("GRANT SELECT ON bank.customers TO backend_handoff_reader")
        elif extra == "parent":
            pg.execute("GRANT pg_read_all_data TO backend_handoff_reader")
        elif extra == "owner":
            pg.execute("ALTER TABLE bank.customers OWNER TO backend_handoff_reader")
        elif extra == "schema_create":
            pg.execute("GRANT CREATE ON SCHEMA bank TO backend_handoff_reader")
        elif extra == "other_table":
            pg.execute("CREATE TABLE bank.extra(secret text)")
            pg.execute("GRANT SELECT(secret) ON bank.extra TO backend_handoff_reader")
        elif extra == "grant_option":
            pg.execute(
                "GRANT SELECT(customer_id) ON bank.customers TO backend_handoff_reader "
                "WITH GRANT OPTION"
            )
        elif extra == "attribute":
            pg.execute("ALTER ROLE backend_handoff_reader CREATEROLE")
        elif extra == "default_acl":
            pg.execute("ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO backend_handoff_reader")
        elif extra == "database":
            pg.execute("GRANT CONNECT ON DATABASE postgres TO backend_handoff_reader")
        else:
            pg.execute("CREATE FUNCTION bank.extra() RETURNS int LANGUAGE sql AS 'SELECT 1'")
            pg.execute("GRANT EXECUTE ON FUNCTION bank.extra() TO backend_handoff_reader")
    with pytest.raises(psycopg.errors.RaiseException, match="reader_has_excess_privileges"):
        apply(grant_store, "configured_backend")
    assert not member(grant_store, "configured_backend")


@pytest.mark.parametrize("member_type", ["LOGIN", "NOLOGIN", "ADMIN"])
def test_existing_reader_with_unexpected_members_cannot_expose_new_columns(
    grant_store, member_type
):
    with grant_store.connect() as pg:
        pg.execute("CREATE ROLE backend_handoff_reader NOLOGIN")
        if member_type == "ADMIN":
            pg.execute("GRANT backend_handoff_reader TO backend_api WITH ADMIN OPTION")
        else:
            if member_type == "NOLOGIN":
                pg.execute("ALTER ROLE configured_backend NOLOGIN")
            pg.execute("GRANT backend_handoff_reader TO configured_backend")
    with pytest.raises(psycopg.errors.RaiseException, match="reader_has_unexpected_members"):
        apply(grant_store, "backend_api")
    with grant_store.connect() as pg:
        assert not pg.execute(
            "SELECT has_column_privilege('backend_handoff_reader','bank.customers',"
            "'customer_id','SELECT') AS allowed"
        ).fetchone()["allowed"]


@pytest.mark.parametrize("membership", ["inherit", "nested", "set_only", "reapply"])
def test_backend_login_cannot_transitively_expose_reader_to_other_roles(grant_store, membership):
    if membership == "reapply":
        apply(grant_store, "configured_backend")
    with grant_store.connect() as pg:
        if membership == "set_only":
            pg.execute("GRANT configured_backend TO backend_api WITH INHERIT FALSE, SET TRUE")
        else:
            pg.execute("GRANT configured_backend TO backend_api")
        if membership == "nested":
            pg.execute("ALTER ROLE backend_api NOLOGIN")
            pg.execute(sql.SQL("GRANT backend_api TO {}").format(sql.Identifier(QUOTED_ROLE)))
        if membership == "reapply":
            pg.execute(
                "REVOKE SELECT(languages) ON bank.service_agents FROM backend_handoff_reader"
            )
    with pytest.raises(psycopg.errors.RaiseException, match="backend_role_has_members"):
        apply(grant_store, "configured_backend")
    with grant_store.connect() as pg:
        if membership == "reapply":
            assert not pg.execute(
                "SELECT has_column_privilege('backend_handoff_reader','bank.service_agents',"
                "'languages','SELECT') AS allowed"
            ).fetchone()["allowed"]
        else:
            assert (
                pg.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname='backend_handoff_reader'"
                ).fetchone()
                is None
            )
