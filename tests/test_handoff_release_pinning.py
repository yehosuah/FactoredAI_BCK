"""Every handoff mutation must finish before accepted-source cutover can overtake it."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_handoffs import TRIAGE, create
from test_handoffs import backend as handoff_fixture

from factored_bck.handoff import CreateHandoffArguments


@pytest.fixture
def backend(store):
    yield from handoff_fixture.__wrapped__(store)


@pytest.mark.parametrize("operation", ["create", "accept", "resolve", "recover", "reroute"])
def test_publication_cannot_overtake_handoff_mutation(backend, monkeypatch, operation):
    store, service, _, customers, agents = backend
    case = create(backend, severity="critical" if operation == "reroute" else "low")
    if operation in ("resolve", "recover"):
        service.transition(agents[0], case["handoff_id"], "accept")
    if operation == "recover":
        with store.connect() as pg:
            pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='a1'")
    entered, proceed = Event(), Event()
    routing = service._routing

    def paused_routing(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        return routing(*args, **kwargs)

    monkeypatch.setattr(service, "_routing", paused_routing)

    def mutate():
        if operation == "create":
            return service.create(
                customers[0], CreateHandoffArguments(triage=TRIAGE, idempotency_key="cutover")
            )
        if operation in ("accept", "resolve"):
            return service.transition(agents[0], case["handoff_id"], operation)
        return getattr(service, operation)(case["handoff_id"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(mutate)
        try:
            assert entered.wait(5)
            with store.connect() as publisher:
                admitted = publisher.execute(
                    "SELECT pg_try_advisory_xact_lock(7236148201) AS admitted"
                ).fetchone()["admitted"]
                if admitted:
                    publisher.execute(
                        "INSERT INTO bank.releases SELECT 'next-release',manifest "
                        "FROM bank.releases"
                    )
                    publisher.execute("UPDATE bank.current_release SET release_id='next-release'")
            assert admitted is False, "Accepted-release cutover overtook routing before commit"
        finally:
            proceed.set()
        result = future.result(timeout=5)
    assert (
        result["status"]
        == {
            "create": "assigned",
            "accept": "accepted",
            "resolve": "resolved",
            "recover": "assigned",
            "reroute": "queued",
        }[operation]
    )
    with store.connect() as publisher:
        assert publisher.execute(
            "SELECT pg_try_advisory_xact_lock(7236148201) AS admitted"
        ).fetchone()["admitted"]
