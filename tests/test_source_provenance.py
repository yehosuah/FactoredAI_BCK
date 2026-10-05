"""ETL-published fixture provenance survives reads, confirmation and exact replay."""

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from factored_bck.app import create_app


@pytest.mark.parametrize(
    "manifest_kind,expected",
    [
        ("team_generated_fixture", "team_synthetic"),
        ("team_synthetic", "team_synthetic"),
        ("organizer_synthetic", "organizer_synthetic"),
        (None, "organizer_synthetic"),
        ("explicit_null", None),
        ("unsupported_source", None),
    ],
)
def test_published_card_provenance_and_committed_evidence(tool_backend, manifest_kind, expected):
    store, _, context = tool_backend
    with store.connect() as pg:
        pg.execute("DELETE FROM simulator.fixture_cards WHERE product_id='card-1'")
        pg.execute(
            "INSERT INTO bank.products VALUES ('test-release','card-1','customer-1',"
            "'Tarjeta Crédito','TEAM-1234','USD',10,1000,'Active','2026-01-01')"
        )
        manifest = {"contract_version": "card-support-etl-v1"}
        if manifest_kind is not None:
            manifest["source_kind"] = None if manifest_kind == "explicit_null" else manifest_kind
        pg.execute("UPDATE bank.releases SET manifest=%s", (Jsonb(manifest),))
    headers = {"Authorization": "Bearer " + context.session_token.get_secret_value()}
    with TestClient(create_app(store.settings, store=store)) as client:
        card = client.get("/me/cards/card-1", headers=headers)
        prepared = client.post(
            "/me/cards/card-1/actions",
            json={"action": "pause"},
            headers=headers | {"Idempotency-Key": "provenance-pause"},
        )
        if expected is None:
            assert card.status_code == prepared.status_code == 503
            assert store.action_metrics()["total_committed"] == 0
            with store.connect() as pg:
                assert (
                    pg.execute(
                        "SELECT count(*) AS n FROM simulator.action_confirmations"
                    ).fetchone()["n"]
                    == 0
                )
            return
        assert card.status_code == prepared.status_code == 200
        assert card.json()["card"]["source_kind"] == expected
        assert "source_kind_present" not in card.json()["card"]
        assert card.json()["card"]["balance_semantics"] == (
            "team_fixture" if expected == "team_synthetic" else "historical_source_value"
        )
        assert prepared.json()["prepared_state"]["source_kind"] == expected
        path = "/me/action-confirmations/" + prepared.json()["confirmation_id"] + "/confirm"
        receipt = client.post(path, headers=headers)
        assert receipt.status_code == 200 and receipt.json()["verified"]
        assert receipt.json()["evidence"]["source_kind"] == expected
        assert client.post(path, headers=headers).json() == receipt.json()
        assert store.action_metrics()["total_committed"] == 1


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("team_generated_fixture", "team_synthetic"),
        (None, "organizer_synthetic"),
        ("explicit_null", None),
        ("unsupported_source", None),
    ],
)
def test_provisioned_customer_uses_manifest_provenance(
    tool_backend, monkeypatch, tmp_path, capsys, kind, expected
):
    from factored_bck import provision

    store, _, _ = tool_backend
    with store.connect() as pg:
        pg.execute("CREATE TABLE bank.customers(release_id text,customer_id text)")
        pg.execute("INSERT INTO bank.customers VALUES('test-release','new-customer')")
        manifest = {"contract_version": "card-support-etl-v1"}
        if kind is not None:
            manifest["source_kind"] = None if kind == "explicit_null" else kind
        pg.execute("UPDATE bank.releases SET manifest=%s", (Jsonb(manifest),))
    password = "synthetic-customer-password"
    path = tmp_path / "private-password"
    path.write_text(password)
    path.chmod(0o600)
    monkeypatch.setattr(provision, "Settings", lambda: store.settings)
    status = provision.main(
        ["--username", "new-user", "--customer-id", "new-customer", "--password-file", str(path)]
    )
    assert password not in capsys.readouterr().out
    if expected is None:
        assert status == 1
        with store.connect() as pg:
            assert (
                pg.execute(
                    "SELECT count(*) AS n FROM simulator.users WHERE username='new-user'"
                ).fetchone()["n"]
                == 0
            )
    else:
        assert status == 0
        token = store.login("new-user", password, "synthetic-test")["access_token"]
        assert store.session(token)["source_kind"] == expected
